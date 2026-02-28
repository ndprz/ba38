import os
import re
import sqlite3
import unicodedata

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from flask_login import login_required, current_user
from utils import get_db_connection, upload_database, write_log, has_access, is_valid_email, is_valid_phone
from werkzeug.security import generate_password_hash
from PIL import Image, ExifTags
from urllib.parse import urlencode
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



@benevoles_bp.route('/benevoles', methods=['GET'])
@login_required
def benevoles():
    """
    Affiche la liste des bénévoles avec sélection dynamique des colonnes
    et persistance du champ de recherche (comme partenaires).
    """
    if not has_access("benevoles", "lecture"):
        flash("⛔ Accès refusé à la gestion des bénévoles", "danger")
        return redirect(url_for("index"))

    # 📌 Redirection si mobile vers la prise de photo
    user_agent = request.headers.get('User-Agent', '').lower()
    if any(mobile in user_agent for mobile in ["iphone", "android"]):
        return redirect(url_for('benevoles.photo_benevole_mobile'))

    conn = get_db_connection()
    cursor = conn.cursor()

    # 🔹 Récupérer les groupes de champs
    fields_data = cursor.execute("""
        SELECT * FROM field_groups
        WHERE appli = 'benevoles'
        ORDER BY display_order
    """).fetchall()

    # Ne garder que les champs affichables
    fields_data = [f for f in fields_data if f["display_order"] and int(f["display_order"]) > 0]

    # Regrouper par familles
    grouped_fields = {}
    for row in fields_data:
        group = row["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(row)

    # 🔹 Récupération paramètres GET
    selected_columns = request.args.getlist("columns")
    selected_groups = request.args.getlist("selected_groups")
    has_interacted = request.args.get("has_interacted") == "1"
    search_term = request.args.get("search_term", "").strip()  # ✅ persistance recherche

    # Mode TEST → pré-sélectionner coordonnées principales
    test_mode = session.get("test_user", False)
    if test_mode and not selected_columns and not has_interacted:
        selected_groups = [g for g in grouped_fields if g.lower().startswith("coordonnées principales")]
        selected_columns = []
        seen = set()
        for group in selected_groups:
            for field in grouped_fields[group]:
                fname = field["field_name"]
                if fname not in ['id', 'nom'] and fname not in seen:
                    selected_columns.append(fname)
                    seen.add(fname)

    # Si rien de sélectionné, valeur par défaut
    if not selected_columns:
        selected_columns = ["id", "nom"]

    # Compatibilité paramètres CSV
    if len(selected_columns) == 1 and ',' in selected_columns[0]:
        selected_columns = selected_columns[0].split(',')
    if len(selected_groups) == 1 and ',' in selected_groups[0]:
        selected_groups = selected_groups[0].split(',')

    # Pas d’interaction → colonnes par défaut coordonnées principales
    if not has_interacted and not test_mode and not selected_columns:
        selected_groups = [g for g in grouped_fields if g.lower().startswith("coordonnées principales")]
        selected_columns = []
        seen = set()
        for group in selected_groups:
            for field in grouped_fields[group]:
                fname = field["field_name"]
                if fname not in ['id', 'nom'] and fname not in seen:
                    selected_columns.append(fname)
                    seen.add(fname)

    # Supprimer doublons et champs non désirés
    selected_columns = [c for i, c in enumerate(selected_columns) if c not in ['id'] and c not in selected_columns[:i]]

    # Construire la clause SQL
    escaped_columns = [f"`{col}`" for col in selected_columns if col not in ['id', 'nom']]
    columns_clause = ", ".join(["id", "`nom`"] + escaped_columns)

    # 🔎 Une seule requête complète
    rows_full = cursor.execute("""
        SELECT *
        FROM benevoles
        ORDER BY nom COLLATE NOCASE
    """).fetchall()

    conn.close()

    EXCLUDED_SEARCH_FIELDS = {
        "user_modif",
        "date_modif",
        "id"
    }

    def normalize_text(text: str) -> str:
        if not text:
            return ""
        text = str(text).lower()
        text = unicodedata.normalize("NFD", text)
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        return text

    rows = []

    for row in rows_full:
        row_dict = dict(row)

        search_blob_raw = " ".join(
            str(value or "")
            for key, value in row_dict.items()
            if key not in EXCLUDED_SEARCH_FIELDS
        )

        search_blob = normalize_text(search_blob_raw)

        display_dict = {
            "id": row_dict.get("id"),
            "nom": row_dict.get("nom")
        }

        for col in selected_columns:
            if col not in ["id", "nom"]:
                display_dict[col] = row_dict.get(col)

        rows.append({
            "display": display_dict,
            "search_blob": search_blob
        })

    # Photos disponibles
    photo_dir = os.path.join(os.path.dirname(__file__), "static", "photos_benevoles")
    photo_ids = set()
    if os.path.isdir(photo_dir):
        for filename in os.listdir(photo_dir):
            if filename.endswith(".jpg"):
                try:
                    bene_id = int(filename.split(".")[0])
                    photo_ids.add(bene_id)
                except ValueError:
                    continue

    # Droits utilisateur
    user_role = current_user.role.lower()
    lecture_seule = not has_access("benevoles", "ecriture")

    # Forcer coordonnées principales en premier
    grouped_fields_ordered = {}
    if "coordonnées principales" in grouped_fields:
        grouped_fields_ordered["coordonnées principales"] = grouped_fields["coordonnées principales"]
        for k, v in grouped_fields.items():
            if k != "coordonnées principales":
                grouped_fields_ordered[k] = v
    else:
        grouped_fields_ordered = grouped_fields

    return render_template(
        "benevoles.html",
        benevoles=rows,
        grouped_fields=grouped_fields_ordered,
        selected_columns=selected_columns,
        selected_groups=selected_groups,
        user_role=user_role,
        lecture_seule=lecture_seule,
        photo_ids=photo_ids,
        search_term=search_term  # ✅ pour préremplir le champ de recherche
    )



@benevoles_bp.route("/edition_tableau_benevoles")
@login_required
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

    # Lire les paramètres
    selected_columns = request.args.getlist("columns")
    selected_groups = request.args.getlist("selected_groups")

    # Préparer la requête SQL
    escaped_columns = [f"`{col}`" for col in selected_columns if col not in ['id', 'nom']]
    columns_clause = ", ".join(["id", "nom", "prenom"] + escaped_columns)

    rows = cursor.execute(f"SELECT {columns_clause} FROM benevoles ORDER BY nom COLLATE NOCASE").fetchall()

    type_benevole_options = get_type_benevole_options(conn)

    conn.close()

    return render_template("edition_tableau_benevoles.html",
        benevoles=rows,
        selected_columns=selected_columns,
        grouped_fields=grouped_fields,
        selected_groups=selected_groups,
        oui_non_fields=oui_non_fields,
        type_benevole_options=type_benevole_options
    )




@benevoles_bp.route("/create_benevole", methods=["GET", "POST"])
@login_required
def create_benevole():
    if not has_access("benevoles", "ecriture"):
        flash("⛔ Accès refusé : modification non autorisée.", "danger")
        return redirect(url_for("benevoles.benevoles"))


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

            return render_template("create_benevole.html",
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

    return render_template("create_benevole.html",
                        grouped_fields=grouped_fields_ordered,
                        champs_invalides=champs_invalides,
                        type_benevole_options=type_benevole_options)



@benevoles_bp.route('/delete_benevole/<int:benevole_id>', methods=['POST'])
@login_required
def delete_benevole(benevole_id):
    if not has_access("benevoles", "ecriture"):
        flash("⛔ Accès refusé : modification non autorisée.", "danger")
        return redirect(url_for("benevoles.benevoles"))

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
def update_benevole(benevole_id):

    lecture_seule = not has_access("benevoles", "ecriture")  # Ajouté

    conn = get_db_connection()
    cursor = conn.cursor()

    benevole = cursor.execute("SELECT * FROM benevoles WHERE id = ?", (benevole_id,)).fetchone()
    if not benevole:
        conn.close()
        flash("Bénévole introuvable.", "danger")
        return redirect(url_for("benevoles.benevoles"))

    benevole_dict = dict(benevole)
    previous_id, next_id = get_neighbor_benevole_ids_alphabetically(conn, benevole_id)

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

    if request.method == 'POST':
        opts_type_bene = get_type_benevole_options(conn)
        do_upload = request.form.get("do_upload", "1")
        PHOTO_DIR = os.path.join(os.path.dirname(__file__), "static", "photos_benevoles")
        os.makedirs(PHOTO_DIR, exist_ok=True)

        # 📸 Photo
        photo = request.files.get('photo')
        if photo and photo.filename:
            try:
                filename = f"{benevole_id}.jpg"
                full_path = os.path.join(PHOTO_DIR, filename)
                image = Image.open(photo)
                image.thumbnail((300, 300))
                image.save(full_path, format="JPEG", quality=80)
                cursor.execute("""
                    INSERT INTO photos_benevoles (benevole_id, filename)
                    VALUES (?, ?)
                    ON CONFLICT(benevole_id) DO UPDATE SET filename=excluded.filename
                """, (benevole_id, filename))
            except Exception as e:
                flash(f"❌ Erreur lors du traitement de la photo : {e}", "danger")

        updates = {}
        for field in fields_data:
            field_name = field["field_name"]
            if field_name == "id":
                continue
            value = request.form.get(field_name, "").strip()

            # 🔽 Spécifique: type_benevole -> normalisation liste
            if field_name == "type_benevole":
                coerced = coerce_type_benevole(value, opts_type_bene)
                if value and coerced is None:
                    # on marque l'erreur maintenant (plus fiable que plus bas)
                    flash(f"Type de bénévole invalide « {value} ». "
                        f"Valeurs possibles : {', '.join(opts_type_bene)}.", "danger")
                    # réaffichage du formulaire avec erreurs géré plus bas
                value = coerced

            if field_name == "civilite":
                coerced = coerce_civilite(value)
                if value and coerced is None:
                    flash(
                        f"Civilité invalide « {value} ». Valeurs possibles : {', '.join(CIVILITE_OPTIONS)}.",
                        "danger"
                    )
                value = coerced


            updates[field_name] = None if value == "" or value is None else value

        if not updates.get("nom"):
            flash("❌ Le nom du bénévole est obligatoire.", "danger")
        if not updates.get("prenom"):
            flash("❌ Le prénom du bénévole est obligatoire.", "danger")

        erreurs = []
        champs_invalides = []
        for field in fields_data:
            fname = field["field_name"]
            val = updates.get(fname)
            group = field.get("group_name") or "Autres"
            label = fname.replace("_", " ").capitalize()

            if val:
                if "email" in fname.lower() and not is_valid_email(val):
                    erreurs.append(f"Champ invalide dans {group} : {label} ➜ « {val} » n’est pas un email valide.")
                    champs_invalides.append(fname)
                if "tel" in fname.lower() and not is_valid_phone(val):
                    erreurs.append(f"Champ invalide dans {group} : {label} ➜ « {val} » n’est pas un numéro de téléphone valide.")
                    champs_invalides.append(fname)
                if fname == "type_benevole" and (val is not None) and (val not in opts_type_bene):
                    erreurs.append(f"Champ invalide dans {group} : Type de bénévole ➜ « {val} ». "
                                f"Valeurs possibles : {', '.join(opts_type_bene)}.")
                    champs_invalides.append(fname)


        if not updates.get("nom") or not updates.get("prenom") or erreurs:
            for msg in erreurs:
                flash(msg, "danger")

            # 🔁 Réinjecter les valeurs saisies dans field["value"]
            for field in fields_data:
                field["value"] = request.form.get(field["field_name"], "")

            # 🔁 Reconstruire grouped_fields
            grouped_fields = {}
            for field in fields_data:
                group = field.get("group_name") or "Autres"
                grouped_fields.setdefault(group, []).append(field)

            type_benevole_options = get_type_benevole_options(conn)

            conn.close()

            return render_template(
                "update_benevole.html",
                previous_id=previous_id,
                next_id=next_id,
                benevole_id=benevole_id,
                grouped_fields=grouped_fields,
                next_url=next_url,
                benevole_nom=updates.get("nom", ""),
                benevole_prenom=updates.get("prenom", ""),
                photo_filename=None,
                benevole=benevole_dict,
                champs_invalides=champs_invalides,
                type_benevole_options=type_benevole_options,
                lecture_seule=lecture_seule
            )

        # Détection des modifications
        modif_detectee = any(
            (benevole_dict.get(k) or "").strip() != (v or "").strip()
            for k, v in updates.items()
        )

        if modif_detectee:
            if do_upload == "1":
                now = datetime.now()
                updates["date_modif"] = now.strftime("%Y-%m-%d")
                updates["heure_modif"] = now.strftime("%H:%M:%S")
                updates["user_modif"] = current_user.username

            set_clause = ", ".join([f"`{k}` = ?" for k in updates])
            values = list(updates.values()) + [benevole_id]
            try:
                cursor.execute(f"UPDATE benevoles SET {set_clause} WHERE id = ?", values)
                conn.commit()
                if do_upload == "1":
                    upload_database()
                flash("✅ Bénévole mis à jour avec succès.", "success")
            except Exception as e:
                flash(f"❌ Erreur lors de la mise à jour : {e}", "danger")
        else:
            flash("ℹ️ Aucune modification détectée. Rien n’a été enregistré.", "info")

        conn.close()
        go_to = request.form.get("go_to")
        if go_to:
            return redirect(go_to)

        return redirect(url_for("benevoles.update_benevole", benevole_id=benevole_id, **request.args))

    benevole_nom = benevole_dict.get("nom", "")
    benevole_prenom = benevole_dict.get("prenom", "")

    photo_filename = None
    photo_folder = os.path.join(os.path.dirname(__file__), "static", "photos_benevoles")
    photo_path = os.path.join(photo_folder, f"{benevole_id}.jpg")
    if os.path.exists(photo_path):
        photo_filename = f"{benevole_id}.jpg"
    else:
        row = conn.execute("SELECT filename FROM photos_benevoles WHERE benevole_id = ?", (benevole_id,)).fetchone()
        if row:
            photo_filename = row["filename"]

    type_benevole_options = get_type_benevole_options(conn)

    # 🕒 Conversion date_modif en heure FR
    date_modif = benevole_dict.get("date_modif")
    heure_fr = None
    if date_modif:
        try:
            dt = datetime.strptime(date_modif, "%Y-%m-%d %H:%M:%S")
            import pytz
            dt = pytz.utc.localize(dt).astimezone(pytz.timezone("Europe/Paris"))
            heure_fr = dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            heure_fr = date_modif


    conn.close()

    return render_template(
        "update_benevole.html",
        previous_id=previous_id,
        next_id=next_id,
        benevole_id=benevole_id,
        grouped_fields=grouped_fields,
        next_url=next_url,
        benevole_nom=benevole_nom,
        benevole_prenom=benevole_prenom,
        photo_filename=photo_filename,
        benevole=benevole_dict,
        date_modif=heure_fr,
        user_modif=benevole_dict.get("user_modif", ""),
        champs_invalides=[],
        type_benevole_options=type_benevole_options,
        lecture_seule=lecture_seule
    )


@benevoles_bp.route("/update_benevoles_table", methods=["POST"])
@login_required
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
        flash("⛔ Accès refusé : modification non autorisée.", "danger")
        return redirect(url_for("benevoles.benevoles"))

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

    for i in range(total):
        bene_id = request.form.get(f"id_{i}")
        if not bene_id:
            continue

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
            "edition_tableau_benevoles.html",
            benevoles=benevoles_data,
            selected_columns=columns,
            oui_non_fields=oui_non_fields,
            type_benevole_options=type_benevole_options
        )

    if lignes_modifiees == 0:
        flash("ℹ️ Aucune modification détectée.", "info")

    return redirect(
        url_for(
            "benevoles.edition_tableau_benevoles",
            **request.args,
            columns=columns
        )
    )




@benevoles_bp.route('/photo_benevole_mobile', methods=['GET', 'POST'])
@login_required
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
    return render_template("photo_benevole_mobile.html", benevole=benevole, benevoles=tous_les_benevoles, selected_id=selected_id)



@benevoles_bp.route('/upload_photo_benevole/<int:benevole_id>', methods=['POST'])
@login_required
def upload_photo_benevole(benevole_id):
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
        environment = os.getenv("ENVIRONMENT", "dev")
        base_dir = "/home/ndprz/ba380" if environment == "prod" else "/home/ndprz/dev"
        static_dir = os.path.join(base_dir, "static", "photos_benevoles")
        os.makedirs(static_dir, exist_ok=True)

        # ✅ Sauvegarde du fichier
        save_path = os.path.join(static_dir, f"{benevole_id}.jpg")
        img.info.pop('exif', None)
        img.save(save_path, "JPEG", quality=85)

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
            return render_template("desactiver_benevole.html", benevole=benevole)

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
            return render_template("desactiver_benevole.html", benevole=benevole)

    return render_template("desactiver_benevole.html", benevole=benevole)

@benevoles_bp.route("/benevoles/inactifs")
@login_required
def benevoles_archives():
    conn = get_db_connection()
    benevoles = conn.execute("SELECT * FROM benevoles_inactifs ORDER BY nom, prenom").fetchall()
    return render_template("benevoles_archives.html", benevoles=benevoles)



@benevoles_bp.route("/restaurer_benevole/<int:benevole_id>", methods=["POST"])
@login_required
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
def supprimer_definitivement_benevole(benevole_id):
    if not has_access("benevoles", "ecriture"):
        flash("⛔ Accès refusé : suppression non autorisée.", "danger")
        return redirect(url_for("benevoles.benevoles_archives"))

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
def supprimer_photo_benevole(benevole_id):
    """Supprime la photo du bénévole (fichier et enregistrement DB)"""
    try:
        environment = os.getenv("ENVIRONMENT", "dev")
        base_dir = "/home/ndprz/ba380" if environment == "prod" else "/home/ndprz/dev"
        photo_path = os.path.join(base_dir, "static", "photos_benevoles", f"{benevole_id}.jpg")

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
