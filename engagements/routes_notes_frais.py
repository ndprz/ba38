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



# ================================================
# NOTE DE FRAIS
# ============================================================

@engagements_bp.route("/engagements/<int:engagement_id>/note-frais", methods=["GET", "POST"])
@login_required
@require_access("engagements", "ecriture")
def note_frais_engagement(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        # =====================================================
        # ENGAGEMENT
        # =====================================================

        engagement = conn.execute("""

            SELECT
                e.*,

                d.objet,
                d.description,
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
        # SECURITE
        # =====================================================

        if engagement["sous_type_depense"] != "deplacement":

            flash(
                "⚠️ Cet engagement n'est pas un déplacement.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        # =====================================================
        # POST
        # =====================================================

        if request.method == "POST":

            objet = request.form.get("objet", "").strip()

            date_frais = request.form.get(
                "date_frais"
            )

            kms = Decimal(
                request.form.get("kms") or "0"
            )

            peages = Decimal(
                request.form.get("peages") or "0"
            )

            repas = Decimal(
                request.form.get("repas") or "0"
            )

            commentaire = request.form.get(
                "commentaire",
                ""
            ).strip()

            # =================================================
            # CALCUL IK
            # =================================================

            tarif_km = Decimal("0.529")

            montant_ik = (
                kms * tarif_km
            ).quantize(
                Decimal("0.01")
            )

            total = (
                montant_ik
                + peages
                + repas
            ).quantize(
                Decimal("0.01")
            )

            # =================================================
            # GENERATION PDF
            # =================================================

            template_pdf = os.path.join(
                current_app.root_path,
                "templates",
                "pdf",
                "note de frais_voyage.pdf"
            )

            reader = PdfReader(template_pdf)

            write_log(
                f"[PDF FIELDS] {reader.get_fields()}"
            )
            writer = PdfWriter()

            writer.append(reader)

            writer.set_need_appearances_writer()

            # =================================================
            # REMPLISSAGE CHAMPS PDF
            # =================================================

            writer.update_page_form_field_values(

                writer.pages[0],

                {

                    "Frais engagés par Nom Prénom":
                        engagement["demandeur_nom"],

                    "Objet":
                        objet,

                    "62561 Frais déplacement visite Association":
                        "/On",

                    "kilometres":
                        str(kms),

                    "date":
                        date_frais,

                    "Montant IK":
                        str(montant_ik),

                    "Montant Frais":
                        str(peages + repas),

                    "Total à Rembourser":
                        str(total),

                    "Mt /km":
                        str(tarif_km)

                }

            )

            # =================================================
            # DOSSIER
            # =================================================

            upload_dir = os.path.join(
                current_app.root_path,
                "uploads",
                "engagements",
                str(engagement_id)
            )

            os.makedirs(
                upload_dir,
                exist_ok=True
            )

            # =================================================
            # NOM FICHIER
            # =================================================

            nom_original = (
                f"note_frais_{engagement_id}.pdf"
            )

            nom_stockage = (
                f"{uuid.uuid4().hex}.pdf"
            )

            chemin_pdf = os.path.join(
                upload_dir,
                nom_stockage
            )

            # =================================================
            # ECRITURE PDF
            # =================================================

            with open(chemin_pdf, "wb") as output_stream:

                writer.write(output_stream)

            # =================================================
            # BASE
            # =================================================

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
                chemin_pdf,

                "note_frais",

                current_user.email

            ))

            # =================================================
            # WORKFLOW
            # =================================================

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

                "note_frais",

                f"Note de frais générée ({total} €)",

                current_user.id,
                current_user.email

            ))

            conn.commit()

            # =================================================
            # MESSAGE
            # =================================================

            flash(
                (
                    f"✅ Note de frais générée "
                    f"({total} €)"
                ),
                "success"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

    # =========================================================
    # GET
    # =========================================================

    return render_template(
        "engagements/note_frais.html",
        engagement=engagement
    )