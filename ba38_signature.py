# ba38_signature.py
"""
Signature électronique générique via LibreSign : un utilisateur BA38 uploade
n'importe quel PDF (bon de commande, courrier, etc.) et le fait signer, soit
par lui-même, soit en l'envoyant par email à un tiers. Contrairement à
l'Annexe 1 bis (ba38_annexe1bis.py), le document n'a pas de mise en page
connue à l'avance : le signataire place lui-même son pavé de signature dans
l'interface LibreSign (cf. utils_libresign.envoyer_signature_request,
paramètre coordonnees=None).
"""

import os
import shutil
import sqlite3
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from utils import get_db_connection, write_log, upload_database, require_access


signature_bp = Blueprint("signature", __name__)

STATUTS_SIGNATURE = ("envoyee", "signee", "refusee", "expiree")


def _dossier(signature_id):
    base_dir = os.getenv("BA38_BASE_DIR", ".")
    dossier = os.path.join(base_dir, "uploads", "signature_electronique", str(signature_id))
    os.makedirs(dossier, exist_ok=True)
    return dossier


# ========================================
# 📋 Liste + formulaire d'envoi
# ========================================
@signature_bp.route("/signature", methods=["GET"])
@login_required
@require_access("signature_electronique", "ecriture")
def index():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    demandes = conn.execute(
        "SELECT * FROM signatures_electroniques WHERE user_creation = ? ORDER BY id DESC",
        (current_user.email,)
    ).fetchall()
    conn.close()
    return render_template("signature/index.html", demandes=demandes)


# ========================================
# 📤 Envoyer un document pour signature via LibreSign
# ========================================
@signature_bp.route("/signature/envoyer", methods=["POST"])
@login_required
@require_access("signature_electronique", "ecriture")
def envoyer():
    from utils_libresign import envoyer_signature_request, LibreSignError

    fichier = request.files.get("fichier")
    if not fichier or not fichier.filename:
        flash("❌ Aucun fichier sélectionné.", "danger")
        return redirect(url_for("signature.index"))

    if not fichier.filename.lower().endswith(".pdf"):
        flash("❌ Seuls les fichiers PDF sont acceptés.", "danger")
        return redirect(url_for("signature.index"))

    nom_document = (request.form.get("nom_document") or "").strip() or fichier.filename
    type_signataire = request.form.get("type_signataire")
    if type_signataire not in ("moi_meme", "tiers"):
        flash("❌ Choix du signataire invalide.", "danger")
        return redirect(url_for("signature.index"))

    if type_signataire == "moi_meme":
        destinataire_nom = current_user.username
        destinataire_email = current_user.email
    else:
        destinataire_nom = (request.form.get("destinataire_nom") or "").strip()
        destinataire_email = (request.form.get("destinataire_email") or "").strip()
        if not destinataire_nom or not destinataire_email:
            flash("❌ Nom et email du destinataire requis.", "danger")
            return redirect(url_for("signature.index"))

    try:
        coordonnees = {
            "page": int(request.form["sig_page"]),
            "top": round(float(request.form["sig_top"])),
            "left": round(float(request.form["sig_left"])),
            "width": round(float(request.form["sig_width"])),
            "height": round(float(request.form["sig_height"])),
        }
    except (KeyError, ValueError):
        flash("❌ Merci de positionner la signature sur le document avant l'envoi.", "danger")
        return redirect(url_for("signature.index"))

    conn = get_db_connection()
    cur = conn.execute(
        """
        INSERT INTO signatures_electroniques (
            user_creation, date_creation, nom_document, type_signataire,
            destinataire_nom, destinataire_email, statut
        ) VALUES (?, ?, ?, ?, ?, ?, 'envoyee')
        """,
        (current_user.email, date.today().strftime("%Y-%m-%d"), nom_document,
         type_signataire, destinataire_nom, destinataire_email)
    )
    signature_id = cur.lastrowid
    conn.commit()

    chemin_original = os.path.join(_dossier(signature_id), secure_filename(fichier.filename))
    fichier.save(chemin_original)
    with open(chemin_original, "rb") as f:
        pdf_bytes = f.read()

    # Certains PDF (scanners, pilotes d'impression) ont des octets parasites
    # avant l'en-tête %PDF : tolérés par la plupart des lecteurs, mais rejetés
    # par le validateur strict de LibreSign ("Fichier Base64 invalide").
    debut_pdf = pdf_bytes.find(b"%PDF")
    if debut_pdf > 0:
        pdf_bytes = pdf_bytes[debut_pdf:]
        with open(chemin_original, "wb") as f:
            f.write(pdf_bytes)

    prenom, *reste = destinataire_nom.split(" ", 1)
    nom = reste[0] if reste else ""

    try:
        resultat = envoyer_signature_request(
            pdf_bytes=pdf_bytes,
            nom_document=nom_document,
            signataire_prenom=prenom,
            signataire_nom=nom,
            signataire_email=destinataire_email,
            coordonnees=coordonnees,
        )
    except LibreSignError as e:
        write_log(f"❌ LibreSign envoyer (signature {signature_id}) : {e}")
        conn.execute("UPDATE signatures_electroniques SET statut = 'erreur' WHERE id = ?", (signature_id,))
        conn.commit()
        conn.close()
        flash(f"❌ Erreur lors de l'envoi à LibreSign : {e}", "danger")
        return redirect(url_for("signature.index"))

    conn.execute(
        """
        UPDATE signatures_electroniques
        SET libresign_uuid = ?, libresign_file_id = ?, libresign_sign_request_id = ?,
            document_original_path = ?
        WHERE id = ?
        """,
        (resultat["uuid"], resultat["file_id"], resultat["sign_request_id"], chemin_original, signature_id)
    )
    conn.commit()
    conn.close()

    upload_database()
    flash(f"📤 Document envoyé pour signature à {destinataire_email}.", "success")
    return redirect(url_for("signature.index"))


# ========================================
# 🔄 Vérifier le statut auprès de LibreSign (pas de webhook configuré)
# ========================================
@signature_bp.route("/signature/verifier_statut/<int:signature_id>", methods=["POST"])
@login_required
@require_access("signature_electronique", "ecriture")
def verifier_statut(signature_id):
    from utils_libresign import recuperer_statut, telecharger_document_signe, LibreSignError

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    demande = conn.execute(
        "SELECT * FROM signatures_electroniques WHERE id = ? AND user_creation = ?",
        (signature_id, current_user.email)
    ).fetchone()

    if not demande or not demande["libresign_file_id"]:
        conn.close()
        flash("ℹ️ Aucune demande de signature LibreSign associée.", "info")
        return redirect(url_for("signature.index"))

    try:
        statut_data = recuperer_statut(demande["libresign_file_id"])
    except LibreSignError as e:
        conn.close()
        write_log(f"❌ LibreSign verifier_statut (signature {signature_id}) : {e}")
        flash(f"❌ Erreur lors de la vérification auprès de LibreSign : {e}", "danger")
        return redirect(url_for("signature.index"))

    # Même mapping que ba38_annexe1bis.verifier_statut (codes confirmés le 2026-07-16).
    mapping = {3: "signee"}
    nouveau_statut = mapping.get(statut_data.get("status"))

    if not nouveau_statut:
        conn.close()
        flash(f"ℹ️ Statut LibreSign actuel : « {statut_data.get('statusText')} » (pas encore de changement).", "info")
        return redirect(url_for("signature.index"))

    document_signe_path = None
    if nouveau_statut == "signee":
        try:
            pdf_signe = telecharger_document_signe(demande["libresign_uuid"])
            chemin = os.path.join(_dossier(signature_id), f"signe_{int(datetime.now().timestamp())}.pdf")
            with open(chemin, "wb") as f:
                f.write(pdf_signe)
            document_signe_path = chemin
        except LibreSignError as e:
            write_log(f"⚠️ Téléchargement document signé échoué (signature {signature_id}) : {e}")

    conn.execute(
        """
        UPDATE signatures_electroniques
        SET statut = ?,
            date_signature = CASE WHEN ? = 'signee' THEN ? ELSE date_signature END,
            document_signe_path = COALESCE(?, document_signe_path)
        WHERE id = ?
        """,
        (nouveau_statut, nouveau_statut, date.today().strftime("%Y-%m-%d"), document_signe_path, signature_id)
    )
    conn.commit()
    conn.close()

    upload_database()
    flash(f"✅ Statut mis à jour : {nouveau_statut}.", "success")
    return redirect(url_for("signature.index"))


# ========================================
# 📥 Téléchargement (original ou signé)
# ========================================
@signature_bp.route("/signature/telecharger/<int:signature_id>/<variante>", methods=["GET"])
@login_required
@require_access("signature_electronique", "ecriture")
def telecharger(signature_id, variante):
    if variante not in ("original", "signe"):
        flash("❌ Variante de téléchargement invalide.", "danger")
        return redirect(url_for("signature.index"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    demande = conn.execute(
        "SELECT * FROM signatures_electroniques WHERE id = ? AND user_creation = ?",
        (signature_id, current_user.email)
    ).fetchone()
    conn.close()

    if not demande:
        flash("❌ Demande de signature introuvable.", "danger")
        return redirect(url_for("signature.index"))

    colonne = "document_signe_path" if variante == "signe" else "document_original_path"
    chemin = demande[colonne]
    if not chemin or not os.path.exists(chemin):
        flash("❌ Fichier introuvable.", "danger")
        return redirect(url_for("signature.index"))

    suffixe = "_signe" if variante == "signe" else ""
    nom_fichier = f"{demande['nom_document']}{suffixe}.pdf"
    return send_file(chemin, as_attachment=True, download_name=nom_fichier, mimetype="application/pdf")


# ========================================
# 🗑️ Supprimer une demande (base + fichiers)
# ========================================
@signature_bp.route("/signature/supprimer/<int:signature_id>", methods=["POST"])
@login_required
@require_access("signature_electronique", "ecriture")
def supprimer(signature_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    demande = conn.execute(
        "SELECT * FROM signatures_electroniques WHERE id = ? AND user_creation = ?",
        (signature_id, current_user.email)
    ).fetchone()

    if not demande:
        conn.close()
        flash("❌ Demande de signature introuvable.", "danger")
        return redirect(url_for("signature.index"))

    conn.execute("DELETE FROM signatures_electroniques WHERE id = ?", (signature_id,))
    conn.commit()
    conn.close()

    shutil.rmtree(_dossier(signature_id), ignore_errors=True)

    upload_database()
    flash("🗑️ Demande de signature supprimée.", "success")
    return redirect(url_for("signature.index"))
