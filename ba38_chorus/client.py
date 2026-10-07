import base64
import os
import re
import time
from datetime import datetime

import requests
from flask import session

URLS = {
    "prod": {
        "oauth": "https://oauth.piste.gouv.fr/api/oauth/token",
        "api": "https://api.piste.gouv.fr/cpro",
    },
    "sandbox": {
        "oauth": "https://sandbox-oauth.piste.gouv.fr/api/oauth/token",
        "api": "https://sandbox-api.piste.gouv.fr/cpro",
    },
}

# Destinataires fictifs du matelas de données de qualification : en sandbox,
# le vrai SIRET du partenaire (inconnu de la qualification) est remplacé par
# le destinataire fictif qui a les mêmes exigences.
SANDBOX_DEST_SANS_PARAMETRE = "12345678200051"
SANDBOX_DEST_EJ_OBLIGATOIRE = "12345678200036"
SANDBOX_DEST_SERVICE_OBLIGATOIRE = "12345678200028"
SANDBOX_CODE_SERVICE = "SERVICE_DEST_SERV_OBL"

# Pause entre deux appels : PISTE renvoie des 401 intermittents quand les
# appels s'enchaînent trop vite.
PAUSE_ENTRE_APPELS = 1.0


class ChorusErreur(Exception):
    """Refus de Chorus Pro ou de PISTE, message lisible pour l'utilisateur."""


def environnement_chorus():
    """
    "prod" ou "sandbox". Garde-fou (même principe que les mails, voir
    mémoire "Garde-fou email DEV") : l'instance DEV et les comptes
    test_only déposent TOUJOURS en qualification, quelle que soit la
    valeur de CHORUS_ENV — seule l'instance PROD peut déposer pour de vrai.
    """
    if os.getenv("ENVIRONMENT", "").upper() == "DEV":
        return "sandbox"
    try:
        if session.get("test_user"):
            return "sandbox"
    except RuntimeError:
        pass
    env = os.getenv("CHORUS_ENV", "sandbox").strip().lower()
    return env if env in URLS else "sandbox"


def _nettoyer_siret(siret):
    return re.sub(r"\D", "", str(siret or ""))


class ClientChorus:

    def __init__(self, env=None):
        self.env = env or environnement_chorus()
        prefixe = "CHORUS_SANDBOX_" if self.env == "sandbox" else "CHORUS_"
        self.client_id = os.getenv(prefixe + "CLIENT_ID", "").strip()
        self.client_secret = os.getenv(prefixe + "CLIENT_SECRET", "").strip()
        self.login = os.getenv(prefixe + "TECH_LOGIN", "").strip()
        self.password = os.getenv(prefixe + "TECH_PASSWORD", "").strip()
        manquants = [n for n, v in (("CLIENT_ID", self.client_id), ("CLIENT_SECRET", self.client_secret),
                                    ("TECH_LOGIN", self.login), ("TECH_PASSWORD", self.password)) if not v]
        if manquants:
            raise ChorusErreur(f"Identifiants Chorus Pro ({self.env}) manquants dans .env : "
                               + ", ".join(prefixe + m for m in manquants))
        self._jeton = None
        self._id_fournisseur = None

    @property
    def libelle_env(self):
        return "qualification (test)" if self.env == "sandbox" else "PRODUCTION"

    # ------------------------------------------------------------------
    # Bas niveau
    # ------------------------------------------------------------------
    def _obtenir_jeton(self):
        # PISTE refuse aléatoirement (~1 fois sur 10, "invalid_client" HTTP 400)
        # des identifiants pourtant valides : on réessaie avant d'abandonner.
        for tentative in range(5):
            rep = requests.post(URLS[self.env]["oauth"], data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "openid",
            }, timeout=30)
            if rep.status_code == 200:
                self._jeton = rep.json()["access_token"]
                return
            time.sleep(2 * (tentative + 1))
        raise ChorusErreur(f"Jeton PISTE refusé ({rep.status_code}) après 5 essais : {rep.text[:300]}")

    def appel(self, chemin, corps):
        """POST JSON ; lève ChorusErreur si HTTP ≠ 200 ou codeRetour ≠ 0."""
        if not self._jeton:
            self._obtenir_jeton()
        cpro_account = base64.b64encode(f"{self.login}:{self.password}".encode()).decode()

        for tentative in range(3):
            time.sleep(PAUSE_ENTRE_APPELS)
            rep = requests.post(URLS[self.env]["api"] + chemin, headers={
                "Authorization": f"Bearer {self._jeton}",
                "cpro-account": cpro_account,
                "Content-Type": "application/json;charset=utf-8",
                "Accept": "application/json;charset=utf-8",
            }, json=corps, timeout=60)
            if rep.status_code == 401 and tentative < 2:
                time.sleep(2 * (tentative + 1))
                self._obtenir_jeton()
                continue
            break

        if rep.status_code != 200:
            raise ChorusErreur(f"Chorus Pro {chemin} : HTTP {rep.status_code} {rep.text[:300]}")
        donnees = rep.json()
        if donnees.get("codeRetour") not in (0, None):
            raise ChorusErreur(f"{donnees.get('libelle') or 'Refus Chorus Pro'} (code {donnees.get('codeRetour')})")
        return donnees

    # ------------------------------------------------------------------
    # Structures
    # ------------------------------------------------------------------
    def id_fournisseur(self):
        """idStructureCPP de la structure émettrice rattachée au compte technique."""
        if self._id_fournisseur is None:
            res = self.appel("/transverses/v1/recuperer/structures/actives/fournisseur", {})
            structures = res.get("listeStructures") or []
            if not structures:
                raise ChorusErreur("Aucune structure fournisseur active pour ce compte technique")
            self._id_fournisseur = structures[0]["idStructureCPP"]
        return self._id_fournisseur

    def exigences_destinataire(self, siret):
        """
        Ce que le destinataire impose : {"designation", "service", "ej",
        "ej_ou_service"} (booléens). Lève ChorusErreur si SIRET inconnu.
        """
        siret = _nettoyer_siret(siret)
        res = self.appel("/structures/v1/rechercher", {
            "structure": {"identifiantStructure": siret, "typeIdentifiantStructure": "SIRET"}
        })
        structures = res.get("listeStructures") or []
        if not structures:
            raise ChorusErreur(f"SIRET {siret} inconnu de Chorus Pro ({self.libelle_env})")
        detail = self.appel("/structures/v1/consulter", {
            "idStructureCPP": structures[0]["idStructureCPP"], "codeLangue": "fr",
        })
        p = detail.get("parametres") or {}
        return {
            "id": structures[0]["idStructureCPP"],
            "designation": structures[0].get("designationStructure", ""),
            "service": bool(p.get("codeServiceDoitEtreRenseigne")),
            "ej": bool(p.get("numeroEJDoitEtreRenseigne")),
            "ej_ou_service": bool(p.get("gestionNumeroEJOuCodeService")),
        }

    def services_destinataire(self, id_structure):
        """
        Codes des services actifs d'une structure destinataire, ou None si
        la liste est incomplète (pagination non paramétrable : l'API refuse
        "parametres", 10 résultats par défaut).
        """
        res = self.appel("/structures/v1/rechercher/services", {"idStructure": int(id_structure)})
        services = res.get("listeServices") or []
        total = (res.get("parametresRetour") or {}).get("total", len(services))
        if total > len(services):
            return None
        return [sv["codeService"] for sv in services if sv.get("estActif", True)]

    def destinataire_effectif(self, siret, code_service, numero_ej):
        """
        (siret, code_service) réellement envoyés. En PROD : inchangés.
        En sandbox : destinataire fictif du matelas ayant les mêmes
        exigences que les références renseignées sur la fiche.
        """
        if self.env != "sandbox":
            return _nettoyer_siret(siret), (code_service or "").strip() or None
        if (numero_ej or "").strip():
            return SANDBOX_DEST_EJ_OBLIGATOIRE, None
        if (code_service or "").strip():
            return SANDBOX_DEST_SERVICE_OBLIGATOIRE, SANDBOX_CODE_SERVICE
        return SANDBOX_DEST_SANS_PARAMETRE, None

    # ------------------------------------------------------------------
    # Factures
    # ------------------------------------------------------------------
    def deposer_facture(self, pdf_bytes, nom_fichier, numero_facture, siret_destinataire,
                        montant, designation, code_service=None, numero_ej=None, commentaire=None):
        """
        Dépose le PDF puis soumet la facture (sans TVA, virement).
        Retourne {"identifiant", "numero", "statut", "date_depot"}.
        """
        siret, service = self.destinataire_effectif(siret_destinataire, code_service, numero_ej)
        numero = str(numero_facture)
        if self.env == "sandbox":
            # Chorus refuse un n° déjà déposé : en qualification on rejoue
            # souvent la même facture, d'où un suffixe horodaté.
            numero = f"Q{numero}-{datetime.now():%y%m%d%H%M%S}"

        # pieceJointeNom : 50 caractères maximum (refus HTTP 400 au-delà)
        base_nom = nom_fichier[:-4] if nom_fichier.lower().endswith(".pdf") else nom_fichier
        nom_fichier = base_nom[:46].rstrip(" -") + ".pdf"

        pj = self.appel("/transverses/v1/ajouter/fichier", {
            "pieceJointeFichier": base64.b64encode(pdf_bytes).decode(),
            "pieceJointeNom": nom_fichier,
            "pieceJointeTypeMime": "application/pdf",
            "pieceJointeExtension": "PDF",
        })

        montant = round(float(montant or 0), 2)
        references = {
            "deviseFacture": "EUR",
            "typeFacture": "FACTURE",
            "typeTva": "SANS_TVA",
            "modePaiement": "VIREMENT",
        }
        if (numero_ej or "").strip():
            references["numeroBonCommande"] = numero_ej.strip()

        destinataire = {"codeDestinataire": siret}
        if service:
            destinataire["codeServiceExecutant"] = service

        corps = {
            "modeDepot": "DEPOT_PDF_API",
            "numeroFactureSaisi": numero,
            "dateFacture": datetime.now().strftime("%Y-%m-%d"),
            "destinataire": destinataire,
            "fournisseur": {"idFournisseur": self.id_fournisseur()},
            "cadreDeFacturation": {"codeCadreFacturation": "A1_FACTURE_FOURNISSEUR"},
            "references": references,
            "lignePoste": [{
                "lignePosteNumero": 1,
                "lignePosteReference": "PARTICIPATION",
                "lignePosteDenomination": designation[:40],
                "lignePosteQuantite": 1,
                "lignePosteUnite": "forfait",
                "lignePosteMontantUnitaireHT": montant,
                "lignePosteMontantRemiseHT": 0,
                "lignePosteTauxTvaManuel": 0,
            }],
            "ligneTva": [{
                "ligneTvaMontantBaseHtParTaux": montant,
                "ligneTvaMontantTvaParTaux": 0,
                "ligneTvaTauxManuel": 0,
            }],
            "montantTotal": {
                "montantHtTotal": montant,
                "montantTVA": 0,
                "montantTtcTotal": montant,
                "montantRemiseGlobaleTTC": 0,
                "montantAPayer": montant,
            },
            "pieceJointePrincipale": [{
                "pieceJointePrincipaleDesignation": "Facture",
                "pieceJointePrincipaleId": pj["pieceJointeId"],
            }],
        }
        if commentaire:
            corps["commentaire"] = commentaire[:1000]

        res = self.appel("/factures/v1/soumettre", corps)
        return {
            "identifiant": res.get("identifiantFactureCPP"),
            "numero": res.get("numeroFacture") or numero,
            "statut": res.get("statutFacture"),
            "date_depot": res.get("dateDepot"),
        }

    def statut_facture(self, identifiant):
        res = self.appel("/factures/v1/consulter/fournisseur", {"identifiantFactureCPP": int(identifiant)})
        return (res.get("facture") or {}).get("statutFacture")
