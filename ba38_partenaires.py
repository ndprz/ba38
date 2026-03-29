import os
import sqlite3
import base64
import re
import unicodedata

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from utils import get_db_connection, upload_database, has_access, write_log, is_valid_email, is_valid_phone, require_access
from urllib.parse import urlencode
from flask_wtf import FlaskForm
from wtforms import HiddenField
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import simpleSplit
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak, Flowable
from reportlab.pdfgen import canvas
from reportlab.lib.enums import TA_LEFT
from datetime import datetime
from flask import send_file
from io import BytesIO


def header_footer(canvas, doc, title, nom_association):
    # --- EN-TÊTE ---
    canvas.saveState()

    # Logo
    logo_path = "static/images/logo.png"
    if os.path.exists(logo_path):
        canvas.drawImage(logo_path, x=40, y=A4[1] - 60, width=1.5*cm, height=1.5*cm)

    # Titre centré
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawCentredString(A4[0]/2, A4[1] - 40, title)

    # Nom de l'association
    if nom_association:
        canvas.setFont("Helvetica-Bold", 14)
        canvas.setFillColorRGB(0, 0, 1)  # Bleu
        canvas.drawCentredString(A4[0]/2, A4[1] - 55, nom_association)
        canvas.setFillColorRGB(0, 0, 0)  # Réinitialiser en noir


    # --- PIED DE PAGE ---
    page_num = canvas.getPageNumber()
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(A4[0] - 40, 20, f"Page {page_num}")

    canvas.restoreState()

def checkbox(checked=False):
    """Retourne une case à cocher en texte."""
    return "☑" if checked else "☐"

def _normalize_name(s: str) -> str:
    """
    Normalisation légère pour comparer des noms « logiquement » égaux :
    - trim + espaces internes réduits
    - suppression des diacritiques
    - casefold (équivalent lower robuste)
    """
    s = (s or "").strip()
    s = " ".join(s.split())
    s_nf = unicodedata.normalize("NFD", s)
    s_nf = "".join(ch for ch in s_nf if unicodedata.category(ch) != "Mn")
    return s_nf.casefold()

class CSRFForm(FlaskForm):
    """Formulaire minimal uniquement pour valider le token CSRF."""
    pass



partenaires_bp = Blueprint("partenaires", __name__)

@partenaires_bp.route("/partenaires", methods=["GET", "POST"])
@login_required
@require_access("associations", "lecture")
def partenaires():

    # ======================================================
    # 🔐 Accès
    # ======================================================

    lecture_seule = not has_access("associations", "ecriture")

    # ======================================================
    # 🔌 DB
    # ======================================================
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # ======================================================
    # 🧩 Champs configurés
    # ======================================================
    fields_data = cursor.execute("""
        SELECT *
        FROM field_groups
        WHERE appli = 'associations'
        ORDER BY display_order
    """).fetchall()

    # filtrer display_order > 0
    def _ok_display_order(row):
        try:
            return row["display_order"] and int(row["display_order"]) > 0
        except:
            return False

    fields_data = [f for f in fields_data if _ok_display_order(f)]

    # ======================================================
    # 🗂️ Groupes (UI)
    # ======================================================
    grouped_fields = {}
    for row in fields_data:
        group = row["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(row)

    # 👉 IMPORTANT : toutes les colonnes sont envoyées
    selected_columns = [row["field_name"] for row in fields_data]
    selected_groups = list(grouped_fields.keys())

    # ======================================================
    # 👤 Gestion rôle CAR
    # ======================================================
    user_role = current_user.role.lower()
    voir_toutes = request.values.get("voir_toutes") == "1"

    is_car = (user_role == "car") and not voir_toutes
    car_value = current_user.username if is_car else None

    voir_non_valides = request.values.get("voir_non_valides") == "1"

    # ======================================================
    # 🔎 Requête principale (TOUT)
    # ======================================================
    if is_car:
        query = """
            SELECT *
            FROM associations
            WHERE LOWER(car) = LOWER(?)
        """
        params = [car_value]

        if voir_non_valides:
            query += " AND LOWER(validite) = 'non'"
        else:
            query += " AND (validite IS NULL OR LOWER(validite) != 'non')"

        query += " ORDER BY nom_association COLLATE NOCASE"

        rows_full = cursor.execute(query, params).fetchall()

    else:
        query = """
            SELECT *
            FROM associations
        """

        if voir_non_valides:
            query += " WHERE LOWER(validite) = 'non'"
        else:
            query += " WHERE validite IS NULL OR LOWER(validite) != 'non'"

        query += " ORDER BY nom_association COLLATE NOCASE"

        rows_full = cursor.execute(query).fetchall()

    # ======================================================
    # 🔎 Construction affichage + search_blob
    # ======================================================
    rows = []

    for row in rows_full:

        excluded = ["user_modif"]

        search_blob = " ".join(
            str(row[k] or "")
            for k in row.keys()
            if k not in excluded
        )

        rows.append({
            "display": row,
            "search_blob": search_blob
        })

    conn.close()

    # ======================================================
    # 🎨 Rendu
    # ======================================================
    return render_template(
        "partenaires/partenaires.html",
        rows=rows,
        grouped_fields=grouped_fields,
        selected_columns=selected_columns,
        selected_groups=selected_groups,
        voir_toutes=voir_toutes,
        user_role=user_role,
        lecture_seule=lecture_seule,
        voir_non_valides=voir_non_valides
    )

@partenaires_bp.route("/create_partner", methods=["GET", "POST"])
@login_required
@require_access("associations", "ecriture")
def create_partner():

    conn = get_db_connection()
    cursor = conn.cursor()

    # 📋 Charger la configuration des champs dynamiques
    rows = cursor.execute("""
        SELECT field_name, type_champ, group_name, is_required
        FROM field_groups
        WHERE appli = 'associations'
        ORDER BY display_order
    """).fetchall()

    fields_config = []
    grouped_fields = {}
    for row in rows:
        field = dict(row)
        field["is_required"] = bool(field.pop("is_required"))
        field["value"] = ""  # Valeur par défaut vide
        fields_config.append(field)

        group = field.get("group_name") or "Autres"
        grouped_fields.setdefault(group, []).append(field)

    # 📋 Récupérer les options CAR disponibles
    car_options_query = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'car'").fetchall()
    car_options = [row["param_value"] for row in car_options_query]

    if request.method == "POST":

        valeurs = {}
        champs_invalides = []
        erreurs = []

        # 🧪 Lecture et validation de chaque champ dynamique
        for field in fields_config:
            fname = field["field_name"]
            ftype = field["type_champ"]
            raw_value = request.form.get(fname, "").strip()

            # ✨ Nettoyage spécifique si champ de type téléphone
            cleaned_value = re.sub(r"\D", "", raw_value) if ftype == "tel" else raw_value
            valeurs[fname] = cleaned_value if cleaned_value else None

            # ❗ Vérification des champs requis
            if field["is_required"] and not raw_value:
                erreurs.append(f"Le champ requis « {fname} » est vide.")
                champs_invalides.append(fname)

            # 📧 Validation email
            if raw_value and "email" in fname.lower() and not is_valid_email(raw_value):
                erreurs.append(f"Adresse email invalide dans « {fname} » ➜ « {raw_value} »")
                champs_invalides.append(fname)

            # ☎️ Validation téléphone
            if ftype == "tel" and raw_value and not is_valid_phone(cleaned_value):
                erreurs.append(f"Téléphone invalide dans « {fname} » ➜ « {raw_value} »")
                champs_invalides.append(fname)

        # ✅ Valeur par défaut si champ 'validite' non renseigné
        if not valeurs.get("validite"):
            valeurs["validite"] = "oui"

        # ❌ Retour si erreurs détectées
        if erreurs:
            for msg in erreurs:
                flash(f"❌ {msg}", "danger")

            # Réinjecter les valeurs initiales dans le formulaire
            for field in fields_config:
                field["value"] = request.form.get(field["field_name"], "")

            conn.close()
            return render_template(
                "partenaires/create_partenaire.html",
                fields_config=fields_config,
                grouped_fields=grouped_fields,
                car_options=car_options,
                champs_invalides=champs_invalides
            )

        # ✅ Insertion en base si tout est valide
        now = datetime.now()
        valeurs["date_modif"] = now.strftime("%Y-%m-%d")
        valeurs["heure_modif"] = now.strftime("%H:%M:%S")
        valeurs["user_modif"] = current_user.username

        champs = ", ".join(f"`{k}`" for k in valeurs.keys())
        placeholders = ", ".join("?" for _ in valeurs)
        values = list(valeurs.values())

        try:
            cursor.execute(f"INSERT INTO associations ({champs}) VALUES ({placeholders})", values)
            conn.commit()
            upload_database()
            flash("✅ Partenaire créé avec succès.", "success")
            return redirect(url_for("partenaires.partenaires"))
        except Exception as e:
            flash(f"❌ Erreur lors de l'insertion : {e}", "danger")

    conn.close()
    return render_template(
        "partenaires/create_partenaire.html",
        fields_config=fields_config,
        grouped_fields=grouped_fields,
        car_options=car_options,
        champs_invalides=[]
    )


@partenaires_bp.route("/duplicate_partner/<int:partner_id>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def duplicate_partner(partner_id):

    new_name_raw = (request.form.get("new_nom_association") or "").strip()
    if not new_name_raw:
        flash("❌ Le nom de la nouvelle association est requis.", "danger")
        return redirect(url_for("partenaires.update_partner", partner_id=partner_id))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    src = cur.execute("SELECT * FROM associations WHERE id = ? OR ID = ? OR Id = ?",
                      (partner_id, partner_id, partner_id)).fetchone()
    if not src:
        conn.close()
        flash("❌ Association source introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    old_name_raw = src["nom_association"] if "nom_association" in src.keys() else ""

    # --- contrôles de nom (différent + anti-doublon simple) ---
    import unicodedata
    def _norm(s:str)->str:
        s = (s or "").strip()
        s = " ".join(s.split())
        s = unicodedata.normalize("NFD", s)
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return s.casefold()

    if _norm(new_name_raw) == _norm(old_name_raw):
        conn.close()
        flash("⚠️ Le nouveau nom doit être différent de celui de l’association copiée.", "warning")
        return redirect(url_for("partenaires.update_partner", partner_id=partner_id))

    exists = cur.execute(
        "SELECT 1 FROM associations WHERE LOWER(nom_association) = LOWER(?)",
        (new_name_raw,)
    ).fetchone()
    if exists:
        conn.close()
        flash("⚠️ Une association porte déjà ce nom. Veuillez en choisir un autre.", "warning")
        return redirect(url_for("partenaires.update_partner", partner_id=partner_id))

    # --- colonnes & exclusion robuste des PK (et de toute variante d'ID) ---
    cols_info = cur.execute("PRAGMA table_info(associations)").fetchall()
    cols = [r["name"] for r in cols_info]
    pk_cols = {r["name"] for r in cols_info if r["pk"]}  # toutes colonnes PK
    # Exclure aussi toute colonne qui s’appelle id avec autre casse
    id_like = {c for c in cols if c.lower() == "id"}
    exclude = pk_cols | id_like

    data = {c: src[c] for c in cols if c not in exclude}

    # --- surcharges ---
    from datetime import datetime
    data["nom_association"] = new_name_raw
    now = datetime.now()
    data["date_modif"] = now.strftime("%Y-%m-%d")
    data["heure_modif"] = now.strftime("%H:%M:%S")
    data["user_modif"] = getattr(current_user, "username", "inconnu")

    champs = ", ".join(f"`{k}`" for k in data.keys())
    placeholders = ", ".join("?" for _ in data)
    values = list(data.values())

    try:
        cur.execute(f"INSERT INTO associations ({champs}) VALUES ({placeholders})", values)
        conn.commit()
        new_id = cur.lastrowid
        try:
            upload_database()
        except Exception:
            pass
        flash(f"✅ Association dupliquée vers « {new_name_raw} ».", "success")
        return redirect(url_for("partenaires.update_partner", partner_id=new_id))
    except Exception as e:
        conn.rollback()
        flash(f"❌ Erreur lors de la duplication : {e}", "danger")
        return redirect(url_for("partenaires.update_partner", partner_id=partner_id))
    finally:
        conn.close()



@partenaires_bp.route("/update_partner/<int:partner_id>", methods=["GET", "POST"])
@login_required
@require_access("associations", "lecture")
def update_partner(partner_id):
    """
    Page de mise à jour d’un partenaire (association).

    ⚙️ Fonctionnalités :
    - Affiche le formulaire avec les champs dynamiques regroupés par familles (field_groups).
    - Valide les modifications (emails, téléphones, etc.).
    - Réinjecte les valeurs saisies en cas d’erreur.
    - Met à jour la base uniquement si des modifications sont détectées (via form_hash).
    - Affiche la date/heure de dernière modification en heure française + l’utilisateur modificateur.
    - Navigation Suivant / Précédent / Retour : déclenche d’abord une mise à jour si demandé,
      puis redirige vers la cible.
    """

    # 🔒 Vérification des droits

    lecture_seule = not has_access("associations", "ecriture")

    conn = get_db_connection()
    cursor = conn.cursor()

    # 🔍 Récupération du partenaire
    partner = cursor.execute("SELECT * FROM associations WHERE id = ?", (partner_id,)).fetchone()
    if not partner:
        conn.close()
        flash("Association introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    partner_dict = dict(partner)

    # 📌 Récupération des voisins alphabétiques (Précédent/Suivant)
    previous_id, next_id = get_neighbor_ids_alphabetically(conn, partner_id)

    # 🔢 Champs dynamiques
    fields_rows = cursor.execute("""
        SELECT * FROM field_groups
        WHERE appli = 'associations'
        ORDER BY display_order
    """).fetchall()

    fields_data = []
    for row in fields_rows:
        field = dict(row)
        fname = field["field_name"]
        field["value"] = partner_dict.get(fname, partner_dict.get(fname.lower(), ""))
        fields_data.append(field)

    # 📦 Regroupement par familles
    grouped_fields = {}
    for field in fields_data:
        group = field["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(field)

    # 📋 Liste des réseaux nationaux
    reseaux = cursor.execute("""
        SELECT param_value FROM parametres
        WHERE param_name = 'RESEAUX_NATIONAUX'
        ORDER BY param_value
    """).fetchall()
    liste_reseaux = [r["param_value"] for r in reseaux]

    # 🚗 Options CAR
    car_options_query = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'car'").fetchall()
    car_options = [{"param_value": row[0]} for row in car_options_query]

    # 🔁 Paramètres de navigation à préserver (utile pour le retour à la liste)
    search_term = request.values.get("search", "")
    limit = request.values.get("limit", "7")
    selected_columns = request.values.getlist("columns")
    selected_groups = request.values.getlist("selected_groups")
    query_params = {
        "search": search_term,
        "limit": limit,
        "columns": selected_columns,
        "selected_groups": selected_groups
    }

    # 📝 Gestion POST (mise à jour et navigation)
    if request.method == "POST":
        go_to = request.form.get("go_to")          # Cible navigation (Suivant, Précédent, Retour)
        do_upload = request.form.get("do_upload", "1")  # "1" = on sauvegarde avant de naviguer

        # ❌ Blocage écriture en lecture seule
        if lecture_seule and do_upload == "1":
            conn.close()
            flash("⛔ Vous n’avez pas les droits pour modifier cette association.", "danger")
            return redirect(url_for("partenaires.update_partner", partner_id=partner_id, **request.args))

        updates = {}
        champs_invalides = []
        erreurs = []

        # ✅ Gestion des champs multiples
        produits_souhaites = ",".join(request.form.getlist("produits_souhaites"))
        autres_approvisionnements = ",".join(request.form.getlist("autres_approvisionnements"))

        if produits_souhaites:
            updates["produits_souhaites"] = produits_souhaites
        if autres_approvisionnements:
            updates["autres_approvisionnements"] = autres_approvisionnements

        # ✅ Autres champs
        for field in fields_data:
            fname = field["field_name"]
            if fname in ("id", "produits_souhaites", "autres_approvisionnements"):
                continue
            val = request.form.get(fname, "").strip()
            updates[fname] = None if val == "" else val

        # 🔍 Validation email / téléphone
        for field in fields_data:
            fname = field["field_name"]
            label = fname.replace("_", " ").capitalize()
            group = field.get("group_name", "Autres") or "Autres"
            val = (updates.get(fname) or "").strip()
            if val:
                if field.get("type_champ") == "email" and not is_valid_email(val):
                    erreurs.append(f"Champ invalide dans {group} : {label} ➜ « {val} » n’est pas un email valide.")
                    champs_invalides.append(fname)
                if "tel" in fname.lower() and not is_valid_phone(val):
                    erreurs.append(f"Champ invalide dans {group} : {label} ➜ « {val} » n’est pas un numéro de téléphone valide.")
                    champs_invalides.append(fname)

        # ⛔ Si erreurs → réafficher formulaire
        if erreurs:
            for msg in erreurs:
                flash(msg, "danger")
            for field in fields_data:
                fname = field["field_name"]
                field["value"] = request.form.get(fname, "").strip()
            grouped_fields = {}
            for field in fields_data:
                group = field["group_name"] or "Autres"
                grouped_fields.setdefault(group, []).append(field)
            conn.close()
            return render_template(
                "partenaires/update_partenaire.html",
                partenaire=partner_dict,
                partner_id=partner_id,
                nom_association=request.form.get("nom_association") or partner_dict.get("nom_association", "Nom inconnu"),
                grouped_fields=grouped_fields,
                liste_reseaux=liste_reseaux,
                car_options=car_options,
                next_url=urlencode(query_params, doseq=True),
                previous_id=previous_id,
                next_id=next_id,
                lecture_seule=lecture_seule,
                date_modif=partner_dict.get("date_modif", ""),
                heure_modif=partner_dict.get("heure_modif", ""),
                user_modif=partner_dict.get("user_modif", ""),
                champs_invalides=champs_invalides,
                form_hash=request.form.get("form_hash", ""),
                data=dict(partner)
            )

        # 🔒 Horodatage si des modifs détectées
        if do_upload == "1":
            inputs_for_hash = [
                f"{f['field_name']}:{request.form.get(f['field_name'], '').strip()}"
                for f in fields_data if f["field_name"] != "id"
            ]
            computed_hash = base64.b64encode("|#|".join(inputs_for_hash).encode("utf-8")).decode("utf-8")
            received_hash = request.form.get("form_hash", "")
            if computed_hash != received_hash:
                now = datetime.now()
                updates["date_modif"] = now.strftime("%Y-%m-%d")
                updates["heure_modif"] = now.strftime("%H:%M:%S")
                updates["user_modif"] = current_user.username

            # 💾 Mise à jour
            if updates:
                set_clause = ", ".join([f"`{k}` = ?" for k in updates])
                values = list(updates.values()) + [partner_id]
                try:
                    cursor.execute(f"UPDATE associations SET {set_clause} WHERE id = ?", values)
                    conn.commit()
                    upload_database()
                    flash("✅ Association mise à jour avec succès.", "success")
                except Exception as e:
                    flash(f"❌ Erreur lors de la mise à jour : {e}", "danger")

        conn.close()

        # 🚀 Navigation après mise à jour
        if go_to:
            return redirect(go_to)
        else:
            return redirect(url_for("partenaires.update_partner", partner_id=partner_id))

    # 🧮 Calcul du form_hash initial (GET)
    inputs_for_hash = [
        f"{field['field_name']}:{field['value'] or ''}" for field in fields_data if field['field_name'] != "id"
    ]
    form_hash = base64.b64encode("|#|".join(inputs_for_hash).encode("utf-8")).decode("utf-8")

    # 🕒 Conversion date_modif + heure_modif en heure FR
    date_modif = partner_dict.get("date_modif")
    heure_modif = partner_dict.get("heure_modif")
    heure_fr = None
    if date_modif and heure_modif:
        try:
            dt_str = f"{date_modif} {heure_modif}"
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            import pytz
            dt = pytz.utc.localize(dt).astimezone(pytz.timezone("Europe/Paris"))
            heure_fr = dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            heure_fr = f"{date_modif} {heure_modif}"
    elif date_modif:
        heure_fr = date_modif

    conn.close()
    return render_template(
        "partenaires/update_partenaire.html",
        partenaire=partner_dict,
        partner_id=partner_id,
        nom_association=partner_dict.get("nom_association", "Nom inconnu"),
        grouped_fields=grouped_fields,
        liste_reseaux=liste_reseaux,
        car_options=car_options,
        next_url=urlencode(query_params, doseq=True),
        previous_id=previous_id,
        next_id=next_id,
        lecture_seule=lecture_seule,
        date_modif=heure_fr,
        user_modif=partner_dict.get("user_modif", ""),
        champs_invalides=[],
        form_hash=form_hash,
        data=dict(partner)
    )



@partenaires_bp.route("/delete_partner/<int:partner_id>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def delete_partner(partner_id):

    if confirmation == "oui" and second_confirmation == "supprimer":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM associations WHERE ID = ?", (partner_id,))
        conn.commit()
        upload_database()  # Sauvegarde automatique sur Google Drive
        conn.close()
        flash("✅ Partenaire supprimé avec succès.", "success")
        return redirect(url_for('partenaires.partenaires'))
    else:
        flash("❌ Suppression annulée ou confirmation incorrecte.", "danger")
        return redirect(url_for('partenaires.update_partner', partner_id=partner_id))




@partenaires_bp.route("/edition_tableau_associations", methods=["GET", "POST"])
@login_required
@require_access("associations", "lecture")
def edition_tableau_associations():

    fields_data = cursor.execute("""
        SELECT * FROM field_groups
        WHERE appli = 'associations'
        ORDER BY display_order
    """).fetchall()

    grouped_fields = {}
    for row in fields_data:
        group = row["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(row)

    # ✅ Détection des champs de type oui/non
    oui_non_fields = [row["field_name"] for row in fields_data if row["type_champ"] == "oui_non"]

    selected_columns = request.values.getlist("columns")
    voir_toutes = request.args.get("voir_toutes") == "1"
    user_role = current_user.role.lower()
    is_car = user_role == "car" and not voir_toutes
    car_value = current_user.username if is_car else None

    escaped_columns = [f"`{col}`" for col in selected_columns if col != "nom_association"]
    columns_clause = ", ".join(["ID", "`nom_association`"] + escaped_columns)

    if is_car:
        query = f"""
            SELECT {columns_clause}
            FROM associations
            WHERE LOWER(car) = LOWER(?)
              AND (validite IS NULL OR LOWER(validite) != 'non')
            ORDER BY nom_association COLLATE NOCASE
        """
        rows = cursor.execute(query, (car_value,)).fetchall()
    else:
        query = f"""
            SELECT {columns_clause}
            FROM associations
            WHERE validite IS NULL OR LOWER(validite) != 'non'
            ORDER BY nom_association COLLATE NOCASE
        """
        rows = cursor.execute(query).fetchall()

    conn.close()

    return render_template("partenaires/edition_tableau_associations.html",
                           rows=rows,
                           selected_columns=selected_columns,
                           user_role=user_role,
                           oui_non_fields=oui_non_fields)



@partenaires_bp.route('/generate_annexe1/<int:partner_id>', methods=['POST'])
@login_required
@require_access("associations", "lecture")
def generate_annexe1(partner_id):
    """ Génère un PDF pour Annexe 1 avec mise en page, logos et entêtes de groupes. """
    return generate_pdf_annexe1bis(partner_id, ['coordonnées principales', 'annexe 1 bis'], "ANNEXE 1 BIS")


@partenaires_bp.route("/update_associations_table", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def update_associations_table():

    conn = get_db_connection()
    cursor = conn.cursor()

    total = int(request.form.get("total_rows", 0))
    columns = request.form.getlist("columns")

    # ✅ Vérification du nombre de colonnes
    if len(columns) > 40:
        flash("⚠️ Trop de colonnes sélectionnées. Veuillez limiter votre sélection à 40 colonnes maximum.", "danger")
        return redirect(url_for("partenaires.partenaires"))


    erreurs = []
    lignes_modifiees = 0
    associations_data = []

    for i in range(total):
        asso_id = request.form.get(f"id_{i}")
        if not asso_id:
            continue

        old_row = cursor.execute("SELECT * FROM associations WHERE ID = ?", (asso_id,)).fetchone()
        if not old_row:
            continue

        asso_dict = dict(old_row)
        modifications = {}
        champs_invalides = []

        for col in columns:
            old_val = (asso_dict[col] or "").strip() if asso_dict[col] else ""
            new_val = request.form.get(f"{col}_{i}", "").strip()

            if new_val != old_val:
                if "email" in col.lower() and new_val and not is_valid_email(new_val):
                    erreurs.append(f"Ligne {i + 1}, champ {col} : adresse email invalide « {new_val} »")
                    champs_invalides.append(col)
                elif "tel" in col.lower() and new_val and not is_valid_phone(new_val):
                    erreurs.append(f"Ligne {i + 1}, champ {col} : numéro de téléphone invalide « {new_val} »")
                    champs_invalides.append(col)
                else:
                    modifications[col] = new_val if new_val else None

        if modifications and not champs_invalides:
            now = datetime.now()
            modifications["date_modif"] = now.strftime("%Y-%m-%d")
            modifications["heure_modif"] = now.strftime("%H:%M:%S")
            modifications["user_modif"] = current_user.username

            set_clause = ", ".join([f"`{k}` = ?" for k in modifications])
            values = list(modifications.values()) + [asso_id]
            cursor.execute(f"UPDATE associations SET {set_clause} WHERE ID = ?", values)
            lignes_modifiees += 1
        else:
            row_data = {
                "id": asso_id,
                "champs_invalides": champs_invalides,
                "valeurs": {col: request.form.get(f"{col}_{i}", "").strip() for col in columns},
                "nom": request.form.get(f"nom_association_{i}", "") or asso_dict.get("nom_association", "")
            }
            associations_data.append(row_data)

    conn.commit()
    conn.close()

    if lignes_modifiees > 0:
        upload_database()
        flash(f"✅ {lignes_modifiees} ligne(s) modifiée(s) avec succès.", "success")

    if erreurs:
        for msg in erreurs:
            flash(f"❌ {msg}", "danger")

        field_config = get_db_connection().execute("""
            SELECT field_name, type_champ FROM field_groups
            WHERE appli = 'associations'
        """).fetchall()
        oui_non_fields = [row["field_name"] for row in field_config if row["type_champ"] == "oui_non"]

        return render_template(
            "partenaires/edition_tableau_associations.html",
            rows=associations_data,
            selected_columns=columns,
            oui_non_fields=oui_non_fields
        )

    if lignes_modifiees == 0:
        flash("ℹ️ Aucune modification détectée.", "info")

    return redirect(url_for(
        "partenaires.edition_tableau_associations",
        columns=columns
    ))


def get_neighbor_ids_alphabetically(conn, current_id):
    cursor = conn.cursor()
    cursor.execute("SELECT id, nom_association FROM associations ORDER BY nom_association COLLATE NOCASE")
    rows = cursor.fetchall()
    ids = [row[0] for row in rows]

    try:
        idx = ids.index(current_id)
        previous_id = ids[idx - 1] if idx > 0 else None
        next_id = ids[idx + 1] if idx < len(ids) - 1 else None
    except ValueError:
        previous_id = next_id = None

    return previous_id, next_id



def generate_pdf(partner_id, groups, title):
    # Connexion à la base
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1) Récupérer uniquement les champs du bon groupe et appli=associations
    placeholders = ', '.join('?' for _ in groups)
    cursor.execute(f"""
        SELECT group_name, field_name
        FROM field_groups
        WHERE group_name IN ({placeholders}) AND appli = 'associations'
        ORDER BY display_order
    """, (*groups,))
    field_rows = cursor.fetchall()

    if not field_rows:
        conn.close()
        return "Aucun champ trouvé pour ces groupes", 404

    # 2) Vérifier les colonnes existantes
    table_cols = [row[1] for row in cursor.execute("PRAGMA table_info(associations)").fetchall()]
    valid_fields = [row for row in field_rows if row["field_name"] in table_cols]

    if not valid_fields:
        conn.close()
        return "Aucun champ valide trouvé dans la table associations", 404

    # 3) Construire la requête SQL
    cols = ", ".join([f"`{row['field_name']}`" for row in valid_fields])
    cursor.execute(f"SELECT {cols} FROM associations WHERE ID = ?", (partner_id,))
    values = cursor.fetchone()
    conn.close()

    if not values:
        return "Aucune donnée trouvée pour ce partenaire", 404

    # 4) Création du PDF
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left_margin = 50
    right_margin = width - 50
    middle_margin = (left_margin + right_margin) / 2
    y_position = height - 100
    line_height = 12
    field_spacing = 5

    # Logo
    logo_path = "static/images/logo.png"
    if os.path.exists(logo_path):
        pdf.drawImage(logo_path, left_margin, height - 80, width=50, height=50)

    # Titre
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawCentredString(width / 2, height - 60, title)

    # Ajout des champs groupés
    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(colors.black)
    current_group = None

    for row in valid_fields:
        group_name = row["group_name"].capitalize()
        field_name = row["field_name"]
        field_value = str(values[field_name]) if values[field_name] is not None else ""

        # Nouveau groupe
        if group_name != current_group:
            pdf.setFont("Helvetica-Bold", 12)
            pdf.setFillColor(colors.lightgrey)
            pdf.rect(left_margin - 5, y_position - 18, right_margin - left_margin + 10, 18, fill=1, stroke=0)
            pdf.setFillColor(colors.black)
            pdf.drawString(left_margin, y_position - 14, group_name)
            y_position -= (25 + field_spacing)
            pdf.setFont("Helvetica", 10)
            current_group = group_name

        pdf.drawString(left_margin + 10, y_position, f"{field_name}:")
        wrapped_text = simpleSplit(field_value, 'Helvetica', 10, right_margin - middle_margin - 10)
        if not wrapped_text:
            y_position -= line_height
        for line in wrapped_text:
            pdf.drawString(middle_margin, y_position, line)
            y_position -= line_height
        y_position -= field_spacing

        if y_position < 60:
            pdf.setFont("Helvetica", 8)
            date_du_jour = datetime.today().strftime('%d/%m/%Y')
            pdf.drawString(left_margin, 30, "Banque Alimentaire de l'Isère - Service Partenariat")
            pdf.drawString(right_margin - 150, 30, f"{date_du_jour} - Page {pdf.getPageNumber()}")
            pdf.showPage()

            if os.path.exists(logo_path):
                pdf.drawImage(logo_path, left_margin, height - 80, width=50, height=50)
            pdf.setFont("Helvetica-Bold", 16)
            pdf.drawCentredString(width / 2, height - 60, title)
            pdf.setFont("Helvetica", 10)
            y_position = height - 100

    # Pied de page
    pdf.setFont("Helvetica", 8)
    date_du_jour = datetime.today().strftime('%d/%m/%Y')
    pdf.drawString(left_margin, 30, "Banque Alimentaire de l'Isère - Service Partenariat")
    pdf.drawString(right_margin - 150, 30, f"{date_du_jour} - Page {pdf.getPageNumber()}")

    pdf.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"{title.replace(' ', '_').lower()}_{partner_id}.pdf", mimetype='application/pdf')



# Nouvelle version generate_pdf_annexe1bis

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


def generate_pdf_annexe1bis(partner_id, groups=None, title="Annexe 1 bis : Informations sur le Partenaire"):
    # --- Connexion et récupération des données ---
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    partner = cursor.execute("SELECT * FROM associations WHERE id = ?", (partner_id,)).fetchone()
    conn.close()
    if not partner:
        return "Partenaire introuvable", 404

    data = dict(partner)

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


    # --- Libellés centrés ---
    elements.append(Paragraph("Une fiche par point de distribution", style_n))
    elements.append(Spacer(1, 0.2 * cm))
    date_visite = data.get("date_de_la_visite") or datetime.today().strftime('%d/%m/%Y')
    elements.append(Paragraph(f"Date de mise à jour : {safe_paragraph_value(date_visite)}", style_n))
    elements.append(Spacer(1, 0.4 * cm))

    # ==============================================================================================================
    # === 1. Informations principales ===
    # ==============================================================================================================

    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph("1. Informations sur le partenaire", style_h2))
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
    elements.append(Spacer(5, 0.5 * cm))
    table_signature = Table(
        [[
            Paragraph("Date :", style_n_left),
            "",
            Paragraph("Signature responsable association :", style_n_left)
        ]],
        colWidths=[3 * cm, 7 * cm, 7 * cm]
    )
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
        onFirstPage=lambda c, d: header_footer(c, d, title, data.get("nom_association", "")),
        onLaterPages=lambda c, d: header_footer(c, d, title, data.get("nom_association", ""))
    )
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"annexe1bis_{partner_id}.pdf", mimetype='application/pdf')



from flask import flash

@partenaires_bp.route("/test_flash")
def test_flash():
    flash("Message de test SUCCESS", "success")
    flash("Message de test DANGER", "danger")
    flash("Message de test WARNING", "warning")
    flash("Message de test INFO", "info")
    return redirect(url_for("partenaires.partenaires"))



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


@partenaires_bp.route("/envoi_mail", methods=["GET", "POST"])
@login_required
@require_access("associations", "lecture")
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
            return redirect(url_for("partenaires.envoi_mail", assoc_id=assoc_id))

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
            "partenaires/envoi_mail.html",
            destinataires=destinataires,
            messages=messages,
            gmail_url=gmail_url,
            retour_url=retour_url,
            assoc_id=assoc_id  # ➕ pour le lien vers messages_predefinis
        )

    conn.close()
    return render_template(
        "partenaires/envoi_mail.html",
        destinataires=destinataires,
        messages=messages,
        retour_url=retour_url,
        assoc_id=assoc_id  # ➕ pour le lien vers messages_predefinis
    )


# ➕ CRUD messages préenregistrés
@partenaires_bp.route("/messages_predefinis", methods=["GET", "POST"])
@login_required
@require_access("associations", "lecture")
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
            return redirect(url_for("partenaires.messages_predefinis", assoc_id=assoc_id_from_form))
        return redirect(url_for("partenaires.messages_predefinis"))

    messages = cur.execute("SELECT * FROM messages_predefinis ORDER BY id DESC").fetchall()
    conn.close()

    return render_template("partenaires/messages_predefinis.html", messages=messages, assoc_id=assoc_id)


@partenaires_bp.route("/messages_predefinis/delete/<int:mid>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def delete_message(mid):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages_predefinis WHERE id = ?", (mid,))
    conn.commit()
    conn.close()
    flash("🗑️ Message supprimé", "warning")

    assoc_id = request.args.get("assoc_id")
    if assoc_id:
        return redirect(url_for("partenaires.messages_predefinis", assoc_id=assoc_id))
    return redirect(url_for("partenaires.messages_predefinis"))


@partenaires_bp.route("/messages_predefinis/edit/<int:mid>", methods=["GET", "POST"])
@login_required
@require_access("associations", "ecriture")
def edit_message(mid):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    assoc_id = request.args.get("assoc_id") or request.form.get("assoc_id")
    message = cur.execute("SELECT * FROM messages_predefinis WHERE id = ?", (mid,)).fetchone()
    if not message:
        flash("❌ Message introuvable", "danger")
        if assoc_id:
            return redirect(url_for("partenaires.messages_predefinis", assoc_id=assoc_id))
        return redirect(url_for("partenaires.messages_predefinis"))

    if request.method == "POST":
        titre = (request.form.get("titre") or "").strip()
        contenu = (request.form.get("contenu") or "").strip()
        if titre and contenu:
            cur.execute("UPDATE messages_predefinis SET titre=?, contenu=? WHERE id=?", (titre, contenu, mid))
            conn.commit()
            flash("✅ Message modifié", "success")
            conn.close()
            if assoc_id:
                return redirect(url_for("partenaires.messages_predefinis", assoc_id=assoc_id))
            return redirect(url_for("partenaires.messages_predefinis"))
        else:
            flash("❌ Merci de remplir tous les champs", "danger")

    conn.close()
    # (Si tu as un template 'edit_message.html', pense à y ajouter un bouton retour aussi)
    return render_template("partenaires/edit_message.html", message=message)
