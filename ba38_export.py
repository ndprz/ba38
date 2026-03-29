from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, session
from flask_login import login_required, current_user
from utils import get_db_connection, get_db_path, write_log, has_access
from weasyprint import HTML
from docx import Document
import pandas as pd
import os
import io
import json
import sqlite3


# ================================================================
# 🧩 Blueprint : EXPORTS / GÉNÉRATEUR EXCEL / FICHIERS
# ================================================================
export_data_bp = Blueprint("export_data", __name__)

EXPORTS_DIR = os.path.join(os.path.dirname(__file__), "exports")


# ================================================================
# 📊 GÉNÉRATEUR EXCEL PERSONNALISÉ
# ================================================================
@export_data_bp.route("/generateur_excel", methods=["GET", "POST"])
@login_required
def generateur_excel():
    """🔧 Générateur Excel personnalisé avec traçage complet (debug write_log)."""

    # write_log("🚀 Entrée dans generateur_excel()"

    filters = {}
    mode_or_val = 0
    selected_columns = []
    preview_data = None
    total_count = 0

    db_path = get_db_path()
    # write_log(f"🗂 Base utilisée : {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 🔄 Charger les presets existants
    presets = cursor.execute(
        "SELECT id, preset_name, appli, date_created FROM export_presets WHERE user_email=? ORDER BY date_created DESC",
        (current_user.email,),
    ).fetchall()
    # write_log(f"📚 {len(presets)} presets trouvés pour {current_user.email}")

    data_type = request.form.get("data_type", "benevoles")
    action = request.form.get("action")
    # write_log(f"➡️ Action demandée = {action}, data_type = {data_type}")

    if not has_access(data_type, "lecture"):
        flash("⛔ Accès non autorisé.", "danger")
        return redirect(url_for("index"))

    # Lecture des métadonnées
    fields_data = cursor.execute(
        """
        SELECT field_name, group_name, display_order, type_champ
        FROM field_groups
        WHERE appli = ?
        ORDER BY display_order
        """,
        (data_type,),
    ).fetchall()
    # write_log(f"📋 {len(fields_data)} champs trouvés pour {data_type}")

    # Regroupement
    grouped_fields = {}
    for row in fields_data:
        group = row["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(row)

    # Forcer ordre
    ordered_grouped_fields = {}
    normalized_groups = {k.lower(): k for k in grouped_fields.keys()}
    if "coordonnées principales" in normalized_groups:
        true_key = normalized_groups["coordonnées principales"]
        ordered_grouped_fields[true_key] = grouped_fields.pop(true_key)
    for k, v in grouped_fields.items():
        ordered_grouped_fields[k] = v
    grouped_fields = ordered_grouped_fields

    bene_values = [
        r[0]
        for r in cursor.execute(
            "SELECT DISTINCT param_value FROM parametres WHERE param_name='type_benevole'"
        ).fetchall()
    ]
    # write_log(f"📊 Valeurs type_benevole = {bene_values}")

    # ===================================================================
    # 🧠 Chargement d’un preset existant
    # ===================================================================
    preset_id = request.form.get("preset_id")
    if action == "load_preset" and preset_id:
        # write_log(f"📥 Chargement du preset ID={preset_id}")
        preset = cursor.execute(
            """
            SELECT id, preset_name, appli, selected_columns, filters_json, COALESCE(mode_or, 0) AS mode_or
            FROM export_presets
            WHERE id=? AND user_email=?
            """,
            (preset_id, current_user.email),
        ).fetchone()

        if preset:
            data_type = preset["appli"]
            selected_columns = (
                preset["selected_columns"].split(",") if preset["selected_columns"] else []
            )
            filters = json.loads(preset["filters_json"] or "{}")
            mode_or_val = int(preset["mode_or"] or 0)

            # write_log(f"✅ Profil '{preset['preset_name']}' chargé.")
            # write_log(f"🔹 selected_columns = {selected_columns}")
            # write_log(f"🔹 filters_json = {preset['filters_json']}")
            # write_log(f"🔹 filters (dict) = {filters}")
            # write_log(f"🔹 mode_or_val = {mode_or_val}")

            conn.close()
            return render_template(
                "generateur_excel.html",
                base_template="base_assos.html"
                if data_type == "associations"
                else "base_bene.html",
                grouped_fields=grouped_fields,
                data_type=data_type,
                selected_columns=selected_columns,
                search="",
                type_benevole_values=bene_values,
                sql_debug=None,
                presets=presets,
                filters=filters,  # ✅ passage explicite
                mode_or=("on" if mode_or_val == 1 else ""),
            )
        else:
            flash("⚠️ Profil introuvable ou non autorisé.", "warning")
            write_log(f"❌ Aucun preset trouvé pour ID={preset_id}")

    # ===================================================================
    # 📋 Construction du SQL
    # ===================================================================
    just_switching = request.form.get("just_switching_type")
    selected_columns = request.form.getlist("columns")
    search = request.form.get("search", "")
    use_or = bool(request.form.get("mode_or"))
    # write_log(f"🧱 Requête : just_switching={just_switching}, use_or={use_or}")

    where_clauses, params = [], []
    oui_non_clauses = []

    for field in fields_data:
        f = field["field_name"]
        t = (field["type_champ"] or "").lower()

        # 🔹 Récupération prioritaire : formulaire sinon preset
        value = request.form.get(f"filter_{f}")
        if value is None:
            value = filters.get(f"filter_{f}", "")
        else:
            value = value.strip()
            # Ne considérer vide que si vraiment vide, pas "contains"
            if value.lower() == "contains":
                value = ""

        # 🔸 Ignorer les valeurs techniques ou vides
        if not value or str(value).lower() == "contains":
            continue

        # write_log(f"🧩 Filtre détecté : {f} = {value} (type {t})")
        # if value == "contains":
            # write_log(f"⚠️ Ignoré filtre fantôme pour {f} (value=contains)")


        if t == "oui_non":
            clause = f"LOWER(TRIM({f}))=?"
            oui_non_clauses.append((clause, value.lower()))
        else:
            operator = request.form.get(f"operator_{f}", "contains")
            if operator == "startswith":
                clause = f"LOWER({f}) LIKE ?"
                params.append(value.lower() + "%")
            else:
                clause = f"LOWER({f}) LIKE ?"
                params.append("%" + value.lower() + "%")
            where_clauses.append(clause)

    if oui_non_clauses:
        if use_or:
            where_clauses.append("(" + " OR ".join(c[0] for c in oui_non_clauses) + ")")
        else:
            where_clauses.extend(c[0] for c in oui_non_clauses)
        params.extend(c[1] for c in oui_non_clauses)

    if search:
        where_clauses.append("(nom LIKE ? OR prenom LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    columns_sql = ", ".join(selected_columns) if selected_columns else "id"
    query = f"SELECT {columns_sql} FROM {data_type} {where_sql}"
    # write_log(f"🧠 SQL généré : {query}")
    # write_log(f"🧩 PARAMS = {params}")

    # ===================================================================
    # 📊 Actions principales
    # ===================================================================
    if request.method == "POST" and not just_switching:
        df = pd.read_sql_query(query, conn, params=params)
        total_count = len(df)
        # write_log(f"📈 {total_count} lignes trouvées")

        if action == "preview":
            preview_data = df.head(20)
            conn.close()
            # write_log("👀 Aperçu du résultat généré")
            return render_template(
                "generateur_excel.html",
                base_template="base_assos.html"
                if data_type == "associations"
                else "base_bene.html",
                grouped_fields=grouped_fields,
                data_type=data_type,
                selected_columns=selected_columns,
                search=search,
                type_benevole_values=bene_values,
                preview_data=preview_data,
                total_count=total_count,
                sql_debug=f"{query}\nPARAMS = {params}",
                presets=presets,
                filters=filters,
                mode_or=("on" if use_or else ""),
            )

        if action == "save_preset":
            preset_name = request.form.get("preset_name", "").strip()
            if not preset_name:
                flash("⚠️ Merci de donner un nom à votre profil.", "warning")
            else:
                filters = {}
                for k, v in request.form.items():
                    if not k.startswith("filter_"):
                        continue
                    # ⛔ ignorer les filtres vides ou contenant juste "contains"
                    if not v.strip() or v.strip().lower() == "contains":
                        continue
                    # 🔄 correction de l'inversion chauffeur_ramasse
                    true_field = k.replace("filter_chauffeur_ramasse", "filter_ramasse_chauffeur")
                    filters[true_field] = v.strip()
                    # write_log(f"💾 Filtres sauvegardés = {filters}")

                data = {
                    "columns": selected_columns,
                    "filters": filters,
                    "mode_or": int(use_or),
                }
                # write_log(f"💾 Sauvegarde preset {preset_name} : {data}")
                cursor.execute(
                    """
                    INSERT INTO export_presets (user_email, preset_name, appli, selected_columns, filters_json, mode_or)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current_user.email,
                        preset_name,
                        data_type,
                        ",".join(selected_columns),
                        json.dumps(data["filters"]),
                        data["mode_or"],
                    ),
                )
                conn.commit()
                flash(f"✅ Profil '{preset_name}' enregistré.", "success")
                conn.close()
                return redirect(url_for("export_data.generateur_excel"))

        elif action == "delete_preset":
            preset_id = request.form.get("preset_id")
            if preset_id:
                # write_log(f"🗑️ Suppression demandée pour preset ID={preset_id}")
                cursor.execute(
                    "DELETE FROM export_presets WHERE id=? AND user_email=?",
                    (preset_id, current_user.email),
                )
                conn.commit()
                flash("🗑️ Profil supprimé avec succès.", "success")
                # write_log(f"✅ Preset ID={preset_id} supprimé pour {current_user.email}")
                return redirect(url_for("export_data.generateur_excel"))

            else:
                flash("⚠️ Aucun profil sélectionné à supprimer.", "warning")
                # write_log("⚠️ Tentative de suppression sans preset sélectionné")


        if action == "export":
            # write_log("📤 Export Excel demandé")
            output = io.BytesIO()
            df.to_excel(output, index=False, engine="openpyxl")
            output.seek(0)
            conn.close()
            return send_file(
                output,
                as_attachment=True,
                download_name=f"export_{data_type}.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    conn.close()
    # write_log(f"🏁 Fin generateur_excel() — retour du template standard.")
    # write_log(f"🎯 Filters envoyés au template : {filters}")

    return render_template(
        "generateur_excel.html",
        base_template="base_assos.html" if data_type == "associations" else "base_bene.html",
        grouped_fields=grouped_fields,
        data_type=data_type,
        selected_columns=selected_columns,
        search="",
        type_benevole_values=bene_values,
        sql_debug=None,
        presets=presets,
        filters=filters,   # ✅ passage correct (dict entier)
        mode_or=("on" if mode_or_val == 1 else ""),
    )


# ================================================================
# 📦 ROUTE : génération de fichiers standards
# ================================================================
@export_data_bp.route("/generation_fichiers", methods=["GET", "POST"])
@login_required
def generation_fichiers():
    """Exports Excel dynamiques basés sur field_groups"""

    source = request.args.get("source", "assos")
    base_template = "base_assos.html" if source == "assos" else "base_bene.html"

    if source == "assos":
        if not has_access("associations", "lecture"):
            flash("⛔ Accès non autorisé.", "danger")
            return redirect(url_for("index"))
    else:
        if not has_access("benevoles", "lecture"):
            flash("⛔ Accès non autorisé.", "danger")
            return redirect(url_for("index"))

    if request.method == "POST":
        data_type = request.form.get("data_type", "all")

        conn = get_db_connection()
        cursor = conn.cursor()

        # ======================================================
        # 🔎 Déterminer la table et le filtre associé
        # ======================================================

        if data_type in ["all", "active", "inactive", "indicateurs_etats", "besoins"]:
            table = "associations"
            appli = "associations"
        elif data_type == "benevoles_complet":
            table = "benevoles"
            appli = "benevoles"
        else:
            flash("Type d'extraction inconnu", "danger")
            return redirect(url_for("export_data.generation_fichiers"))

        # ======================================================
        # 📋 Récupération des champs depuis field_groups
        # ======================================================

        fields = cursor.execute("""
            SELECT field_name
            FROM field_groups
            WHERE appli = ?
            ORDER BY display_order
        """, (appli,)).fetchall()

        field_names = [row["field_name"] for row in fields]

        if not field_names:
            flash("⚠️ Aucun champ défini dans field_groups.", "warning")
            return redirect(url_for("export_data.generation_fichiers"))

        columns_sql = ", ".join([f'"{c}"' for c in field_names])

        # ======================================================
        # 📊 Filtres spécifiques
        # ======================================================

        where_clause = ""

        if data_type == "active":
            where_clause = "WHERE validite = 'oui'"

        elif data_type == "inactive":
            where_clause = "WHERE validite != 'oui'"

        # indicateurs_etats et besoins
        # 👉 on exporte maintenant tous les champs field_groups
        #    (plus cohérent que sélection partielle codée en dur)

        query = f"SELECT {columns_sql} FROM {table} {where_clause}"

        df = pd.read_sql_query(query, conn)
        conn.close()

        # ======================================================
        # 📤 Génération Excel
        # ======================================================

        file_map = {
            "all": "extraction_toutes_associations.xlsx",
            "active": "extraction_associations_actives.xlsx",
            "inactive": "extraction_associations_inactives.xlsx",
            "indicateurs_etats": "extraction_indicateurs_etats.xlsx",
            "besoins": "extraction_associations_besoins.xlsx",
            "benevoles_complet": "benevoles.xlsx",
        }

        file_name = file_map.get(data_type, "export.xlsx")

        output = io.BytesIO()
        df.to_excel(output, index=False, engine="openpyxl")
        output.seek(0)

        return send_file(
            output,
            as_attachment=True,
            download_name=file_name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # ======================================================
    # Rendu initial page
    # ======================================================

    conn = get_db_connection()
    cursor = conn.cursor()

    fields_data = cursor.execute("""
        SELECT *
        FROM field_groups
        WHERE appli = 'associations'
        ORDER BY display_order
    """).fetchall()

    conn.close()

    grouped_fields = {}
    for row in fields_data:
        group = row["group_name"] or "Autres"
        grouped_fields.setdefault(group, []).append(row)

    return render_template(
        "generation_fichiers.html",
        base_template=base_template,
        source=source,
        user_role=current_user.role.lower(),
        grouped_fields=grouped_fields,
    )


__all__ = ["export_data_bp"]
