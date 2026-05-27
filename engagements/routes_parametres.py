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
# PAGE PRINCIPALE PARAMETRAGE ENGAGEMENTS
# ============================================================

@engagements_bp.route("/engagements/parametres")
@login_required
@require_access("engagement_parametres", "lecture")
def engagements_parametres_main():

    return render_template("engagements/parametres.html")


# ============================================================
# GESTION DES POLES
# ============================================================

@engagements_bp.route("/gestion_poles", methods=["GET", "POST"])
@login_required
@require_access("engagement_parametres", "lecture")
def gestion_poles():

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        if request.method == "POST":

            if not has_access(
                "engagement_parametres",
                "ecriture"
            ):
                abort(403)

            pole_ids = request.form.getlist("pole_ids[]")

            for pole_id in pole_ids:

                nom_affiche = request.form.get(
                    f"nom_affiche_{pole_id}"
                )

                responsable_id = (
                    request.form.get(
                        f"responsable_id_{pole_id}"
                    ) or None
                )

                supp1_id = (
                    request.form.get(
                        f"suppleant1_id_{pole_id}"
                    ) or None
                )

                supp2_id = (
                    request.form.get(
                        f"suppleant2_id_{pole_id}"
                    ) or None
                )

                validation_presidence_email = request.form.get(
                    f"validation_presidence_email_{pole_id}"
                )

                tresorier_user_id = (
                    request.form.get(
                        f"tresorier_user_id_{pole_id}"
                    ) or None
                )

                if not responsable_id:

                    flash(
                        "⚠️ Chaque pôle doit avoir un responsable.",
                        "warning"
                    )

                    return redirect(
                        url_for("engagements.gestion_poles")
                    )

                conn.execute("""
                    UPDATE engagement_poles
                    SET
                        nom_affiche = ?,
                        responsable_id = ?,
                        suppleant1_id = ?,
                        suppleant2_id = ?,
                        validation_presidence_email = ?,
                        tresorier_user_id = ?
                    WHERE id = ?
                """, (
                    nom_affiche,
                    responsable_id,
                    supp1_id,
                    supp2_id,
                    validation_presidence_email,
                    tresorier_user_id,
                    pole_id
                ))

            conn.commit()

            flash(
                "✅ Pôles mis à jour.",
                "success"
            )

            return redirect(
                url_for("engagements.gestion_poles")
            )

        poles = conn.execute("""
            SELECT p.*,
                   u1.email AS responsable_email,
                   u2.email AS supp1_email,
                   u3.email AS supp2_email,
                   u4.email AS tresorier_email
            FROM engagement_poles p
            LEFT JOIN users u1 ON u1.id = p.responsable_id
            LEFT JOIN users u2 ON u2.id = p.suppleant1_id
            LEFT JOIN users u3 ON u3.id = p.suppleant2_id
            LEFT JOIN users u4 ON u4.id = p.tresorier_user_id
            ORDER BY p.nom_affiche
        """).fetchall()

        users = conn.execute("""
            SELECT id, email
            FROM users
            ORDER BY email
        """).fetchall()

    return render_template(
        "engagements/gestion_poles.html",
        poles=poles,
        users=users
    )

# ============================================================
# SUPPRESSION POLE
# ============================================================

@engagements_bp.route(
    "/engagements/parametres/poles/<int:pole_id>/delete",
    methods=["GET", "POST"]
)
@login_required
@require_access("engagement_parametres", "ecriture")
def delete_pole(pole_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        pole = conn.execute("""
            SELECT *
            FROM engagement_poles
            WHERE id = ?
        """, (pole_id,)).fetchone()

        if not pole:

            flash(
                "⚠️ Pôle introuvable.",
                "warning"
            )

            return redirect(
                url_for("engagements.gestion_poles")
            )

        # =====================================================
        # VERIFICATION UTILISATION
        # =====================================================

        nb_engagements = conn.execute("""
            SELECT COUNT(*) as nb
            FROM engagements
            WHERE pole_id = ?
        """, (pole_id,)).fetchone()["nb"]

        if nb_engagements > 0:

            flash(
                "⚠️ Impossible de supprimer "
                "un pôle déjà utilisé.",
                "danger"
            )

            return redirect(
                url_for("engagements.gestion_poles")
            )

        # =====================================================
        # SUPPRESSION
        # =====================================================

        conn.execute("""
            DELETE FROM engagement_poles
            WHERE id = ?
        """, (pole_id,))

        conn.commit()

        write_log(
            f"[ENGAGEMENTS] Suppression pôle "
            f"{pole['nom_affiche']}"
        )

    flash(
        "✅ Pôle supprimé.",
        "success"
    )

    return redirect(
        url_for("engagements.gestion_poles")
    )





# ============================================================
# PARAMETRES ENGAGEMENTS
# ============================================================

@engagements_bp.route(
    "/engagements/parametres/workflow",
    methods=["GET", "POST"]
)
@login_required
@require_access("engagement_parametres", "ecriture")
def engagements_parametres_workflow():

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        # =====================================================
        # ENREGISTREMENT
        # =====================================================

        if request.method == "POST":

            ids = request.form.getlist("id[]")

            for param_id in ids:

                montant_max = request.form.get(f"montant_max_{param_id}")

                un_devis = "o" if request.form.get(f"un_devis_{param_id}") else "n"

                trois_devis = (
                    "o"
                    if request.form.get(f"trois_devis_{param_id}")
                    else "n"
                )

                accord_resp_pole = (
                    "o"
                    if request.form.get(f"accord_resp_pole_{param_id}")
                    else "n"
                )

                accord_presidence = (
                    "o"
                    if request.form.get(f"accord_presidence_{param_id}")
                    else "n"
                )

                conn.execute("""
                    UPDATE engagements_parametres
                    SET
                        montant_max = ?,
                        un_devis = ?,
                        trois_devis = ?,
                        accord_resp_pole = ?,
                        accord_presidence = ?
                    WHERE id = ?
                """, (
                    montant_max,
                    un_devis,
                    trois_devis,
                    accord_resp_pole,
                    accord_presidence,
                    param_id
                ))

            conn.commit()

            flash("✅ Paramètres enregistrés.", "success")

            return redirect(
                url_for(
                    "engagements.engagements_parametres_workflow"
                )
            )

        # =====================================================
        # CHARGEMENT
        # =====================================================

        paliers = conn.execute("""
            SELECT *
            FROM engagements_parametres
            ORDER BY ordre_affichage
        """).fetchall()

    return render_template(
        "engagements/engagements_parametres.html",
        paliers=paliers
    )

