import sqlite3
import sqlite3
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required
from ba38_utilitaires.core import get_db_path, require_access
import pandas as pd
import io
import os
import base64
import binascii
from datetime import datetime
from flask import abort, send_from_directory
from flask_login import current_user
from PIL import Image
from ba38_utilitaires.core import has_access

droit_image_bp = Blueprint("droit_image", __name__)

# ==========================================
# PAGE PRINCIPALE
# ==========================================
@droit_image_bp.route("/droit_image")
@login_required
@require_access("droit_image", "lecture")
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
            d.lien_drive,
            d.signature_fichier,
            d.date_signature
        FROM droit_image d
        INNER JOIN benevoles b ON b.id = d.id
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
@require_access("droit_image", "lecture")
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
        INNER JOIN benevoles b ON b.id = d.id
        """

        params = []

        if filtre and filtre != "tous":
            sql += " WHERE d.acceptation=? "
            params.append(filtre)

        sql += " ORDER BY nom, prenom"

        df = pd.read_sql_query(sql, conn, params=params)

    output = io.BytesIO()

    with pd.ExcelWriter(output) as writer:
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

@droit_image_bp.route("/update_droit_image", methods=["POST"])
@login_required
@require_access("droit_image", "ecriture")
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


# ---------------------------------------------------------
# SIGNATURE TACTILE (module photo bénévole mobile)
# ---------------------------------------------------------

def _dossier_signatures():
    return os.getenv("SIGNATURES_DROIT_IMAGE_DIR", "/srv/ba38/signatures_droit_image")


def _peut_saisir_droit_image():
    return (
        has_access("benevoles", "ecriture")
        or has_access("photos_benevoles", "ecriture")
        or has_access("droit_image", "ecriture")
    )


@droit_image_bp.route("/droit_image/signer/<int:benevole_id>", methods=["POST"])
@login_required
def signer_droit_image(benevole_id):
    """Accord total (avec signature au doigt) ou refus, depuis la page mobile."""
    if not _peut_saisir_droit_image():
        abort(403)

    choix = request.form.get("choix")
    if choix not in ("accord_total", "refus"):
        flash("❌ Choix invalide", "danger")
        return redirect(url_for("benevoles.photo_benevole_mobile", benevole_id=benevole_id))

    now = datetime.now()
    nom_fichier = None

    if choix == "accord_total":
        data = request.form.get("signature", "")
        prefixe = "data:image/png;base64,"
        if not data.startswith(prefixe) or len(data) > 2_000_000:
            flash("❌ Signature manquante ou invalide", "danger")
            return redirect(url_for("benevoles.photo_benevole_mobile", benevole_id=benevole_id))
        try:
            brut = base64.b64decode(data[len(prefixe):], validate=True)
            img = Image.open(io.BytesIO(brut))
            img.load()
        except (binascii.Error, ValueError, OSError):
            flash("❌ Signature illisible", "danger")
            return redirect(url_for("benevoles.photo_benevole_mobile", benevole_id=benevole_id))

        # Fond blanc (le canvas est transparent) puis PNG réencodé : on ne
        # stocke jamais les octets reçus tels quels.
        img = img.convert("RGBA")
        fond = Image.new("RGB", img.size, (255, 255, 255))
        fond.paste(img, mask=img.split()[3])
        # Signature vide : image entièrement blanche
        if fond.convert("L").getextrema()[0] > 250:
            flash("❌ Le bénévole n'a pas signé", "danger")
            return redirect(url_for("benevoles.photo_benevole_mobile", benevole_id=benevole_id))
        fond.thumbnail((800, 400))

        dossier = _dossier_signatures()
        os.makedirs(dossier, exist_ok=True)
        nom_fichier = f"{benevole_id}_{now.strftime('%Y%m%d-%H%M%S')}.png"
        fond.save(os.path.join(dossier, nom_fichier), "PNG")

    with sqlite3.connect(get_db_path()) as conn:
        conn.execute("INSERT OR IGNORE INTO droit_image(id) VALUES (?)", (benevole_id,))
        conn.execute("""
            UPDATE droit_image
            SET acceptation = ?, signature_fichier = ?, date_signature = ?,
                signature_saisie_par = ?
            WHERE id = ?
        """, (
            "Accord Total" if choix == "accord_total" else "Refus",
            nom_fichier,
            now.strftime("%Y-%m-%d %H:%M:%S"),
            current_user.username,
            benevole_id,
        ))
        conn.commit()

    flash("✅ Droit à l'image enregistré"
          + (" avec signature" if nom_fichier else " (refus)"), "success")
    return redirect(url_for("benevoles.photo_benevole_mobile", benevole_id=benevole_id))


@droit_image_bp.route("/droit_image/signature/<int:benevole_id>")
@login_required
@require_access("droit_image", "lecture")
def voir_signature_droit_image(benevole_id):
    with sqlite3.connect(get_db_path()) as conn:
        row = conn.execute(
            "SELECT signature_fichier FROM droit_image WHERE id = ?", (benevole_id,)
        ).fetchone()
    if not row or not row[0]:
        abort(404)
    return send_from_directory(_dossier_signatures(), os.path.basename(row[0]))
