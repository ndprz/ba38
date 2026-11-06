import base64
import io
import os
import qrcode
import sqlite3
from datetime import datetime

from flask import (abort, flash, redirect, render_template, request,
                   send_from_directory, url_for)
from flask_login import current_user, login_required

from ba38_utilitaires.core import get_db_path, has_access, write_log
from ba38_intervenants import intervenants_bp
from ba38_intervenants import fichiers

APPLI = "intervenants_temporaires"


def _peut_ecrire():
    return (
        has_access(APPLI, "ecriture")
        or has_access("photos_benevoles", "ecriture")
        or has_access("benevoles", "ecriture")
    )


def _peut_lire():
    return _peut_ecrire() or has_access(APPLI, "lecture")


def _garde(ecriture=False):
    if not (_peut_ecrire() if ecriture else _peut_lire()):
        abort(403)


def _db():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _propre(champ, maxi=200):
    return (request.form.get(champ) or "").strip()[:maxi]


def _get(conn, iid):
    row = conn.execute("SELECT * FROM intervenants_temporaires WHERE id = ?", (iid,)).fetchone()
    if not row:
        abort(404)
    return row


def _photo_existe(iid):
    return os.path.exists(os.path.join(fichiers.dossier_photos(), f"{iid}.jpg"))


# ---------------------------------------------------------------- liste
@intervenants_bp.route("/")
@login_required
def liste():
    _garde()
    with _db() as conn:
        rows = conn.execute("""
            SELECT * FROM intervenants_temporaires
            ORDER BY cree_le DESC, nom COLLATE NOCASE, prenom COLLATE NOCASE
        """).fetchall()
    intervenants = [dict(r, photo=_photo_existe(r["id"])) for r in rows]
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_Q,
                       box_size=10, border=4)
    qr.add_data(url_for("intervenants.liste", _external=True))
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return render_template("intervenants/liste.html", intervenants=intervenants,
                           peut_ecrire=_peut_ecrire(),
                           qr_code_b64=base64.b64encode(buf.getvalue()).decode())


@intervenants_bp.route("/creer", methods=["POST"])
@login_required
def creer():
    _garde(ecriture=True)
    nom, prenom = _propre("nom"), _propre("prenom")
    if not nom or not prenom:
        flash("❌ Le nom et le prénom sont obligatoires", "danger")
        return redirect(url_for("intervenants.liste"))
    with _db() as conn:
        cur = conn.execute("""
            INSERT INTO intervenants_temporaires
                (nom, prenom, telephone, email, organisme, cree_le, cree_par)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (nom, prenom, _propre("telephone", 40) or None, _propre("email") or None,
              _propre("organisme") or None,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S"), current_user.username))
        iid = cur.lastrowid
    flash("✅ Intervenant créé : ajoutez sa photo et son droit à l'image", "success")
    return redirect(url_for("intervenants.fiche", iid=iid))


# ---------------------------------------------------------------- fiche
@intervenants_bp.route("/<int:iid>")
@login_required
def fiche(iid):
    _garde()
    with _db() as conn:
        it = _get(conn, iid)
    return render_template("intervenants/fiche.html", it=it, photo=_photo_existe(iid),
                           peut_ecrire=_peut_ecrire())


@intervenants_bp.route("/<int:iid>/infos", methods=["POST"])
@login_required
def modifier_infos(iid):
    _garde(ecriture=True)
    nom, prenom = _propre("nom"), _propre("prenom")
    if not nom or not prenom:
        flash("❌ Le nom et le prénom sont obligatoires", "danger")
        return redirect(url_for("intervenants.fiche", iid=iid))
    with _db() as conn:
        _get(conn, iid)
        conn.execute("""
            UPDATE intervenants_temporaires
            SET nom=?, prenom=?, telephone=?, email=?, organisme=? WHERE id=?
        """, (nom, prenom, _propre("telephone", 40) or None, _propre("email") or None,
              _propre("organisme") or None, iid))
    flash("✅ Informations enregistrées", "success")
    return redirect(url_for("intervenants.fiche", iid=iid))


@intervenants_bp.route("/<int:iid>/photo", methods=["POST"])
@login_required
def envoyer_photo(iid):
    _garde(ecriture=True)
    with _db() as conn:
        _get(conn, iid)
    f = request.files.get("photo")
    if not f or not f.filename:
        flash("❌ Aucune photo reçue", "danger")
    else:
        try:
            fichiers.enregistrer_photo(f.stream, iid)
            flash("✅ Photo enregistrée", "success")
        except ValueError as e:
            flash(f"❌ {e}", "danger")
    return redirect(url_for("intervenants.fiche", iid=iid))


@intervenants_bp.route("/<int:iid>/droit_image", methods=["POST"])
@login_required
def droit_image(iid):
    _garde(ecriture=True)
    choix = request.form.get("choix")
    if choix not in ("accord_total", "refus"):
        flash("❌ Choix invalide", "danger")
        return redirect(url_for("intervenants.fiche", iid=iid))
    now = datetime.now()
    nom_fichier = None
    if choix == "accord_total":
        nom_fichier = f"tmp_{iid}_{now.strftime('%Y%m%d-%H%M%S')}.png"
        try:
            fichiers.enregistrer_signature(request.form.get("signature", ""), nom_fichier)
        except ValueError as e:
            flash(f"❌ {e}", "danger")
            return redirect(url_for("intervenants.fiche", iid=iid))
    with _db() as conn:
        _get(conn, iid)
        conn.execute("""
            UPDATE intervenants_temporaires
            SET acceptation=?, signature_fichier=?, date_signature=?, signature_saisie_par=?
            WHERE id=?
        """, ("Accord Total" if choix == "accord_total" else "Refus", nom_fichier,
              now.strftime("%Y-%m-%d %H:%M:%S"), current_user.username, iid))
    flash("✅ Droit à l'image enregistré" + (" avec signature" if nom_fichier else " (refus)"),
          "success")
    return redirect(url_for("intervenants.fiche", iid=iid))


@intervenants_bp.route("/<int:iid>/supprimer", methods=["POST"])
@login_required
def supprimer(iid):
    """Suppression définitive (RGPD) : fiche, photo et signatures."""
    _garde(ecriture=True)
    with _db() as conn:
        it = _get(conn, iid)
        conn.execute("DELETE FROM intervenants_temporaires WHERE id = ?", (iid,))
    chemins = [os.path.join(fichiers.dossier_photos(), f"{iid}.jpg")]
    dossier = fichiers.dossier_signatures()
    if os.path.isdir(dossier):
        chemins += [os.path.join(dossier, n) for n in os.listdir(dossier)
                    if n.startswith(f"tmp_{iid}_") and n.endswith(".png")]
    for c in chemins:
        try:
            os.remove(c)
        except FileNotFoundError:
            pass
    try:
        write_log(f"Intervenant temporaire supprimé : {it['prenom']} {it['nom']} (id {iid})")
    except Exception:
        pass
    flash("🗑️ Intervenant supprimé (fiche, photo et signature)", "success")
    return redirect(url_for("intervenants.liste"))


# ---------------------------------------------------------------- fichiers
@intervenants_bp.route("/<int:iid>/photo.jpg")
@login_required
def voir_photo(iid):
    _garde()
    if not _photo_existe(iid):
        abort(404)
    return send_from_directory(fichiers.dossier_photos(), f"{iid}.jpg")


@intervenants_bp.route("/<int:iid>/signature.png")
@login_required
def voir_signature(iid):
    _garde()
    with _db() as conn:
        it = _get(conn, iid)
    if not it["signature_fichier"]:
        abort(404)
    return send_from_directory(fichiers.dossier_signatures(),
                               os.path.basename(it["signature_fichier"]))
