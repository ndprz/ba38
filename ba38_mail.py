# ba38_mail.py
import sqlite3
from urllib.parse import urlencode
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from utils import get_db_connection, is_valid_email, write_log

mail_bp = Blueprint("mail", __name__)

def render_message_with_data(template, data):
    """
    Remplace les {{champs}} par les valeurs présentes dans 'data' (sqlite3.Row ou dict).
    Exemple : "Bonjour {{nom_association}}" -> "Bonjour ACME".
    """
    if not template:
        return ""
    text = str(template)
    # Remplacement naïf, clé à clé
    for key in data.keys():
        placeholder = "{{" + key + "}}"
        value = "" if data[key] is None else str(data[key])
        text = text.replace(placeholder, value)
    return text

@mail_bp.route("/envoi_mail", methods=["GET", "POST"])
@login_required
def envoi_mail():
    assoc_id = request.args.get("assoc_id", type=int)
    if not assoc_id:
        flash("❌ Association introuvable", "danger")
        return redirect(url_for("partenaires.partenaires"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Champs email du groupe "Responsables et Statuts"
    champs_email = [c["field_name"] for c in cur.execute("""
        SELECT field_name
        FROM field_groups
        WHERE group_name = 'Responsables et Statuts'
        AND type_champ = 'email'
        ORDER BY display_order
    """).fetchall()]

    # ➕ Ajouter toujours courriel_association en tête
    final_champs_email = ["courriel_association"] + [c for c in champs_email if c != "courriel_association"]

    # Association (clé 'Id' majuscule dans ton schéma)
    a = cur.execute("SELECT * FROM associations WHERE Id = ?", (assoc_id,)).fetchone()
    if not a:
        flash("❌ Association introuvable", "danger")
        return redirect(url_for("partenaires.partenaires"))

    # Destinataires
    destinataires = []
    for champ in final_champs_email:
        mail = a[champ] if champ in a.keys() else None
        if mail:
            destinataires.append({
                "assoc_id": a["Id"],
                "assoc_nom": a["nom_association"],
                "fonction": champ.replace("courriel_", "").replace("_", " ").capitalize(),
                "email": mail
            })

    # Messages préenregistrés (création table si besoin)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages_predefinis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titre TEXT NOT NULL,
            contenu TEXT NOT NULL
        )
    """)
    conn.commit()
    messages = cur.execute("SELECT * FROM messages_predefinis ORDER BY id DESC").fetchall()

    retour_url = url_for("partenaires.update_partner", partner_id=assoc_id)

    # POST : construire l'URL Gmail
    if request.method == "POST":
        choisis = request.form.getlist("destinataires")
        sujet = (request.form.get("sujet") or "").strip()
        message = (request.form.get("message") or "").strip()
        modele_id = request.form.get("modele")

        # Si un modèle est choisi, on l'utilise comme base du message
        if modele_id:
            row = cur.execute("SELECT titre, contenu FROM messages_predefinis WHERE id = ?", (modele_id,)).fetchone()
            if row:
                # Le modèle (contenu) est rendu avec les valeurs de l'association
                message = render_message_with_data(row["contenu"], a)
                # Si le sujet est vide, on met le titre du modèle (également rendu)
                if not sujet:
                    sujet = render_message_with_data(row["titre"], a)

        # Dans tous les cas, si l'utilisateur a tapé des {{champs}} dans le sujet/message,
        # on les remplace aussi (idempotent si déjà substitué).
        sujet = render_message_with_data(sujet, a)
        message = render_message_with_data(message, a)

        mails = [d["email"] for d in destinataires if d["email"] in choisis]

        if not mails or not sujet or not message:
            flash("❌ Merci de renseigner destinataires, sujet et message.", "danger")
            return redirect(url_for("mail.envoi_mail", assoc_id=assoc_id))

        # Encodage correct des paramètres pour Gmail
        params = urlencode({
            "view": "cm",
            "fs": "1",
            "to": ",".join(mails),
            "su": sujet,
            "body": message
        })
        gmail_url = f"https://mail.google.com/mail/?{params}"

        write_log(f"📧 Gmail URL générée pour assoc {assoc_id} → to={mails}, su='{sujet}', len(body)={len(message)}")

        return render_template(
            "envoi_mail.html",
            destinataires=destinataires,
            messages=messages,
            gmail_url=gmail_url,
            retour_url=retour_url,
            assoc_id=assoc_id  # ➕ pour le lien vers messages_predefinis
        )

    conn.close()
    return render_template(
        "envoi_mail.html",
        destinataires=destinataires,
        messages=messages,
        retour_url=retour_url,
        assoc_id=assoc_id  # ➕ pour le lien vers messages_predefinis
    )


# ➕ CRUD messages préenregistrés
@mail_bp.route("/messages_predefinis", methods=["GET", "POST"])
@login_required
def messages_predefinis():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages_predefinis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titre TEXT NOT NULL,
            contenu TEXT NOT NULL
        )
    """)
    conn.commit()

    # On garde assoc_id si on vient de /envoi_mail
    assoc_id = request.args.get("assoc_id")

    if request.method == "POST":
        titre = (request.form.get("titre") or "").strip()
        contenu = (request.form.get("contenu") or "").strip()
        assoc_id_from_form = request.form.get("assoc_id") or assoc_id
        if titre and contenu:
            cur.execute("INSERT INTO messages_predefinis (titre, contenu) VALUES (?, ?)", (titre, contenu))
            conn.commit()
            flash("✅ Message enregistré", "success")
        else:
            flash("❌ Merci de remplir tous les champs", "danger")
        # On conserve assoc_id dans l'URL pour garder le bouton retour
        if assoc_id_from_form:
            return redirect(url_for("mail.messages_predefinis", assoc_id=assoc_id_from_form))
        return redirect(url_for("mail.messages_predefinis"))

    messages = cur.execute("SELECT * FROM messages_predefinis ORDER BY id DESC").fetchall()
    conn.close()

    return render_template("messages_predefinis.html", messages=messages, assoc_id=assoc_id)


@mail_bp.route("/messages_predefinis/delete/<int:mid>", methods=["POST"])
@login_required
def delete_message(mid):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages_predefinis WHERE id = ?", (mid,))
    conn.commit()
    conn.close()
    flash("🗑️ Message supprimé", "warning")

    assoc_id = request.args.get("assoc_id")
    if assoc_id:
        return redirect(url_for("mail.messages_predefinis", assoc_id=assoc_id))
    return redirect(url_for("mail.messages_predefinis"))


@mail_bp.route("/messages_predefinis/edit/<int:mid>", methods=["GET", "POST"])
@login_required
def edit_message(mid):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    assoc_id = request.args.get("assoc_id") or request.form.get("assoc_id")
    message = cur.execute("SELECT * FROM messages_predefinis WHERE id = ?", (mid,)).fetchone()
    if not message:
        flash("❌ Message introuvable", "danger")
        if assoc_id:
            return redirect(url_for("mail.messages_predefinis", assoc_id=assoc_id))
        return redirect(url_for("mail.messages_predefinis"))

    if request.method == "POST":
        titre = (request.form.get("titre") or "").strip()
        contenu = (request.form.get("contenu") or "").strip()
        if titre and contenu:
            cur.execute("UPDATE messages_predefinis SET titre=?, contenu=? WHERE id=?", (titre, contenu, mid))
            conn.commit()
            flash("✅ Message modifié", "success")
            conn.close()
            if assoc_id:
                return redirect(url_for("mail.messages_predefinis", assoc_id=assoc_id))
            return redirect(url_for("mail.messages_predefinis"))
        else:
            flash("❌ Merci de remplir tous les champs", "danger")

    conn.close()
    # (Si tu as un template 'edit_message.html', pense à y ajouter un bouton retour aussi)
    return render_template("edit_message.html", message=message)
