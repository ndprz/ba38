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
# TRANSMISSION TRESORERIE
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/transmettre-tresorerie",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def transmettre_tresorerie(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT
                e.*,
                d.objet,
                d.montant_total,
                d.type_engagement,
                p.nom_affiche,
                u.email AS tresorier_email

            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            LEFT JOIN engagement_poles p
                ON p.id = e.pole_id

            LEFT JOIN users u
                ON u.id = p.tresorier_user_id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        ancien_statut = engagement["statut"]

        conn.execute("""
            UPDATE engagements
            SET statut = 'a_payer'
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
            "transmission_tresorerie",
            ancien_statut,
            "a_payer",
            "Transmission à la trésorerie",
            current_user.id,
            current_user.email
        ))

        # =====================================================
        # MAIL TRESORERIE
        # =====================================================

        if engagement["tresorier_email"]:

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

    Un engagement est prêt pour règlement.

    Engagement :
    #{engagement_id}

    Objet :
    {engagement["objet"]}

    Montant :
    {engagement["montant_total"]:.2f} €

    Lien :
    {lien}

    ---
    BA38
    """

            envoyer_mail(
                sujet=sujet,
                destinataires=[
                    engagement["tresorier_email"]
                ],
                texte=texte
            )

        conn.commit()

    flash(
        "✅ Demande transmise à la trésorerie.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )




# ============================================================
# TRESORERIE - PAIEMENT / REMBOURSEMENT
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/reglee",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def marquer_reglee(engagement_id):

    numero_ecriture_ebp = request.form.get(
        "numero_ecriture_ebp", ""
    ).strip()

    if not numero_ecriture_ebp:

        flash(
            "⚠️ Le numéro d'écriture EBP est obligatoire "
            "pour marquer l'engagement réglé.",
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
            SELECT
                e.*,
                d.type_engagement
            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        ancien_statut = engagement["statut"]

        # =====================================================
        # DETERMINATION STATUT
        # =====================================================

        nouveau_statut = "reglee"

        commentaire = (
            f"Règlement effectué (écriture EBP n° "
            f"{numero_ecriture_ebp})"
        )

        # =====================================================
        # UPDATE ENGAGEMENT
        # =====================================================

        conn.execute("""
            UPDATE engagements
            SET
                statut = ?,
                paye_le = CURRENT_TIMESTAMP,
                paye_par = ?,
                numero_ecriture_ebp = ?
            WHERE id = ?
        """, (
            nouveau_statut,
            current_user.id,
            numero_ecriture_ebp,
            engagement_id
        ))

        # =====================================================
        # WORKFLOW
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

            "reglement",

            ancien_statut,
            nouveau_statut,

            commentaire,

            current_user.id,
            current_user.email
        ))

        conn.commit()

        # =====================================================
        # LOG
        # =====================================================

        write_log(
            f"[ENGAGEMENTS] Paiement engagement "
            f"#{engagement_id} -> {nouveau_statut}"
        )

    flash(
        "✅ Engagement réglé.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )

# ============================================================
# RETRANSMISSION APRES REFUS
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/retransmettre",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def retransmettre_engagement(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT
                e.*,
                d.objet,
                d.montant_total,
                d.type_engagement,
                d.sous_type_depense,
                p.nom_affiche,
                u.email AS tresorier_email

            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            LEFT JOIN engagement_poles p
                ON p.id = e.pole_id

            LEFT JOIN users u
                ON u.id = p.tresorier_user_id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        # ====================================================
        # Seul le demandeur peut retransmettre
        # ====================================================

        if engagement["demandeur_id"] != current_user.id:

            abort(403)

        if engagement["statut"] != "refuse":

            flash(
                "⚠️ Cette demande n'est pas refusée.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        # ====================================================
        # Détermination du nouveau statut
        # ====================================================


        nouveau_statut = "a_payer"


        ancien_statut = engagement["statut"]

        # ====================================================
        # Mise à jour engagement
        # ====================================================

        conn.execute("""
            UPDATE engagements
            SET statut = ?
            WHERE id = ?
        """, (
            nouveau_statut,
            engagement_id
        ))

        # ====================================================
        # Workflow
        # ====================================================

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
            "retransmission",
            ancien_statut,
            nouveau_statut,
            "Dossier corrigé puis retransmis à la trésorerie",
            current_user.id,
            current_user.email
        ))

        # =====================================================
        # MAIL TRESORERIE
        # =====================================================

        if engagement["tresorier_email"]:

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

            Un engagement préalablement refusé est à nouveau
            prêt pour règlement.

            Engagement :
            #{engagement_id}

            Objet :
            {engagement["objet"]}

            Montant :
            {engagement["montant_total"]:.2f} €

            Lien :
            {lien}

            ---
            BA38
            """

            envoyer_mail(
                sujet=sujet,
                destinataires=[
                    engagement["tresorier_email"]
                ],
                texte=texte
            )

        conn.commit()

    flash(
        "✅ Demande retransmise.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )
