from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required

from utils import get_db_path, write_log, require_access

import sqlite3

from engagements import engagements_bp
from ba38_engagements_abonnements import generer_engagements_abonnements

JOUR_MOIS_MIN = 1
JOUR_MOIS_MAX = 28


# ============================================================
# LISTE DES ABONNEMENTS
# ============================================================

@engagements_bp.route("/engagements/parametres/abonnements")
@login_required
@require_access("engagement_parametres", "lecture")
def gestion_abonnements():

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        abonnements = conn.execute("""
            SELECT
                e.id,
                e.statut,
                e.abonnement_actif,
                e.abonnement_jour_mois,
                e.abonnement_derniere_generation_le,
                p.nom_affiche AS pole_nom,
                d.objet,
                d.montant_total,
                d.fournisseur_nom,
                (
                    SELECT COUNT(*)
                    FROM engagements enf
                    WHERE enf.abonnement_parent_id = e.id
                ) AS nb_generes
            FROM engagements e
            LEFT JOIN engagement_poles p ON p.id = e.pole_id
            LEFT JOIN engagements_depenses d ON d.engagement_id = e.id
            WHERE e.est_modele_abonnement = 1
            ORDER BY e.abonnement_actif DESC, d.objet COLLATE NOCASE
        """).fetchall()

    return render_template(
        "engagements/gestion_abonnements.html",
        abonnements=abonnements
    )


# ============================================================
# ACTIVER / MODIFIER UN ABONNEMENT
# ============================================================

@engagements_bp.route(
    "/engagements/detail_engagement/<int:engagement_id>/abonnement/configurer",
    methods=["POST"]
)
@login_required
@require_access("engagement_parametres", "ecriture")
def abonnement_configurer(engagement_id):

    jour_mois = request.form.get("abonnement_jour_mois", type=int)

    if not jour_mois or not (JOUR_MOIS_MIN <= jour_mois <= JOUR_MOIS_MAX):

        flash(
            f"⚠️ Le jour du mois doit être compris entre "
            f"{JOUR_MOIS_MIN} et {JOUR_MOIS_MAX}.",
            "warning"
        )

        return redirect(url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        ))

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT id, type, deleted
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement or engagement["type"] != "depense":
            abort(404)

        conn.execute("""
            UPDATE engagements
            SET
                est_modele_abonnement = 1,
                abonnement_actif = 1,
                abonnement_jour_mois = ?
            WHERE id = ?
        """, (jour_mois, engagement_id))

        conn.commit()

    write_log(
        f"[ABONNEMENTS] Engagement #{engagement_id} marqué comme "
        f"abonnement (jour {jour_mois} du mois)"
    )

    flash("✅ Abonnement configuré.", "success")

    return redirect(url_for(
        "engagements.detail_engagement",
        engagement_id=engagement_id
    ))


# ============================================================
# PAUSE / REPRISE
# ============================================================

@engagements_bp.route(
    "/engagements/detail_engagement/<int:engagement_id>/abonnement/toggle",
    methods=["POST"]
)
@login_required
@require_access("engagement_parametres", "ecriture")
def abonnement_toggle(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT id, abonnement_actif
            FROM engagements
            WHERE id = ? AND est_modele_abonnement = 1
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        nouvel_etat = 0 if engagement["abonnement_actif"] else 1

        conn.execute("""
            UPDATE engagements
            SET abonnement_actif = ?
            WHERE id = ?
        """, (nouvel_etat, engagement_id))

        conn.commit()

    flash(
        "✅ Abonnement réactivé." if nouvel_etat else "⏸️ Abonnement mis en pause.",
        "success"
    )

    return redirect(request.referrer or url_for(
        "engagements.detail_engagement",
        engagement_id=engagement_id
    ))


# ============================================================
# SUPPRESSION DE L'ABONNEMENT (l'engagement d'origine est conservé)
# ============================================================

@engagements_bp.route(
    "/engagements/detail_engagement/<int:engagement_id>/abonnement/supprimer",
    methods=["POST"]
)
@login_required
@require_access("engagement_parametres", "ecriture")
def abonnement_supprimer(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.execute("""
            UPDATE engagements
            SET
                est_modele_abonnement = 0,
                abonnement_actif = 1,
                abonnement_jour_mois = NULL,
                abonnement_derniere_generation_le = NULL
            WHERE id = ?
        """, (engagement_id,))

        conn.commit()

    write_log(f"[ABONNEMENTS] Abonnement retiré de l'engagement #{engagement_id}")

    flash("✅ Cet engagement n'est plus un abonnement récurrent.", "success")

    return redirect(request.referrer or url_for(
        "engagements.detail_engagement",
        engagement_id=engagement_id
    ))


# ============================================================
# GENERATION MANUELLE IMMEDIATE
# ============================================================

@engagements_bp.route(
    "/engagements/parametres/abonnements/generer_maintenant",
    methods=["POST"]
)
@login_required
@require_access("engagement_parametres", "ecriture")
def abonnement_generer_maintenant():

    crees = generer_engagements_abonnements(db_path=get_db_path())

    if crees:
        flash(
            f"✅ {len(crees)} engagement(s) généré(s) : "
            + ", ".join(f"#{i}" for i in crees),
            "success"
        )
    else:
        flash("ℹ️ Aucun abonnement à générer aujourd'hui.", "info")

    return redirect(url_for("engagements.gestion_abonnements"))
