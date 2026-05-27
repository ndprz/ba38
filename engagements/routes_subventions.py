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
# GESTION SUBVENTIONS
# ============================================================

@engagements_bp.route(
    "/engagements/parametres/subventions",
    methods=["GET", "POST"]
)
@login_required
@require_access("engagement_parametres", "lecture")
def gestion_subventions():

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        # =====================================================
        # ENREGISTREMENT
        # =====================================================

        if request.method == "POST":

            if not has_access(
                "engagement_parametres",
                "ecriture"
            ):
                abort(403)

            ids = request.form.getlist("id[]")

            # =================================================
            # UPDATE EXISTANTS
            # =================================================

            for sub_id in ids:

                nom_subvention = request.form.get(
                    f"nom_subvention_{sub_id}"
                )

                commentaire = request.form.get(
                    f"commentaire_{sub_id}"
                )

                nom_organisme = request.form.get(
                    f"nom_organisme_{sub_id}"
                )

                accord_le = request.form.get(
                    f"accord_le_{sub_id}"
                )

                utilisation_date_debut = request.form.get(
                    f"utilisation_date_debut_{sub_id}"
                )

                utilisation_date_fin = request.form.get(
                    f"utilisation_date_fin_{sub_id}"
                )

                montant_prevu = (
                    request.form.get(
                        f"montant_prevu_{sub_id}"
                    ) or 0
                )

                montant_recu = (
                    request.form.get(
                        f"montant_recu_{sub_id}"
                    ) or 0
                )

                date_montant_recu = request.form.get(
                    f"date_montant_recu_{sub_id}"
                )

                montant_utilise = (
                    request.form.get(
                        f"montant_utilise_{sub_id}"
                    ) or 0
                )

                montant_restant = (
                    Decimal(str(montant_recu))
                    - Decimal(str(montant_utilise))
                )

                conn.execute("""

                    UPDATE engagement_subventions

                    SET

                        nom_subvention = ?,
                        commentaire = ?,
                        nom_organisme = ?,

                        accord_le = ?,

                        utilisation_date_debut = ?,
                        utilisation_date_fin = ?,

                        montant_prevu = ?,
                        montant_recu = ?,
                        date_montant_recu = ?,

                        montant_utilise = ?,
                        montant_restant = ?

                    WHERE id = ?

                """, (

                    nom_subvention,
                    commentaire,
                    nom_organisme,

                    accord_le,

                    utilisation_date_debut,
                    utilisation_date_fin,

                    montant_prevu,
                    montant_recu,
                    date_montant_recu,

                    montant_utilise,
                    montant_restant,

                    sub_id

                ))

            # =================================================
            # AJOUT
            # =================================================

            nouvelle_subvention = request.form.get(
                "nouvelle_subvention"
            )

            if nouvelle_subvention:

                conn.execute("""

                    INSERT INTO engagement_subventions (

                        nom_subvention

                    )
                    VALUES (?)

                """, (

                    nouvelle_subvention,

                ))

            conn.commit()

            flash(
                "✅ Subventions enregistrées.",
                "success"
            )

            return redirect(
                url_for(
                    "engagements.gestion_subventions"
                )
            )

        # =====================================================
        # CHARGEMENT
        # =====================================================

        subventions = conn.execute("""

            SELECT *
            FROM engagement_subventions
            ORDER BY nom_subvention

        """).fetchall()

    return render_template(
        "engagements/gestion_subventions.html",
        subventions=subventions
    )

# ============