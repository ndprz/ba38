import os
import sqlite3
import base64
import re
import subprocess
import sys
import unicodedata
import json

from flask import Blueprint, render_template, render_template_string, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from ba38_utilitaires.core import get_db_connection, upload_database, has_access, write_log, is_valid_email, is_valid_multi_email, is_valid_phone, require_access, get_db_path
from ba38_utilitaires.organisation import get_organisation
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


def checkbox(checked=False):
    """Retourne une case à cocher en texte."""
    return "☑" if checked else "☐"

def _champ_email_valide(fname, val):
    """
    Valide un champ email : courriel_association accepte plusieurs adresses
    séparées par ';' (ex: "a@x.fr;b@y.fr"), les autres champs email restent
    limités à une seule adresse.
    """
    if (fname or "").lower() == "courriel_association":
        return is_valid_multi_email(val)
    return is_valid_email(val)

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

@partenaires_bp.route("/partenaires")
@login_required
@require_access("associations", "lecture")
def partenaires():

    return redirect(
        url_for('partenaires.partenaires_tabulator')
    )


# # ============================================================
# # ROUTE TABULATOR
# # ============================================================

@partenaires_bp.route("/partenaires_tabulator")
@login_required
@require_access("associations", "lecture")
def partenaires_tabulator():

    db_path = get_db_path()

    voir_non_valides = request.args.get("voir_non_valides")

    voir_toutes = request.args.get("voir_toutes") == "1"

    user_role = (current_user.role or "").lower()

    is_car = user_role == "car"

    car_value = current_user.username

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # ============================================================
        # FILTRE VALIDITE
        # ============================================================

        if voir_non_valides:

            validite_clause = """
                (
                    validite IS NULL
                    OR TRIM(validite) = ''
                    OR LOWER(TRIM(validite)) != 'oui'
                )
            """

        else:

            validite_clause = """
                LOWER(TRIM(COALESCE(validite, ''))) = 'oui'
            """

        # ============================================================
        # FILTRE CAR
        # ============================================================

        if is_car and not voir_toutes:

            query = f"""
                SELECT *
                FROM associations
                WHERE {validite_clause}
                AND LOWER(TRIM(COALESCE(car, ''))) = LOWER(TRIM(?))
                ORDER BY nom_association
            """

            write_log(
                f"[CAR FILTER] user={current_user.username} "
                f"role={current_user.role} "
                f"is_car={is_car} "
                f"voir_toutes={voir_toutes}"
            )

            cursor.execute(query, (car_value,))

        else:

            query = f"""
                SELECT *
                FROM associations
                WHERE {validite_clause}
                ORDER BY nom_association
            """

            cursor.execute(query)

        rows = cursor.fetchall()

        write_log(
            f"[CAR FILTER] nb associations={len(rows)}"
        )

        cursor.execute("""
            SELECT *
            FROM field_groups
            WHERE appli = 'associations'
            ORDER BY
                CASE
                    WHEN LOWER(group_name) = 'coordonnées principales'
                    THEN 0
                    ELSE 1
                END,
                group_name COLLATE NOCASE,
                display_order
        """)

        fields = cursor.fetchall()

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
    # DATA
    # ============================================================

    table_data = []

    for row in rows:

        d = {}

        for key in row.keys():

            d[normalize(key)] = row[key]

        table_data.append(d)

    # ============================================================
    # COLONNES TABULATOR
    # ============================================================

    columns = [

        {
            "title": "ID",
            "field": "id",
            "minWidth": 80,
            "widthGrow": 1,
            "frozen": True,
            "hozAlign": "center"
        },

        {
            "title": "Nom",
            "field": "nom_association",
            "tooltip": True,
            "minWidth": 320,
            "widthGrow": 4,
            "frozen": True
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
            "nom_association"
        ]:
            continue

        # ========================================================
        # TYPE CHAMP
        # ========================================================

        type_champ = (
            field["type_champ"] or ""
        ).lower()

        # ========================================================
        # CONFIG FILTRE
        # ========================================================

        header_filter = "list"

        header_filter_params = {

            "valuesLookup": "active",

            "clearable": True,

            "autocomplete": True,

            "sort": "asc"
        }

        header_filter_func = "="

        # ========================================================
        # LARGEURS DYNAMIQUES
        # ========================================================

        min_width = 125

        width_grow = 1

        hoz_align = "left"

        # ========================================================
        # OUI / NON
        # ========================================================

        if type_champ == "oui_non":

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
        # CODES
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
        # ADRESSES / COMMENTAIRES
        # ========================================================

        elif (
            "adresse" in normalized
            or
            "commentaire" in normalized
            or
            "observation" in normalized
        ):

            min_width = 320

            width_grow = 3

        # ========================================================
        # DRIVE LINK
        # ========================================================

        if normalized == "drive_link":

            columns.append({

                "title": "Drive",

                "field": normalized,

                "tooltip": True,

                "minWidth": 140,

                "widthGrow": 1,

                "hozAlign": "center",

                "formatter": "link",

                "formatterParams": {

                    "label": "📁 Dossier",

                    "target": "_blank"
                }
            })

            continue

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

            "headerFilter": header_filter,

            "headerFilterParams": header_filter_params,

            "headerFilterFunc": header_filter_func
        }

        # ========================================================
        # EMAIL FORMATTER
        # ========================================================

        if (
            "courriel" in normalized
            or
            "email" in normalized
        ):

            col["formatter"] = "emailFormatter"

        columns.append(col)

    return render_template(
        "partenaires/partenaires_tabulator.html",
        table_data=table_data,
        grouped_fields=grouped_fields,
        columns=columns,
        voir_non_valides=voir_non_valides
    )


# ============================================================
# SAUVEGARDE MANUELLE VERS DRIVE
# ============================================================

@partenaires_bp.route("/backup_manuel", methods=["POST"])
@login_required
@require_access("associations", "lecture")
def backup_manuel():
    """
    Déclenche immédiatement le même script que le cron horaire
    (backup_db_to_drive.py) plutôt que d'attendre la prochaine
    exécution planifiée.
    """

    script_path = "/srv/ba38/scripts_taches/backup_db_to_drive.py"
    python_path = "/srv/ba38/prod/venv/bin/python"

    try:
        result = subprocess.run(
            [python_path, script_path],
            capture_output=True,
            text=True,
            timeout=300
        )

        success = result.returncode == 0

        write_log(
            f"{'✅' if success else '❌'} Sauvegarde manuelle Drive "
            f"déclenchée par {getattr(current_user, 'username', 'inconnu')}"
        )

        return jsonify({
            "success": success,
            "message": (
                "✅ Sauvegarde envoyée sur Drive avec succès."
                if success else
                "❌ Échec de la sauvegarde. Consultez les logs."
            )
        })

    except subprocess.TimeoutExpired:
        write_log("❌ Sauvegarde manuelle Drive : timeout")
        return jsonify({
            "success": False,
            "message": "❌ La sauvegarde a dépassé le délai autorisé."
        })

    except Exception as e:
        write_log(f"❌ Sauvegarde manuelle Drive : erreur {e}")
        return jsonify({
            "success": False,
            "message": f"❌ Erreur lors de la sauvegarde : {e}"
        })



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
            if raw_value and "email" in fname.lower() and not _champ_email_valide(fname, raw_value):
                erreurs.append(f"Adresse email invalide dans « {fname} » ➜ « {raw_value} »")
                champs_invalides.append(fname)

            # ☎️ Validation téléphone
            if ftype == "tel" and raw_value and not is_valid_phone(cleaned_value):
                erreurs.append(f"Téléphone invalide dans « {fname} » ➜ « {raw_value} »")
                champs_invalides.append(fname)

            # 📏 Nom d'association limité à 30 caractères (limite VIF)
            if fname == "nom_association" and len(raw_value) > 30:
                erreurs.append(
                    f"Le nom de l'association « {raw_value} » dépasse 30 caractères (limite VIF)."
                )
                champs_invalides.append(fname)

            # 🔢 Validation numérique
            if (
                field["type_champ"] == "number"
                and raw_value
            ):
                try:
                    float(
                        raw_value.replace(",", ".")
                    )
                except ValueError:
                    erreurs.append(
                        f"Valeur numérique invalide dans « {fname} » ➜ « {raw_value} »"
                    )
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
        next_id = cursor.execute("SELECT COALESCE(MAX(Id), 0) + 1 FROM associations").fetchone()[0]
        valeurs["Id"] = next_id

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
            return redirect(url_for("partenaires.partenaires_tabulator"))
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

    if len(new_name_raw) > 30:
        flash("❌ Le nom de l’association ne doit pas dépasser 30 caractères (limite VIF).", "danger")
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
    data["Id"] = cur.execute("SELECT COALESCE(MAX(Id), 0) + 1 FROM associations").fetchone()[0]
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
        new_id = data["Id"]
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
    ============================================================
    MODIFICATION D’UNE ASSOCIATION
    ============================================================

    Fonctionnalités :
    - affichage dynamique des champs depuis field_groups
    - gestion des groupes repliables
    - gestion des droits par application (access_app)
    - lecture seule partielle ou totale
    - validation emails / téléphones
    - hash anti faux positifs de modification
    - navigation précédent / suivant
    - sauvegarde automatique Google Drive
    """

    conn = get_db_connection()
    cursor = conn.cursor()

    # ============================================================
    # CHARGEMENT ASSOCIATION
    # ============================================================

    partner = cursor.execute(
        """
        SELECT *
        FROM associations
        WHERE id = ?
        """,
        (partner_id,)
    ).fetchone()

    if not partner:

        conn.close()

        flash(
            "Association introuvable.",
            "danger"
        )

        return redirect(
            url_for("partenaires.partenaires")
        )

    partner_dict = dict(partner)

    # ============================================================
    # DROITS GENERAUX
    # ============================================================

    lecture_seule = not has_access(
        "associations",
        "ecriture"
    )

    user_role = (
        current_user.role or ""
    ).lower()

    # ============================================================
    # CAS SPECIAL CAR
    # ============================================================

    if user_role == "car":

        car_assoc = (
            (partner_dict.get("CAR") or "")
            .strip()
            .lower()
        )

        current_car = (
            (current_user.username or "")
            .strip()
            .lower()
        )

        write_log(
            f"""
    DEBUG CAR

    car_assoc   = {repr(car_assoc)}
    current_car = {repr(current_car)}

    lecture_seule AVANT = {lecture_seule}
    """
        )

        # ========================================================
        # SI LE CAR N'EST PAS LE CAR DE L'ASSOCIATION
        # ========================================================

        if car_assoc != current_car:

            lecture_seule = True

            write_log(
                "⛔ CAR hors périmètre → lecture seule"
            )

        else:

            write_log(
                "✅ CAR autorisé sur cette association"
            )
            write_log(
                f"""
            DEBUG CAR

            partner car brut = {repr(partner_dict.get("car"))}
            username brut    = {repr(current_user.username)}

            car_assoc        = {repr(car_assoc)}
            current_car      = {repr(current_car)}

            egalite          = {car_assoc == current_car}
            """
            )


    # ============================================================
    # PARTENAIRE PRECEDENT / SUIVANT
    # ============================================================

    previous_id, next_id = get_neighbor_ids_alphabetically(
        conn,
        partner_id
    )

    # ============================================================
    # CHAMPS DYNAMIQUES
    # ============================================================

    fields_rows = cursor.execute(
        """
        SELECT *
        FROM field_groups
        WHERE appli = 'associations'
        ORDER BY display_order
        """
    ).fetchall()

    fields_data = []

    for row in fields_rows:

        field = dict(row)

        fname = field["field_name"]

        field["value"] = partner_dict.get(
            fname,
            partner_dict.get(
                fname.lower(),
                ""
            )
        )

        # ========================================================
        # DROITS PAR APPLICATION
        # ========================================================

        field_access_app = field.get(
            "access_app"
        )

        if field_access_app:

            field["readonly"] = not has_access(
                field_access_app,
                "ecriture"
            )

        else:

            field["readonly"] = lecture_seule

        fields_data.append(field)

    # ============================================================
    # AU MOINS UN CHAMP MODIFIABLE ?
    # ============================================================

    can_edit_any_field = any(
        not field["readonly"]
        for field in fields_data
    )

    # ============================================================
    # GROUPES
    # ============================================================

    grouped_fields = {}

    for field in fields_data:

        group = (
            field["group_name"]
            or "Autres"
        )

        grouped_fields.setdefault(
            group,
            []
        ).append(field)

    # ============================================================
    # TRI DES GROUPES
    # ============================================================

    grouped_fields = dict(

        sorted(

            grouped_fields.items(),

            key=lambda item: (

                # Coordonnées principales toujours en premier
                0 if item[0].strip().lower() == "coordonnées principales"

                else 1,

                # Puis tri alphabétique
                item[0].strip().lower()
            )
        )
    )


    # ============================================================
    # RESEAUX NATIONAUX
    # ============================================================

    reseaux = cursor.execute(
        """
        SELECT param_value
        FROM parametres
        WHERE param_name = 'RESEAUX_NATIONAUX'
        ORDER BY param_value
        """
    ).fetchall()

    liste_reseaux = [
        r["param_value"]
        for r in reseaux
    ]

    # ============================================================
    # OPTIONS CAR
    # ============================================================

    car_options_query = cursor.execute(
        """
        SELECT param_value
        FROM parametres
        WHERE param_name = 'car'
        """
    ).fetchall()

    car_options = [
        {"param_value": row[0]}
        for row in car_options_query
    ]

    # ============================================================
    # PARAMETRES NAVIGATION
    # ============================================================

    search_term = request.values.get(
        "search",
        ""
    )

    limit = request.values.get(
        "limit",
        "7"
    )

    selected_columns = request.values.getlist(
        "columns"
    )

    selected_groups = request.values.getlist(
        "selected_groups"
    )

    query_params = {

        "search": search_term,

        "limit": limit,

        "columns": selected_columns,

        "selected_groups": selected_groups
    }

    # ============================================================
    # POST
    # ============================================================

    if request.method == "POST":

        go_to = request.form.get("go_to")

        do_upload = request.form.get(
            "do_upload",
            "1"
        )

        # ========================================================
        # AUCUN DROIT D'ECRITURE
        # ========================================================

        if not can_edit_any_field and do_upload == "1":

            conn.close()

            flash(
                "⛔ Vous n’avez pas les droits pour modifier cette association.",
                "danger"
            )

            return redirect(
                url_for(
                    "partenaires.update_partner",
                    partner_id=partner_id
                )
            )

        updates = {}

        champs_invalides = []

        erreurs = []

        # ========================================================
        # CHAMPS
        # ========================================================

        for field in fields_data:

            fname = field["field_name"]

            if fname == "id":

                continue

            # ====================================================
            # SECURITE PAR CHAMP
            # ====================================================

            if field["readonly"]:

                continue

            val = request.form.get(
                fname,
                ""
            ).strip()

            updates[fname] = (
                None if val == "" else val
            )

        # ========================================================
        # VALIDATIONS
        # ========================================================

        for field in fields_data:

            fname = field["field_name"]

            label = (
                fname
                .replace("_", " ")
                .capitalize()
            )

            group = (
                field.get("group_name", "Autres")
                or "Autres"
            )

            val = (
                updates.get(fname)
                or ""
            ).strip()

            if val:

                if (
                    field.get("type_champ") == "email"
                    and not _champ_email_valide(fname, val)
                ):

                    erreurs.append(
                        f"Champ invalide dans {group} : "
                        f"{label} ➜ « {val} » "
                        f"n’est pas un email valide."
                    )

                    champs_invalides.append(fname)

                if (
                    "tel" in fname.lower()
                    and not is_valid_phone(val)
                ):

                    erreurs.append(
                        f"Champ invalide dans {group} : "
                        f"{label} ➜ « {val} » "
                        f"n’est pas un numéro valide."
                    )

                    champs_invalides.append(fname)

                if (
                    fname == "nom_association"
                    and len(val) > 30
                    and val != (partner_dict.get(fname) or "").strip()
                ):

                    erreurs.append(
                        f"Champ invalide dans {group} : "
                        f"{label} ➜ « {val} » "
                        f"dépasse 30 caractères (limite VIF)."
                    )

                    champs_invalides.append(fname)

                if (
                    field.get("type_champ") == "number"
                    and val
                ):
                    try:
                        float(
                            val.replace(",", ".")
                        )
                    except ValueError:
                        erreurs.append(
                            f"Champ invalide dans {group} : "
                            f"{label} ➜ « {val} » "
                            f"n’est pas une valeur numérique valide."
                        )

                        champs_invalides.append(fname)

        # ========================================================
        # ERREURS VALIDATION
        # ========================================================

        if erreurs:

            for msg in erreurs:

                flash(
                    msg,
                    "danger"
                )

            for field in fields_data:

                fname = field["field_name"]

                field["value"] = request.form.get(
                    fname,
                    ""
                ).strip()

            grouped_fields = {}

            for field in fields_data:

                group = (
                    field["group_name"]
                    or "Autres"
                )

                grouped_fields.setdefault(
                    group,
                    []
                ).append(field)

            besoins_historique = [
                dict(h) for h in cursor.execute(
                    """
                    SELECT contenu, date_creation,
                           date_remplacement, user_saisie
                    FROM besoins_particuliers_historique
                    WHERE association_id = ?
                    ORDER BY date_creation DESC
                    """,
                    (partner_id,)
                ).fetchall()
            ]

            conn.close()

            return render_template(
                "partenaires/update_partenaire.html",
                partenaire=partner_dict,
                partner_id=partner_id,
                nom_association=request.form.get(
                    "nom_association"
                ) or partner_dict.get(
                    "nom_association",
                    "Nom inconnu"
                ),
                grouped_fields=grouped_fields,
                liste_reseaux=liste_reseaux,
                car_options=car_options,
                next_url=urlencode(
                    query_params,
                    doseq=True
                ),
                previous_id=previous_id,
                next_id=next_id,
                lecture_seule=lecture_seule,
                date_modif=partner_dict.get(
                    "date_modif",
                    ""
                ),
                heure_modif=partner_dict.get(
                    "heure_modif",
                    ""
                ),
                user_modif=partner_dict.get(
                    "user_modif",
                    ""
                ),
                champs_invalides=champs_invalides,
                form_hash=request.form.get(
                    "form_hash",
                    ""
                ),
                data=dict(partner),
                can_edit_any_field=can_edit_any_field,
                besoins_historique=besoins_historique
            )

        # ========================================================
        # HASH MODIFICATIONS
        # ========================================================

        if do_upload == "1":

            inputs_for_hash = [

                f"{f['field_name']}:"
                f"{request.form.get(f['field_name'], '').strip()}"

                for f in fields_data

                if f["field_name"] != "id"
            ]

            computed_hash = base64.b64encode(
                "|#|".join(inputs_for_hash).encode("utf-8")
            ).decode("utf-8")

            received_hash = request.form.get(
                "form_hash",
                ""
            )

            if computed_hash != received_hash:

                now = datetime.now()

                updates["date_modif"] = now.strftime(
                    "%Y-%m-%d"
                )

                updates["heure_modif"] = now.strftime(
                    "%H:%M:%S"
                )

                updates["user_modif"] = (
                    current_user.username
                )

            # ====================================================
            # UPDATE SQL
            # ====================================================

            if updates:

                # ------------------------------------------------
                # HISTORIQUE BESOINS PARTICULIERS
                # ------------------------------------------------

                if "besoins_particuliers" in updates:

                    old_val = (
                        partner_dict.get("besoins_particuliers") or ""
                    ).strip()

                    new_val = (
                        updates.get("besoins_particuliers") or ""
                    ).strip()

                    if old_val != new_val:

                        today = datetime.now().strftime("%Y-%m-%d")

                        cursor.execute(
                            """
                            UPDATE besoins_particuliers_historique
                            SET date_remplacement = ?
                            WHERE association_id = ?
                              AND date_remplacement IS NULL
                            """,
                            (today, partner_id)
                        )

                        cursor.execute(
                            """
                            INSERT INTO besoins_particuliers_historique
                            (association_id, contenu, date_creation,
                             date_remplacement, user_saisie)
                            VALUES (?, ?, ?, NULL, ?)
                            """,
                            (
                                partner_id,
                                new_val or None,
                                today,
                                current_user.username
                            )
                        )

                set_clause = ", ".join([
                    f"`{k}` = ?"
                    for k in updates
                ])

                values = list(
                    updates.values()
                ) + [partner_id]

                try:

                    cursor.execute(
                        f"""
                        UPDATE associations
                        SET {set_clause}
                        WHERE id = ?
                        """,
                        values
                    )

                    conn.commit()

                    upload_database()

                    flash(
                        "✅ Association mise à jour avec succès.",
                        "success"
                    )

                except Exception as e:

                    flash(
                        f"❌ Erreur lors de la mise à jour : {e}",
                        "danger"
                    )

        conn.close()

        # ========================================================
        # NAVIGATION
        # ========================================================

        if go_to:

            return redirect(go_to)

        return redirect(
            url_for(
                "partenaires.update_partner",
                partner_id=partner_id
            )
        )

    # ============================================================
    # HASH INITIAL
    # ============================================================

    inputs_for_hash = [

        f"{field['field_name']}:{field['value'] or ''}"

        for field in fields_data

        if field['field_name'] != "id"
    ]

    form_hash = base64.b64encode(
        "|#|".join(inputs_for_hash).encode("utf-8")
    ).decode("utf-8")

    # ============================================================
    # DATE MODIF FR
    # ============================================================

    date_modif = partner_dict.get(
        "date_modif"
    )

    heure_modif = partner_dict.get(
        "heure_modif"
    )

    heure_fr = None

    if date_modif and heure_modif:

        try:

            dt_str = f"{date_modif} {heure_modif}"

            dt = datetime.strptime(
                dt_str,
                "%Y-%m-%d %H:%M:%S"
            )

            import pytz

            dt = pytz.utc.localize(dt).astimezone(
                pytz.timezone("Europe/Paris")
            )

            heure_fr = dt.strftime(
                "%d/%m/%Y %H:%M"
            )

        except Exception:

            heure_fr = (
                f"{date_modif} {heure_modif}"
            )

    elif date_modif:

        heure_fr = date_modif

    besoins_historique = [
        dict(h) for h in cursor.execute(
            """
            SELECT contenu, date_creation,
                   date_remplacement, user_saisie
            FROM besoins_particuliers_historique
            WHERE association_id = ?
            ORDER BY date_creation DESC
            """,
            (partner_id,)
        ).fetchall()
    ]

    conn.close()

    return render_template(
        "partenaires/update_partenaire.html",
        partenaire=partner_dict,
        partner_id=partner_id,
        nom_association=partner_dict.get(
            "nom_association",
            "Nom inconnu"
        ),
        grouped_fields=grouped_fields,
        liste_reseaux=liste_reseaux,
        car_options=car_options,
        next_url=urlencode(
            query_params,
            doseq=True
        ),
        previous_id=previous_id,
        next_id=next_id,
        lecture_seule=lecture_seule,
        date_modif=heure_fr,
        user_modif=partner_dict.get(
            "user_modif",
            ""
        ),
        champs_invalides=[],
        form_hash=form_hash,
        can_edit_any_field=can_edit_any_field,
        data=dict(partner),
        besoins_historique=besoins_historique
    )


@partenaires_bp.route("/delete_partner/<int:partner_id>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def delete_partner(partner_id):
    confirmation = request.form.get("confirm")
    second_confirmation = request.form.get("confirm_final")

    if confirmation == "oui" and second_confirmation == "supprimer":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM associations WHERE ID = ?", (partner_id,))
        conn.commit()
        upload_database()  # Sauvegarde automatique sur Google Drive
        conn.close()
        flash("✅ Partenaire supprimé avec succès.", "success")
        return redirect(url_for('partenaires.partenaires_tabulator'))
    else:
        flash("❌ Suppression annulée ou confirmation incorrecte.", "danger")
        return redirect(url_for('partenaires.update_partner', partner_id=partner_id))




@partenaires_bp.route("/edition_tableau_associations", methods=["GET", "POST"])
@login_required
@require_access("associations", "lecture")
def edition_tableau_associations():

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # 🔹 Récupération des champs
        fields_data = conn.execute("""
            SELECT * FROM field_groups
            WHERE appli = 'associations'
            ORDER BY display_order
        """).fetchall()

        number_fields = [
            row["field_name"]
            for row in fields_data
            if row["type_champ"] == "number"
        ]

        grouped_fields = {}
        for row in fields_data:
            group = row["group_name"] or "Autres"
            grouped_fields.setdefault(group, []).append(row)

        # ✅ Détection des champs oui/non
        oui_non_fields = [
            row["field_name"] for row in fields_data
            if row["type_champ"] == "oui_non"
        ]

        number_fields = [
            row["field_name"]
            for row in fields_data
            if row["type_champ"] == "number"
        ]

        selected_columns = request.values.getlist("columns")

        filtered_ids_raw = request.values.get(
            "filtered_ids",
            "[]"
        )

        try:

            filtered_ids = json.loads(
                filtered_ids_raw
            )

        except Exception:

            filtered_ids = []

        voir_toutes = request.args.get("voir_toutes") == "1"
        user_role = current_user.role.lower()
        is_car = user_role == "car" and not voir_toutes
        car_value = current_user.username if is_car else None

        escaped_columns = [
            f"`{col}`" for col in selected_columns
            if col != "nom_association"
        ]

        columns_clause = ", ".join(
            ["ID", "`nom_association`"] + escaped_columns
        )

        params = []

        where_clauses = [

            "(validite IS NULL OR LOWER(validite) != 'non')"
        ]

        # ========================================================
        # FILTRE CAR
        # ========================================================

        if is_car:

            where_clauses.append(

                "LOWER(car) = LOWER(?)"
            )

            params.append(car_value)

        # ========================================================
        # FILTRE IDS TABULATOR
        # ========================================================

        if filtered_ids:

            placeholders = ",".join(
                "?" for _ in filtered_ids
            )

            where_clauses.append(

                f"ID IN ({placeholders})"
            )

            params.extend(filtered_ids)

        where_sql = " AND ".join(where_clauses)

        query = f"""
            SELECT {columns_clause}
            FROM associations
            WHERE {where_sql}
            ORDER BY nom_association COLLATE NOCASE
        """

        rows = conn.execute(
            query,
            params
        ).fetchall()

    return render_template(
        "partenaires/edition_tableau_associations.html",
        rows=rows,
        selected_columns=selected_columns,
        user_role=user_role,
        oui_non_fields=oui_non_fields,
        number_fields=number_fields,
        filtered_ids=filtered_ids
    )


@partenaires_bp.route("/update_associations_table", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def update_associations_table():


    conn = get_db_connection()
    cursor = conn.cursor()

    total = int(request.form.get("total_rows", 0))
    columns = request.form.getlist("columns")

    field_config = cursor.execute("""
        SELECT field_name, type_champ
        FROM field_groups
        WHERE appli='associations'
    """).fetchall()

    field_types = {

        row["field_name"]: row["type_champ"]

        for row in field_config
    }

    filtered_ids_raw = request.form.get(
        "filtered_ids",
        "[]"
    )

    filtered_ids = json.loads(
        filtered_ids_raw
    )


    # ✅ Vérification du nombre de colonnes
    if len(columns) > 40:
        flash("⚠️ Trop de colonnes sélectionnées. Veuillez limiter votre sélection à 40 colonnes maximum.", "danger")
        return redirect(url_for("partenaires.partenaires_tabulator"))


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
            db_key = None
            field_type = field_types.get(col)

            for k in asso_dict.keys():

                if k.lower() == col.lower():

                    db_key = k
                    break

            old_val = ""

            if db_key:

                old_val = (
                    str(asso_dict.get(db_key) or "")
                    .strip()
                )
                new_val = request.form.get(f"{col}_{i}", "").strip()

            if new_val != old_val:
                if "email" in col.lower() and new_val and not _champ_email_valide(col, new_val):
                    erreurs.append(f"Ligne {i + 1}, champ {col} : adresse email invalide « {new_val} »")
                    champs_invalides.append(col)
                elif "tel" in col.lower() and new_val and not is_valid_phone(new_val):
                    erreurs.append(f"Ligne {i + 1}, champ {col} : numéro de téléphone invalide « {new_val} »")
                    champs_invalides.append(col)

                elif field_type == "number" and new_val:
                    try:
                        float(
                            new_val.replace(",", ".")
                        )
                        modifications[col] = new_val

                    except ValueError:
                        erreurs.append(
                            f"Ligne {i + 1}, champ {col} : "
                            f"valeur numérique invalide « {new_val} »"
                        )

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

    # Repost en POST plutôt qu'un redirect GET classique : avec beaucoup
    # d'associations filtrées, l'URL générée (filtered_ids + columns) peut
    # dépasser la taille de ligne de requête autorisée par Gunicorn.
    return render_template_string(
        """
        <form id="repost-edition-tableau" method="POST"
              action="{{ url_for('partenaires.edition_tableau_associations') }}">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            {% for col in columns %}
            <input type="hidden" name="columns" value="{{ col }}">
            {% endfor %}
            <input type="hidden" name="filtered_ids" value="{{ filtered_ids }}">
        </form>
        <script>document.getElementById('repost-edition-tableau').submit();</script>
        """,
        columns=columns,
        filtered_ids=json.dumps(filtered_ids)
    )


def get_neighbor_ids_alphabetically(conn, current_id):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, nom_association FROM associations "
        "WHERE validite IS NULL OR validite != 'non' "
        "ORDER BY nom_association COLLATE NOCASE"
    )
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
    org = get_organisation()

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
    logo_path = org["logo_path"]
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
            pdf.drawString(left_margin, 30, org["footer_partenariat"])
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
    pdf.drawString(left_margin, 30, org["footer_partenariat"])
    pdf.drawString(right_margin - 150, 30, f"{date_du_jour} - Page {pdf.getPageNumber()}")

    pdf.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"{title.replace(' ', '_').lower()}_{partner_id}.pdf", mimetype='application/pdf')




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




def beautify_title(name):

    return (
        name
        .replace("_", " ")
        .replace("-", " ")
        .strip()
        .title()
    )



