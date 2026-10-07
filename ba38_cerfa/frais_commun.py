"""Fonctions partagées (admin / envois / questionnaire public) du CERFA
abandon de frais bénévoles."""

import os
import secrets
import sqlite3

from flask import session, url_for

from ba38_cerfa.calculs import charger_json, totaux_declaration
from ba38_cerfa.constants import STATUTS, STATUTS_VALIDES
from ba38_cerfa.pdf import generer_cerfa_pdf, generer_courrier_remboursement_pdf

from ba38_utilitaires.core import render_modele_email
from ba38_utilitaires.organisation import get_organisation


# ============================================================================
# 🗄️ Bases : le lien public n'a pas de session → la base cible est portée
# par le préfixe du jeton (« t- » = base de test des comptes test_only).
# ============================================================================
PREFIXE_TEST = "t-"
PREFIXE_REEL = "p-"


def chemin_base(test):
    base_dir = os.getenv("BA38_BASE_DIR")
    fichier = os.getenv("SQLITE_DB_TEST" if test else "SQLITE_DB")
    if not base_dir or not fichier:
        raise RuntimeError("BA38_BASE_DIR / SQLITE_DB non définis")
    return os.path.join(base_dir, fichier)


def session_en_base_test():
    try:
        return bool(session.get("test_user"))
    except RuntimeError:
        return False


def nouveau_token():
    prefixe = PREFIXE_TEST if session_en_base_test() else PREFIXE_REEL
    return prefixe + secrets.token_urlsafe(24)


def connexion(test):
    conn = sqlite3.connect(chemin_base(test))
    conn.row_factory = sqlite3.Row
    return conn


def connexion_token(token):
    """Connexion à la base correspondant au jeton, ou None si jeton mal formé."""
    if not token or len(token) < 20:
        return None
    if token.startswith(PREFIXE_TEST):
        return connexion(True)
    if token.startswith(PREFIXE_REEL):
        return connexion(False)
    return None


def dossier_pieces(declaration):
    base_dir = os.getenv("BA38_BASE_DIR", ".")
    sous = "test" if declaration["token"].startswith(PREFIXE_TEST) else "reel"
    dossier = os.path.join(base_dir, "uploads", "cerfa_frais", sous,
                           str(declaration["campagne_id"]), str(declaration["id"]))
    os.makedirs(dossier, exist_ok=True)
    return dossier


# ============================================================================
# 📚 Lectures
# ============================================================================
def lister_lieux(conn, actifs_seulement=True):
    sql = "SELECT * FROM cerfa_frais_lieux"
    if actifs_seulement:
        sql += " WHERE actif = 1"
    sql += " ORDER BY ordre, id"
    return conn.execute(sql).fetchall()


def lieux_declaration(conn, declaration):
    """Lieux actifs + lieux désactivés depuis mais déjà renseignés par le
    bénévole (pour ne jamais perdre de journées saisies)."""
    lieux = list(lister_lieux(conn, actifs_seulement=False))
    journees = charger_json(declaration["journees_json"]) if declaration else {}
    return [l for l in lieux if l["actif"] or any((journees.get(str(l["id"])) or {}).values())]


def pieces_declaration(conn, declaration_id):
    return conn.execute(
        "SELECT * FROM cerfa_frais_pieces WHERE declaration_id = ? ORDER BY depose_le",
        (declaration_id,),
    ).fetchall()


def libelle_statut(statut):
    return STATUTS.get(statut, statut)


# ============================================================================
# ✉️ Variables des mails / courrier
# ============================================================================
def lien_questionnaire(declaration):
    return url_for("cerfa.frais_questionnaire", token=declaration["token"], _external=True)


def contexte_variables(declaration, campagne, lien=None):
    montant = declaration["montant_arrondi"] if declaration["imposable"] == 1 else declaration["montant"]
    return {
        "civilite": declaration["civilite"] or "Madame, Monsieur",
        "prenom": declaration["prenom"] or "",
        "nom": declaration["nom"] or "",
        "annee_frais": campagne["annee_frais"],
        "annee_declaration": campagne["annee_frais"] + 1,
        "date_limite": _date_fr(campagne["date_limite"]),
        "lien": lien or "",
        "numero": declaration["numero_document"] or "",
        "montant": _nombre_fr(montant),
        "total_journees": declaration["total_journees"] or 0,
        "nb_tickets": campagne["nb_tickets_par_journee"] or 0,
        "prix_ticket": _nombre_fr(campagne["prix_ticket_tag"], 2),
    }


def rendre(texte, contexte):
    return render_modele_email(texte or "", contexte)


def _date_fr(valeur):
    if not valeur:
        return "la date indiquée"
    try:
        a, m, j = str(valeur)[:10].split("-")
        return f"{j}/{m}/{a}"
    except ValueError:
        return str(valeur)


def _nombre_fr(valeur, decimales=None):
    if valeur is None:
        return ""
    v = float(valeur)
    if decimales is None:
        decimales = 0 if v == int(v) else 2
    return f"{v:,.{decimales}f}".replace(",", " ").replace(".", ",")


# ============================================================================
# 📄 Document d'une déclaration validée (CERFA ou courrier remboursement)
# ============================================================================
def nom_fichier_document(declaration):
    prefixe = "CERFA" if declaration["imposable"] == 1 else "Remboursement_trajets"
    nom = "".join(ch for ch in f"{declaration['nom'] or ''}_{declaration['prenom'] or ''}" if ch.isalnum() or ch in "_-")
    return f"{prefixe}_{declaration['numero_document']}_{nom}.pdf"


def construire_document(conn, declaration, campagne):
    """Retourne les octets du PDF (déclaration validée uniquement)."""
    if declaration["statut"] not in STATUTS_VALIDES or not declaration["numero_document"]:
        raise ValueError("Déclaration non validée : aucun document à générer")

    if declaration["imposable"] == 1:
        return generer_cerfa_pdf({
            "numero": declaration["numero_document"],
            "civilite": declaration["civilite"],
            "nom": declaration["nom"],
            "prenom": declaration["prenom"],
            "rue": declaration["rue"],
            "complement_adresse": declaration["complement_adresse"],
            "code_postal": declaration["code_postal"],
            "ville": declaration["ville"],
            "email": declaration["email"],
            "montant_arrondi": declaration["montant_arrondi"],
            "annee_frais": campagne["annee_frais"],
            "date_document": declaration["valide_le"],
        })

    lieux = lieux_declaration(conn, declaration)
    totaux = totaux_declaration(declaration, campagne, lieux)
    civilite = (declaration["civilite"] or "").strip()
    civilite = {"M": "Monsieur", "M.": "Monsieur", "Mme": "Madame"}.get(civilite, civilite)
    adresse = [
        f"{civilite} {declaration['prenom'] or ''} {(declaration['nom'] or '').upper()}".strip(),
        declaration["rue"],
        declaration["complement_adresse"],
        f"{declaration['code_postal'] or ''} {(declaration['ville'] or '').upper()}".strip(),
    ]
    contexte = contexte_variables(declaration, campagne)
    contexte["civilite"] = civilite or "Madame, Monsieur"
    org = get_organisation()
    return generer_courrier_remboursement_pdf({
        "numero": declaration["numero_document"],
        "annee_frais": campagne["annee_frais"],
        "adresse": adresse,
        "texte": rendre(campagne["courrier_remboursement_texte"], contexte),
        "detail": [(p["lieu"]["nom"], p["journees"]) for p in totaux["par_lieu"]],
        "total_journees": declaration["total_journees"],
        "nb_tickets": campagne["nb_tickets_par_journee"],
        "prix_ticket": campagne["prix_ticket_tag"],
        "montant": declaration["montant"],
        "date_document": declaration["valide_le"],
        "signataire_nom": campagne["signataire_nom"] or "",
        "signataire_qualite": campagne["signataire_qualite"] or "",
        "organisation": org,
    })


def statut_document_remis(declaration):
    """Statut après remise du document : terminé pour un CERFA, encore à
    rembourser pour un courrier de remboursement (non-imposable)."""
    return "termine" if declaration["imposable"] == 1 else "courrier_envoye"
