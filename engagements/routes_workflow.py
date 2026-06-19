# ba38_engagements.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash, abort
from flask import current_app
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, has_access, write_log, require_access
from utils import get_real_ip
from utils import envoyer_mail, is_valid_iban
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime
from werkzeug.utils import secure_filename
from decimal import Decimal
from pypdf import PdfReader, PdfWriter

import sqlite3
import os
import uuid


from engagements import engagements_bp



# ============================================================
# VALIDATION POLE
# ============================================================

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

        ids_autorises = [
            pole["responsable_id"],
            pole["suppleant1_id"],
            pole["suppleant2_id"]
        ]

        if current_user.id not in ids_autorises:
            abort(403)

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

            flash(
                "⚠️ Aucun palier trouvé.",
                "danger"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

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
            nouveau_statut,
            "Validation du responsable de pôle",
            current_user.id,
            current_user.email
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
                "validation_pole",
                "a_payer",
                "Transmission automatique à la trésorerie (déplacement)",
                current_user.id,
                current_user.email
            ))


            # =====================================================
            # MAIL TRESORERIE AUTO (DEPLACEMENT)
            # =====================================================

            if engagement["sous_type_depense"] == "deplacement":

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

    # =========================================================
    # MESSAGE
    # =========================================================

    if nouveau_statut == "validation_presidence":

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
# VALIDATION presidence
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/valider-presidence",
    methods=["POST"]
)
@login_required
@require_access("engagements", "admin")
def valider_engagement_presidence(engagement_id):

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
            "Validation presidence",
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
        # SUPPRESSION DOSSIER PDF
        # =====================================================

        upload_dir = (
            f"/srv/ba38/uploads/engagements/{engagement_id}"
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
        )

    flash(
        "✅ Engagement supprimé définitivement.",
        "success"
    )

    return redirect(
        url_for("engagements.engagements_main")
    )




