import os
import sqlite3
from datetime import datetime, timedelta

import jwt

from utils import get_db_path, write_log, envoyer_mail

# ============================================================================
# Configuration
# ============================================================================
# Doit rester identique à VALIDATION_POLE_TOKEN_VALIDITE_JOURS (utils.py) :
# les liens de validation générés ici sont vérifiés par
# engagements.routes_workflow.valider_engagement_pole_lien via
# utils.verifier_token_validation_pole().
VALIDATION_POLE_TOKEN_VALIDITE_JOURS = 14


def _base_url():
    return os.getenv("APP_BASE_URL", "").rstrip("/")


def _generer_token_validation_pole(engagement_id, user_id):
    secret = os.getenv("FLASK_SECRET_KEY")

    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY non défini")

    payload = {
        "action": "valider_pole",
        "engagement_id": engagement_id,
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(
            days=VALIDATION_POLE_TOKEN_VALIDITE_JOURS
        )
    }

    return jwt.encode(payload, secret, algorithm="HS256")


def _trouver_palier(conn, montant):
    return conn.execute("""
        SELECT *
        FROM engagements_parametres
        WHERE actif = 1
        AND montant_max >= ?
        ORDER BY montant_max
        LIMIT 1
    """, (str(montant),)).fetchone()


def _notifier_pole(conn, modele, engagement_id, statut, montant):

    pole = conn.execute("""
        SELECT
            p.nom_affiche,
            p.responsable_id,
            p.suppleant1_id,
            p.suppleant2_id,
            u1.email AS responsable_email,
            u2.email AS supp1_email,
            u3.email AS supp2_email
        FROM engagement_poles p
        LEFT JOIN users u1 ON u1.id = p.responsable_id
        LEFT JOIN users u2 ON u2.id = p.suppleant1_id
        LEFT JOIN users u3 ON u3.id = p.suppleant2_id
        WHERE p.id = ?
    """, (modele["pole_id"],)).fetchone()

    if not pole:
        return

    sujet = f"Engagement récurrent généré automatiquement #{engagement_id}"
    lien = f"{_base_url()}/detail_engagement/{engagement_id}"

    if statut == "validation_pole":

        destinataires_pole = list(set(filter(None, [
            (pole["responsable_id"], pole["responsable_email"])
            if pole["responsable_id"] and pole["responsable_email"] else None,
            (pole["suppleant1_id"], pole["supp1_email"])
            if pole["suppleant1_id"] and pole["supp1_email"] else None,
            (pole["suppleant2_id"], pole["supp2_email"])
            if pole["suppleant2_id"] and pole["supp2_email"] else None,
        ])))

        for user_id, user_email in destinataires_pole:

            token = _generer_token_validation_pole(engagement_id, user_id)
            lien_validation = (
                f"{_base_url()}/engagements/{engagement_id}"
                f"/valider-pole-lien/{token}"
            )

            texte = f"""
Bonjour,

Un engagement récurrent (abonnement) vient d'être généré
automatiquement et nécessite votre validation.

Pôle :
{pole["nom_affiche"]}

Objet :
{modele["objet"]}

Montant :
{montant:.2f} €

Valider directement, sans vous connecter :
{lien_validation}

Ou en vous connectant à l'application :
{lien}

---
BA38
"""

            envoyer_mail(sujet=sujet, destinataires=[user_email], texte=texte)

    else:

        destinataires = list(set(filter(None, [
            pole["responsable_email"],
            pole["supp1_email"],
            pole["supp2_email"],
        ])))

        texte = f"""
Bonjour,

Un engagement récurrent (abonnement) vient d'être généré
automatiquement.

Pôle :
{pole["nom_affiche"]}

Objet :
{modele["objet"]}

Montant :
{montant:.2f} €

Accéder à la demande :
{lien}

---
BA38
"""

        envoyer_mail(sujet=sujet, destinataires=destinataires, texte=texte)


def _creer_engagement_enfant(conn, modele, aujourdhui):

    montant = modele["montant_total"]

    palier = _trouver_palier(conn, montant)

    if not palier:
        write_log(
            f"⚠️ [ABONNEMENTS] Aucun palier de validation configuré pour "
            f"le montant {montant} (modèle #{modele['engagement_id']}) "
            f"— génération ignorée."
        )
        return None

    if palier["accord_resp_pole"] == "o":
        statut = "validation_pole"
    elif palier["accord_presidence"] == "o":
        statut = "validation_presidence"
    else:
        statut = "valide"

    cur = conn.execute("""
        INSERT INTO engagements (
            type,
            demandeur_id,
            demandeur_nom,
            demandeur_email,
            pole_id,
            statut,
            signature_le,
            signature_user_agent,
            abonnement_parent_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "depense",
        modele["demandeur_id"],
        modele["demandeur_nom"],
        modele["demandeur_email"],
        modele["pole_id"],
        statut,
        aujourdhui.isoformat(),
        "Génération automatique mensuelle (abonnement)",
        modele["engagement_id"],
    ))

    engagement_id = cur.lastrowid

    conn.execute("""
        INSERT INTO engagements_depenses (
            engagement_id,
            objet,
            description,
            rubrique,
            precision_rubrique,
            subvention_id,
            montant_total,
            attestation_comparaison,
            devis_necessaire,
            nb_devis,
            type_engagement,
            beneficiaire_user_id,
            beneficiaire_benevole_id,
            beneficiaire_nom,
            fournisseur_id,
            fournisseur_nom,
            fournisseur_adresse,
            fournisseur_telephone,
            fournisseur_email,
            fournisseur_iban,
            sous_type_depense
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """, (
        engagement_id,
        modele["objet"],
        modele["description"],
        modele["rubrique"],
        modele["precision_rubrique"],
        modele["subvention_id"],
        montant,
        modele["type_engagement"],
        modele["beneficiaire_user_id"],
        modele["beneficiaire_benevole_id"],
        modele["beneficiaire_nom"],
        modele["fournisseur_id"],
        modele["fournisseur_nom"],
        modele["fournisseur_adresse"],
        modele["fournisseur_telephone"],
        modele["fournisseur_email"],
        modele["fournisseur_iban"],
        modele["sous_type_depense"],
    ))

    conn.execute("""
        INSERT INTO engagements_workflow (
            engagement_id,
            action,
            ancien_statut,
            nouveau_statut,
            commentaire,
            user_id,
            user_email
        )
        VALUES (?, 'generation_auto_abonnement', NULL, ?, ?, NULL, 'system')
    """, (
        engagement_id,
        statut,
        f"Génération automatique mensuelle à partir de "
        f"l'abonnement #{modele['engagement_id']}"
    ))

    _notifier_pole(conn, modele, engagement_id, statut, montant)

    return engagement_id


def generer_engagements_abonnements(db_path=None, aujourdhui=None):
    """
    Parcourt les engagements marqués comme modèles d'abonnement actifs
    et crée, pour chacun d'eux, un nouvel engagement "enfant" (même
    objet / montant / fournisseur / pôle) dès que le jour du mois
    configuré est atteint et qu'aucun enfant n'a déjà été généré pour
    le mois en cours.

    Utilisable aussi bien depuis une route Flask (bouton "Générer
    maintenant") que depuis le script cron scripts_taches/
    generer_engagements_abonnements.py (aucune dépendance à
    current_app / request).

    Retourne la liste des ids des engagements créés.
    """

    db_path = db_path or get_db_path()
    aujourdhui = aujourdhui or datetime.now()
    mois_courant = aujourdhui.strftime("%Y-%m")

    crees = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        modeles = conn.execute("""
            SELECT
                e.id AS engagement_id,
                e.demandeur_id,
                e.demandeur_nom,
                e.demandeur_email,
                e.pole_id,
                e.abonnement_jour_mois,
                e.abonnement_derniere_generation_le,
                d.objet,
                d.description,
                d.rubrique,
                d.precision_rubrique,
                d.subvention_id,
                d.montant_total,
                d.type_engagement,
                d.beneficiaire_user_id,
                d.beneficiaire_benevole_id,
                d.beneficiaire_nom,
                d.fournisseur_id,
                d.fournisseur_nom,
                d.fournisseur_adresse,
                d.fournisseur_telephone,
                d.fournisseur_email,
                d.fournisseur_iban,
                d.sous_type_depense
            FROM engagements e
            JOIN engagements_depenses d ON d.engagement_id = e.id
            WHERE e.est_modele_abonnement = 1
            AND e.abonnement_actif = 1
            AND e.deleted = 0
            AND e.abonnement_jour_mois IS NOT NULL
            AND e.abonnement_jour_mois <= ?
        """, (aujourdhui.day,)).fetchall()

        for modele in modeles:

            deja_genere_ce_mois = (
                modele["abonnement_derniere_generation_le"]
                and modele["abonnement_derniere_generation_le"][:7]
                == mois_courant
            )

            if deja_genere_ce_mois:
                continue

            nouvel_id = _creer_engagement_enfant(conn, modele, aujourdhui)

            if not nouvel_id:
                continue

            conn.execute("""
                UPDATE engagements
                SET abonnement_derniere_generation_le = ?
                WHERE id = ?
            """, (aujourdhui.isoformat(), modele["engagement_id"]))

            crees.append(nouvel_id)

            write_log(
                f"✅ [ABONNEMENTS] Engagement #{nouvel_id} généré "
                f"depuis le modèle #{modele['engagement_id']}"
            )

        conn.commit()

    return crees
