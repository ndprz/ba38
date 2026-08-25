# ba38_annexe1bis.py
import sqlite3
import os
import re
from io import BytesIO
from datetime import date, datetime

from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, send_file
)
from flask_login import login_required, current_user

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import simpleSplit
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image,
    PageBreak, Flowable
)
from reportlab.lib.enums import TA_LEFT

from utils import (
    get_db_connection,
    write_log,
    upload_database,
    require_access,
    upload_file_to_drive
)


annexe1bis_bp = Blueprint("annexe1bis", __name__)

STATUTS_SIGNATURE = ("brouillon", "envoyee", "signee", "refusee", "expiree")

# Champs à saisie multiple (cases à cocher, valeurs jointes par une virgule),
# même convention que dans la fiche partenaire (ba38_partenaires.update_partner).
CHAMPS_MULTIPLES = ("produits_souhaites", "autres_approvisionnements")


def clean_row(row):
    """Convertit sqlite3.Row en dict et remplace None par '' """
    if not row:
        return {}
    return {k: (v if v is not None else "") for k, v in dict(row).items()}


def get_annexe_field_defs(cursor):
    """Définitions des 84 champs de l'annexe 1 bis, pilotées par field_groups
    (mêmes métadonnées que celles utilisées historiquement pour le formulaire
    partenaire, retaguées appli='annexe1bis' lors de la migration)."""
    rows = cursor.execute(
        """
        SELECT field_name, type_champ, group_name, display_order, is_required
        FROM field_groups
        WHERE appli = 'annexe1bis'
        ORDER BY display_order
        """
    ).fetchall()
    return [dict(r) for r in rows]


def build_fields_data(cursor, values):
    """Associe à chaque définition de champ sa valeur courante (depuis `values`,
    un dict de valeurs d'annexe, éventuellement vide pour une nouvelle annexe)."""
    fields = get_annexe_field_defs(cursor)
    for field in fields:
        field["value"] = values.get(field["field_name"], "") or ""
    return fields


def get_latest_annexe(cursor, partner_id):
    row = cursor.execute(
        """
        SELECT * FROM annexe1bis
        WHERE partenaire_id = ?
        ORDER BY date_creation DESC, id DESC
        LIMIT 1
        """,
        (partner_id,)
    ).fetchone()
    return clean_row(row)


def get_liste_reseaux(cursor):
    reseaux = cursor.execute(
        """
        SELECT param_value FROM parametres
        WHERE param_name = 'RESEAUX_NATIONAUX'
        ORDER BY param_value
        """
    ).fetchall()
    return [r["param_value"] for r in reseaux]


# ========================================
# 📋 Historique des annexes d'un partenaire
# ========================================
@annexe1bis_bp.route("/annexe1bis/<int:partner_id>")
@login_required
@require_access("associations", "lecture")
def liste(partner_id):
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        partenaire = cursor.execute(
            "SELECT * FROM associations WHERE id = ?", (partner_id,)
        ).fetchone()

        annexes = cursor.execute(
            """
            SELECT * FROM annexe1bis
            WHERE partenaire_id = ?
            ORDER BY date_creation DESC, id DESC
            """,
            (partner_id,)
        ).fetchall()

        conn.close()
    except Exception as e:
        write_log(f"❌ Erreur chargement annexe1bis : {e}")
        flash("Erreur lors du chargement des annexes 1 bis.", "danger")
        return redirect(url_for("partenaires.update_partner", partner_id=partner_id))

    return render_template(
        "annexe1bis/annexe1bis_liste.html",
        partenaire=clean_row(partenaire),
        annexes=[clean_row(a) for a in annexes],
        partner_id=partner_id
    )


def _save_annexe_fields(cursor, annexe_id, request_form, fields_defs, statut_actuel="brouillon"):
    """Construit et exécute l'UPDATE des 84 champs dynamiques + workflow de
    signature pour une annexe existante. `fields_defs` = get_annexe_field_defs().
    `statut_actuel` : statut déjà en base, utilisé comme valeur de repli quand le
    <select> statut_signature est désactivé côté template (cas 'envoyee', piloté
    par Yousign) et n'est donc pas transmis dans le formulaire."""
    data = {}

    for champ in CHAMPS_MULTIPLES:
        valeurs = ",".join(request_form.getlist(champ))
        data[champ] = valeurs or None

    for field in fields_defs:
        fname = field["field_name"]
        if fname in CHAMPS_MULTIPLES:
            continue
        val = (request_form.get(fname, "") or "").strip()
        data[fname] = val or None

    # Workflow de signature
    statut_signature = request_form.get("statut_signature", statut_actuel)
    if statut_signature not in STATUTS_SIGNATURE:
        statut_signature = "brouillon"
    data["statut_signature"] = statut_signature
    data["date_envoi_signature"] = request_form.get("date_envoi_signature") or None
    data["date_signature"] = request_form.get("date_signature") or None
    data["date_creation"] = request_form.get("date_creation") or date.today().strftime("%Y-%m-%d")

    set_clause = ", ".join(f"`{k}` = ?" for k in data)
    params = list(data.values()) + [annexe_id]

    cursor.execute(f"UPDATE annexe1bis SET {set_clause} WHERE id = ?", params)


# ========================================
# 🆕 Nouvelle annexe (préremplie depuis la dernière)
# ========================================
@annexe1bis_bp.route("/annexe1bis/<int:partner_id>/nouvelle", methods=["GET", "POST"])
@login_required
@require_access("associations", "ecriture")
def nouvelle(partner_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    partenaire = cur.execute(
        "SELECT * FROM associations WHERE id = ?", (partner_id,)
    ).fetchone()
    if not partenaire:
        conn.close()
        flash("⛔ Partenaire introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    fields_defs = get_annexe_field_defs(cur)

    if request.method == "POST":
        data = {}

        for champ in CHAMPS_MULTIPLES:
            valeurs = ",".join(request.form.getlist(champ))
            data[champ] = valeurs or None

        for field in fields_defs:
            fname = field["field_name"]
            if fname in CHAMPS_MULTIPLES:
                continue
            val = (request.form.get(fname, "") or "").strip()
            data[fname] = val or None

        data["partenaire_id"] = partner_id
        data["date_creation"] = request.form.get("date_creation") or date.today().strftime("%Y-%m-%d")
        data["user_creation"] = current_user.username
        data["statut_signature"] = "brouillon"

        cols = ", ".join(f"`{k}`" for k in data)
        placeholders = ", ".join("?" for _ in data)
        cur.execute(
            f"INSERT INTO annexe1bis ({cols}) VALUES ({placeholders})",
            list(data.values())
        )
        conn.commit()
        conn.close()

        upload_database()
        flash("✅ Nouvelle annexe 1 bis enregistrée.", "success")
        return redirect(url_for("annexe1bis.liste", partner_id=partner_id))

    # 🔁 Préremplissage avec la dernière annexe du partenaire (tous statuts confondus),
    # sauf si l'utilisateur a explicitement demandé une annexe vierge.
    vierge = request.args.get("vierge") == "1"

    last_annexe = {} if vierge else get_latest_annexe(cur, partner_id)

    valeurs = dict(last_annexe)
    last_date = valeurs.get("date_creation") if not vierge else None

    fields_data = build_fields_data(cur, valeurs)

    # Repart des valeurs de la dernière annexe (pour l'affichage conditionnel de
    # statut_autre / reseau_national), mais avec un workflow de signature neuf.
    annexe = {
        **valeurs,
        "date_creation": date.today().strftime("%Y-%m-%d"),
        "statut_signature": "brouillon",
        "date_envoi_signature": "",
        "date_signature": "",
    }

    liste_reseaux = get_liste_reseaux(cur)
    conn.close()

    return render_template(
        "annexe1bis/annexe1bis_form.html",
        partenaire=clean_row(partenaire),
        annexe=annexe,
        fields=fields_data,
        liste_reseaux=liste_reseaux,
        partner_id=partner_id,
        annexe_id=None,
        last_date=last_date,
        lecture_seule=False
    )


# ========================================
# 👁️ Consulter une annexe
# ========================================
@annexe1bis_bp.route("/annexe1bis/view/<int:annexe_id>")
@login_required
@require_access("associations", "lecture")
def view(annexe_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    annexe = cur.execute("SELECT * FROM annexe1bis WHERE id = ?", (annexe_id,)).fetchone()
    if not annexe:
        conn.close()
        flash("❌ Annexe introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    partenaire = cur.execute(
        "SELECT * FROM associations WHERE id = ?", (annexe["partenaire_id"],)
    ).fetchone()

    fields_data = build_fields_data(cur, dict(annexe))
    liste_reseaux = get_liste_reseaux(cur)
    conn.close()

    return render_template(
        "annexe1bis/annexe1bis_form.html",
        partenaire=clean_row(partenaire),
        annexe=clean_row(annexe),
        fields=fields_data,
        liste_reseaux=liste_reseaux,
        partner_id=annexe["partenaire_id"],
        annexe_id=annexe_id,
        lecture_seule=True
    )


# ========================================
# ✏️ Modifier une annexe (tant qu'elle n'est pas signée)
# ========================================
@annexe1bis_bp.route("/annexe1bis/modifier/<int:annexe_id>", methods=["GET", "POST"])
@login_required
@require_access("associations", "ecriture")
def modifier(annexe_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    annexe = cur.execute("SELECT * FROM annexe1bis WHERE id = ?", (annexe_id,)).fetchone()
    if not annexe:
        conn.close()
        flash("❌ Annexe introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    if annexe["statut_signature"] == "signee":
        conn.close()
        flash("🔒 Cette annexe est signée et n'est plus modifiable. Créez une nouvelle annexe si besoin.", "warning")
        return redirect(url_for("annexe1bis.view", annexe_id=annexe_id))

    fields_defs = get_annexe_field_defs(cur)

    if request.method == "POST":
        _save_annexe_fields(cur, annexe_id, request.form, fields_defs, annexe["statut_signature"])
        conn.commit()
        conn.close()

        upload_database()
        flash("✅ Annexe 1 bis mise à jour avec succès.", "success")
        return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))

    partenaire = cur.execute(
        "SELECT * FROM associations WHERE id = ?", (annexe["partenaire_id"],)
    ).fetchone()

    fields_data = build_fields_data(cur, dict(annexe))
    liste_reseaux = get_liste_reseaux(cur)
    conn.close()

    return render_template(
        "annexe1bis/annexe1bis_form.html",
        partenaire=clean_row(partenaire),
        annexe=clean_row(annexe),
        fields=fields_data,
        liste_reseaux=liste_reseaux,
        partner_id=annexe["partenaire_id"],
        annexe_id=annexe_id,
        lecture_seule=False
    )


# ========================================
# 🗑️ Supprimer une annexe
# ========================================
@annexe1bis_bp.route("/annexe1bis/supprimer/<int:annexe_id>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def supprimer(annexe_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        annexe = cursor.execute(
            "SELECT partenaire_id FROM annexe1bis WHERE id = ?", (annexe_id,)
        ).fetchone()

        if not annexe:
            conn.close()
            flash("❌ Annexe introuvable.", "danger")
            return redirect(url_for("partenaires.partenaires"))

        partner_id = annexe["partenaire_id"]

        cursor.execute("DELETE FROM annexe1bis WHERE id = ?", (annexe_id,))
        conn.commit()
        conn.close()

        upload_database()
        flash("✅ Annexe 1 bis supprimée.", "success")
        return redirect(url_for("annexe1bis.liste", partner_id=partner_id))

    except Exception as e:
        write_log(f"❌ Erreur suppression annexe1bis {annexe_id} : {e}")
        flash("Erreur lors de la suppression.", "danger")
        return redirect(url_for("partenaires.partenaires"))


@annexe1bis_bp.route("/annexe1bis/pdf/<int:annexe_id>")
@login_required
@require_access("associations", "lecture")
def pdf(annexe_id):
    # Si l'annexe est signée, servir le vrai PDF signé (avec le tampon de
    # signature) plutôt que de régénérer un PDF vierge via ReportLab — sinon
    # ce bouton est trompeur (il montrait un document sans aucune signature
    # visible, alors que l'annexe est bien signée).
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    annexe = conn.execute(
        "SELECT document_signe_path FROM annexe1bis WHERE id = ?", (annexe_id,)
    ).fetchone()
    conn.close()

    chemin_signe = annexe["document_signe_path"] if annexe else None
    if chemin_signe and os.path.exists(chemin_signe):
        return send_file(
            chemin_signe,
            as_attachment=True,
            download_name=f"annexe1bis_{annexe_id}_signe.pdf",
            mimetype='application/pdf'
        )

    try:
        return generate_pdf_annexe1bis(annexe_id)
    except Exception as e:
        write_log(f"❌ Erreur génération PDF annexe1bis {annexe_id} : {e}")
        return f"Erreur génération PDF : {e}", 500


@annexe1bis_bp.route("/annexe1bis/telecharger_signe/<int:annexe_id>")
@login_required
@require_access("associations", "lecture")
def telecharger_signe(annexe_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    annexe = conn.execute("SELECT document_signe_path FROM annexe1bis WHERE id = ?", (annexe_id,)).fetchone()
    conn.close()

    chemin = annexe["document_signe_path"] if annexe else None
    if not chemin or not os.path.exists(chemin):
        flash("❌ Document signé introuvable.", "danger")
        return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))

    return send_file(
        chemin,
        as_attachment=True,
        download_name=f"annexe1bis_{annexe_id}_signe.pdf",
        mimetype='application/pdf'
    )


def _extraire_dossier_drive_id(drive_link):
    """Extrait l'ID de dossier depuis une URL du type
    https://drive.google.com/drive/folders/<ID> (champ `drive_link` des
    associations, groupe 'coordonnées principales')."""
    if not drive_link:
        return None
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", drive_link)
    return m.group(1) if m else None


@annexe1bis_bp.route("/annexe1bis/enregistrer_drive/<int:annexe_id>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def enregistrer_drive(annexe_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    annexe = cur.execute("SELECT * FROM annexe1bis WHERE id = ?", (annexe_id,)).fetchone()
    if not annexe:
        conn.close()
        flash("❌ Annexe introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    partenaire = cur.execute(
        "SELECT * FROM associations WHERE id = ?", (annexe["partenaire_id"],)
    ).fetchone()
    conn.close()

    chemin = annexe["document_signe_path"]
    if not chemin or not os.path.exists(chemin):
        flash("❌ Document signé introuvable.", "danger")
        return redirect(url_for("annexe1bis.view", annexe_id=annexe_id))

    dossier_id = _extraire_dossier_drive_id(partenaire["drive_link"] if partenaire else None)
    if not dossier_id:
        flash("⛔ Aucun dossier Drive renseigné sur la fiche partenaire (coordonnées principales).", "danger")
        return redirect(url_for("annexe1bis.view", annexe_id=annexe_id))

    date_nom = (annexe["date_signature"] or annexe["date_envoi_signature"] or date.today().strftime("%Y-%m-%d")).replace("-", "")
    nom_fichier = f"annexe 1 bis {date_nom}.pdf"

    try:
        upload_file_to_drive(chemin, dossier_id, filename=nom_fichier)
    except Exception as e:
        write_log(f"❌ Upload Drive annexe1bis {annexe_id} : {e}")
        flash(f"❌ Erreur lors de l'envoi vers le Drive : {e}", "danger")
        return redirect(url_for("annexe1bis.view", annexe_id=annexe_id))

    flash(f"📁 PDF signé enregistré dans le Drive de l'association sous « {nom_fichier} ».", "success")
    return redirect(url_for("annexe1bis.view", annexe_id=annexe_id))


def _split_nom_president(nom_complet):
    """Découpage naïf 'Prénom Nom' -> (prenom, nom). Limite connue : ne gère
    pas les noms composés avec certitude, à affiner si besoin."""
    parts = (nom_complet or "").strip().split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1 and parts[0]:
        return parts[0], "Président"
    return "Président", "Association"


def _dossier_signe(annexe_id):
    base_dir = os.getenv("BA38_BASE_DIR", ".")
    dossier = os.path.join(base_dir, "uploads", "annexe1bis", str(annexe_id))
    os.makedirs(dossier, exist_ok=True)
    return dossier


# ========================================
# 📤 Envoyer pour signature via LibreSign
# ========================================
@annexe1bis_bp.route("/annexe1bis/envoyer_signature/<int:annexe_id>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def envoyer_signature(annexe_id):
    from utils_libresign import envoyer_signature_request, LibreSignError, COORDONNEES_PAVE_SIGNATURE

    data = charger_donnees_pdf_annexe1bis(annexe_id)
    if data is None:
        flash("❌ Annexe ou partenaire introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    if data.get("statut_signature") not in ("brouillon", "refusee"):
        flash("⛔ Cette annexe n'est pas dans un état permettant l'envoi en signature.", "warning")
        return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))

    courriel_president = (data.get("courriel_president") or "").strip()
    if not courriel_president:
        flash(
            "⛔ Impossible d'envoyer : aucun email de président renseigné sur la fiche partenaire.",
            "danger"
        )
        return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))

    prenom, nom = _split_nom_president(data.get("nom_president_ou_officiel"))
    nom_document = f"Annexe 1 bis - {data.get('nom_association') or 'Partenaire'}"

    try:
        pdf_bytes = _build_pdf_bytes(data)
        resultat = envoyer_signature_request(
            pdf_bytes=pdf_bytes,
            nom_document=nom_document,
            signataire_prenom=prenom,
            signataire_nom=nom,
            signataire_email=courriel_president,
            coordonnees=COORDONNEES_PAVE_SIGNATURE,
        )
    except LibreSignError as e:
        write_log(f"❌ LibreSign envoyer_signature annexe {annexe_id} : {e}")
        flash(f"❌ Erreur lors de l'envoi à LibreSign : {e}", "danger")
        return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))

    conn = get_db_connection()
    conn.execute(
        """
        UPDATE annexe1bis
        SET statut_signature = 'envoyee',
            date_envoi_signature = ?,
            libresign_uuid = ?,
            libresign_file_id = ?,
            libresign_sign_request_id = ?,
            destinataire_signature_nom = ?,
            destinataire_signature_email = ?
        WHERE id = ?
        """,
        (
            date.today().strftime("%Y-%m-%d"),
            resultat["uuid"],
            resultat["file_id"],
            resultat["sign_request_id"],
            f"{prenom} {nom}",
            courriel_president,
            annexe_id,
        )
    )
    conn.commit()
    conn.close()

    upload_database()
    flash(f"📤 Annexe envoyée pour signature à {courriel_president}.", "success")
    return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))


# ========================================
# 🔄 Vérifier le statut auprès de LibreSign (pas de webhook configuré)
# ========================================
@annexe1bis_bp.route("/annexe1bis/verifier_statut/<int:annexe_id>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def verifier_statut(annexe_id):
    from utils_libresign import recuperer_statut, telecharger_document_signe, LibreSignError

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    annexe = conn.execute("SELECT * FROM annexe1bis WHERE id = ?", (annexe_id,)).fetchone()

    if not annexe or not annexe["libresign_file_id"]:
        conn.close()
        flash("ℹ️ Aucune demande de signature LibreSign associée à cette annexe.", "info")
        return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))

    file_id = annexe["libresign_file_id"]

    try:
        statut_data = recuperer_statut(file_id)
    except LibreSignError as e:
        conn.close()
        write_log(f"❌ LibreSign verifier_statut annexe {annexe_id} : {e}")
        flash(f"❌ Erreur lors de la vérification auprès de LibreSign : {e}", "danger")
        return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))

    # Codes confirmés le 2026-07-16 : 0=Brouillon, 1=Prêt à signer, 3=Signés.
    # Refus/expiration non testés en conditions réelles — pas de mapping pour
    # l'instant, à ajuster dès qu'un cas réel sera observé.
    mapping = {3: "signee"}
    nouveau_statut = mapping.get(statut_data.get("status"))

    if not nouveau_statut:
        conn.close()
        flash(f"ℹ️ Statut LibreSign actuel : « {statut_data.get('statusText')} » (pas encore de changement).", "info")
        return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))

    document_signe_path = None
    if nouveau_statut == "signee":
        try:
            pdf_signe = telecharger_document_signe(annexe["libresign_uuid"])
            chemin = os.path.join(_dossier_signe(annexe_id), f"signe_{int(datetime.now().timestamp())}.pdf")
            with open(chemin, "wb") as f:
                f.write(pdf_signe)
            document_signe_path = chemin
        except LibreSignError as e:
            write_log(f"⚠️ Téléchargement document signé échoué (annexe {annexe_id}) : {e}")

    conn.execute(
        """
        UPDATE annexe1bis
        SET statut_signature = ?,
            date_signature = CASE WHEN ? = 'signee' THEN ? ELSE date_signature END,
            document_signe_path = COALESCE(?, document_signe_path)
        WHERE id = ?
        """,
        (nouveau_statut, nouveau_statut, date.today().strftime("%Y-%m-%d"), document_signe_path, annexe_id)
    )
    conn.commit()
    conn.close()

    upload_database()
    flash(f"✅ Statut mis à jour : {nouveau_statut}.", "success")
    return redirect(url_for("annexe1bis.modifier", annexe_id=annexe_id))


# ========================================
# 🪝 Webhook Yousign (préparé, pas encore activable : accès webhook non
# disponible sur le compte sandbox utilisé — cf. mémoire ba38_annexe1bis_migration)
# ========================================
@annexe1bis_bp.route("/webhooks/yousign", methods=["POST"])
def webhook_yousign():
    import hmac
    import hashlib

    secret = os.getenv("YOUSIGN_WEBHOOK_SECRET", "")
    signature_recue = request.headers.get("X-Yousign-Signature-256", "")
    raw_body = request.get_data()

    if secret:
        digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        signature_attendue = f"sha256={digest}"
        if not hmac.compare_digest(signature_recue, signature_attendue):
            write_log("⛔ Webhook Yousign : signature invalide")
            return "invalid signature", 403

    payload = request.get_json(silent=True) or {}
    write_log(f"🪝 Webhook Yousign reçu : {payload}")

    # Le schéma exact du payload n'a pas pu être confirmé via la documentation
    # publique (accès webhook indisponible au moment de l'écriture) : on essaie
    # plusieurs emplacements plausibles pour rester tolérant tant qu'on n'a pas
    # un exemple réel à observer.
    event_name = payload.get("event_name") or payload.get("type")
    sr = payload.get("data", {}).get("signature_request") or payload.get("signature_request") or {}
    signature_request_id = sr.get("id")

    if not event_name or not signature_request_id:
        write_log("⚠️ Webhook Yousign : event_name ou signature_request_id introuvable dans le payload")
        return "", 200

    mapping = {
        "signature_request.done": "signee",
        "signature_request.declined": "refusee",
        "signature_request.rejected": "refusee",
        "signature_request.expired": "expiree",
    }
    nouveau_statut = mapping.get(event_name)
    if not nouveau_statut:
        return "", 200

    conn = get_db_connection()
    annexe = conn.execute(
        "SELECT id FROM annexe1bis WHERE yousign_signature_request_id = ?",
        (signature_request_id,)
    ).fetchone()

    if not annexe:
        conn.close()
        write_log(f"⚠️ Webhook Yousign : aucune annexe pour signature_request_id={signature_request_id}")
        return "", 200

    annexe_id = annexe["id"] if isinstance(annexe, sqlite3.Row) else annexe[0]

    document_signe_path = None
    if nouveau_statut == "signee":
        try:
            from utils_yousign import telecharger_document_signe
            pdf_signe = telecharger_document_signe(signature_request_id)
            chemin = os.path.join(_dossier_signe(annexe_id), f"signe_{int(datetime.now().timestamp())}.pdf")
            with open(chemin, "wb") as f:
                f.write(pdf_signe)
            document_signe_path = chemin
        except Exception as e:
            write_log(f"⚠️ Téléchargement document signé (webhook) échoué : {e}")

    conn.execute(
        """
        UPDATE annexe1bis
        SET statut_signature = ?,
            date_signature = CASE WHEN ? = 'signee' THEN ? ELSE date_signature END,
            document_signe_path = COALESCE(?, document_signe_path)
        WHERE id = ?
        """,
        (nouveau_statut, nouveau_statut, date.today().strftime("%Y-%m-%d"), document_signe_path, annexe_id)
    )
    conn.commit()
    conn.close()

    upload_database()
    write_log(f"✅ Webhook Yousign : annexe {annexe_id} → {nouveau_statut}")
    return "", 200


@annexe1bis_bp.app_template_filter('datetimeformat')
def datetimeformat(value, format='%d/%m/%Y'):
    if not value:
        return ''
    try:
        return datetime.strptime(value, '%Y-%m-%d').strftime(format)
    except ValueError:
        try:
            return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').strftime(format)
        except ValueError:
            return value


# ============================================================
# 📄 Export PDF (ReportLab) — mise en page calquée sur le gabarit
# officiel "Annexe 1 bis / Point de distribution" fourni par
# l'utilisateur (voir templates/pdf/annexe_1_bis.pdf s'il est
# déposé sur le serveur, sinon référence conservée dans la
# conversation). Génération 100% pilotée par le code (pas de
# remplissage de formulaire PDF externe).
# ============================================================
def header_footer(canvas, doc, title, subtitle):
    # --- EN-TÊTE ---
    canvas.saveState()

    # Logo
    logo_path = "static/images/logo.png"
    if os.path.exists(logo_path):
        canvas.drawImage(logo_path, x=40, y=A4[1] - 60, width=1.5*cm, height=1.5*cm)

    # Titre centré
    canvas.setFont("Helvetica-Bold", 14)
    canvas.setFillColor(colors.darkblue)
    canvas.drawCentredString(A4[0]/2, A4[1] - 38, title)

    # Sous-titre centré
    if subtitle:
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawCentredString(A4[0]/2, A4[1] - 53, subtitle)
    canvas.setFillColorRGB(0, 0, 0)  # Réinitialiser en noir

    # Filet horizontal sous l'en-tête
    canvas.setStrokeColorRGB(0.6, 0.6, 0.6)
    canvas.line(40, A4[1] - 68, A4[0] - 40, A4[1] - 68)
    canvas.setStrokeColorRGB(0, 0, 0)

    # --- PIED DE PAGE ---
    page_num = canvas.getPageNumber()
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(A4[0] - 40, 20, f"Page {page_num}")

    canvas.restoreState()
class CheckBox(Flowable):
    def __init__(self, checked=False, size=9):
        super().__init__()
        self.checked = checked
        self.size = size
        self.width = self.size
        self.height = self.size  # Centrage vertical

    def draw(self):
        self.canv.rect(0, 0, self.size, self.size)
        if self.checked:
            self.canv.line(0, 0, self.size, self.size)
            self.canv.line(0, self.size, self.size, 0)



def safe_paragraph_value(value):
    """Retourne une chaîne vide si value est None, sinon la valeur convertie en str."""
    return str(value) if value is not None else ""


def charger_donnees_pdf_annexe1bis(annexe_id):
    """
    Charge la ligne `annexe1bis` + l'association associée et les fusionne en un
    seul dict (aucune collision de clé possible entre les deux tables). Retourne
    None si l'annexe ou le partenaire est introuvable.
    """
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    annexe = cursor.execute("SELECT * FROM annexe1bis WHERE id = ?", (annexe_id,)).fetchone()
    if not annexe:
        conn.close()
        return None

    partner_id = annexe["partenaire_id"]
    partner = cursor.execute("SELECT * FROM associations WHERE id = ?", (partner_id,)).fetchone()
    conn.close()
    if not partner:
        return None

    return {**dict(partner), **dict(annexe)}


def generate_pdf_annexe1bis(annexe_id):
    data = charger_donnees_pdf_annexe1bis(annexe_id)
    if data is None:
        return "Annexe ou partenaire introuvable", 404

    pdf_bytes = _build_pdf_bytes(data)
    return send_file(
        BytesIO(pdf_bytes),
        as_attachment=True,
        download_name=f"annexe1bis_{annexe_id}.pdf",
        mimetype='application/pdf'
    )


def _build_pdf_bytes(data):
    title = "ANNEXE 1 BIS"
    subtitle = "Point de distribution"

    # --- Styles PDF ---
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=2.5 * cm, bottomMargin=2 * cm)
    styles = getSampleStyleSheet()
    cell_style = styles["Normal"]
    style_h1 = ParagraphStyle('h1', parent=styles['Heading1'], alignment=1, fontSize=14, textColor=colors.darkblue)
    style_h2 = ParagraphStyle('h2', parent=styles['Heading2'], textColor=colors.darkblue, spaceBefore=12)
    style_h_partner = ParagraphStyle('h_partner', parent=styles['Heading1'], alignment=1, fontSize=14, textColor=colors.darkblue, spaceAfter=12)
    style_n = ParagraphStyle('centered', parent=styles['Normal'], alignment=1)
    # Style normal aligné à gauche explicitement (sécurise l’alignement)
    style_n_left = ParagraphStyle(
        'Normal_Left',
        parent=styles['Normal'],
        alignment=TA_LEFT,
        fontName='Helvetica',
        fontSize=10,
        leading=12,
        spaceAfter=6,
    )
    # Style titre (gras, bleu foncé)
    style_title = ParagraphStyle(
        'Title',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        textColor=colors.darkblue,
        spaceAfter=6,
        leading=14,
    )

    # Style header tableau (fond beige, centré, gras)
    style_header = ParagraphStyle(
        'Header',
        parent=styles['Normal'],
        alignment=1,  # centré
        backColor=colors.beige,
        fontName='Helvetica-Bold'
    )

    elements = []

    # ==============================================================================================================
    # === 1. Informations principales ===
    # ==============================================================================================================

    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph("1. Informations sur le partenaire", style_h2))
    elements.append(Paragraph(f"Nom du partenaire / point de distribution : {safe_paragraph_value(data.get('nom_association'))}", style_n_left))
    elements.append(Paragraph(f"Numéro de SIRET : {safe_paragraph_value(data.get('code_SIRET'))}", style_n_left))
    elements.append(Paragraph(f"Adresse e-mail : {safe_paragraph_value(data.get('courriel_association'))}", style_n_left))
    adresse = " ".join(filter(None, [
        safe_paragraph_value(data.get('adresse_association_1')),
        safe_paragraph_value(data.get('adresse_association_2')),
        safe_paragraph_value(data.get('CP')),
        safe_paragraph_value(data.get('COMMUNE'))
    ]))
    elements.append(Paragraph(f"Adresse lieu de distribution : {adresse}", style_n_left))
    elements.append(Paragraph(f"Téléphone : {safe_paragraph_value(data.get('tel_association'))}", style_n_left))
    elements.append(Paragraph(f"Adresse du siège : {safe_paragraph_value(data.get('adresse_siege_complete'))}", style_n_left))
    elements.append(Paragraph(f"Adresse courrier : {safe_paragraph_value(data.get('adresse_courrier_complete'))}", style_n_left))
    elements.append(Paragraph(f"Secteur Géographique : {safe_paragraph_value(data.get('secteur_geographique'))}", style_n_left))
    elements.append(Paragraph(f"Nombre de Bénévoles : {safe_paragraph_value(data.get('combien_de_benevoles'))}", style_n_left))
    elements.append(Paragraph(f"Nombre de Salariés : {safe_paragraph_value(data.get('combien_de_salaries'))}", style_n_left))
    elements.append(Spacer(1, 0.5 * cm))

    # Interlocuteurs ===
    style_h2_sous = ParagraphStyle('h2_sous', parent=style_h2, textColor=colors.darkblue, fontSize=12, leftIndent=0)
    elements.append(Paragraph("Interlocuteurs chez le partenaire", style_h2_sous))

    # Présence d’un travailleur social (alignement horizontal parfait)
    presence = safe_paragraph_value(data.get('presence_travailleur_social', 'non'))

    block_oui = Table([[CheckBox(presence == 'oui', size=9), Paragraph("Oui", style_n_left)]],
                    colWidths=[0.6 * cm, 1.5 * cm])
    block_non = Table([[CheckBox(presence == 'non', size=9), Paragraph("Non", style_n_left)]],
                    colWidths=[0.6 * cm, 1.5 * cm])

    presence_paragraph = [
        CheckBox(presence == 'oui', size=9),
        Spacer(0.2 * cm, 0),
        Paragraph("Oui", style_n_left),
        Spacer(0.5 * cm, 0),
        CheckBox(presence == 'non', size=9),
        Spacer(0.2 * cm, 0),
        Paragraph("Non", style_n_left)
    ]

    presence = safe_paragraph_value(data.get('presence_travailleur_social', 'non'))

    presence_line = Table(
        [[
            Paragraph("Présence d’un travailleur social :", style_n_left),
            CheckBox(presence == 'oui', size=9), Paragraph("Oui", style_n_left),
            CheckBox(presence == 'non', size=9), Paragraph("Non", style_n_left)
        ]],
        colWidths=[6 * cm, 0.7 * cm, 2 * cm, 0.7 * cm, 2 * cm],
        rowHeights=[0.5 * cm]
    )
    presence_line.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, 0), 'CENTER'),
        ('ALIGN', (3, 0), (3, 0), 'CENTER')
    ]))
    elements.append(presence_line)
    elements.append(Spacer(1, 0.3 * cm))


    # Tableau des interlocuteurs
    interlocuteurs = [
        [
            Paragraph("Rôle", cell_style),
            Paragraph("Nom / Prénom", cell_style),
            Paragraph("Téléphone", cell_style),
            Paragraph("Courriel", cell_style),
            Paragraph("Statut", cell_style)
        ],
        [
            Paragraph("Président", cell_style),
            Paragraph(safe_paragraph_value(data.get("nom_president_ou_officiel")), cell_style),
            Paragraph(safe_paragraph_value(data.get("tel_president_officiel_1")), cell_style),
            Paragraph(safe_paragraph_value(data.get("courriel_president")), cell_style),
            Paragraph(safe_paragraph_value(data.get("statut_president")), cell_style)
        ],
        [
            Paragraph("Distribution", cell_style),
            Paragraph(safe_paragraph_value(data.get("responsable_distribution")), cell_style),
            Paragraph(safe_paragraph_value(data.get("tel_resp_distribution_1")), cell_style),
            Paragraph(safe_paragraph_value(data.get("courriel_distribution")), cell_style),
            Paragraph(safe_paragraph_value(data.get("statut_resp_distribution")), cell_style)
        ],
        [
            Paragraph("Trésorerie", cell_style),
            Paragraph(safe_paragraph_value(data.get("responsable_tresorerie")), cell_style),
            Paragraph(safe_paragraph_value(data.get("tel_resp_tresorerie_1")), cell_style),
            Paragraph(safe_paragraph_value(data.get("courriel_resp_tresorerie")), cell_style),
            Paragraph(safe_paragraph_value(data.get("statut_resp_tresorerie")), cell_style)
        ],
        [
            Paragraph("Hygiène / Sécurité", cell_style),
            Paragraph(safe_paragraph_value(data.get("responsable_HySA")), cell_style),
            Paragraph(safe_paragraph_value(data.get("tel_resp_Hysa_1")), cell_style),
            Paragraph(safe_paragraph_value(data.get("courriel_resp_Hysa")), cell_style),
            Paragraph(safe_paragraph_value(data.get("statut_resp_hysa")), cell_style)
        ],
        [
            Paragraph("TIXADI/Indicateurs État", cell_style),
            Paragraph(safe_paragraph_value(data.get("responsable_IE")), cell_style),
            Paragraph(safe_paragraph_value(data.get("tel_resp_IE")), cell_style),
            Paragraph(safe_paragraph_value(data.get("courriel_resp_IE1")), cell_style),
            Paragraph(safe_paragraph_value(data.get("statut_resp_ie")), cell_style)
        ],
        [
            Paragraph("Chargé Accueil/accompagnement social", cell_style),
            Paragraph(safe_paragraph_value(data.get("responsable_accueil")), cell_style),
            Paragraph(safe_paragraph_value(data.get("tel_resp_accueil")), cell_style),
            Paragraph(safe_paragraph_value(data.get("courriel_resp_accueil")), cell_style),
            Paragraph(safe_paragraph_value(data.get("statut_resp_accueil")), cell_style)
        ],
        [
            Paragraph("Contact Collecte", cell_style),
            Paragraph(safe_paragraph_value(data.get("responsable_collecte")), cell_style),
            Paragraph(safe_paragraph_value(data.get("tel_resp_collecte")), cell_style),
            Paragraph(safe_paragraph_value(data.get("courriel_resp_collecte")), cell_style),
            Paragraph(safe_paragraph_value(data.get("statut_resp_collecte")), cell_style)
        ],
        [
            Paragraph("Contact Proxidon", cell_style),
            Paragraph(safe_paragraph_value(data.get("responsable_proxidon")), cell_style),
            Paragraph(safe_paragraph_value(data.get("tel_resp_proxidon")), cell_style),
            Paragraph(safe_paragraph_value(data.get("courriel_resp_proxidon")), cell_style),
            Paragraph(safe_paragraph_value(data.get("statut_resp_proxidon")), cell_style)
        ]
    ]

    table = Table(interlocuteurs, colWidths=[3 * cm, 4.5 * cm, 3 * cm, 5 * cm, 2.5 * cm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.black),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP')
    ]))
    elements.append(table)
    elements.append(Spacer(1, 0.5 * cm))


    # Saut de page avant la section 2
    elements.append(PageBreak())


    # ==============================================================================================================
    # === 2. Habilitation ===
    # ==============================================================================================================

    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph("2. Habilitation", style_h2))
    statut = safe_paragraph_value(data.get("statut"))

    habilitation_table = [
        ["Statut :",
        CheckBox(statut == 'Association', size=9), "Association",
        CheckBox(statut == 'CCAS/CIAS', size=9), "CCAS/CIAS",
        CheckBox(statut == 'Autres', size=9), "Autres"]
    ]

    table_hab = Table(
        habilitation_table,
        colWidths=[2.5 * cm, 0.9 * cm, 3 * cm, 0.9 * cm, 3 * cm, 0.9 * cm, 3 * cm],
        rowHeights=[0.7 * cm]
    )
    table_hab.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, 0), 'CENTER'),
        ('ALIGN', (3, 0), (3, 0), 'CENTER'),
        ('ALIGN', (5, 0), (5, 0), 'CENTER'),
    ]))
    elements.append(table_hab)

    if statut == "Autres":
        elements.append(Paragraph(f"Précisions : {safe_paragraph_value(data.get('statut_autre'))}", style_n_left))

    # Texte en italique
    style_italique = ParagraphStyle('italique', parent=styles['Normal'], fontName='Helvetica-Oblique')
    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph(
        "A noter : Les CCAS, CIAS et Mairies sont des personnes morales de droit public "
        "et ne sont pas concernés par l’habilitation", style_italique))

    elements.append(Spacer(1, 0.5 * cm))

    # Champ 'Appartient Grand Réseau Habilitation Nationale'
    appartient_reseau = safe_paragraph_value(data.get('appartient_grand_reseau_habilitation_nationale', 'non'))
    reseau_national = safe_paragraph_value(data.get('reseau_national', ''))

    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph(
        "Le Partenaire appartient à un grand réseau ayant une habilitation nationale :", style_n_left))

    # Tableau Oui/Non pour l'appartenance au réseau
    table_reseau = Table(
        [[
            CheckBox(appartient_reseau == 'oui', size=9), Paragraph("Oui", style_n_left),
            CheckBox(appartient_reseau != 'oui', size=9), Paragraph("Non", style_n_left)
        ]],
        colWidths=[0.7 * cm, 2 * cm, 0.7 * cm, 2 * cm],
        rowHeights=[0.5 * cm]
    )
    table_reseau.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 0), (2, -1), 'CENTER')
    ]))
    elements.append(table_reseau)

    # Affichage du réseau national si 'oui'
    if appartient_reseau == 'oui' and reseau_national:
        elements.append(Spacer(1, 0.2 * cm))
        elements.append(Paragraph(f"Réseau national : {reseau_national}", style_n_left))

    # Si non, le Partenaire a une habilitation régionale
    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph(
        "Si non, le Partenaire a une habilitation régionale "
        "(pour trouver l’Arrêté Préfectoral, saisir sur internet “le nom de la région” suivi de “habilitation aide alimentaire”)",
        style_n_left
    ))

    # Ligne : Habilitation régionale
    habilitation_reg = safe_paragraph_value(data.get('habilitation_regionale', 'non'))
    date_agrement = safe_paragraph_value(data.get('date_agrement_regional', ''))
    date_fin = safe_paragraph_value(data.get('date_FIN_habilitation', ''))

    table_hab_reg = Table(
        [[
            CheckBox(habilitation_reg == 'oui', size=9),
            Paragraph("Oui", style_n_left),
            CheckBox(habilitation_reg != 'oui', size=9),
            Paragraph("Non", style_n_left),
            Paragraph(f"Date Arrêté : {date_agrement}", style_n_left),
            Paragraph(f"Date Fin : {date_fin}", style_n_left)
        ]],
        colWidths=[0.7 * cm, 1.5 * cm, 0.7 * cm, 1.5 * cm, 4.5 * cm, 4.5 * cm]
    )
    table_hab_reg.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
    ]))
    elements.append(table_hab_reg)

    # Ligne suivante : Habilitation régionale en cours
    elements.append(Spacer(1, 0.1 * cm))  # Pas d'espace supplémentaire
    elements.append(Paragraph(
    "Habilitation en cours d'instruction ")),
    style_n_left
    habilitation_encours = safe_paragraph_value(data.get('habilitation_regionale_encours', 'non'))
    date_prochaine = safe_paragraph_value(data.get('habilitation_regionale_en_cours_prochaine_session', ''))

    table_hab_encours = Table(
        [[
            CheckBox(habilitation_encours == 'oui', size=9),
            Paragraph("Oui", style_n_left),
            CheckBox(habilitation_encours != 'oui', size=9),
            Paragraph("Non", style_n_left),
            Paragraph(f"Prochaine session : {date_prochaine}", style_n_left)
        ]],
        colWidths=[0.7 * cm, 1.5 * cm, 0.7 * cm, 1.5 * cm, 9 * cm]
    )
    table_hab_encours.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
    ]))
    elements.append(table_hab_encours)

    # Ligne suivante : Catégorie 1 ou 2
    categorie = safe_paragraph_value(data.get('categorie', ''))

    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph("Catégorie du partenaire (à remplir par la B.A.) :", style_n_left))

    table_categorie = Table(
        [[
            CheckBox(categorie == 'Catégorie 1', size=9),
            Paragraph("Catégorie 1", style_n_left),
            CheckBox(categorie == 'Catégorie 2', size=9),
            Paragraph("Catégorie 2", style_n_left)
        ]],
        colWidths=[0.7 * cm, 3 * cm, 0.7 * cm, 3 * cm]
    )
    table_categorie.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
    ]))
    elements.append(table_categorie)

    elements.append(Paragraph("Rappel : ", style_n_left))
    elements.append(Paragraph("- Les partenaires dits de catégorie 1 sont les autres associations et les CCAS.", style_n_left))
    elements.append(Paragraph("- Les partenaires dits de catégorie 2 sont : les unités locales Croix-Rouge française, les comités du Secours Populaire, les Restaurants du Cœur.", style_n_left))




    # Saut de page avant la section 3
    elements.append(PageBreak())

    # ==============================================================================================================
    # === 3 Activité du Partenaire ===
    # ==============================================================================================================


    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph("3. Activité du partenaire (plusieurs réponses possibles)", style_h2))
    # === Modes de distribution de l’aide alimentaire ===
    elements.append(Spacer(1, 0.4 * cm))
    elements.append(Paragraph("Modes de distribution de l’aide alimentaire", style_h2))

    modes_table = Table([[
        CheckBox(data.get('mode_distrib_colis') == 'oui', size=9),
        Paragraph("Colis", style_n_left),
        CheckBox(data.get('mode_distrib_maraude') == 'oui', size=9),
        Paragraph("Maraude", style_n_left),
        CheckBox(data.get('mode_distrib_repas') == 'oui', size=9),
        Paragraph("Repas", style_n_left),
        CheckBox(data.get('mode_distrib_petit_dejeuner') == 'oui', size=9),
        Paragraph("Petit Déjeuner/Collation", style_n_left),
    ]], colWidths=[0.7 * cm, 3 * cm, 0.7 * cm, 3 * cm, 0.7 * cm, 2 * cm, 0.7 * cm, 5 * cm])
    modes_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    elements.append(modes_table)

    # === Particularité ===
    elements.append(Spacer(1, 0.4 * cm))
    elements.append(Paragraph("Particularité", style_h2))

    part_table = Table([
        [
            CheckBox(data.get('particularite_hebergement_longue_duree') == 'oui', size=9),
            Paragraph("Hébergement longue durée (ex : CHRS)", style_n_left),
            CheckBox(data.get('particularite_hebergement_urgence') == 'oui', size=9),
            Paragraph("Hébergement d’urgence", style_n_left),
        ],
        [
            CheckBox(data.get('particularite_dispositif_itinerant') == 'oui', size=9),
            Paragraph("Dispositif itinérant", style_n_left),
            CheckBox(data.get('particularite_livraison_domicile') == 'oui', size=9),
            Paragraph("Livraison au domicile des personnes", style_n_left),
        ],
        [
            CheckBox(data.get('activite_principale_aide_alimentaire') == 'oui', size=9),
            Paragraph("L’aide alimentaire est-elle votre activité dominante ?", style_n_left),
            "", ""  # Colonnes vides pour aligner
        ]
    ], colWidths=[0.7 * cm, 6.5 * cm, 0.7 * cm, 8 * cm])
    part_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    elements.append(part_table)

    # === Publics majoritairement accueillis ===
    elements.append(Spacer(1, 0.4 * cm))
    elements.append(Paragraph("Publics majoritairement accueillis", style_h2))

    publics_table = Table([
        [
            CheckBox(data.get('public_accueilli_enfants_bas_age') == 'oui', size=9),
            Paragraph("Enfants bas âge (0-3 ans)", style_n_left),
        ],
        [
            CheckBox(data.get('public_accueilli_mineurs_isoles') == 'oui', size=9),
            Paragraph("Mineurs isolés", style_n_left),
        ],
        [
            CheckBox(data.get('public_accueilli_jeunes_travailleurs_etudiants') == 'oui', size=9),
            Paragraph("Dispositif jeunes travailleurs/étudiants", style_n_left),
        ],
        [
            CheckBox(data.get('public_accueilli_femmes_victimes_violence') == 'oui', size=9),
            Paragraph("Femmes victimes de violences conjugales", style_n_left),
        ]
    ], colWidths=[0.7 * cm, 12 * cm])
    publics_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    elements.append(publics_table)

    # Saut de page avant la section 4
    elements.append(PageBreak())

    # ==============================================================================================================
    # == 4. APPROVISIONNEMENTS =====================================================================================
    # ==============================================================================================================

    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph("4. Approvisionnements", style_h2))
    elements.append(Spacer(1, 0.3 * cm))

    # Produits souhaités (sous forme de puces)
    produits_souhaites = safe_paragraph_value(data.get('produits_souhaites', ''))
    if produits_souhaites:
        produits_list = [p.strip() for p in produits_souhaites.split(",") if p.strip()]
        if produits_list:
            elements.append(Paragraph("Produits de la BA souhaités par le partenaire :", style_n_left))
            for prod in produits_list:
                elements.append(Paragraph(f"• {prod}", style_n_left))
            elements.append(Spacer(1, 0.2 * cm))

    # Autres approvisionnements
    autres_appro = safe_paragraph_value(data.get('autres_approvisionnements', ''))
    if autres_appro:
        autres_list = [p.strip() for p in autres_appro.split(",") if p.strip()]
        if autres_list:
            elements.append(Paragraph("Autres approvisionnements :", style_n_left))
            for prod in autres_list:
                elements.append(Paragraph(f"• {prod}", style_n_left))
            elements.append(Spacer(1, 0.2 * cm))

    # Souhaits de conventionnement (trois lignes explicites avec texte + case à droite)
    # elements.append(Paragraph("Souhaits de conventionnement / projets :", style_n_left))
    # elements.append(Spacer(1, 0.1 * cm))

    wishes = [
        ("Le partenaire souhaite des produits FSE :", safe_paragraph_value(data.get('partenaire_souhaite_FSE')) == "oui"),
        ("Le partenaire souhaite une convention délégation-retrait :", safe_paragraph_value(data.get('partenaire_souhaite_convention_delegation_retrait')) == "oui"),
        ("Le partenaire souhaite une convention PROXIDON :", safe_paragraph_value(data.get('partenaire_souhaite_convention_PROXIDON')) == "oui"),
    ]
    for texte, coche in wishes:
        elements.append(
            Table(
                [[Paragraph(texte, style_n_left), CheckBox(coche, size=9)]],
                colWidths=[11.5 * cm, 1 * cm],
                style=[
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ]
            )
        )



    # Saut de page avant la section 5
    elements.append(PageBreak())

    # ==============================================================================================================
    # == 5. DISTRIBUTION ===========================================================================================
    # ==============================================================================================================

    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph("5. DISTRIBUTION", style_h2))
    elements.append(Spacer(1, 0.2 * cm))

    # Fonctionnement toute l'année : Oui / Non (case à cocher sur la même ligne)

    fonctionnement_toute_annee = safe_paragraph_value(data.get('distribution_toute_annee', '')).lower()



    style_left_no_indent = ParagraphStyle(
        'left_no_indent',
        parent=style_n_left,
        leftIndent=0,
        spaceBefore=0,
    spaceAfter=0,
    )

    elements.append(
        Table(
            [[
                Paragraph("Fonctionnement toute l’année :", style_left_no_indent),
                CheckBox(fonctionnement_toute_annee == "oui", size=9), Paragraph("Oui", style_left_no_indent),
                CheckBox(fonctionnement_toute_annee == "non", size=9), Paragraph("Non", style_left_no_indent)
            ]],
            colWidths=[8 * cm, 0.7 * cm, 1.2 * cm, 0.7 * cm, 1.2 * cm],
            style=[
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ]
        )
    )

    elements.append(Spacer(1, 0.2 * cm))


    # Si non, période de fermeture
    periode_fermeture = safe_paragraph_value(data.get('periode_de_fermeture', ''))
    if periode_fermeture:
        elements.append(Paragraph(f"Sinon, période de fermeture : {periode_fermeture}", style_n_left))

    # Alternative à la fermeture
    alternative_fermeture = safe_paragraph_value(data.get('alternative_fermeture', ''))
    if alternative_fermeture:
        elements.append(Paragraph(f"Alternative à la fermeture : {alternative_fermeture}", style_n_left))
    elements.append(Spacer(1, 0.2 * cm))

    # Fréquence de passage souhaitée à la BA
    elements.append(Paragraph("Fréquence de passage souhaitée à la Banque Alimentaire :", style_n_left))
    freq_ba = safe_paragraph_value(data.get('frequence', ''))
    if freq_ba:
        elements.append(Paragraph(freq_ba, style_n_left))
    elements.append(Spacer(1, 0.2 * cm))

    # Jours et horaires d'enlèvement convenus avec la BA (et entrepôt)
    jour_enl = safe_paragraph_value(data.get('jour_de_passage_a_la_BAI', ''))
    heure_enl = safe_paragraph_value(data.get('heure_de_passage', ''))
    emplacement_enl = safe_paragraph_value(data.get('Emplacement', ''))
    elements.append(Paragraph("Jours et horaires d’enlèvement convenus avec la BA (précisez l’entrepôt d’enlèvement) :", style_n_left))
    if any([jour_enl, heure_enl, emplacement_enl]):
        txt = " / ".join([s for s in [jour_enl, heure_enl, emplacement_enl] if s])
        elements.append(Paragraph(txt, style_n_left))
    elements.append(Spacer(1, 0.2 * cm))

    # Livraison par la BAI (champ à créer oui/non)
    livraison_bai = safe_paragraph_value(data.get('livraison_par_bai', '')).lower()
    elements.append(
        Table([[
            Paragraph("Livraison par la BAI :", style_n_left),
            CheckBox(livraison_bai == "oui", size=9), Paragraph("Oui", style_n_left),
            CheckBox(livraison_bai == "non", size=9), Paragraph("Non", style_n_left)
        ]], colWidths=[5*cm, 0.7*cm, 1.2*cm, 0.7*cm, 1.2*cm],
        style=[('VALIGN', (0,0), (-1,-1), 'MIDDLE')])
    )
    elements.append(Spacer(1, 0.2 * cm))

    # Jours et horaires de distribution alimentaire
    jour_dist = safe_paragraph_value(data.get('jour_distribution', ''))
    heure_dist = safe_paragraph_value(data.get('heure', ''))
    freq_dist = safe_paragraph_value(data.get('frequence', ''))
    elements.append(Paragraph("Jours et horaires de distribution alimentaire :", style_n_left))
    txt_dist = " / ".join([s for s in [jour_dist, heure_dist, freq_dist] if s])
    if txt_dist:
        elements.append(Paragraph(txt_dist, style_n_left))
    elements.append(Spacer(1, 0.5 * cm))


    # Saut de page avant la section 6
    elements.append(PageBreak())

    # ==============================================================================================================
    # == 6. BESOINS ET MOYENS DU PARTENAIRE ========================================================================
    # ==============================================================================================================

    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph("6. BESOINS ET MOYENS DU PARTENAIRE :", style_title))
    elements.append(Paragraph("Équipements/Locaux :", style_n_left))
    elements.append(Spacer(1, 0.1 * cm))

    equipements = [
        ("Pièce d’accueil", "piece_accueil_nbre", "piece_accueil_volume_surface"),
        ("Cuisine", "cuisine_nbre", "cuisine_volume_surface"),
        ("Local de distribution", "local_de_distribution_nbre", "local_de_distribution_volume_surface"),
        ("Local d’entreposage", "local_entreposage_nbre", "local_entreposage_volume_surface"),
        ("Chambre froide positive*", "chambre_froide_positive_nbre", "chambre_froide_positive_volume_surface"),
        ("Chambre froide négative*", "chambre_froide_negative_nbre", "chambre_froide_negative_volume_surface"),
        ("Congélateur*", "congelateur_nbre", "congelateur_volume_surface"),
        ("Réfrigérateur*", "refrigerateur_nbre", "refrigerateur_volume_surface"),
        ("Container isotherme agréé", "container_isotherme_agree_nbre", "container_isotherme_agree_volume_surface"),
        ("Glacière", "glaciere_nbre", "glaciere_volume_surface"),
        ("Plaques eutectiques", "plaques_eutectiques_nbre", "plaques_eutectiques_volume_surface"),
        ("Véhicule frigorifique*", "vehicule_frigorifique_nbre", "vehicule_frigorifique_volume_surface"),
        ("Véhicule isotherme", "vehicule_isotherme_nbre", "vehicule_isotherme_volume_surface"),
        ("Autre véhicule (préciser)", "autre_vehicule_nbre", "autre_vehicule_volume_surface"),
    ]

    table_data = [
        [Paragraph("Équipements/Locaux", style_header),
        Paragraph("Nombre", style_header),
        Paragraph("Volume ou Surface", style_header)]
    ]

    for label, champ_nb, champ_vol in equipements:
        val_nb = safe_paragraph_value(data.get(champ_nb))
        val_vol = safe_paragraph_value(data.get(champ_vol))
        table_data.append([
            Paragraph(label, style_n_left),
            Paragraph(val_nb, style_n_left),
            Paragraph(val_vol, style_n_left),
        ])

    table = Table(table_data, colWidths=[8 * cm, 3 * cm, 5 * cm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.beige),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph("*avec thermomètre et procédure de relevé ou d’enregistrement des températures", style_n_left))
    elements.append(Spacer(1, 0.3 * cm))


    # Récupérer les valeurs dans la base
    logiciel_autre = safe_paragraph_value(data.get("Logiciel_autre", "non")).lower()
    logiciel_autre_lequel = safe_paragraph_value(data.get("Logiciel_autre_lequel", ""))
    logiciel_ticadi_utilise = safe_paragraph_value(data.get("logiciel_Ticadi_utilise", ""))

    # Titre et question logiciel autre
    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph("Logiciel de gestion de l’activité :", style_title))

    elements.append(
        Table(
            [[
                Paragraph("Présence d’un logiciel de gestion de l’activité d’aide alimentaire mis à disposition par un autre réseau d’aide alimentaire :", style_n_left),
                CheckBox(logiciel_autre == "oui", size=9), Paragraph("Oui", style_n_left),
                CheckBox(logiciel_autre == "non", size=9), Paragraph("Non", style_n_left),
            ]],
            colWidths=[11 * cm, 0.7 * cm, 1.0 * cm, 0.7 * cm, 1.0 * cm],
            style=[('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]
        )
    )
    elements.append(Spacer(1, 0.2 * cm))

    # Si oui lequel ?
    elements.append(Paragraph(f"Si oui, lequel ? {logiciel_autre_lequel}", style_n_left))
    elements.append(Spacer(1, 0.3 * cm))

    # Note sur TICADI
    elements.append(Paragraph(
        "Si le Partenaire ne dispose pas d’un logiciel de gestion porté par un réseau national, "
        "le Partenaire accepte d’installer TICADI et signera la convention TICADI.", style_n_left))
    elements.append(Spacer(1, 0.2 * cm))

    # Champ logiciel_Ticadi_utilise (existant)
    elements.append(Paragraph(f"Logiciel TICADI utilisé : {logiciel_ticadi_utilise}", style_n_left))
    elements.append(Spacer(1, 0.3 * cm))




    # Saut de page avant la section 7
    elements.append(PageBreak())

    # ==============================================================================================================
    # == 7. LES PERSONNES ACCUILLIES ===============================================================================
    # ==============================================================================================================

    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph("7. LES PERSONNES ACCUEILLIES", style_title))

    # Existence d’une procédure d’éligibilité
    crit_eligibilite = safe_paragraph_value(data.get("criteres_d_eligibilite_de_l_aide_par_ecrit", "")).lower()

    elements.append(
        Table(
            [[
                Paragraph("Existence d’une procédure d’éligibilité :", style_n_left),
                CheckBox(crit_eligibilite == "oui", size=9), Paragraph("Oui", style_n_left),
                CheckBox(crit_eligibilite == "non", size=9), Paragraph("Non, en cours de réalisation", style_n_left),
            ]],
            colWidths=[9 * cm, 0.7 * cm, 2 * cm, 0.7 * cm, 5.5 * cm],
            style=[('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]
        )
    )
    elements.append(Spacer(1, 0.3 * cm))

    # Nombre de bénéficiaires et foyers
    nb_annuel = safe_paragraph_value(data.get("nbre_beneficiaires_annuel_previsionnel", ""))
    nb_trimestriel = safe_paragraph_value(data.get("nbre_beneficiaires_trimestriels_previsionnel", ""))
    nb_foyers = safe_paragraph_value(data.get("nbre_foyers", ""))

    elements.append(Paragraph(f"❖ Nombre de bénéficiaires annuel (prévisionnel) : {nb_annuel}", style_n_left))
    elements.append(Paragraph(f"❖ Nombre de bénéficiaires trimestriel (prévisionnel) : {nb_trimestriel}", style_n_left))
    elements.append(Paragraph(f"❖ Nombre de foyers : {nb_foyers}", style_n_left))
    elements.append(Spacer(1, 0.5 * cm))

    # Date et signature
    def fmt_date(value):
        value = safe_paragraph_value(value)
        if not value:
            return "—"
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return value

    date_creation_fmt = fmt_date(data.get("date_creation"))
    date_signature_fmt = fmt_date(data.get("date_signature"))

    elements.append(Spacer(5, 0.5 * cm))

    # Le pavé de signature lui-même est positionné par LibreSign via des
    # coordonnées passées à l'API (cf. utils_libresign.py), pas par une ancre
    # texte embarquée dans le PDF (contrairement à l'ancien système Yousign,
    # abandonné le 2026-07-16 — voir mémoire ba38_annexe1bis_migration). Le
    # tableau ci-dessous ne contient donc plus que du texte visible normal.
    table_signature = Table(
        [[
            Paragraph(f"Date de création : {date_creation_fmt}", style_n_left),
            Paragraph(f"Date de signature : {date_signature_fmt}", style_n_left),
        ],
        [
            Paragraph("Signature responsable association :", style_n_left),
            Paragraph("", style_n_left),
        ]],
        colWidths=[8.5 * cm, 8.5 * cm],
        rowHeights=[0.7 * cm, 1.8 * cm]
    )
    table_signature.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(table_signature)


    # --- Pied de page ---
    def footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        date_du_jour = datetime.today().strftime('%d/%m/%Y')
        canvas.drawString(1.5 * cm, 1 * cm, "Banque Alimentaire de l'Isère - Service Partenariat")
        canvas.drawRightString(A4[0] - 1.5 * cm, 1 * cm, f"{date_du_jour} - Page {doc.page}")
        canvas.restoreState()

    doc.build(
        elements,
        onFirstPage=lambda c, d: header_footer(c, d, title, subtitle),
        onLaterPages=lambda c, d: header_footer(c, d, title, subtitle)
    )
    return buffer.getvalue()
