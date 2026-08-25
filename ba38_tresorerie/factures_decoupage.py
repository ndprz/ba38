import os
import re

from flask import request, render_template, flash, redirect, url_for, send_file, session
from flask_login import login_required

from utils import write_log, require_access

from ba38_tresorerie import tresorerie_bp
from ba38_tresorerie.factures_upload import extract_pages


@tresorerie_bp.route('/factures_decoupage', methods=['GET', 'POST'])
@login_required
@require_access("tresorerie", "ecriture")
def factures_decoupage():

    import os
    import time
    import tempfile
    import zipfile
    from flask import send_file
    from PyPDF2 import PdfReader, PdfWriter

    if request.method == "POST":

        file = request.files.get("pdf_file")

        if not file:
            flash("❌ Fichier manquant", "danger")
            return redirect(url_for("tresorerie.factures_decoupage"))

        # ============================
        # Sauvegarde temporaire
        # ============================
        tmp_dir = tempfile.mkdtemp(prefix="decoupage_")
        input_pdf_path = os.path.join(tmp_dir, "input.pdf")
        file.save(input_pdf_path)

        reader = PdfReader(input_pdf_path)

        # ============================
        # 🔧 Extraction logique (reuse existant)
        # ============================
        pages = extract_pages(input_pdf_path)   # ⚠️ ta fonction existante

        fichiers_generes = []

        # ============================
        # Génération PDF individuels
        # ============================
        for i, p in enumerate(pages):

            if not p["pages"]:
                continue

            writer = PdfWriter()

            for page_num in p["pages"]:

                page = reader.pages[page_num - 1]

                try:
                    text = page.extract_text() or ""
                except:
                    text = ""

                # garder uniquement les vraies factures
                if "NET À PAYER" not in text:
                    continue

                writer.add_page(page)

            nom = p.get("nom") or f"facture_{i+1}"
            nom_clean = re.sub(r"[^A-Za-z0-9_\-]", "_", nom)

            output_path = os.path.join(tmp_dir, f"{nom_clean}.pdf")

            with open(output_path, "wb") as f:
                writer.write(f)

            fichiers_generes.append(output_path)

        if not fichiers_generes:
            flash("❌ Aucune facture détectée", "danger")
            return redirect(url_for("tresorerie.factures_decoupage"))

        # ============================
        # Création ZIP
        # ============================
        zip_path = os.path.join(tmp_dir, "factures.zip")

        with zipfile.ZipFile(zip_path, 'w') as z:
            for f in fichiers_generes:
                z.write(f, os.path.basename(f))

        # ============================
        # Préparation téléchargement via redirect
        # ============================
        session["zip_path"] = zip_path

        flash("✅ Découpe terminée – téléchargement en cours...", "success")

        return redirect(url_for("tresorerie.telecharger_factures"))

    return render_template("tresorerie/decouper_factures.html")


@tresorerie_bp.route("/download_zip")
@login_required
@require_access("tresorerie", "ecriture")
def download_zip():

    zip_path = session.get("zip_path")

    if not zip_path or not os.path.exists(zip_path):
        return "Fichier introuvable", 404

    response = send_file(
        zip_path,
        as_attachment=True,
        download_name="factures_decoupees.zip"
    )

    # 🧹 nettoyage
    try:
        os.remove(zip_path)
        os.rmdir(os.path.dirname(zip_path))  # supprime tmp_dir
    except Exception as e:
        write_log(f"⚠️ nettoyage ZIP impossible : {e}")

    session.pop("zip_path", None)

    return response
