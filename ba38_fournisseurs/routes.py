from flask import Blueprint, render_template, request, redirect, flash, url_for
from flask_login import current_user, login_required

import sqlite3
import pytz
import base64
from datetime import datetime
from ba38_utilitaires.core import get_db_path, get_db_connection, upload_database, has_access, write_log, is_valid_email, is_valid_phone, row_get, require_access
from ba38_utilitaires.organisation import get_organisation


fournisseurs_bp = Blueprint('fournisseurs', __name__)

# Colonnes à afficher (on exclut lundi/mardi/mercredi/jeudi + horaires)
COLUMNS = [
    ("id", "ID"),
    ("nom", "Nom"),
    ("enseigne", "Enseigne"),
    ("societe", "Société"),
    ("Type_frs", "Type"),
    ("tel_mobile", "Mobile"),
    ("tel", "Téléphone"),
    ("mail", "Email"),
    ("adresse", "Adresse 1"),
    ("adresse2", "Adresse 2"),
    ("cp", "CP"),
    ("ville", "Ville"),
    ("notes", "Notes"),
    ("actif", "Actif"),
    ("date_creation", "Créé le"),
    ("date_modif", "Modifié le"),
    ("user_modif", "Par"),
]

def _connect():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn

def beautify_title(name):

    return (
        name
        .replace("_", " ")
        .replace("-", " ")
        .strip()
        .title()
    )


@fournisseurs_bp.route('/fournisseurs')
@login_required
@require_access("fournisseurs", "lecture")
def liste_fournisseurs():

    lecture_seule = not has_access("fournisseurs", "ecriture")

    with _connect() as conn:

        fields = conn.execute("""
            SELECT *
            FROM field_groups
            WHERE appli = 'fournisseurs'
            ORDER BY
                CASE
                    WHEN LOWER(group_name) = 'coordonnees principales'
                    THEN 0
                    ELSE 1
                END,
                group_name COLLATE NOCASE,
                display_order
        """).fetchall()

        rows = conn.execute("""
            SELECT *
            FROM fournisseurs
            ORDER BY nom COLLATE NOCASE
        """).fetchall()

    # ============================================================
    # GROUPES (pour le panneau "Options d'affichage")
    # ============================================================

    grouped_fields = {}

    for field in fields:

        field_name = field["field_name"]

        if field_name == "nom":
            continue

        group_name = field["group_name"] or "Autres"

        grouped_fields.setdefault(group_name, []).append({
            "field_name": field_name
        })

    table_data = [dict(row) for row in rows]

    # ============================================================
    # COLONNES TABULATOR
    # ============================================================

    LISTE_FIELDS = {"type_frs", "enseigne", "famille_fournisseur"}

    columns = [

        {
            "title": "ID",
            "field": "id",
            "minWidth": 70,
            "widthGrow": 0,
            "frozen": True
        },

        {
            "title": "Nom",
            "field": "nom",
            "tooltip": True,
            "minWidth": 220,
            "widthGrow": 2,
            "frozen": True
        }
    ]

    for field in fields:

        field_name = field["field_name"]

        if field_name == "nom":
            continue

        type_champ = (field["type_champ"] or "").lower()

        header_filter = "input"

        header_filter_params = {
            "placeholder": "Filtrer..."
        }

        min_width = 130
        width_grow = 1
        hoz_align = "left"

        if type_champ == "oui_non":

            header_filter = "list"

            header_filter_params = {
                "values": {"": "Tous", "oui": "Oui", "non": "Non"},
                "clearable": True
            }

            min_width = 100
            width_grow = 0
            hoz_align = "center"

        elif type_champ == "liste" or field_name in LISTE_FIELDS:

            header_filter = "list"

            header_filter_params = {
                "valuesLookup": True,
                "clearable": True,
                "autocomplete": True
            }

            min_width = 160

        elif "mail" in field_name:
            min_width = 240
            width_grow = 2

        elif "tel" in field_name:
            min_width = 140

        elif "date" in field_name:
            min_width = 130
            hoz_align = "center"

        elif field_name in ("adresse", "adresse2", "notes"):
            min_width = 260
            width_grow = 2

        col = {
            "title": beautify_title(field_name),
            "field": field_name,
            "tooltip": True,
            "headerTooltip": field_name,
            "minWidth": min_width,
            "widthGrow": width_grow,
            "hozAlign": hoz_align,
            "sorter": "string",
            "headerFilter": header_filter,
            "headerFilterParams": header_filter_params,
        }

        if field_name == "drive_link":
            col["formatter"] = "driveFormatter"
            col["headerSort"] = False

        if "mail" in field_name:
            col["formatter"] = "emailFormatter"

        columns.append(col)

    return render_template(
        "fournisseurs/fournisseurs.html",
        table_data=table_data,
        columns=columns,
        grouped_fields=grouped_fields,
        lecture_seule=lecture_seule
    )


@fournisseurs_bp.route('/fournisseurs/<int:fournisseur_id>/update', methods=['GET', 'POST'])
@login_required
@require_access("fournisseurs", "lecture")
def update_fournisseur(fournisseur_id):
    """
    Page de mise à jour d’un fournisseur.

    ⚙️ Fonctionnalités :
    - Affiche les champs dynamiques configurés dans field_groups (appli=fournisseurs).
    - Valide et met à jour uniquement si des modifications sont détectées (via form_hash).
    - Affiche date/heure de dernière modification en heure française + utilisateur modificateur.
    - Navigation Suivant / Précédent / Retour : sauvegarde (si do_upload=1) puis redirige.
    """

    lecture_seule = not has_access("fournisseurs", "ecriture")
    conn = get_db_connection()
    cursor = conn.cursor()

    # 🔍 Récupérer le fournisseur
    fournisseur = cursor.execute("SELECT * FROM fournisseurs WHERE id = ?", (fournisseur_id,)).fetchone()
    if not fournisseur:
        conn.close()
        flash("⛔ Fournisseur introuvable", "danger")
        return redirect(url_for("fournisseurs.liste_fournisseurs"))

    fournisseur_dict = dict(fournisseur)

    # 🔢 Champs dynamiques
    fields = cursor.execute("""
        SELECT * FROM field_groups
        WHERE appli = 'fournisseurs'
        ORDER BY display_order
    """).fetchall()

    fields_data = []
    for row in fields:
        field = dict(row)
        fname = field["field_name"]
        field["value"] = fournisseur_dict.get(fname)
        fields_data.append(field)

    # 📦 Regroupement
    grouped_fields = {}
    for field in fields_data:
        group = field["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(field)

    # 🔁 Voisins alphabétiques
    # Navigation alphabétique insensible à la casse
    previous_id = cursor.execute(
        "SELECT id FROM fournisseurs WHERE LOWER(nom) < LOWER(?) ORDER BY LOWER(nom) DESC LIMIT 1",
        (fournisseur_dict["nom"],)
    ).fetchone()

    next_id = cursor.execute(
        "SELECT id FROM fournisseurs WHERE LOWER(nom) > LOWER(?) ORDER BY LOWER(nom) ASC LIMIT 1",
        (fournisseur_dict["nom"],)
    ).fetchone()

    # ⏰ Conversion de la date/heure au format FR
    date_modif = fournisseur_dict.get("date_modif")
    heure_fr = None
    if date_modif:
        try:
            dt = datetime.strptime(date_modif, "%Y-%m-%d %H:%M:%S")
            import pytz
            dt = pytz.utc.localize(dt).astimezone(pytz.timezone("Europe/Paris"))
            heure_fr = dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            heure_fr = date_modif

    # 📝 Gestion POST
    if request.method == "POST":
        go_to = request.form.get("go_to")
        do_upload = request.form.get("do_upload", "1")

        if lecture_seule and do_upload == "1":
            conn.close()
            flash("⛔ Vous n’avez pas les droits pour modifier ce fournisseur.", "danger")
            return redirect(url_for("fournisseurs.update_fournisseur", fournisseur_id=fournisseur_id))

        updates = {}
        for field in fields_data:
            fname = field["field_name"]
            if fname == "id":
                continue
            val = request.form.get(fname, "").strip()
            if fname == "nom" and not val:
                # ⚠️ empêcher d’écraser le nom par vide
                val = fournisseur_dict.get("nom")
            updates[fname] = None if val == "" else val

        # 🔒 Hash pour détecter les changements
        inputs_for_hash = [
            f"{f['field_name']}:{request.form.get(f['field_name'], '').strip()}"
            for f in fields_data if f["field_name"] != "id"
        ]
        computed_hash = base64.b64encode("|#|".join(inputs_for_hash).encode("utf-8")).decode("utf-8")
        received_hash = request.form.get("form_hash", "")

        if do_upload == "1":
            if computed_hash != received_hash:
                now = datetime.utcnow()
                updates["date_modif"] = now.strftime("%Y-%m-%d %H:%M:%S")
                updates["user_modif"] = current_user.username

                if updates:
                    set_clause = ", ".join([f"{k} = ?" for k in updates])
                    values = list(updates.values()) + [fournisseur_id]
                    try:
                        cursor.execute(f"UPDATE fournisseurs SET {set_clause} WHERE id = ?", values)
                        conn.commit()
                        upload_database()
                        flash("✅ Fournisseur mis à jour avec succès.", "success")
                    except Exception as e:
                        flash(f"❌ Erreur lors de la mise à jour : {e}", "danger")
            else:
                flash("ℹ️ Aucune modification détectée. Rien n’a été mis à jour.", "info")

        conn.close()
        if go_to:
            return redirect(go_to)
        else:
            return redirect(url_for("fournisseurs.update_fournisseur", fournisseur_id=fournisseur_id))

    # 🧮 Calcul du form_hash initial (GET)
    inputs_for_hash = [
        f"{field['field_name']}:{field['value'] or ''}" for field in fields_data if field['field_name'] != "id"
    ]
    form_hash = base64.b64encode("|#|".join(inputs_for_hash).encode("utf-8")).decode("utf-8")

    # 📋 Charger paramètres (type_frs etc.)
    params = cursor.execute("SELECT param_name, param_value FROM parametres").fetchall()
    param_dict = {}
    for row in params:
        param_dict.setdefault(row["param_name"], []).append(row["param_value"])

    conn.close()

    # ✅ Affichage du template
    return render_template(
        "fournisseurs/update_fournisseur.html",
        fournisseur=fournisseur_dict,
        grouped_fields=grouped_fields,
        fournisseur_id=fournisseur_id,
        nom_fournisseur=fournisseur_dict["nom"],
        previous_id=previous_id["id"] if previous_id else None,
        next_id=next_id["id"] if next_id else None,
        date_modif=heure_fr,
        user_modif=fournisseur_dict.get("user_modif", ""),
        lecture_seule=lecture_seule,
        next_url=request.query_string.decode("utf-8") or "",
        parametres=param_dict,
        form_hash=form_hash
    )

@fournisseurs_bp.route('/fournisseurs/create', methods=['GET', 'POST'])
@login_required
@require_access("fournisseurs", "ecriture")
def create_fournisseur():
    """Création d’un fournisseur avec enseigne et type_frs liés aux paramètres."""

    conn = get_db_connection()
    cursor = conn.cursor()

    # Charger les paramètres
    params = cursor.execute(
        "SELECT param_name, param_value FROM parametres"
    ).fetchall()

    param_dict = {}
    for row in params:
        param_dict.setdefault(row["param_name"], []).append(row["param_value"])

    if request.method == "POST":

        nom = request.form.get("nom", "").strip()
        if not nom:
            flash("⚠️ Le nom du fournisseur est obligatoire.", "danger")
            conn.close()
            return render_template(
                "fournisseurs/create_fournisseur.html",
                parametres=param_dict
            )

        enseigne = request.form.get("enseigne", "").strip()
        type_frs = request.form.get("type_frs", "").strip()
        famille_fournisseur = request.form.get("famille_fournisseur", "").strip()
        tel = request.form.get("tel", "").strip()
        mail = request.form.get("mail", "").strip()
        adresse = request.form.get("adresse", "").strip()
        ville = request.form.get("ville", "").strip()
        notes = request.form.get("notes", "").strip()

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        try:
            cursor.execute("""
                INSERT INTO fournisseurs
                (nom, enseigne, type_frs, famille_fournisseur, tel, mail, adresse, ville, notes,
                 date_creation, date_modif, user_modif)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                nom, enseigne, type_frs, famille_fournisseur, tel, mail,
                adresse, ville, notes,
                now, now,
                current_user.username or current_user.email
            ))

            conn.commit()
            upload_database()
            flash("✅ Fournisseur créé avec succès.", "success")

            return redirect(url_for("fournisseurs.liste_fournisseurs"))

        except Exception as e:
            conn.rollback()
            write_log(f"❌ Erreur création fournisseur : {e}")
            flash("Erreur lors de la création.", "danger")

            return render_template(
                "fournisseurs/create_fournisseur.html",
                parametres=param_dict
            )

        finally:
            conn.close()

    conn.close()
    return render_template(
        "fournisseurs/create_fournisseur.html",
        parametres=param_dict
    )


@fournisseurs_bp.route('/fournisseurs/<int:fournisseur_id>/delete', methods=['POST'])
@login_required
@require_access("fournisseurs", "ecriture")
def delete_fournisseur(fournisseur_id):

    confirm = request.form.get("confirm")
    confirm_final = request.form.get("confirm_final")

    if confirm == "oui" and confirm_final == "supprimer":
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("DELETE FROM fournisseurs WHERE id = ?", (fournisseur_id,))
        conn.commit()
        flash("✅ Fournisseur supprimé avec succès", "success")
        return redirect(url_for("fournisseurs.liste_fournisseurs"))
    else:
        flash("❌ Suppression annulée ou non confirmée", "warning")
        return redirect(url_for("fournisseurs.update_fournisseur", fournisseur_id=fournisseur_id))


# === 📌 Gestion des contacts fournisseurs ===

@fournisseurs_bp.route("/fournisseurs/<int:fournisseur_id>/contacts")
@login_required
@require_access("fournisseurs", "lecture")
def liste_contacts_fournisseur(fournisseur_id):
    """Affiche tous les contacts liés à un fournisseur."""
    conn = get_db_connection()
    cursor = conn.cursor()

    fournisseur = cursor.execute("SELECT * FROM fournisseurs WHERE id = ?", (fournisseur_id,)).fetchone()
    if not fournisseur:
        conn.close()
        flash("⛔ Fournisseur introuvable", "danger")
        return redirect(url_for("fournisseurs.liste_fournisseurs"))

    contacts = cursor.execute(
        "SELECT * FROM fournisseurs_contacts WHERE fournisseur_id = ? ORDER BY nom, prenom",
        (fournisseur_id,)
    ).fetchall()
    conn.close()

    return render_template(
        "fournisseurs/contacts_fournisseur.html",
        fournisseur=fournisseur,
        contacts=contacts,
        lecture_seule=not has_access("fournisseurs", "ecriture")
    )


@fournisseurs_bp.route('/fournisseurs/<int:fournisseur_id>/contacts/create', methods=['GET', 'POST'])
@login_required
@require_access("fournisseurs", "ecriture")
def create_contact_fournisseur(fournisseur_id):

    conn = get_db_connection()
    cursor = conn.cursor()

    # 🔍 Charger le fournisseur (pour affichage du nom)
    fournisseur = cursor.execute("SELECT * FROM fournisseurs WHERE id = ?", (fournisseur_id,)).fetchone()
    if not fournisseur:
        conn.close()
        flash("⛔ Fournisseur introuvable", "danger")
        return redirect(url_for("fournisseurs.liste_fournisseurs"))

    if request.method == "POST":
        prenom = request.form.get("prenom")
        nom = request.form.get("nom")
        fonction = request.form.get("fonction")
        tel_mobile = request.form.get("tel_mobile")
        tel_fixe = request.form.get("tel_fixe")
        email = request.form.get("email")
        adresse1 = request.form.get("adresse1")
        cp = request.form.get("cp")
        ville = request.form.get("ville")
        notes = request.form.get("notes")

        # 🔒 Valeurs sûres pour respecter les CHECK constraints
        est_referent = request.form.get("est_referent")
        if est_referent not in ("oui", "non"):
            est_referent = "non"

        actif = request.form.get("actif")
        if actif not in ("oui", "non"):
            actif = "oui"

        cursor.execute("""
            INSERT INTO fournisseurs_contacts
            (fournisseur_id, prenom, nom, fonction, tel_mobile, tel_fixe, email,
             adresse1, cp, ville, est_referent, actif, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (fournisseur_id, prenom, nom, fonction, tel_mobile, tel_fixe, email,
              adresse1, cp, ville, est_referent, actif, notes))
        conn.commit()
        conn.close()

        flash("✅ Contact fournisseur ajouté avec succès.", "success")
        return redirect(url_for("fournisseurs.liste_contacts_fournisseur", fournisseur_id=fournisseur_id))

    conn.close()
    return render_template(
        "fournisseurs/create_contact_fournisseur.html",
        fournisseur_id=fournisseur_id,
        fournisseur=fournisseur
    )

@fournisseurs_bp.route('/fournisseurs/<int:fournisseur_id>/contacts/<int:contact_id>/update', methods=['GET', 'POST'])
@login_required
@require_access("fournisseurs", "ecriture")
def update_contact_fournisseur(fournisseur_id, contact_id):

    conn = get_db_connection()
    cursor = conn.cursor()

    # 🔍 Charger le fournisseur pour affichage
    fournisseur = cursor.execute("SELECT * FROM fournisseurs WHERE id = ?", (fournisseur_id,)).fetchone()
    if not fournisseur:
        conn.close()
        flash("⛔ Fournisseur introuvable", "danger")
        return redirect(url_for("fournisseurs.liste_fournisseurs"))

    # 🔍 Charger le contact
    contact = cursor.execute(
        "SELECT * FROM fournisseurs_contacts WHERE id = ? AND fournisseur_id = ?",
        (contact_id, fournisseur_id)
    ).fetchone()
    if not contact:
        conn.close()
        flash("⛔ Contact introuvable", "danger")
        return redirect(url_for("fournisseurs.liste_contacts_fournisseur", fournisseur_id=fournisseur_id))

    if request.method == "POST":
        prenom = request.form.get("prenom")
        nom = request.form.get("nom")
        fonction = request.form.get("fonction")
        tel_mobile = request.form.get("tel_mobile")
        tel_fixe = request.form.get("tel_fixe")
        email = request.form.get("email")
        adresse1 = request.form.get("adresse1")
        cp = request.form.get("cp")
        ville = request.form.get("ville")
        est_referent = request.form.get("est_referent", "non")
        actif = request.form.get("actif", "oui")
        notes = request.form.get("notes")

        cursor.execute("""
            UPDATE fournisseurs_contacts
            SET prenom=?, nom=?, fonction=?, tel_mobile=?, tel_fixe=?, email=?,
                adresse1=?, cp=?, ville=?, est_referent=?, actif=?, notes=?
            WHERE id=? AND fournisseur_id=?
        """, (prenom, nom, fonction, tel_mobile, tel_fixe, email,
              adresse1, cp, ville, est_referent, actif, notes,
              contact_id, fournisseur_id))
        conn.commit()
        conn.close()

        flash("✅ Contact fournisseur mis à jour avec succès.", "success")
        return redirect(url_for("fournisseurs.liste_contacts_fournisseur", fournisseur_id=fournisseur_id))

    conn.close()
    return render_template(
        "fournisseurs/update_contact_fournisseur.html",
        contact=contact,
        fournisseur_id=fournisseur_id,
        fournisseur=fournisseur  # ✅ ajouté
    )


@fournisseurs_bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
@login_required
@require_access("fournisseurs", "ecriture")
def delete_contact_fournisseur(contact_id):
    """Supprime un contact fournisseur."""
    conn = get_db_connection()
    cursor = conn.cursor()
    contact = cursor.execute("SELECT * FROM fournisseurs_contacts WHERE id = ?", (contact_id,)).fetchone()
    if not contact:
        conn.close()
        flash("⛔ Contact introuvable.", "danger")
        return redirect(url_for("fournisseurs.liste_fournisseurs"))

    fournisseur_id = contact["fournisseur_id"]
    cursor.execute("DELETE FROM fournisseurs_contacts WHERE id = ?", (contact_id,))
    conn.commit()
    conn.close()
    flash("🗑️ Contact supprimé avec succès.", "success")
    upload_database()
    return redirect(url_for("fournisseurs.liste_contacts_fournisseur", fournisseur_id=fournisseur_id))


@fournisseurs_bp.route("/fournisseurs/export_excel")
@login_required
@require_access("fournisseurs", "lecture")
def export_fournisseurs_excel():
    """
    Exporte les fournisseurs + leurs contacts dans un fichier Excel multi-onglets.
    """
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    fournisseurs = cursor.execute("SELECT * FROM fournisseurs").fetchall()
    contacts = cursor.execute("""
        SELECT c.*, f.nom AS fournisseur_nom
        FROM fournisseurs_contacts c
        LEFT JOIN fournisseurs f ON f.id = c.fournisseur_id
    """).fetchall()
    conn.close()

    # Convertir en DataFrame
    import pandas as pd
    df_fournisseurs = pd.DataFrame([dict(r) for r in fournisseurs])
    df_contacts = pd.DataFrame([dict(r) for r in contacts])

    # Création Excel en mémoire
    from io import BytesIO
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_fournisseurs.to_excel(writer, index=False, sheet_name="Fournisseurs")
        df_contacts.to_excel(writer, index=False, sheet_name="Contacts")

    output.seek(0)

    # Envoi du fichier
    from flask import send_file
    return send_file(
        output,
        as_attachment=True,
        download_name="fournisseurs_contacts.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


from flask import send_file, flash, redirect, url_for
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph
from reportlab.lib.units import cm
import sqlite3
from datetime import datetime

@fournisseurs_bp.route("/fournisseurs/<int:fournisseur_id>/export_fiche", methods=["GET"])
@login_required
@require_access("fournisseurs", "lecture")
def export_fiche_fournisseur(fournisseur_id):
    """
    Génère une fiche PDF pour un fournisseur :
    - Informations générales du fournisseur (y compris enseigne).
    - Notes et adresses sur plusieurs lignes si nécessaire.
    - Liste des contacts du fournisseur.
    """

    conn = get_db_connection()
    cursor = conn.cursor()

    fournisseur = cursor.execute(
        "SELECT * FROM fournisseurs WHERE id = ?", (fournisseur_id,)
    ).fetchone()
    if not fournisseur:
        conn.close()
        flash("⛔ Fournisseur introuvable", "danger")
        return redirect(url_for("fournisseurs.liste_fournisseurs"))

    fournisseur = dict(fournisseur)

    contacts = cursor.execute(
        "SELECT * FROM fournisseurs_contacts WHERE fournisseur_id = ?", (fournisseur_id,)
    ).fetchall()
    conn.close()

    org = get_organisation()

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # Styles pour Paragraph
    styles = getSampleStyleSheet()
    normal_style = styles["Normal"]

    # En-tête organisme
    c.setFont("Helvetica", 9)
    c.drawString(50, height - 30, org["nom"])

    # Titre
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, f"Fiche Fournisseur : {fournisseur.get('nom', '')}")

    # Informations générales
    c.setFont("Helvetica", 10)
    y = height - 100
    infos = [
        ("Enseigne", fournisseur.get("enseigne", "")),
        ("Société", fournisseur.get("societe", "")),
        ("Adresse", fournisseur.get("adresse", "")),
        ("Adresse 2", fournisseur.get("adresse2", "")),
        ("CP", fournisseur.get("cp", "")),
        ("Ville", fournisseur.get("ville", "")),
        ("Téléphone", fournisseur.get("tel", "")),
        ("Mobile", fournisseur.get("tel_mobile", "")),
        ("Email", fournisseur.get("mail", "")),
        ("Type", fournisseur.get("type_frs", "")),
        ("Code VIF", fournisseur.get("code_vif", "")),
        ("Ramasse", fournisseur.get("ramasse", "")),
        ("Actif", fournisseur.get("actif", "")),
        ("Notes", fournisseur.get("notes", "")),
    ]

    for label, value in infos:
        if not value:
            continue
        if label in ("Notes", "Adresse", "Adresse 2"):
            # ✅ Gestion multi-lignes
            para = Paragraph(f"<b>{label} :</b> {value.replace(chr(10), '<br/>')}", normal_style)
            w, h = para.wrap(width - 100, y)
            if y - h < 50:  # saut de page si plus de place
                c.showPage()
                y = height - 100
            para.drawOn(c, 50, y - h)
            y -= h + 10
        else:
            c.drawString(50, y, f"{label} : {value}")
            y -= 20
        if y < 50:  # sécurité saut de page
            c.showPage()
            y = height - 100

    # Séparateur
    c.line(50, y, width - 50, y)
    y -= 30

    # Contacts
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Contacts :")
    y -= 20
    c.setFont("Helvetica", 10)

    for contact in contacts:
        contact = dict(contact)
        lignes_contact = [
            f"{contact.get('prenom', '')} {contact.get('nom', '')} - {contact.get('fonction', '')}",
            f"Tél: {contact.get('tel_mobile', '') or contact.get('tel_fixe', '')} - Email: {contact.get('email', '')}",
            f"Adresse: {contact.get('adresse1', '')}, {contact.get('cp', '')} {contact.get('ville', '')}",
            f"Référent: {contact.get('est_referent', '')} | Actif: {contact.get('actif', '')}",
            f"Notes: {contact.get('notes', '')}" if contact.get("notes") else "",
        ]
        for line in lignes_contact:
            if not line.strip():
                continue
            para = Paragraph(line, normal_style)
            w, h = para.wrap(width - 100, y)
            if y - h < 50:
                c.showPage()
                y = height - 100
            para.drawOn(c, 60, y - h)
            y -= h + 5
        y -= 10

    c.showPage()
    c.save()
    buffer.seek(0)

    filename = f"Fiche_Fournisseur_{fournisseur.get('nom','').replace(' ', '_')}.pdf"
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype="application/pdf")
