import json
import os
import re
import sqlite3
import unicodedata


from flask import Blueprint, render_template, render_template_string, request, redirect, url_for, flash, session, jsonify, current_app, abort, send_from_directory
from flask_login import login_required, current_user
from ba38_utilitaires.core import get_db_connection, upload_database, write_log, has_access, is_valid_email, is_valid_phone, require_access, get_db_path
from werkzeug.security import generate_password_hash
from PIL import Image, ExifTags
from urllib.parse import urlencode, quote_plus, quote
from dotenv import load_dotenv
from datetime import datetime


load_dotenv()

TYPE_BENE_PARAM = "type_benevole"

def get_type_benevole_options(conn):
    """
    Lit les valeurs autorisées dans parametres (param_name=type_benevole).
    Retourne une liste ordonnée.
    """
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT param_value FROM parametres WHERE param_name = ? ORDER BY id",
        (TYPE_BENE_PARAM,)
    ).fetchall()
    return [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]



def coerce_type_benevole(value, options):
    """
    Nettoie la saisie et la fait correspondre (sans casse/espaces) à une valeur canonique d'options.
    - Retourne None si vide.
    - Retourne la valeur *canonique* (telle que stockée dans parametres) si match.
    - Sinon None (tu pourras lever une erreur côté formulaire/tableau).
    """
    if value is None:
        return None
    v = re.sub(r"\s+", " ", str(value)).strip()
    if not v:
        return None
    low = v.lower()
    for opt in options:
        if low == str(opt).strip().lower():
            return opt
    return None


CIVILITE_OPTIONS = ["Monsieur", "Madame"]

def coerce_civilite(value):
    if not value:
        return None
    v = value.strip().lower()
    for opt in CIVILITE_OPTIONS:
        if v == opt.lower():
            return opt
    return None



benevoles_bp = Blueprint("benevoles", __name__)






@benevoles_bp.route("/api/quick_create_benevole", methods=["POST"])
@login_required
@require_access("benevoles", "ecriture")
def api_quick_create_benevole():

    current_app.logger.info(
        f"API quick_create_benevole called by user={current_user.id}"
    )

    try:
        data = request.get_json(force=True)

        nom = data.get("nom", "").strip()
        prenom = data.get("prenom", "").strip()
        civilite = data.get("civilite", "")
        type_benevole = data.get("type_benevole", "")
        telephone = data.get("telephone_portable", "")

        if not nom or not prenom or not civilite or not type_benevole:
            return jsonify(success=False, error="Champs obligatoires manquants")

        conn = get_db_connection()
        cur = conn.cursor()

        current_app.logger.info(
            f"Creating benevole: {nom} {prenom} / {type_benevole}"
        )

        cur.execute("""
            INSERT INTO benevoles (civilite, nom, prenom, telephone_portable, type_benevole)
            VALUES (?, ?, ?, ?, ?)
        """, (civilite, nom, prenom, telephone or None, type_benevole))

        benevole_id = cur.lastrowid
        conn.commit()
        conn.close()

        upload_database()

        return jsonify(
            success=True,
            id=benevole_id,
            nom=nom,
            prenom=prenom
        )

    except Exception as e:
        current_app.logger.exception(
            "❌ Exception api_quick_create_benevole"
        )

        write_log(f"❌ Erreur création bénévole rapide : {e}")
        return jsonify(success=False, error="Erreur serveur")



# ============================================================
# ROUTE TABULATOR BENEVOLES
# ============================================================

@benevoles_bp.route("/benevoles")
@login_required
@require_access("benevoles", "lecture")
def benevoles():

    return redirect(
        url_for("benevoles.benevoles_tabulator")
    )


# ============================================================
# ROUTE TABULATOR
# ============================================================

@benevoles_bp.route("/benevoles_tabulator")
@login_required
@require_access("benevoles", "lecture")
def benevoles_tabulator():

    db_path = get_db_path()

    conn = sqlite3.connect(db_path)

    conn.row_factory = sqlite3.Row

    cursor = conn.cursor()

    # ============================================================
    # CHAMPS DYNAMIQUES
    # ============================================================

    cursor.execute("""
        SELECT *
        FROM field_groups
        WHERE appli = 'benevoles'
        ORDER BY
            CASE
                WHEN LOWER(group_name)
                     = 'coordonnées principales'
                THEN 0
                ELSE 1
            END,
            group_name COLLATE NOCASE,
            display_order
    """)

    fields = cursor.fetchall()

    # ============================================================
    # BENEVOLES
    # ============================================================

    cursor.execute("""
        SELECT *
        FROM benevoles
        ORDER BY nom COLLATE NOCASE
    """)

    rows = cursor.fetchall()

    # ============================================================
    # DROIT IMAGE
    # ============================================================

    droit_image_map = {}

    try:

        rows_droit = cursor.execute("""
            SELECT id, acceptation
            FROM droit_image
        """).fetchall()

        droit_image_map = {

            row["id"]: row["acceptation"]

            for row in rows_droit
        }

    except Exception:

        droit_image_map = {}

    conn.close()

    # ============================================================
    # NORMALISATION
    # ============================================================

    def normalize(name):

        return (
            name.lower()
            .replace(" ", "_")
            .replace("é", "e")
            .replace("è", "e")
            .replace("ê", "e")
            .replace("à", "a")
            .replace("ù", "u")
            .replace("ô", "o")
            .replace("-", "_")
        )

    # ============================================================
    # GROUPES
    # ============================================================

    grouped_fields = {}

    for field in fields:

        group_name = field["group_name"] or "Autres"

        field_name = field["field_name"]

        normalized = normalize(field_name)

        grouped_fields.setdefault(group_name, []).append({

            "field_name": field_name,

            "normalized": normalized
        })

    # ============================================================
    # FONCTIONS (FILTRE)
    # ============================================================

    fonctions_options = [

        {

            "field_name": field["field_name"],

            "normalized": normalize(field["field_name"])
        }

        for field in fields

        if (field["group_name"] or "").lower() == "fonction"
        and (field["type_champ"] or "").lower() == "oui_non"
    ]

    conn_pref = sqlite3.connect(db_path)

    conn_pref.row_factory = sqlite3.Row

    user_row = conn_pref.execute(
        "SELECT fonctions_filter FROM users WHERE id = ?",
        (current_user.id,)
    ).fetchone()

    conn_pref.close()

    fonctions_filter_selected = []

    if user_row and user_row["fonctions_filter"]:

        try:

            fonctions_filter_selected = json.loads(user_row["fonctions_filter"])

        except (TypeError, ValueError):

            fonctions_filter_selected = []

    # ============================================================
    # PHOTOS
    # ============================================================

    photo_dir = os.getenv("PHOTOS_BENEVOLES_DIR", "/srv/ba38/photos_benevoles")

    photo_ids = set()

    if os.path.isdir(photo_dir):

        for filename in os.listdir(photo_dir):

            if filename.endswith(".jpg"):

                try:

                    bene_id = int(filename.split(".")[0])

                    photo_ids.add(bene_id)

                except ValueError:

                    continue

    # ============================================================
    # TABLE DATA
    # ============================================================

    table_data = []

    for row in rows:

        d = {}

        for key in row.keys():

            d[normalize(key)] = row[key]

        bene_id = row["id"]

        # ========================================================
        # PHOTO
        # ========================================================

        if bene_id in photo_ids:

            d["photo_url"] = url_for(
                "benevoles.serve_photo_benevole",
                filename=f"{bene_id}.jpg"
            )

        else:

            d["photo_url"] = None

        # ========================================================
        # DROIT IMAGE
        # ========================================================

        statut = droit_image_map.get(bene_id)

        if statut == "Accord Total":

            d["droit_image"] = "🟢"

        elif statut == "Accord Entrepot":

            d["droit_image"] = "🟠"

        elif statut == "Refus":

            d["droit_image"] = "🔴"

        else:

            d["droit_image"] = "⚪"

        table_data.append(d)

    # ============================================================
    # COLONNES TABULATOR
    # ============================================================

    columns = [

        {
            "title": "Action",
            "field": "id",
            "width": 130,
            "frozen": True,
            "hozAlign": "center",
            "headerSort": False,
            "formatter": "buttonCross"
        },

        {
            "title": "ID",
            "field": "id",
            "minWidth": 80,
            "widthGrow": 1,
            "frozen": True
        },

        {
            "title": "Nom",
            "field": "nom",
            "tooltip": True,
            "minWidth": 240,
            "widthGrow": 3,
            "frozen": True
        },

        {
            "title": "📸",
            "field": "photo_url",
            "width": 70,
            "hozAlign": "center",
            "frozen": True,
            "formatter": "photoFormatter",
            "headerSort": False
        },

        {
            "title": "Droit image",
            "field": "droit_image",
            "minWidth": 110,
            "widthGrow": 1,
            "hozAlign": "center",
            "frozen": True,
            "formatter": "html"
        }
    ]

    # ============================================================
    # COLONNES DYNAMIQUES
    # ============================================================

    for field in fields:

        field_name = field["field_name"]

        normalized = normalize(field_name)

        if normalized in [
            "id",
            "nom"
        ]:
            continue

        # ========================================================
        # TYPE CHAMP
        # ========================================================

        type_champ = (
            field["type_champ"] or ""
        ).lower()

        # ========================================================
        # FILTRE PAR DEFAUT
        # ========================================================

        header_filter = "input"

        header_filter_params = {

            "placeholder": "Filtrer..."
        }

        # ========================================================
        # LARGEURS DYNAMIQUES
        # ========================================================

        min_width = 125

        width_grow = 1

        hoz_align = "left"

        # ========================================================
        # CHAMPS OUI/NON
        # ========================================================

        if type_champ == "oui_non":

            header_filter = "list"

            header_filter_params = {

                "values": {

                    "": "Tous",

                    "oui": "Oui",

                    "non": "Non"
                },

                "clearable": True
            }

            min_width = 110

            width_grow = 0

            hoz_align = "center"

        # ========================================================
        # EMAILS
        # ========================================================

        elif (
            "courriel" in normalized
            or
            "email" in normalized
        ):

            min_width = 260

            width_grow = 2

        # ========================================================
        # TELEPHONES
        # ========================================================

        elif (
            "tel" in normalized
            or
            "telephone" in normalized
        ):

            min_width = 150

            width_grow = 1

        # ========================================================
        # DATES
        # ========================================================

        elif (
            "date" in normalized
        ):

            min_width = 140

            width_grow = 1

            hoz_align = "center"

        # ========================================================
        # CODES / IDS
        # ========================================================

        elif (
            "code" in normalized
            or
            normalized.endswith("_id")
        ):

            min_width = 120

            width_grow = 1

            hoz_align = "center"

        # ========================================================
        # ADRESSES
        # ========================================================

        elif (
            "rue" in normalized
            or
            "adresse" in normalized
            or
            "commentaire" in normalized
            or
            "observation" in normalized
        ):

            min_width = 320

            width_grow = 3

        # ========================================================
        # PRENOM
        # ========================================================

        elif normalized == "prenom":

            min_width = 180

            width_grow = 2

        # ========================================================
        # CONFIG COLONNE
        # ========================================================

        col = {

            "title": beautify_title(field_name),

            "field": normalized,

            "tooltip": True,

            "headerTooltip": field_name,

            "minWidth": min_width,

            "widthGrow": width_grow,

            "hozAlign": hoz_align,

            "sorter": "string",

            "editor": "list",

            "headerFilter": header_filter,

            "headerFilterParams": header_filter_params,
        }

        # ========================================================
        # FORMAT EMAIL
        # ========================================================

        if (
            "courriel" in normalized
            or
            "email" in normalized
        ):

            col["formatter"] = "emailFormatter"

        # ========================================================
        # FORMAT DATE
        # ========================================================

        if "date" in normalized:

            col["formatter"] = "dateFormatter"

        columns.append(col)



    lecture_seule = not has_access(
        "benevoles",
        "ecriture"
    )

    return render_template(

        "benevoles/benevoles_tabulator.html",

        table_data=table_data,

        grouped_fields=grouped_fields,

        columns=columns,

        lecture_seule=lecture_seule,

        photo_ids=photo_ids,

        fonctions_options=fonctions_options,

        fonctions_filter_selected=fonctions_filter_selected
    )


@benevoles_bp.route("/api/save_fonctions_filter", methods=["POST"])
@login_required
@require_access("benevoles", "lecture")
def save_fonctions_filter():

    data = request.get_json(silent=True) or {}

    fonctions = data.get("fonctions", [])

    if not isinstance(fonctions, list):

        return jsonify({"success": False}), 400

    fonctions = [str(f) for f in fonctions]

    conn = get_db_connection()

    conn.execute(
        "UPDATE users SET fonctions_filter = ? WHERE id = ?",
        (json.dumps(fonctions), current_user.id)
    )

    conn.commit()

    conn.close()

    return jsonify({"success": True})



@benevoles_bp.route("/edition_tableau_benevoles", methods=["GET", "POST"])
@login_required
@require_access("benevoles", "ecriture")
def edition_tableau_benevoles():

    conn = get_db_connection()
    cursor = conn.cursor()

    # Lire les champs disponibles
    fields_data = cursor.execute("""
        SELECT * FROM field_groups
        WHERE appli = 'benevoles'
        ORDER BY display_order
    """).fetchall()

    grouped_fields = {}
    for row in fields_data:
        group = row["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(row)

    # 🔁 Extraire les champs de type oui/non
    oui_non_fields = [row["field_name"] for row in fields_data if row["type_champ"] == "oui_non"]

    # Lire les paramètres (GET ou POST : le formulaire passe en POST pour
    # éviter une ligne de requête trop longue avec beaucoup de bénévoles/colonnes)
    selected_columns = request.values.getlist("columns")
    selected_groups = request.values.getlist("selected_groups")

    benevole_ids = request.values.getlist("benevole_ids")

    # Préparer la requête SQL
    escaped_columns = [f"`{col}`" for col in selected_columns if col not in ['id', 'nom']]
    columns_clause = ", ".join(["id", "nom", "prenom"] + escaped_columns)

    # ============================================================
    # FILTRE SUR IDS VISIBLES
    # ============================================================

    if benevole_ids:

        placeholders = ",".join(["?"] * len(benevole_ids))

        rows = cursor.execute(
            f"""
            SELECT {columns_clause}
            FROM benevoles
            WHERE id IN ({placeholders})
            ORDER BY nom COLLATE NOCASE
            """,
            benevole_ids
        ).fetchall()

    else:

        rows = cursor.execute(
            f"""
            SELECT {columns_clause}
            FROM benevoles
            ORDER BY nom COLLATE NOCASE
            """
        ).fetchall()

    type_benevole_options = get_type_benevole_options(conn)

    conn.close()

    return render_template("benevoles/edition_tableau_benevoles.html",
        benevoles=rows,
        selected_columns=selected_columns,
        grouped_fields=grouped_fields,
        selected_groups=selected_groups,
        oui_non_fields=oui_non_fields,
        type_benevole_options=type_benevole_options
    )




@benevoles_bp.route("/create_benevole", methods=["GET", "POST"])
@login_required
@require_access("benevoles", "ecriture")
def create_benevole():


    conn = get_db_connection()
    cursor = conn.cursor()

    erreurs = []
    champs_invalides = []
    values = {}

    type_benevole_options = get_type_benevole_options(conn)

    # 🔄 On stocke la version brute
    rows = cursor.execute("""
        SELECT * FROM field_groups
        WHERE appli = 'benevoles'
        ORDER BY display_order
    """).fetchall()

    # On transforme les rows en dictionnaires modifiables
    fields_data = [dict(row) for row in rows]

    # Construction initiale de grouped_fields
    grouped_fields = {}
    for field in fields_data:
        group = field["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(field)

    grouped_fields_ordered = {}
    if "coordonnées principales" in grouped_fields:
        grouped_fields_ordered["coordonnées principales"] = grouped_fields["coordonnées principales"]
        for k, v in grouped_fields.items():
            if k != "coordonnées principales":
                grouped_fields_ordered[k] = v
    else:
        grouped_fields_ordered = grouped_fields

    if request.method == 'POST':
        champs_invalides = []
        erreurs = []
        values = {}

        for field in fields_data:
            field_name = field["field_name"]
            value = request.form.get(field_name, "").strip()

            # 🔽 Spécifique: type_benevole -> normalisation liste
            if field_name == "type_benevole":
                opts = get_type_benevole_options(conn)
                coerced = coerce_type_benevole(value, opts)
                if value and coerced is None:
                    erreurs.append(f"Type de bénévole invalide « {value} ». "
                                f"Valeurs possibles : {', '.join(opts)}.")
                    champs_invalides.append("type_benevole")
                value = coerced

            # Pour les oui/non, valeur par défaut
            if field["type_champ"] == "oui_non" and value == "":
                value = "non"

            if field_name == "civilite":
                coerced = coerce_civilite(value)
                if value and coerced is None:
                    erreurs.append(
                        f"Civilité invalide « {value} ». Valeurs possibles : {', '.join(CIVILITE_OPTIONS)}."
                    )
                    champs_invalides.append("civilite")
                value = coerced


            # Validation email / téléphone
            if value:
                if "email" in field_name.lower() and not is_valid_email(value):
                    erreurs.append(f"Champ invalide : {field_name} ➜ « {value} » n’est pas un email valide.")
                    champs_invalides.append(field_name)
                if "tel" in field_name.lower() and not is_valid_phone(value):
                    erreurs.append(f"Champ invalide : {field_name} ➜ « {value} » n’est pas un numéro de téléphone valide.")
                    champs_invalides.append(field_name)

            values[field_name] = None if value in ("", None) else value

        # Retrait champ ID
        values.pop("id", None)

        if not values.get('nom'):
            flash("❌ Le nom du bénévole est obligatoire.", "danger")
            erreurs.append("Champ nom manquant")
            champs_invalides.append("nom")

        if not values.get('prenom'):
            flash("❌ Le prénom du bénévole est obligatoire.", "danger")
            erreurs.append("Champ prénom manquant")
            champs_invalides.append("prenom")

        if erreurs:
            # Réinjecter les valeurs dans les champs
            for field in fields_data:
                fname = field["field_name"]
                field["value"] = request.form.get(fname, "")

            # Reconstruire grouped_fields avec valeurs mises à jour
            grouped_fields = {}
            for field in fields_data:
                group = field["group_name"] or "Autres"
                grouped_fields.setdefault(group, []).append(field)

            grouped_fields_ordered = {}
            if "coordonnées principales" in grouped_fields:
                grouped_fields_ordered["coordonnées principales"] = grouped_fields["coordonnées principales"]
                for k, v in grouped_fields.items():
                    if k != "coordonnées principales":
                        grouped_fields_ordered[k] = v
            else:
                grouped_fields_ordered = grouped_fields

            type_benevole_options = get_type_benevole_options(conn)

            conn.close()

            return render_template("benevoles/create_benevole.html",
                                grouped_fields=grouped_fields_ordered,
                                champs_invalides=champs_invalides,
                                type_benevole_options=type_benevole_options)
        try:
            now = datetime.now()
            values["date_modif"] = now.strftime("%Y-%m-%d")
            values["heure_modif"] = now.strftime("%H:%M:%S")
            values["user_modif"] = current_user.username

            columns = ", ".join([f"`{k}`" for k in values])
            placeholders = ", ".join(["?"] * len(values))
            cursor.execute(
                f"INSERT INTO benevoles ({columns}) VALUES ({placeholders})",
                list(values.values())
            )
            conn.commit()
            upload_database()
            flash("✅ Bénévole créé avec succès.", "success")
            return redirect(url_for("benevoles.benevoles"))

        except Exception as e:
            flash(f"❌ Erreur lors de la création : {e}", "danger")
            conn.close()
            return redirect(url_for("benevoles.create_benevole"))

    # Si GET : initialisation vide

    # Si GET : initialisation des valeurs par défaut
    annee_courante = str(datetime.now().year)

    for field in fields_data:
        fname = field["field_name"]
        if fname == "annee_arrivee_bai":
            field["value"] = annee_courante
        else:
            field["value"] = ""

    type_benevole_options = get_type_benevole_options(conn)

    conn.close()

    return render_template("benevoles/create_benevole.html",
                        grouped_fields=grouped_fields_ordered,
                        champs_invalides=champs_invalides,
                        type_benevole_options=type_benevole_options)



@benevoles_bp.route('/delete_benevole/<int:benevole_id>', methods=['POST'])
@login_required
@require_access("benevoles", "ecriture")
def delete_benevole(benevole_id):

    confirm = request.form.get("confirm_final")
    if confirm != "supprimer":
        flash("❌ Suppression annulée. Confirmation invalide.", "warning")
        return redirect(url_for("benevoles.update_benevole", benevole_id=benevole_id))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM benevoles WHERE id = ?", (benevole_id,))

        now = datetime.now()
        date_modif = now.strftime("%Y-%m-%d")
        heure_modif = now.strftime("%H:%M:%S")
        user_modif = current_user.username

        conn.commit()
        upload_database()
        flash("✅ Bénévole supprimé avec succès.", "success")
    except Exception as e:
        flash(f"Erreur lors de la suppression : {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for("benevoles.benevoles"))

def get_neighbor_benevole_ids_alphabetically(conn, current_id):
    cursor = conn.cursor()
    cursor.execute("SELECT id, nom FROM benevoles ORDER BY nom COLLATE NOCASE")
    rows = cursor.fetchall()
    ids = [row[0] for row in rows]

    try:
        idx = ids.index(current_id)
        previous_id = ids[idx - 1] if idx > 0 else None
        next_id = ids[idx + 1] if idx < len(ids) - 1 else None
    except ValueError:
        previous_id = next_id = None

    return previous_id, next_id




@benevoles_bp.route('/update_benevole/<int:benevole_id>', methods=['GET', 'POST'])
@login_required
@require_access("benevoles", "lecture")
def update_benevole(benevole_id):

    conn = get_db_connection()
    cursor = conn.cursor()

    can_edit = has_access("benevoles", "ecriture")
    can_upload_photo = has_access("photos_benevoles", "ecriture")
    lecture_seule = not can_edit

    benevole = cursor.execute(
        "SELECT * FROM benevoles WHERE id = ?",
        (benevole_id,)
    ).fetchone()

    if not benevole:
        conn.close()
        flash("Bénévole introuvable.", "danger")
        return redirect(url_for("benevoles.benevoles"))

    benevole_dict = dict(benevole)
    previous_id, next_id = get_neighbor_benevole_ids_alphabetically(conn, benevole_id)

    # 🔹 Champs dynamiques
    rows = cursor.execute("""
        SELECT * FROM field_groups
        WHERE appli = 'benevoles'
        ORDER BY display_order
    """).fetchall()

    fields_data = [dict(row) for row in rows]

    for field in fields_data:
        field["value"] = benevole_dict.get(field["field_name"], "")

    grouped_fields = {}
    for field in fields_data:
        group = field.get("group_name") or "Autres"
        grouped_fields.setdefault(group, []).append(field)

    # 🔹 Navigation
    search_term = request.values.get("search", "")
    limit = request.values.get("limit", "10")
    selected_columns = request.values.getlist("columns")
    selected_groups = request.values.getlist("selected_groups")

    query_params = {
        "search": search_term,
        "limit": limit,
        "columns": selected_columns,
        "selected_groups": selected_groups
    }

    next_url = url_for("benevoles.benevoles") + "?" + urlencode(query_params, doseq=True)

    # =========================================================
    # 🔁 POST
    # =========================================================
    if request.method == 'POST':

        if not can_edit and not can_upload_photo:
            abort(403)

        opts_type_bene = get_type_benevole_options(conn)
        do_upload = request.form.get("do_upload", "1")

        photo_dir = os.getenv("PHOTOS_BENEVOLES_DIR", "/srv/ba38/photos_benevoles")
        os.makedirs(photo_dir, exist_ok=True)

        # =====================================================
        # 📸 PHOTO (indépendant du droit édition)
        # =====================================================
        if can_upload_photo:
            photo = request.files.get('photo')
            if photo and photo.filename:
                try:
                    filename = f"{benevole_id}.jpg"
                    full_path = os.path.join(photo_dir, filename)

                    image = Image.open(photo)
                    image.thumbnail((300, 300))
                    image.save(full_path, format="JPEG", quality=80)

                    cursor.execute("""
                        INSERT INTO photos_benevoles (benevole_id, filename)
                        VALUES (?, ?)
                        ON CONFLICT(benevole_id)
                        DO UPDATE SET filename=excluded.filename
                    """, (benevole_id, filename))

                    conn.commit()

                    flash("✅ Photo mise à jour", "success")

                except Exception as e:
                    flash(f"❌ Erreur photo : {e}", "danger")

        # =====================================================
        # ✏️ UPDATE DONNÉES (uniquement si droit)
        # =====================================================
        if can_edit:

            updates = {}

            for field in fields_data:
                field_name = field["field_name"]
                if field_name == "id":
                    continue

                value = request.form.get(field_name, "").strip()

                # type_benevole
                if field_name == "type_benevole":
                    coerced = coerce_type_benevole(value, opts_type_bene)
                    if value and coerced is None:
                        flash(
                            f"Type invalide « {value} » ({', '.join(opts_type_bene)})",
                            "danger"
                        )
                    value = coerced

                # civilité
                if field_name == "civilite":
                    coerced = coerce_civilite(value)
                    if value and coerced is None:
                        flash("Civilité invalide", "danger")
                    value = coerced

                updates[field_name] = None if value == "" else value

            # 🔹 validations
            erreurs = []

            if not updates.get("nom"):
                erreurs.append("Nom obligatoire")

            if not updates.get("prenom"):
                erreurs.append("Prénom obligatoire")

            for field in fields_data:
                fname = field["field_name"]
                val = updates.get(fname)

                if val:
                    if "email" in fname.lower() and not is_valid_email(val):
                        erreurs.append(f"{fname} invalide")

                    if "tel" in fname.lower() and not is_valid_phone(val):
                        erreurs.append(f"{fname} invalide")

            # 🔹 erreurs → retour formulaire
            if erreurs:
                for e in erreurs:
                    flash(e, "danger")

                conn.close()

                return render_template(
                    "benevoles/update_benevole.html",
                    previous_id=previous_id,
                    next_id=next_id,
                    benevole_id=benevole_id,
                    grouped_fields=grouped_fields,
                    next_url=next_url,
                    benevole=benevole_dict,
                    lecture_seule=lecture_seule,
                    can_upload_photo=can_upload_photo
                )

            # 🔹 sauvegarde
            now = datetime.now()

            updates["date_modif"] = now.strftime("%Y-%m-%d")
            updates["heure_modif"] = now.strftime("%H:%M:%S")
            updates["user_modif"] = current_user.username

            set_clause = ", ".join([f"`{k}`=?" for k in updates])
            values = list(updates.values()) + [benevole_id]

            cursor.execute(
                f"UPDATE benevoles SET {set_clause} WHERE id=?",
                values
            )

            conn.commit()

            if do_upload == "1":
                upload_database()

            flash("✅ Bénévole mis à jour", "success")

        conn.close()

        return redirect(
            url_for("benevoles.update_benevole", benevole_id=benevole_id)
        )

    # =========================================================
    # 🔹 GET
    # =========================================================

    photo_filename = None

    photo_path = os.path.join(
        os.getenv("PHOTOS_BENEVOLES_DIR", "/srv/ba38/photos_benevoles"),
        f"{benevole_id}.jpg"
    )

    if os.path.exists(photo_path):
        photo_filename = f"{benevole_id}.jpg"

    conn.close()

    return render_template(
        "benevoles/update_benevole.html",
        previous_id=previous_id,
        next_id=next_id,
        benevole_id=benevole_id,
        grouped_fields=grouped_fields,
        next_url=next_url,
        benevole=benevole_dict,
        photo_filename=photo_filename,
        lecture_seule=lecture_seule,
        can_upload_photo=can_upload_photo
    )

@benevoles_bp.route("/update_benevoles_table", methods=["POST"])
@login_required
@require_access("benevoles", "ecriture")
def update_benevoles_table():
    """
    Met à jour plusieurs bénévoles via un tableau modifiable.

    - Vérifie les droits d’écriture.
    - Applique les modifications uniquement sur les colonnes sélectionnées.
    - Valide les emails et téléphones.
    - Vérifie que le prénom est présent si modifié.
    - Réinjecte les valeurs et messages d'erreurs si problème.
    """




    if not has_access("benevoles", "ecriture"):
        abort(403)

    conn = get_db_connection()
    cursor = conn.cursor()

    opts_type_bene = get_type_benevole_options(conn)

    total = int(request.form.get("total_rows", 0))
    columns = request.form.getlist("columns")

    # ✅ Vérification du nombre de colonnes
    if len(columns) > 40:
        flash("⚠️ Trop de colonnes sélectionnées. Veuillez limiter votre sélection à 40 colonnes maximum.", "danger")
        return redirect(url_for("benevoles.benevoles"))

    erreurs = []
    lignes_modifiees = 0
    benevoles_data = []
    edited_ids = []

    for i in range(total):
        bene_id = request.form.get(f"id_{i}")
        if not bene_id:
            continue

        edited_ids.append(bene_id)

        old_row = cursor.execute("SELECT * FROM benevoles WHERE id = ?", (bene_id,)).fetchone()
        if not old_row:
            continue

        bene_dict = dict(old_row)
        modifications = {}
        champs_invalides = []

        for col in columns:
            old_val = (bene_dict[col] or "").strip() if bene_dict[col] else ""
            new_val = request.form.get(f"{col}_{i}", "").strip()

            if new_val != old_val:
                if col.lower().startswith("email") and new_val and not is_valid_email(new_val):
                    erreurs.append(f"Ligne {i + 1}, champ {col} : adresse email invalide « {new_val} »")
                    champs_invalides.append(col)
                elif col.lower().startswith("tel") and new_val and not is_valid_phone(new_val):
                    erreurs.append(f"Ligne {i + 1}, champ {col} : numéro de téléphone invalide « {new_val} »")
                    champs_invalides.append(col)
                elif col == "type_benevole":
                    nv = coerce_type_benevole(new_val, opts_type_bene)
                    if new_val and nv is None:
                        erreurs.append(
                            f"Ligne {i + 1}, champ {col} : valeur non autorisée « {new_val} ». "
                            f"Valeurs possibles : {', '.join(opts_type_bene)}"
                        )
                        champs_invalides.append(col)
                    else:
                        modifications[col] = nv  # None si vide, sinon valeur canonique
                elif col == "civilite":
                    nv = coerce_civilite(new_val)
                    if new_val and nv is None:
                        erreurs.append(
                            f"Ligne {i+1}, champ civilite : valeur non autorisée « {new_val} »."
                        )
                        champs_invalides.append(col)
                    else:
                        modifications[col] = nv

                else:
                    modifications[col] = new_val if new_val else None

        # Vérification prénom obligatoire si modifié ou supprimé
        if "prenom" in columns:
            prenom_val = request.form.get(f"prenom_{i}", "").strip()
            if not prenom_val:
                erreurs.append(f"Ligne {i + 1} : le prénom est obligatoire.")
                champs_invalides.append("prenom")

        # Enregistrement si pas d’erreurs
        if modifications and not champs_invalides:
            now = datetime.now()
            modifications["date_modif"] = now.strftime("%Y-%m-%d")
            modifications["heure_modif"] = now.strftime("%H:%M:%S")
            modifications["user_modif"] = current_user.username

            set_clause = ", ".join([f"`{k}` = ?" for k in modifications])
            values = list(modifications.values()) + [bene_id]
            cursor.execute(f"UPDATE benevoles SET {set_clause} WHERE id = ?", values)
            lignes_modifiees += 1
        else:
            # 📌 Récupération fiable de nom/prénom même s’ils ne sont pas dans `columns`
            nom_val = request.form.get(f"nom_{i}") or bene_dict.get("nom", "")
            prenom_val = request.form.get(f"prenom_{i}") or bene_dict.get("prenom", "")

            row_data = {
                "id": bene_id,
                "champs_invalides": champs_invalides,
                "valeurs": {col: request.form.get(f"{col}_{i}", "").strip() for col in columns},
                "nom": nom_val,
                "prenom": prenom_val
            }
            benevoles_data.append(row_data)

    conn.commit()
    conn.close()

    if lignes_modifiees > 0:
        upload_database()
        flash(f"✅ {lignes_modifiees} ligne(s) modifiée(s) avec succès.", "success")

    if erreurs:
        for msg in erreurs:
            flash(f"❌ {msg}", "danger")

        # 🔁 Rechargement config des champs pour les listes déroulantes
        conn = get_db_connection()
        cursor = conn.cursor()
        field_config = cursor.execute("""
            SELECT field_name, type_champ FROM field_groups
            WHERE appli = 'benevoles'
        """).fetchall()

        type_benevole_options = get_type_benevole_options(conn)

        conn.close()

        oui_non_fields = [row["field_name"] for row in field_config if row["type_champ"] == "oui_non"]

        return render_template(
            "benevoles/edition_tableau_benevoles.html",
            benevoles=benevoles_data,
            selected_columns=columns,
            oui_non_fields=oui_non_fields,
            type_benevole_options=type_benevole_options
        )

    if lignes_modifiees == 0:
        flash("ℹ️ Aucune modification détectée.", "info")

    # Repost en POST plutôt qu'un redirect GET classique : avec beaucoup de
    # bénévoles filtrés, l'URL générée (benevole_ids + columns) peut dépasser
    # la taille de ligne de requête autorisée (même bug déjà corrigé côté
    # partenaires/edition_tableau_associations).
    return render_template_string(
        """
        <form id="repost-edition-tableau" method="POST"
              action="{{ url_for('benevoles.edition_tableau_benevoles') }}">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            {% for col in columns %}
            <input type="hidden" name="columns" value="{{ col }}">
            {% endfor %}
            {% for bid in benevole_ids %}
            <input type="hidden" name="benevole_ids" value="{{ bid }}">
            {% endfor %}
        </form>
        <script>document.getElementById('repost-edition-tableau').submit();</script>
        """,
        columns=columns,
        benevole_ids=edited_ids
    )




@benevoles_bp.route('/photo_benevole_mobile', methods=['GET', 'POST'])
@login_required
@require_access("benevoles", "ecriture")
def photo_benevole_mobile():
    conn = get_db_connection()
    cursor = conn.cursor()
    tous_les_benevoles = cursor.execute("""
        SELECT id, nom, prenom FROM benevoles ORDER BY nom, prenom
    """).fetchall()

    benevole = None
    selected_id = None

    if request.method == "POST":
        selected_id = request.form.get("benevole_id")
        if selected_id:
            row = cursor.execute("""
                SELECT id, nom, prenom FROM benevoles WHERE id = ?
            """, (selected_id,)).fetchone()
            if row:
                benevole = dict(row)
            else:
                flash("❌ Bénévole introuvable", "danger")

    conn.close()
    return render_template("benevoles/photo_benevole_mobile.html", benevole=benevole, benevoles=tous_les_benevoles, selected_id=selected_id)


@benevoles_bp.route('/photos_benevoles/<filename>')
@login_required
def serve_photo_benevole(filename):

    photo_dir = os.getenv("PHOTOS_BENEVOLES_DIR", "/srv/ba38/photos_benevoles")

    return send_from_directory(photo_dir, filename)

@benevoles_bp.route('/upload_photo_benevole/<int:benevole_id>', methods=['POST'])
@login_required
def upload_photo_benevole(benevole_id):

    if not (
        has_access("benevoles", "ecriture")
        or has_access("photos_benevoles", "ecriture")
    ):
        abort(403)

    """
    Upload ou remplace la photo d’un bénévole.
    - Sur mobile : reste sur /photo_benevole_mobile
    - Sur ordinateur : retourne sur la fiche du bénévole
    """
    if 'photo' not in request.files:
        flash("❌ Aucun fichier reçu", "danger")
        return redirect(url_for('benevoles.update_benevole', benevole_id=benevole_id))

    file = request.files['photo']
    if file.filename == '':
        flash("❌ Nom de fichier vide", "danger")
        return redirect(url_for('benevoles.update_benevole', benevole_id=benevole_id))

    try:
        img = Image.open(file.stream)

        # ✅ Corrige orientation EXIF si nécessaire
        try:
            for orientation in ExifTags.TAGS.keys():
                if ExifTags.TAGS[orientation] == 'Orientation':
                    break
            exif = img._getexif()
            if exif and orientation in exif:
                if exif[orientation] == 3:
                    img = img.rotate(180, expand=True)
                elif exif[orientation] == 6:
                    img = img.rotate(270, expand=True)
                elif exif[orientation] == 8:
                    img = img.rotate(90, expand=True)
        except Exception as e:
            print(f"⚠️ Impossible d’appliquer l’orientation EXIF : {e}")

        # ✅ Conversion et redimensionnement
        img = img.convert("RGB")
        img.thumbnail((400, 400))

        # ✅ Détermine le bon répertoire
        photo_dir = os.getenv("PHOTOS_BENEVOLES_DIR", "/srv/ba38/photos_benevoles")
        os.makedirs(photo_dir, exist_ok=True)

        # ✅ Sauvegarde du fichier
        save_path = os.path.join(photo_dir, f"{benevole_id}.jpg")
        img.info.pop('exif', None)
        img.save(save_path, "JPEG", quality=85)

        conn = get_db_connection()

        now = datetime.now()

        conn.execute("""
            UPDATE benevoles
            SET date_modif = ?, heure_modif = ?, user_modif = ?
            WHERE id = ?
        """, (
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M:%S"),
            current_user.username,
            benevole_id
        ))

        conn.commit()
        conn.close()

        flash("✅ Photo enregistrée avec succès", "success")

    except Exception as e:
        flash(f"❌ Erreur lors de l'enregistrement : {e}", "danger")

    # 🔁 Détecte si l’utilisateur est sur mobile
    user_agent = request.headers.get('User-Agent', '').lower()
    is_mobile = any(m in user_agent for m in ['iphone', 'android', 'ipad'])

    if is_mobile:
        return redirect(url_for('benevoles.photo_benevole_mobile'))
    else:
        # Retour à la fiche bénévole sur PC
        return redirect(url_for('benevoles.update_benevole', benevole_id=benevole_id))



@benevoles_bp.route("/desactiver_benevole/<int:benevole_id>", methods=["GET", "POST"])
@login_required
@require_access("benevoles", "ecriture")
def desactiver_benevole(benevole_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    benevole = cursor.execute("SELECT * FROM benevoles WHERE id = ?", (benevole_id,)).fetchone()

    if not benevole:
        flash("⛔ Bénévole introuvable.", "danger")
        return redirect(url_for("benevoles.benevoles"))

    if request.method == "POST":
        motif = request.form.get("motif", "").strip()
        if not motif:
            flash("⛔ Le motif est obligatoire.", "danger")
            return render_template("benevoles/desactiver_benevole.html", benevole=benevole)

        now = datetime.now().strftime("%Y-%m-%d")

        try:
            # 🔹 Colonnes des deux tables
            cols_bene = {
                row["name"]
                for row in cursor.execute("PRAGMA table_info(benevoles)").fetchall()
            }
            cols_inactifs = {
                row["name"]
                for row in cursor.execute("PRAGMA table_info(benevoles_inactifs)").fetchall()
            }

            # ✅ Colonnes communes
            colonnes = sorted(cols_bene & cols_inactifs)

            # 🔹 Préparer valeurs communes
            colonnes_sql = ", ".join(f"`{c}`" for c in colonnes)
            placeholders = ", ".join(["?"] * len(colonnes))
            valeurs = [benevole[c] for c in colonnes]

            # 🔹 Ajouter les champs d’archivage s’ils existent
            extra_cols = []
            extra_vals = []

            if "motif_inactivite" in cols_inactifs:
                extra_cols.append("motif_inactivite")
                extra_vals.append(motif)

            if "date_desactivation" in cols_inactifs:
                extra_cols.append("date_desactivation")
                extra_vals.append(now)

            if extra_cols:
                colonnes_sql += ", " + ", ".join(extra_cols)
                placeholders += ", " + ", ".join(["?"] * len(extra_cols))
                valeurs.extend(extra_vals)

            cursor.execute(
                f"INSERT INTO benevoles_inactifs ({colonnes_sql}) VALUES ({placeholders})",
                valeurs
            )

            cursor.execute("DELETE FROM benevoles WHERE id = ?", (benevole_id,))
            conn.commit()

            flash("✅ Bénévole désactivé et archivé avec succès.", "success")
            return redirect(url_for("benevoles.benevoles"))

        except Exception as e:
            conn.rollback()
            flash(f"❌ Erreur lors de la désactivation : {e}", "danger")
            return render_template("benevoles/desactiver_benevole.html", benevole=benevole)

    return render_template("benevoles/desactiver_benevole.html", benevole=benevole)

@benevoles_bp.route("/benevoles/inactifs")
@login_required
@require_access("benevoles", "lecture")
def benevoles_archives():
    conn = get_db_connection()
    benevoles = conn.execute("SELECT * FROM benevoles_inactifs ORDER BY nom, prenom").fetchall()
    return render_template("benevoles/benevoles_archives.html", benevoles=benevoles)



@benevoles_bp.route("/restaurer_benevole/<int:benevole_id>", methods=["POST"])
@login_required
@require_access("benevoles", "ecriture")
def restaurer_benevole(benevole_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    benevole = cursor.execute(
        "SELECT * FROM benevoles_inactifs WHERE id = ?",
        (benevole_id,)
    ).fetchone()

    if not benevole:
        flash("⛔ Bénévole introuvable en archive.", "danger")
        return redirect(url_for("benevoles.benevoles_archives"))

    try:
        # 🔹 Colonnes de chaque table
        cols_benevoles = {
            row["name"]
            for row in cursor.execute("PRAGMA table_info(benevoles)").fetchall()
        }
        cols_inactifs = {
            row["name"]
            for row in cursor.execute("PRAGMA table_info(benevoles_inactifs)").fetchall()
        }

        # ✅ Colonnes communes uniquement
        colonnes = sorted(cols_benevoles & cols_inactifs)

        colonnes_sql = ", ".join(f"`{c}`" for c in colonnes)
        placeholders = ", ".join(["?"] * len(colonnes))
        valeurs = [benevole[c] for c in colonnes]

        cursor.execute(
            f"INSERT INTO benevoles ({colonnes_sql}) VALUES ({placeholders})",
            valeurs
        )

        cursor.execute(
            "DELETE FROM benevoles_inactifs WHERE id = ?",
            (benevole_id,)
        )

        conn.commit()
        upload_database()
        flash("✅ Bénévole restauré avec succès.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"❌ Erreur lors de la restauration : {e}", "danger")

    finally:
        conn.close()

    return redirect(url_for("benevoles.benevoles_archives"))

@benevoles_bp.route("/supprimer_definitivement_benevole/<int:benevole_id>", methods=["POST"])
@login_required
@require_access("benevoles", "ecriture")
def supprimer_definitivement_benevole(benevole_id):

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM benevoles_inactifs WHERE id = ?", (benevole_id,))
        conn.commit()
        flash("🗑️ Bénévole supprimé définitivement.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"❌ Erreur lors de la suppression : {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for("benevoles.benevoles_archives"))


@benevoles_bp.route('/supprimer_photo_benevole/<int:benevole_id>', methods=['POST'])
@login_required
@require_access("benevoles", "ecriture")
def supprimer_photo_benevole(benevole_id):
    """Supprime la photo du bénévole (fichier et enregistrement DB)"""
    try:
        environment = os.getenv("ENVIRONMENT", "dev")
        BASE_DIR = os.getenv("BA38_BASE_DIR", "/srv/ba38")
        base_dir = os.path.join(BASE_DIR, "prod" if environment == "prod" else "dev")
        photo_path = os.path.join(os.getenv("PHOTOS_BENEVOLES_DIR", "/srv/ba38/photos_benevoles"), f"{benevole_id}.jpg")

        # Supprimer le fichier s'il existe
        if os.path.exists(photo_path):
            os.remove(photo_path)

        # Supprimer aussi l’entrée éventuelle dans la table photos_benevoles
        conn = get_db_connection()
        conn.execute("DELETE FROM photos_benevoles WHERE benevole_id = ?", (benevole_id,))
        conn.commit()
        conn.close()

        flash("🗑️ Photo supprimée avec succès.", "success")
    except Exception as e:
        flash(f"❌ Erreur lors de la suppression de la photo : {e}", "danger")

    # Après suppression → retour à la fiche bénévole
    return redirect(url_for("benevoles.update_benevole", benevole_id=benevole_id))



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



@benevoles_bp.route("/envoi_mail_benevoles", methods=["GET", "POST"])
@login_required
@require_access("benevoles", "lecture")
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
        "benevoles/envoi_mail_benevoles.html",
        all_fonctions=all_fonctions,
        selected_fonctions=selected_fonctions,
        destinataires=destinataires,
        messages=messages,
        gmail_url=gmail_url,
        retour_url=retour_url,
        user_email=user_email  # ✅ maintenant bien défini
    )


@benevoles_bp.route("/messages_predefinis_benevoles", methods=["GET", "POST"])
@login_required
@require_access("benevoles", "lecture")
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
            return redirect(url_for("benevoles.messages_predefinis_benevoles"))

        conn = get_db_connection()
        conn.execute(
            "INSERT INTO messages_predefinis (titre, contenu) VALUES (?, ?)",
            (titre, contenu)
        )
        conn.commit()
        conn.close()
        upload_database()
        flash("✅ Modèle ajouté.", "success")
        return redirect(url_for("benevoles.messages_predefinis_benevoles"))

    messages = _charger_messages()
    return render_template("benevoles/messages_predefinis_benevoles.html", messages=messages)


@benevoles_bp.route("/edit_message_bene/<int:mid>", methods=["GET", "POST"])
@login_required
@require_access("benevoles", "lecture")
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
            return redirect(url_for("benevoles.edit_message_bene", mid=mid))

        conn.execute(
            "UPDATE messages_predefinis SET titre = ?, contenu = ? WHERE id = ?",
            (titre, contenu, mid)
        )
        conn.commit()
        conn.close()
        upload_database()
        flash("✅ Modèle mis à jour.", "success")
        return redirect(url_for("benevoles.messages_predefinis_benevoles"))

    row = conn.execute(
        "SELECT id, titre, contenu FROM messages_predefinis WHERE id = ?",
        (mid,)
    ).fetchone()
    conn.close()

    if not row:
        flash("❌ Modèle introuvable.", "danger")
        return redirect(url_for("benevoles.messages_predefinis_benevoles"))

    return render_template(
        "benevoles/messages_predefinis_benevoles.html",
        messages=[dict(row)],  # on affiche juste celui-ci en haut, réutilisation simple
        edit_mode=True,
        edit_id=row["id"],
        edit_titre=row["titre"],
        edit_contenu=row["contenu"]
    )


@benevoles_bp.route("/delete_message_bene/<int:mid>", methods=["POST"])
@login_required
@require_access("benevoles", "ecriture")
def delete_message_bene(mid):
    """Suppression d’un modèle bénévole."""
    conn = get_db_connection()
    conn.execute("DELETE FROM messages_predefinis WHERE id = ?", (mid,))
    conn.commit()
    conn.close()
    upload_database()
    flash("🗑️ Modèle supprimé.", "warning")
    return redirect(url_for("benevoles.messages_predefinis_benevoles"))


def beautify_title(name):

    return (
        name
        .replace("_", " ")
        .replace("-", " ")
        .strip()
        .title()
    )