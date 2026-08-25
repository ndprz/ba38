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


from ba38_engagements import engagements_bp


# ============================================================
# UPLOAD FICHIER ENGAGEMENT
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/upload-fichier",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def upload_fichier_engagement(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT statut
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if engagement["statut"] in ("reglee", "comptabilise", "termine"):

            flash(
                "⚠️ Cet engagement est déjà réglé.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

    fichier = request.files.get("fichier")

    write_log(
        f"[UPLOAD] engagement={engagement_id}"
    )

    write_log(
        f"[UPLOAD] fichier="
        f"{fichier.filename if fichier else 'AUCUN'}"
    )



    type_fichier = request.form.get("type_fichier", "").strip()

    write_log(
        f"[UPLOAD] type={type_fichier}"
    )

    # ========================================================
    # VERIFICATIONS
    # ========================================================

    if not fichier or fichier.filename == "":

        flash(
            "⚠️ Aucun fichier sélectionné.",
            "warning"
        )

        return redirect(
            url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id
            )
        )

    # ========================================================
    # SECURITE NOM
    # ========================================================

    nom_original = secure_filename(
        fichier.filename
    )

    extension = os.path.splitext(
        nom_original
    )[1].lower()

    extensions_autorisees = [
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png"
    ]

    if extension not in extensions_autorisees:

        flash(
            "⚠️ Format non autorisé.",
            "danger"
        )

        return redirect(
            url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id
            )
        )

    # ========================================================
    # DOSSIER STOCKAGE
    # ========================================================

    dossier = os.path.join(
        current_app.root_path,
        "uploads",
        "engagements",
        str(engagement_id)
    )

    os.makedirs(
        dossier,
        exist_ok=True
    )

    # ========================================================
    # NOM PHYSIQUE
    # ========================================================

    nom_stockage = (
        f"{uuid.uuid4().hex}{extension}"
    )

    chemin_complet = os.path.join(
        dossier,
        nom_stockage
    )


    write_log(
        f"[UPLOAD] sauvegarde vers "
        f"{chemin_complet}"
    )
    fichier.save(chemin_complet)
    write_log(
        f"[UPLOAD] fichier sauvegardé"
    )
    # ========================================================
    # BASE
    # ========================================================

    with sqlite3.connect(db_path) as conn:

        conn.execute("""
            INSERT INTO engagements_fichiers (
                engagement_id,
                nom_original,
                nom_stockage,
                chemin_fichier,
                type_fichier,
                uploaded_by,
                uploaded_le
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            engagement_id,
            nom_original,
            nom_stockage,
            chemin_complet,
            type_fichier,
            current_user.email
        ))

        # ====================================================
        # WORKFLOW
        # ====================================================

        conn.execute("""
            INSERT INTO engagements_workflow (
                engagement_id,
                action,
                commentaire,
                user_id,
                user_email
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            engagement_id,
            "upload_document",
            f"Document ajouté : {nom_original}",
            current_user.id,
            current_user.email
        ))

        conn.commit()

    # ========================================================
    # LOG
    # ========================================================

    write_log(
        f"[ENGAGEMENTS] Upload fichier "
        f"{nom_original} "
        f"engagement #{engagement_id}"
    )

    flash(
        "✅ Document ajouté.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )







@engagements_bp.route(
    "/engagements/fichier/<int:fichier_id>"
)
@login_required
@require_access("engagements", "lecture")
def voir_fichier_engagement(fichier_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        fichier = conn.execute("""
            SELECT *
            FROM engagements_fichiers
            WHERE id = ?
        """, (fichier_id,)).fetchone()

        if not fichier:
            abort(404)

        if not os.path.exists(
            fichier["chemin_fichier"]
        ):
            abort(404)

        return send_file(
            fichier["chemin_fichier"],
            download_name=fichier["nom_original"],
            mimetype=fichier["mime_type"]
        )




