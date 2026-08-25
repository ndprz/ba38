"""
Découpe un PDF de factures, crée un PDF par client, génère un ZIP téléchargeable
et gère automatiquement le nettoyage (auto et immédiat).
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_from_directory
from flask_login import login_required
from pypdf import PdfReader, PdfWriter
from pathlib import Path
import re, os, unicodedata, zipfile, datetime, shutil
from ba38_utilitaires.core import get_static_factures_dir, write_log, require_access

factures_bp = Blueprint('factures', __name__)

# === Dossiers dynamiques selon ENVIRONMENT ===
BASE_FACTURES_DIR = Path(get_static_factures_dir())
UPLOAD_FOLDER = BASE_FACTURES_DIR.parent / "uploads"
OUTPUT_FOLDER = BASE_FACTURES_DIR
ARCHIVE_FOLDER = BASE_FACTURES_DIR / "archives"

for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, ARCHIVE_FOLDER]:
    os.makedirs(d, exist_ok=True)

# =====================================================
# 🧩 FONCTIONS UTILES
# =====================================================

def sanitize_filename(name: str) -> str:
    """Nettoie une chaîne pour créer un nom de fichier sûr."""
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:100] or "client_inconnu"

def extract_client_name(text: str) -> str:
    """Extrait le nom du client à partir du texte d'une page PDF."""
    m = re.search(r"ÉCHÉANCEN°\s*BORDEREAU\s*:?\s*\n?(.*?)(?:\n|$)", text, re.IGNORECASE)
    if m:
        return sanitize_filename(m.group(1))
    return "client_inconnu"

def cleanup_old_batches(keep: int = 3):
    """Supprime les anciens lots de factures et ZIP, garde seulement les 'keep' plus récents."""
    try:
        all_dirs = sorted(
            [d for d in OUTPUT_FOLDER.iterdir() if d.is_dir() and d.name != "archives"],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        all_zips = sorted(
            [z for z in ARCHIVE_FOLDER.glob("factures_*.zip")],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        for d in all_dirs[keep:]:
            shutil.rmtree(d, ignore_errors=True)
        for z in all_zips[keep:]:
            z.unlink(missing_ok=True)
        write_log(f"🧹 Nettoyage auto : conservation des {keep} derniers lots")
    except Exception as e:
        write_log(f"❌ Erreur nettoyage : {e}")

def delete_batch(horodatage):
    """Supprime un dossier de factures et son ZIP associé."""
    try:
        dossier = OUTPUT_FOLDER / horodatage
        zip_file = ARCHIVE_FOLDER / f"factures_{horodatage}.zip"
        if dossier.exists():
            shutil.rmtree(dossier, ignore_errors=True)
        if zip_file.exists():
            zip_file.unlink(missing_ok=True)
        write_log(f"🧽 Suppression du lot {horodatage}")
    except Exception as e:
        write_log(f"❌ Erreur delete_batch({horodatage}) : {e}")

# =====================================================
# 🧾 ROUTE PRINCIPALE : Découpage des factures
# =====================================================

@factures_bp.route("/decouper_factures", methods=["GET", "POST"])
@login_required
@require_access("tresorerie", "ecriture")
def decouper_factures():
    if request.method == "POST":
        file = request.files.get("pdf_file")
        if not file:
            flash("Aucun fichier PDF fourni.", "danger")
            return redirect(url_for("factures.decouper_factures"))

        # Sauvegarde temporaire du fichier uploadé
        pdf_path = UPLOAD_FOLDER / file.filename
        file.save(pdf_path)

        try:
            reader = PdfReader(str(pdf_path))
        except Exception as e:
            flash(f"Erreur de lecture PDF : {e}", "danger")
            return redirect(url_for("factures.decouper_factures"))

        # Découpage logique des factures
        factures = []
        facture_pages = []

        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if "ÉCHÉANCEN°" in text and facture_pages:
                factures.append(facture_pages)
                facture_pages = [i]
            else:
                facture_pages.append(i)

        if facture_pages:
            factures.append(facture_pages)

        # Dossier de sortie horodaté
        horodatage = datetime.datetime.now().strftime("%Y-%m-%d_%Hh%M%S")
        dossier_sortie = OUTPUT_FOLDER / horodatage
        dossier_sortie.mkdir(parents=True, exist_ok=True)

        fichiers_crees = []

        for idx, pages in enumerate(factures, start=1):
            first_text = reader.pages[pages[0]].extract_text() or ""
            nom = extract_client_name(first_text)
            outname = f"{nom}_{idx:03d}.pdf"
            outpath = dossier_sortie / outname

            writer = PdfWriter()
            for p in pages:
                writer.add_page(reader.pages[p])

            with open(outpath, "wb") as f:
                writer.write(f)

            fichiers_crees.append(outname)

        # ===============================
        # Création de l'archive ZIP
        # ===============================

        zip_name = f"factures_{horodatage}.zip"
        zip_fullpath = ARCHIVE_FOLDER / zip_name

        # 🔒 Sécurisation : recrée le dossier archives si absent
        ARCHIVE_FOLDER.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_fullpath, "w", zipfile.ZIP_DEFLATED) as zipf:
            for fname in fichiers_crees:
                zipf.write(dossier_sortie / fname, arcname=fname)

        # Nettoyage automatique des anciens lots
        cleanup_old_batches(keep=3)

        flash(f"{len(fichiers_crees)} factures extraites avec succès.", "success")
        return render_template(
            "tresorerie/factures_result.html",
            fichiers=fichiers_crees,
            horodatage=horodatage,
            zipfile=zip_name
        )

    return render_template("tresorerie/decouper_factures.html")

# =====================================================
# 📦 Téléchargement direct du ZIP + suppression immédiate
# =====================================================

@factures_bp.route("/telecharger_zip/<nom_fichier>")
@login_required
@require_access("tresorerie", "ecriture")
def telecharger_zip(nom_fichier):
    """
    Envoie le ZIP au navigateur et supprime immédiatement
    le ZIP + les fichiers PDF correspondants après téléchargement.
    """
    archives_dir = ARCHIVE_FOLDER
    zip_path = archives_dir / nom_fichier

    if not zip_path.exists():
        flash("Fichier ZIP introuvable ou déjà supprimé.", "danger")
        return redirect(url_for("factures.decouper_factures"))

    # Détermine le horodatage pour suppression du dossier
    horodatage = nom_fichier.replace("factures_", "").replace(".zip", "")

    # Envoi du fichier
    response = send_from_directory(archives_dir, nom_fichier, as_attachment=True)

    # Suppression différée (après envoi)
    @response.call_on_close
    def cleanup_after_send():
        try:
            delete_batch(horodatage)
            write_log(f"🧹 Nettoyage immédiat après téléchargement : {nom_fichier}")
        except Exception as e:
            write_log(f"❌ Erreur nettoyage immédiat : {e}")

    return response

# =====================================================
# 🧹 Nettoyage manuel
# =====================================================

@factures_bp.route("/nettoyer_factures", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def nettoyer_factures():
    """Route pour supprimer tous les anciens fichiers et archives."""
    try:
        for d in OUTPUT_FOLDER.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        for z in ARCHIVE_FOLDER.glob("factures_*.zip"):
            z.unlink(missing_ok=True)
        flash("🧹 Tous les fichiers et archives de factures ont été supprimés.", "info")
        write_log("🧹 Nettoyage manuel complet effectué.")
    except Exception as e:
        flash(f"Erreur nettoyage : {e}", "danger")
        write_log(f"❌ Erreur nettoyage manuel : {e}")
    return redirect(url_for("factures.decouper_factures"))
