import os
import sqlite3
from flask import Blueprint, request, render_template, redirect, url_for, flash
from dotenv import dotenv_values
from utils import write_log

env_vars = dotenv_values('/home/ndprz/dev/.env')
rename_bp = Blueprint("rename", __name__)

# Seules les vraies tables utilisateurs sont modifiables
TABLES_AUTORISEES = ["benevoles", "associations"]

BASES = {
    "ba380dev": env_vars.get("SQLITE_DB"),
    "ba380dev_test": env_vars.get("SQLITE_TEST_DB"),
    "ba380": env_vars.get("SQLITE_DB_PROD"),
    "ba380_test": env_vars.get("SQLITE_TEST_DB_PROD")
}

def rename_column(db_path, table, old_col, new_col):
    write_log(f"🔧 rename_column - db: {db_path}, table: {table}, old: {old_col}, new: {new_col}")
    if not new_col or str(new_col).strip().lower() == "none":
        write_log("❌ Abandon : new_col est invalide (None)")
        raise ValueError("new_col est vide ou 'None'")

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        columns_info = cursor.fetchall()
        column_names = [col[1] for col in columns_info]
        write_log(f"📋 Colonnes existantes : {column_names}")

        if old_col not in column_names:
            return f"❌ {old_col} introuvable dans {table}"

        if new_col in column_names:
            return f"⚠️ {new_col} existe déjà dans {table}"

        new_columns_def = []
        for col in columns_info:
            name = new_col if col[1] == old_col else col[1]
            new_columns_def.append(f"{name} {col[2]}")

        columns_sql = ", ".join(new_columns_def)
        columns_copy = ", ".join([
            f"{old_col} AS {new_col}" if col[1] == old_col else col[1]
            for col in columns_info
        ])

        write_log(f"📐 Création table temp avec : {columns_sql}")
        temp_table = f"{table}_temp"

        try:
            cursor.execute(f"CREATE TABLE {temp_table} ({columns_sql})")
            cursor.execute(f"INSERT INTO {temp_table} SELECT {columns_copy} FROM {table}")
            cursor.execute(f"DROP TABLE {table}")
            cursor.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")
            conn.commit()
            return f"✅ Colonne renommée dans {table}"
        except Exception as e:
            conn.rollback()
            write_log(f"❌ Exception dans rename_column : {e}")
            return f"❌ Erreur : {e}"

@rename_bp.route("/rename_field", methods=["GET", "POST"])
def rename_field():
    resultats = []

    if request.method == "POST":
        table = request.form.get("table")
        old_col = request.form.get("old_col", "").strip()
        raw_input = request.form.get("new_col", "").strip()
        new_col = raw_input if raw_input and raw_input.lower() != "none" else None

        write_log(f"📨 Formulaire reçu : table={table}, old_col={old_col}, new_col={new_col}")

        if table not in TABLES_AUTORISEES:
            flash("❌ Table non autorisée", "danger")
            return redirect(url_for("rename.rename_field"))

        for nom_base, chemin in BASES.items():
            write_log(f"🔍 Vérification base {nom_base} → {chemin}")
            if not chemin or not os.path.exists(chemin):
                resultats.append(f"⚠️ Base non trouvée : {nom_base}")
                continue

            try:
                # 🔁 Générer libreX si new_col est vide
                if not new_col:
                    with sqlite3.connect(chemin) as conn:
                        conn.row_factory = sqlite3.Row
                        cursor = conn.cursor()
                        existing_cols = [col[1] for col in cursor.execute(f"PRAGMA table_info({table})")]
                        i = 1
                        while f"libre{i}" in existing_cols:
                            i += 1
                        new_col = f"libre{i}"
                        write_log(f"🆕 Nouveau champ généré automatiquement : {new_col}")

                # 🔁 Mettre à jour field_groups si nécessaire
                with sqlite3.connect(chemin) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    fg = cursor.execute("""
                        SELECT * FROM field_groups
                        WHERE field_name = ? AND appli = ?
                        LIMIT 1
                    """, (old_col, table)).fetchone()

                    if fg:
                        # Mettre à NULL le display_order si libreX
                        if new_col.startswith("libre"):
                            display_order = None
                        else:
                            display_order = fg["display_order"]

                        cursor.execute("""
                            UPDATE field_groups
                            SET field_name = ?, display_order = ?
                            WHERE field_name = ? AND appli = ?
                        """, (new_col, display_order, old_col, table))
                        conn.commit()
                        write_log(f"✏️ field_groups mis à jour : {old_col} → {new_col}, display_order = {display_order}")

                # 🔁 Renommage de la colonne SQL
                result = rename_column(chemin, table, old_col, new_col)
                resultats.append(f"{nom_base} → {result}")

            except Exception as e:
                write_log(f"❌ Exception globale : {e}")
                resultats.append(f"{nom_base} → ❌ Erreur : {e}")

    return render_template("rename_field.html", tables=TABLES_AUTORISEES, resultats=resultats)
