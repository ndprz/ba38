# ba38_engagements.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash, abort
from flask import current_app
from flask_login import login_required, current_user
from ba38_utilitaires.core import get_db_path, get_db_connection, has_access, write_log, require_access
from ba38_utilitaires.core import get_real_ip
from ba38_utilitaires.core import envoyer_mail, is_valid_iban
from ba38_utilitaires.core import verifier_token_validation_pole
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime
from werkzeug.utils import secure_filename
from decimal import Decimal
from pypdf import PdfReader, PdfWriter

import sqlite3
import os
import uuid


from ba38_engagements import engagements_bp



# ============================================================
# VALIDATION POLE
# ============================================================

def _executer_validation_pole(
    conn,
    engagement,
    pole,
    engagement_id,
    user_id,
    user_email
):
    """Applique la validation pôle sur un engagement déjà vérifié
    (pôle existant, utilisateur autorisé). Mutualisé entre la
    validation classique (connectée) et la validation par lien
    sécurisé (sans connexion). Retourne (nouveau_statut, erreur)."""

    # =====================================================
    # RECHERCHE PALIER
    # =====================================================

    montant = engagement["montant_total"] or 0

    palier = conn.execute("""
        SELECT *
        FROM engagements_parametres
        WHERE actif = 1
        AND montant_max >= ?
        ORDER BY montant_max
        LIMIT 1
    """, (str(montant),)).fetchone()

    if not palier:
        return None, "Aucun palier trouvé."

    # =====================================================
    # WORKFLOW
    # =====================================================

    ancien_statut = engagement["statut"]

    # -----------------------------------------------------
    # Déplacement
    # -----------------------------------------------------

    if engagement["sous_type_depense"] == "deplacement":

        nouveau_statut = "a_payer"

    # -----------------------------------------------------
    # Workflow normal
    # -----------------------------------------------------

    elif palier["accord_presidence"] == "o":

        nouveau_statut = "validation_presidence"

    else:

        nouveau_statut = "valide"

    # =====================================================
    # UPDATE ENGAGEMENT
    # =====================================================

    conn.execute("""
        UPDATE engagements
        SET
            statut = ?,
            valide_par_pole_le = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (
        nouveau_statut,
        engagement_id
    ))

    # =====================================================
    # HISTORIQUE
    # =====================================================

    # Le badge "Validé Pôle" de la liste des engagements se base sur
    # nouveau_statut = "valide" dans l'historique. Pour un déplacement,
    # le statut réel saute directement à "a_payer" (transmission auto),
    # donc on historise "valide" ici pour que le jalon "pôle a validé"
    # reste visible, sans changer le statut réellement appliqué ci-dessus.
    statut_historique_validation_pole = (
        "valide"
        if engagement["sous_type_depense"] == "deplacement"
        else nouveau_statut
    )

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
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        engagement_id,
        "validation_pole",
        ancien_statut,
        statut_historique_validation_pole,
        "Validation du responsable de pôle",
        user_id,
        user_email
    ))

    if engagement["sous_type_depense"] == "deplacement":

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
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,
            "transmission_auto_tresorerie",
            "valide",
            "a_payer",
            "Transmission automatique à la trésorerie (déplacement)",
            user_id,
            user_email
        ))

        # =====================================================
        # MAIL TRESORERIE AUTO (DEPLACEMENT)
        # =====================================================

        tresorier = conn.execute("""
            SELECT
                u.email AS tresorier_email,
                p.nom_affiche

            FROM engagement_poles p

            LEFT JOIN users u
                ON u.id = p.tresorier_user_id

            WHERE p.id = ?
        """, (
            engagement["pole_id"],
        )).fetchone()

        if tresorier and tresorier["tresorier_email"]:

            lien = url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id,
                _external=True
            )

            sujet = (
                f"Engagement prêt pour règlement "
                f"#{engagement_id}"
            )

            texte = f"""
    Bonjour,

    Une note de frais déplacement est prête
    pour remboursement.

    Engagement :
    #{engagement_id}

    Montant :
    {montant:.2f} €

    Lien :
    {lien}

    ---
    BA38
    """

            envoyer_mail(
                sujet=sujet,
                destinataires=[
                    tresorier["tresorier_email"]
                ],
                texte=texte
            )

            write_log(
                f"[ENGAGEMENTS] Mail trésorerie envoyé à "
                f"{tresorier['tresorier_email']}"
            )

    conn.commit()

    # =====================================================
    # MAIL presidence
    # =====================================================

    if nouveau_statut == "validation_presidence":

        lien = url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id,
            _external=True
        )

        sujet = (
            f"Validation presidence requise "
            f"#{engagement_id}"
        )

        texte = f"""
    Bonjour,

    Une demande d'engagement nécessite
    une validation presidence.

    Engagement :
    #{engagement_id}

    Montant :
    {montant:.2f} €

    Accès :
    {lien}

    ---
    BA38
    """

        destinataires = []

        if pole["validation_presidence_email"]:

            destinataires = [

                x.strip()

                for x in pole[
                    "validation_presidence_email"
                ].split(";")

                if x.strip()

            ]

        if destinataires:

            envoyer_mail(
                sujet=sujet,
                destinataires=destinataires,
                texte=texte
            )

            write_log(
                f"[ENGAGEMENTS] Mail presidence envoyé à "
                f"{destinataires}"
            )

    # =====================================================
    # LOG
    # =====================================================

    write_log(
        f"[ENGAGEMENTS] Validation pôle "
        f"#{engagement_id} -> {nouveau_statut}"
    )

    return nouveau_statut, None


@engagements_bp.route("/engagements/<int:engagement_id>/valider-pole", methods=["POST"])
@login_required
@require_access("engagements", "lecture")
def valider_engagement_pole(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        # =====================================================
        # ENGAGEMENT
        # =====================================================

        engagement = conn.execute("""
            SELECT
                e.*,
                d.montant_total,
                d.sous_type_depense
            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        # =====================================================
        # VERIFICATION POLE
        # =====================================================

        pole = conn.execute("""
            SELECT
                responsable_id,
                suppleant1_id,
                suppleant2_id,
                validation_presidence_email

            FROM engagement_poles

            WHERE id = ?
        """, (engagement["pole_id"],)).fetchone()

        if not pole:
            abort(403)

        # Si le demandeur est le responsable, seul le suppléant 1 valide.
        # (règle désactivée en DEV pour faciliter les tests)
        est_prod = os.getenv("ENVIRONMENT", "prod").lower() != "dev"

        demandeur_est_responsable = (
            est_prod
            and engagement["demandeur_id"] == pole["responsable_id"]
        )

        if demandeur_est_responsable:
            ids_autorises = [pole["suppleant1_id"]]
        else:
            ids_autorises = [
                pole["responsable_id"],
                pole["suppleant1_id"],
                pole["suppleant2_id"]
            ]

        if current_user.id not in ids_autorises:

            if demandeur_est_responsable and current_user.id == engagement["demandeur_id"]:
                flash(
                    "⚠️ Vous êtes l'auteur de la demande et responsable de pôle, "
                    "vous ne pouvez pas valider votre propre engagement. "
                    "La validation a été demandée au suppléant.",
                    "warning"
                )
            else:
                flash(
                    "⛔ Vous n'êtes pas autorisé à valider cet engagement.",
                    "danger"
                )

            return redirect(url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id
            ))

        nouveau_statut, erreur = _executer_validation_pole(
            conn,
            engagement,
            pole,
            engagement_id,
            current_user.id,
            current_user.email
        )

    # =========================================================
    # MESSAGE
    # =========================================================

    if erreur:

        flash(
            f"⚠️ {erreur}",
            "danger"
        )

    elif nouveau_statut == "validation_presidence":

        flash(
            "✅ Validation pôle effectuée. "
            "Validation presidence requise.",
            "warning"
        )

    else:

        flash(
            "✅ Engagement validé.",
            "success"
        )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )


# ============================================================
# VALIDATION POLE PAR LIEN SECURISE (SANS CONNEXION)
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/valider-pole-lien/<token>",
    methods=["GET", "POST"]
)
def valider_engagement_pole_lien(engagement_id, token):

    payload = verifier_token_validation_pole(token)

    if not payload or payload.get("engagement_id") != engagement_id:

        return render_template(
            "engagements/lien_validation_invalide.html"
        ), 400

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT
                e.*,
                d.montant_total,
                d.sous_type_depense,
                d.objet
            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        pole = conn.execute("""
            SELECT
                responsable_id,
                suppleant1_id,
                suppleant2_id,
                validation_presidence_email,
                nom_affiche

            FROM engagement_poles

            WHERE id = ?
        """, (engagement["pole_id"],)).fetchone()

        if not pole:
            abort(403)

        user_id = payload.get("user_id")

        # Si le demandeur est le responsable, seul le suppléant 1 valide.
        # (règle désactivée en DEV pour faciliter les tests)
        est_prod = os.getenv("ENVIRONMENT", "prod").lower() != "dev"

        if est_prod and engagement["demandeur_id"] == pole["responsable_id"]:
            ids_autorises = [pole["suppleant1_id"]]
        else:
            ids_autorises = [
                pole["responsable_id"],
                pole["suppleant1_id"],
                pole["suppleant2_id"]
            ]

        if user_id not in ids_autorises:

            return render_template(
                "engagements/lien_validation_invalide.html"
            ), 403

        user = conn.execute(
            "SELECT email, username FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

        if not user:

            return render_template(
                "engagements/lien_validation_invalide.html"
            ), 403

        deja_traite = engagement["statut"] != "validation_pole"

        if request.method == "GET" or deja_traite:

            return render_template(
                "engagements/valider_pole_lien.html",
                engagement=engagement,
                pole=pole,
                user=user,
                deja_traite=deja_traite
            )

        # =================================================
        # POST -> EXECUTION DE LA VALIDATION
        # =================================================

        nouveau_statut, erreur = _executer_validation_pole(
            conn,
            engagement,
            pole,
            engagement_id,
            user_id,
            user["email"]
        )

    return render_template(
        "engagements/valider_pole_lien.html",
        engagement=engagement,
        pole=pole,
        user=user,
        deja_traite=True,
        nouveau_statut=nouveau_statut,
        erreur=erreur
    )


# ============================================================
# VALIDATION presidence
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/valider-presidence",
    methods=["POST"]
)
@login_required
@require_access("engagements", "admin")
def valider_engagement_presidence(engagement_id):

    commentaire = request.form.get(
        "commentaire", ""
    ).strip() or "Validation presidence"

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT *
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        ancien_statut = engagement["statut"]

        conn.execute("""
            UPDATE engagements
            SET
                statut = 'valide',
                valide_par_presidence_le = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (engagement_id,))

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
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,
            "validation_presidence",
            ancien_statut,
            "valide",
            commentaire,
            current_user.id,
            current_user.email
        ))


        conn.commit()

        write_log(
            f"[ENGAGEMENTS] Validation presidence "
            f"#{engagement_id}"
        )

    flash(
        "✅ Validation presidence effectuée.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )



# ============================================================
# REFUS ENGAGEMENT
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/refuser",
    methods=["POST"]
)
@login_required
@require_access("engagements", "lecture")
def refuser_engagement(engagement_id):

    motif = request.form.get("motif_refus")

    if not motif:

        flash(
            "⚠️ Motif de refus obligatoire.",
            "warning"
        )

        return redirect(
            url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id
            )
        )

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT *
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        ancien_statut = engagement["statut"]

        # =====================================================
        # UPDATE
        # =====================================================

        conn.execute("""
            UPDATE engagements
            SET
                statut = 'refuse',
                refuse_le = CURRENT_TIMESTAMP,
                refuse_motif = ?
            WHERE id = ?
        """, (
            motif,
            engagement_id
        ))

        # =====================================================
        # HISTORIQUE
        # =====================================================

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
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,

            "refus",

            ancien_statut,
            "refuse",

            motif,

            current_user.id,
            current_user.email
        ))

        conn.execute("""
            INSERT INTO engagements_commentaires (
                engagement_id,
                commentaire,
                user_id,
                cree_le
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            engagement_id,
            f"REFUS : {motif}",
            current_user.id
        ))

        conn.commit()

        # =====================================================
        # MAIL
        # =====================================================

        sujet = (
            f"Demande d'engagement refusée "
            f"#{engagement_id}"
        )

        texte = f"""
            Bonjour,

            Votre demande d'engagement #{engagement_id}
            a été refusée.

            Motif :

            {motif}

            ---
            BA38
            """

        envoyer_mail(
            sujet=sujet,
            destinataires=[engagement["demandeur_email"]],
            texte=texte
        )

        # =====================================================
        # LOG
        # =====================================================

        write_log(
            f"[ENGAGEMENTS] Refus engagement "
            f"#{engagement_id}"
        )

    flash(
        "❌ Engagement refusé.",
        "warning"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )




# ============================================================
# CLOTURE ENGAGEMENT
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/cloturer",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def cloturer_engagement(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT *
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        ancien_statut = engagement["statut"]

        # =====================================================
        # CLOTURE
        # =====================================================

        conn.execute("""
            UPDATE engagements
            SET
                statut = 'termine'
            WHERE id = ?
        """, (engagement_id,))

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
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,
            "cloture",
            ancien_statut,
            "termine",
            "Clôture dossier engagement",
            current_user.id,
            current_user.email
        ))

        conn.commit()

        write_log(
            f"[ENGAGEMENTS] Clôture engagement "
            f"#{engagement_id}"
        )
        write_log(
            f"[ENGAGEMENTS] UPDATE termine #{engagement_id}"
        )

    flash(
        "✅ Engagement clôturé.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )




# ============================================================
# SUPPRESSION LOGIQUE ENGAGEMENT
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/delete",
    methods=["POST"]
)
@login_required
@require_access("engagements", "admin")
def delete_engagement(engagement_id):

    """
    Suppression logique d’un engagement.

    Le principe est :
    - conserver le workflow
    - conserver les PDF
    - conserver les commentaires
    - conserver l’historique

    L’engagement est simplement marqué comme supprimé
    via le champ deleted = 1.

    Cela permet :
    - audit
    - traçabilité
    - restauration éventuelle
    - conservation des devis et validations

    Aucune suppression physique des fichiers n’est réalisée.
    """

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        # =====================================================
        # RECHERCHE ENGAGEMENT
        # =====================================================

        engagement = conn.execute("""
            SELECT *
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:

            flash(
                "⚠️ Engagement introuvable.",
                "warning"
            )

            return redirect(
                url_for("engagements.engagements_main")
            )

        # =====================================================
        # DEJA SUPPRIME
        # =====================================================

        if engagement["deleted"] == 1:

            flash(
                "⚠️ Cet engagement est déjà supprimé.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        # =====================================================
        # MODELE D'ABONNEMENT : ARCHIVAGE INTERDIT
        # =====================================================
        # Archiver le modèle stoppe silencieusement la génération
        # automatique des prochaines échéances (cf. incident du
        # 17/08/2026 sur l'abonnement Mailjet).

        if engagement["est_modele_abonnement"] == 1:

            flash(
                "⛔ Cet engagement est le modèle d'un abonnement "
                "récurrent. L'archiver stopperait la génération "
                "automatique des prochaines échéances. Désactivez "
                "d'abord l'abonnement si vous voulez y mettre fin.",
                "danger"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        # =====================================================
        # SUPPRESSION LOGIQUE
        # =====================================================

        conn.execute("""
            UPDATE engagements
            SET
                deleted = 1,
                deleted_le = CURRENT_TIMESTAMP,
                deleted_by = ?
            WHERE id = ?
        """, (
            current_user.id,
            engagement_id
        ))

        # =====================================================
        # HISTORIQUE WORKFLOW
        # =====================================================

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
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,

            "suppression",

            engagement["statut"],
            "supprime",

            "Suppression logique engagement",

            current_user.id,
            current_user.email
        ))

        conn.commit()

        # =====================================================
        # LOG APPLICATIF
        # =====================================================

        write_log(
            f"[ENGAGEMENTS] Suppression logique "
            f"engagement #{engagement_id} "
            f"par {current_user.email}"
        )

    # =========================================================
    # MESSAGE UTILISATEUR
    # =========================================================

    flash(
        "✅ Engagement archivé avec succès.",
        "success"
    )

    return redirect(
        url_for("engagements.engagements_main")
    )


# ============================================================
# RESTAURATION ENGAGEMENT (DESARCHIVAGE)
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/restaurer",
    methods=["POST"]
)
@login_required
@require_access("engagements", "admin")
def restaurer_engagement(engagement_id):

    """
    Annule la suppression logique d'un engagement (désarchivage),
    en sens inverse de delete_engagement().
    """

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT *
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:

            flash(
                "⚠️ Engagement introuvable.",
                "warning"
            )

            return redirect(
                url_for("engagements.engagements_main")
            )

        # =====================================================
        # CONTROLE : engagement effectivement archivé
        # =====================================================

        if engagement["deleted"] != 1:

            flash(
                "⚠️ Cet engagement n'est pas archivé.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        conn.execute("""
            UPDATE engagements
            SET
                deleted = 0,
                deleted_le = NULL,
                deleted_by = NULL
            WHERE id = ?
        """, (engagement_id,))

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
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,

            "restauration",

            "supprime",
            engagement["statut"],

            "Désarchivage (restauration) engagement",

            current_user.id,
            current_user.email
        ))

        conn.commit()

        write_log(
            f"[ENGAGEMENTS] Désarchivage engagement "
            f"#{engagement_id} par {current_user.email}"
        )

    flash(
        "✅ Engagement désarchivé avec succès.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )


# ============================================================
# SUPPRESSION LOGIQUE GROUPEE (ARCHIVAGE MASSE)
# ============================================================

@engagements_bp.route(
    "/engagements/archiver-masse",
    methods=["POST"]
)
@login_required
@require_access("engagements", "admin")
def archiver_engagements_masse():

    """
    Archivage logique de plusieurs engagements sélectionnés
    depuis la liste principale.

    Reprend exactement le contrôle de delete_engagement() pour
    chaque ligne : l'engagement doit exister, ne pas être déjà
    archivé, et ne pas être le modèle d'un abonnement récurrent.
    Les lignes qui ne remplissent pas cette condition sont
    ignorées (et comptabilisées) plutôt que de faire échouer
    toute l'opération.
    """

    show_deleted = request.form.get(
        "show_deleted"
    ) == "1"

    ids = []

    for valeur in request.form.getlist("engagement_ids"):

        try:
            ids.append(int(valeur))
        except (TypeError, ValueError):
            continue

    if not ids:

        flash(
            "⚠️ Aucun engagement sélectionné.",
            "warning"
        )

        return redirect(
            url_for(
                "engagements.engagements_main",
                show_deleted="1" if show_deleted else "0"
            )
        )

    db_path = get_db_path()

    nb_archives = 0
    nb_ignores = 0

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        for engagement_id in ids:

            engagement = conn.execute("""
                SELECT *
                FROM engagements
                WHERE id = ?
            """, (engagement_id,)).fetchone()

            # =====================================================
            # CONTROLE : engagement existant et pas déjà archivé
            # =====================================================

            if (
                not engagement
                or engagement["deleted"] == 1
                or engagement["est_modele_abonnement"] == 1
            ):
                nb_ignores += 1
                continue

            conn.execute("""
                UPDATE engagements
                SET
                    deleted = 1,
                    deleted_le = CURRENT_TIMESTAMP,
                    deleted_by = ?
                WHERE id = ?
            """, (
                current_user.id,
                engagement_id
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
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                engagement_id,

                "suppression",

                engagement["statut"],
                "supprime",

                "Suppression logique engagement (action groupée)",

                current_user.id,
                current_user.email
            ))

            nb_archives += 1

        conn.commit()

        write_log(
            f"[ENGAGEMENTS] Archivage groupé : "
            f"{nb_archives} archivé(s), {nb_ignores} ignoré(s) "
            f"par {current_user.email}"
        )

    if nb_archives:

        flash(
            f"✅ {nb_archives} engagement(s) archivé(s).",
            "success"
        )

    if nb_ignores:

        flash(
            f"⚠️ {nb_ignores} engagement(s) ignoré(s) "
            f"(introuvable ou déjà archivé).",
            "warning"
        )

    return redirect(
        url_for(
            "engagements.engagements_main",
            show_deleted="1" if show_deleted else "0"
        )
    )


# ============================================================
# PURGE PHYSIQUE ENGAGEMENT
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/purge",
    methods=["POST"]
)
@login_required
@require_access("engagements", "admin")
def purge_engagement(engagement_id):

    """
    Suppression PHYSIQUE définitive.

    ATTENTION :
    - supprime SQL
    - supprime workflow
    - supprime commentaires
    - supprime PDF
    - supprime dossier fichiers

    Réservé administration technique.
    """

    import os
    import shutil

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        # =====================================================
        # RECHERCHE ENGAGEMENT
        # =====================================================

        engagement = conn.execute("""
            SELECT *
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:

            flash(
                "⚠️ Engagement introuvable.",
                "warning"
            )

            return redirect(
                url_for("engagements.engagements_main")
            )

        # =====================================================
        # CONTROLE : SAISIE DE CONFIRMATION
        # =====================================================

        confirmation = request.form.get(
            "confirmation", ""
        ).strip().upper()

        if confirmation != "SUPPRIMER":

            flash(
                "⚠️ Purge annulée : confirmation incorrecte.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        # =====================================================
        # CONTROLE : ECRITURE EBP DEJA RENSEIGNEE
        # =====================================================

        if engagement["numero_ecriture_ebp"]:

            confirmer_ebp = request.form.get(
                "confirmer_ebp"
            ) == "1"

            if not confirmer_ebp:

                flash(
                    f"⚠️ Purge annulée : cet engagement possède "
                    f"une écriture comptable EBP n° "
                    f"« {engagement['numero_ecriture_ebp']} ». "
                    f"Confirmez explicitement pour la supprimer "
                    f"malgré tout.",
                    "warning"
                )

                return redirect(
                    url_for(
                        "engagements.detail_engagement",
                        engagement_id=engagement_id
                    )
                )

        # =====================================================
        # SUPPRESSION DOSSIER PDF
        # =====================================================

        upload_dir = os.path.join(
            current_app.root_path,
            "uploads",
            "engagements",
            str(engagement_id)
        )

        if os.path.exists(upload_dir):

            try:

                shutil.rmtree(upload_dir)

                write_log(
                    f"[ENGAGEMENTS] DOSSIER PDF SUPPRIME "
                    f"{upload_dir}"
                )

            except Exception as e:

                write_log(
                    f"[ENGAGEMENTS] ERREUR SUPPRESSION "
                    f"DOSSIER PDF : {e}"
                )

        # =====================================================
        # SUPPRESSION TABLES FILLES
        # =====================================================

        conn.execute("""
            DELETE FROM engagements_workflow
            WHERE engagement_id = ?
        """, (engagement_id,))

        conn.execute("""
            DELETE FROM engagements_commentaires
            WHERE engagement_id = ?
        """, (engagement_id,))

        conn.execute("""
            DELETE FROM engagements_fichiers
            WHERE engagement_id = ?
        """, (engagement_id,))

        conn.execute("""
            DELETE FROM engagements_depenses
            WHERE engagement_id = ?
        """, (engagement_id,))

        # =====================================================
        # SUPPRESSION TABLE PRINCIPALE
        # =====================================================

        conn.execute("""
            DELETE FROM engagements
            WHERE id = ?
        """, (engagement_id,))

        conn.commit()

        # =====================================================
        # LOG
        # =====================================================

        write_log(
            f"[ENGAGEMENTS] PURGE PHYSIQUE "
            f"engagement #{engagement_id} "
            f"par {current_user.email}"
            + (
                f" — ATTENTION : écriture EBP n° "
                f"{engagement['numero_ecriture_ebp']} supprimée"
                if engagement["numero_ecriture_ebp"]
                else ""
            )
        )

    flash(
        "✅ Engagement supprimé définitivement.",
        "success"
    )

    return redirect(
        url_for("engagements.engagements_main")
    )




