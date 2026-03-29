import sqlite3
import sqlite3
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required
from utils import get_db_path, require_access
import pandas as pd
import io

droit_image_bp = Blueprint("droit_image", __name__)

# ==========================================
# PAGE PRINCIPALE
# ==========================================
@droit_image_bp.route("/droit_image")
@login_required
@require_access("image", "lecture")
def droit_image():

    filtre = request.args.get("acceptation")

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
        INSERT INTO droit_image(id)
        SELECT id FROM benevoles
        WHERE id NOT IN (SELECT id FROM droit_image)
        """)

        conn.commit()

        sql = """
        SELECT
            d.id,
            COALESCE(b.nom, d.nom) AS nom,
            COALESCE(b.prenom, d.prenom) AS prenom,
            d.acceptation,
            d.lien_drive
        FROM droit_image d
        LEFT JOIN benevoles b ON b.id = d.id
        """

        params = []

        if filtre and filtre != "tous":
            sql += " WHERE d.acceptation=? "
            params.append(filtre)

        sql += " ORDER BY nom, prenom"

        cur.execute(sql, params)

        personnes = cur.fetchall()

    return render_template(
        "benevoles/droit_image.html",
        personnes=personnes,
        filtre=filtre
    )


# ==========================================
# EXPORT EXCEL
# ==========================================
@droit_image_bp.route("/export_droit_image")
@login_required
@require_access("image", "lecture")
def export_droit_image():

    filtre = request.args.get("acceptation")

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        sql = """
        SELECT
            d.id,
            COALESCE(b.nom, d.nom) AS nom,
            COALESCE(b.prenom, d.prenom) AS prenom,
            d.acceptation,
            d.lien_drive
        FROM droit_image d
        LEFT JOIN benevoles b ON b.id = d.id
        """

        params = []

        if filtre and filtre != "tous":
            sql += " WHERE d.acceptation=? "
            params.append(filtre)

        sql += " ORDER BY nom, prenom"

        df = pd.read_sql_query(sql, conn, params=params)

    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="droit_image")

    output.seek(0)

    return send_file(
        output,
        download_name="droit_image.xlsx",
        as_attachment=True
    )

# ---------------------------------------------------------
# MISE A JOUR
# ---------------------------------------------------------

@droit_image_bp.route("/update_droit_image")
@login_required
@require_access("image", "ecriture")
def update_droit_image():

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        cur = conn.cursor()

        for key, value in request.form.items():

            if key.startswith("accept_"):

                id_personne = key.replace("accept_", "")
                acceptation = value

                lien = request.form.get(f"lien_{id_personne}")

                cur.execute("""
                UPDATE droit_image
                SET acceptation=?, lien_drive=?
                WHERE id=?
                """, (acceptation, lien, id_personne))

        conn.commit()

    flash("✔️ Mise à jour effectuée", "success")

    return redirect(url_for("droit_image.droit_image"))
