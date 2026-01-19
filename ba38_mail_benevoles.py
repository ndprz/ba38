# ba38_mail_benevoles.py
import sqlite3
from urllib.parse import quote
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from utils import get_db_connection, upload_database, write_log
from urllib.parse import urlencode, quote_plus, quote
from flask_login import current_user

user_email = getattr(current_user, "email", None) or ""



mail_bene_bp = Blueprint("mail_bene", __name__, template_folder="templates")


def _charger_messages():
    """Retourne la liste des messages pré-enregistrés (id, titre, contenu)."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT id, titre, contenu
        FROM messages_predefinis
        ORDER BY id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_fonction_fields():
    """
    Retourne la liste des champs du groupe 'Fonctions' (ou 'Fonction') pour les bénévoles.
    Utilise field_name comme libellé lisible.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT field_name
        FROM field_groups
        WHERE appli = 'benevoles' AND LOWER(group_name) LIKE '%fonction%'
        ORDER BY display_order
    """).fetchall()
    conn.close()

    # Crée un libellé à partir du nom du champ (remplace _ par espace, majuscule initiale)
    return [(r[0], r[0].replace("_", " ").capitalize()) for r in rows]


def _charger_benevoles(fonctions=None, bene_id=None):
    """
    Charge la liste des bénévoles selon filtres :
      - bene_id : un seul bénévole
      - fonctions : liste de champs (ex: ['ramasse_chauffeur','ramasse_equipier'])
    """
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    base_query = "SELECT id, nom, prenom, email FROM benevoles WHERE email IS NOT NULL AND TRIM(email) != ''"
    params = []

    if bene_id:
        base_query += " AND id = ?"
        params.append(bene_id)
    elif fonctions:
        clauses = [f"{f}=?" for f in fonctions]
        conditions = " OR ".join(clauses)
        base_query += f" AND ({conditions})"
        params += ["oui"] * len(fonctions)

    base_query += " ORDER BY nom COLLATE NOCASE"
    rows = cur.execute(base_query, params).fetchall()
    conn.close()

    return [dict(r) for r in rows]




def _build_gmail_url(to_emails, sujet, corps):
    """
    Version 100 % compatible Gmail : ouvre la fenêtre de composition avec les bons destinataires.
    """
    base = "https://mail.google.com/mail/?view=cm&fs=1&tf=1"

    # Gmail attend les emails séparés par virgule sans encodage particulier
    to_part = "&to=" + ",".join(to_emails)

    # On encode le sujet et le corps, mais PAS les virgules du champ "to"
    su_part = "&su=" + quote(sujet or "", safe="")
    body_part = "&body=" + quote(corps or "", safe="")

    return f"{base}{to_part}{su_part}{body_part}"



@mail_bene_bp.route("/envoi_mail_benevoles", methods=["GET", "POST"])
@login_required
def envoi_mail_benevoles():
    from flask_login import current_user
    from flask import flash

    bene_id = request.args.get("bene_id", type=int)
    retour_url = request.args.get("retour_url")

    # ✅ On récupère d'abord l'email de l'utilisateur connecté
    user_email = getattr(current_user, "email", "") or ""
    write_log(f"[DEBUG USER] id={current_user.id}, email={user_email}")

    if not user_email:
        flash("⚠️ Votre compte n’a pas d’adresse email enregistrée. Le champ 'À :' sera vide.", "warning")

    # 🟢 On récupère les fonctions cochées depuis GET ou POST
    selected_fonctions = request.values.getlist("fonctions")
    all_fonctions = _get_fonction_fields()
    messages = _charger_messages()

    gmail_url = None

    # 🟢 Si bouton d'envoi appuyé
    if request.method == "POST" and request.form.get("action") == "envoyer":
        # 🔹 Récupération fiable des destinataires
        to_list = request.form.getlist("destinataires")

        # 🔹 Si vide, tenter la lecture du champ caché
        if not to_list and request.form.get("_dest_list"):
            to_list = [email.strip() for email in request.form["_dest_list"].split(",") if email.strip()]

        sujet = (request.form.get("sujet") or "").strip()
        message = (request.form.get("message") or "")

        write_log(f"[MAIL BENEVOLES] Destinataires finals : {to_list}")

        if not to_list:
            flash("❌ Merci de sélectionner au moins un destinataire.", "danger")
        elif not sujet:
            flash("❌ Le sujet est obligatoire.", "danger")
        else:
            # Nettoyage to_list (parfois des doublons ou espaces)
            to_list = [t.strip() for t in to_list if t.strip()]
            write_log(f"[MAIL BENEVOLES] Destinataires finals : {to_list}")
            gmail_url = _build_gmail_url(to_list, sujet, message)
            flash("✅ Message prêt à être envoyé via Gmail.", "success")

    # 🟢 Sinon : affichage initial ou filtrage par fonctions
    destinataires = _charger_benevoles(fonctions=selected_fonctions, bene_id=bene_id)

    return render_template(
        "envoi_mail_benevoles.html",
        all_fonctions=all_fonctions,
        selected_fonctions=selected_fonctions,
        destinataires=destinataires,
        messages=messages,
        gmail_url=gmail_url,
        retour_url=retour_url,
        user_email=user_email  # ✅ maintenant bien défini
    )


@mail_bene_bp.route("/messages_predefinis_benevoles", methods=["GET", "POST"])
@login_required
def messages_predefinis_benevoles():
    """
    Gestion des modèles de message (communs). Identique aux associations mais avec
    retour vers la page bénévoles.
    """
    if request.method == "POST":
        titre = (request.form.get("titre") or "").strip()
        contenu = (request.form.get("contenu") or "").rstrip()
        if not titre or not contenu:
            flash("❌ Merci de renseigner un titre et un contenu.", "danger")
            return redirect(url_for("mail_bene.messages_predefinis_benevoles"))

        conn = get_db_connection()
        conn.execute(
            "INSERT INTO messages_predefinis (titre, contenu) VALUES (?, ?)",
            (titre, contenu)
        )
        conn.commit()
        conn.close()
        upload_database()
        flash("✅ Modèle ajouté.", "success")
        return redirect(url_for("mail_bene.messages_predefinis_benevoles"))

    messages = _charger_messages()
    return render_template("messages_predefinis_benevoles.html", messages=messages)


@mail_bene_bp.route("/edit_message_bene/<int:mid>", methods=["GET", "POST"])
@login_required
def edit_message_bene(mid):
    """Édition d’un modèle bénévole (titre + contenu)."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    if request.method == "POST":
        titre = (request.form.get("titre") or "").strip()
        contenu = (request.form.get("contenu") or "").rstrip()
        if not titre or not contenu:
            conn.close()
            flash("❌ Merci de renseigner un titre et un contenu.", "danger")
            return redirect(url_for("mail_bene.edit_message_bene", mid=mid))

        conn.execute(
            "UPDATE messages_predefinis SET titre = ?, contenu = ? WHERE id = ?",
            (titre, contenu, mid)
        )
        conn.commit()
        conn.close()
        upload_database()
        flash("✅ Modèle mis à jour.", "success")
        return redirect(url_for("mail_bene.messages_predefinis_benevoles"))

    row = conn.execute(
        "SELECT id, titre, contenu FROM messages_predefinis WHERE id = ?",
        (mid,)
    ).fetchone()
    conn.close()

    if not row:
        flash("❌ Modèle introuvable.", "danger")
        return redirect(url_for("mail_bene.messages_predefinis_benevoles"))

    return render_template(
        "messages_predefinis_benevoles.html",
        messages=[dict(row)],  # on affiche juste celui-ci en haut, réutilisation simple
        edit_mode=True,
        edit_id=row["id"],
        edit_titre=row["titre"],
        edit_contenu=row["contenu"]
    )


@mail_bene_bp.route("/delete_message_bene/<int:mid>", methods=["POST"])
@login_required
def delete_message_bene(mid):
    """Suppression d’un modèle bénévole."""
    conn = get_db_connection()
    conn.execute("DELETE FROM messages_predefinis WHERE id = ?", (mid,))
    conn.commit()
    conn.close()
    upload_database()
    flash("🗑️ Modèle supprimé.", "warning")
    return redirect(url_for("mail_bene.messages_predefinis_benevoles"))
