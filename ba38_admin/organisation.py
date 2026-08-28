import os
import sqlite3

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required

from ba38_utilitaires.core import get_db_path, require_admin_global, write_log
from ba38_utilitaires.organisation import get_organisation

organisation_bp = Blueprint("organisation", __name__, url_prefix="/admin/organisation")

CHAMPS_TEXTE = [
    "nom", "adresse", "tel", "email", "iban", "bic",
    "siren", "naf", "siret", "rna", "footer_partenariat",
]

EXTENSIONS_IMAGE_AUTORISEES = {"png", "jpg", "jpeg"}


def _extension_autorisee(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in EXTENSIONS_IMAGE_AUTORISEES


def _enregistrer_image(fichier, sous_dossier, nom_base):
    """Sauvegarde un upload sous un nom fixe (évite l'accumulation de fichiers
    et tout souci lié au nom d'origine) et retourne le chemin relatif
    stocké dans `organisation` (ex. "static/images/logo_organisation.png")."""
    ext = fichier.filename.rsplit(".", 1)[1].lower()
    dest_dir = os.path.join(current_app.root_path, "static", sous_dossier)
    os.makedirs(dest_dir, exist_ok=True)
    filename = f"{nom_base}.{ext}"
    fichier.save(os.path.join(dest_dir, filename))
    return f"static/{sous_dossier}/{filename}"


@organisation_bp.route("/", methods=["GET", "POST"])
@login_required
@require_admin_global
def organisation_edit():
    if request.method == "POST":
        valeurs = {champ: request.form.get(champ, "").strip() for champ in CHAMPS_TEXTE}

        if not valeurs["nom"] or not valeurs["adresse"]:
            flash("⛔ Le nom et l'adresse sont obligatoires.", "danger")
            return redirect(url_for("organisation.organisation_edit"))

        logo = request.files.get("logo")
        if logo and logo.filename and not _extension_autorisee(logo.filename):
            flash("⛔ Logo : format non autorisé (png/jpg attendus).", "danger")
            return redirect(url_for("organisation.organisation_edit"))

        logo_complet = request.files.get("logo_complet")
        if logo_complet and logo_complet.filename and not _extension_autorisee(logo_complet.filename):
            flash("⛔ Logo complet : format non autorisé (png/jpg attendus).", "danger")
            return redirect(url_for("organisation.organisation_edit"))

        signature = request.files.get("signature")
        if signature and signature.filename and not _extension_autorisee(signature.filename):
            flash("⛔ Signature : format non autorisé (png/jpg attendus).", "danger")
            return redirect(url_for("organisation.organisation_edit"))

        conn = sqlite3.connect(get_db_path())
        try:
            set_clause = ", ".join(f"{champ} = ?" for champ in CHAMPS_TEXTE)
            conn.execute(
                f"UPDATE organisation SET {set_clause} WHERE id = 1",
                [valeurs[c] for c in CHAMPS_TEXTE],
            )

            if logo and logo.filename:
                logo_path = _enregistrer_image(logo, "images", "logo_organisation")
                conn.execute("UPDATE organisation SET logo_path = ? WHERE id = 1", (logo_path,))

            if logo_complet and logo_complet.filename:
                logo_complet_path = _enregistrer_image(logo_complet, "images", "logo_complet_organisation")
                conn.execute("UPDATE organisation SET logo_complet_path = ? WHERE id = 1", (logo_complet_path,))

            if signature and signature.filename:
                signature_path = _enregistrer_image(signature, "signatures", "signature_organisation")
                conn.execute("UPDATE organisation SET signature_path = ? WHERE id = 1", (signature_path,))

            conn.commit()
        finally:
            conn.close()

        write_log("✏️ Organisation modifiée par admin")
        flash("✅ Informations de l'organisme enregistrées.", "success")
        return redirect(url_for("organisation.organisation_edit"))

    org = get_organisation()
    return render_template("admin/organisation.html", org=org)
