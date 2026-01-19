#!/usr/bin/env python3
import sqlite3
import os
import sys

# ✅ Ajout du chemin vers utils.py
sys.path.insert(0, "/home/ndprz/dev")
from utils import write_log

def get_columns(db_path, table_name):
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1]: row for row in cursor.fetchall()}

def compare_and_update_associations_structure():
    dev_path = "/home/ndprz/dev/ba380dev.sqlite"
    prod_path = "/home/ndprz/ba380/ba380.sqlite"
    table_name = "associations"

    write_log("🧪 Début de la comparaison des colonnes associations entre DEV et PROD")

    dev_columns = get_columns(dev_path, table_name)
    prod_columns = get_columns(prod_path, table_name)

    missing_columns = [col for col in dev_columns if col not in prod_columns]

    if not missing_columns:
        write_log("✅ Table associations à jour : aucune colonne manquante.")
        return

    write_log(f"🔍 Colonnes manquantes dans associations (PROD) : {missing_columns}")

    with sqlite3.connect(prod_path) as conn:
        cursor = conn.cursor()
        for col in missing_columns:
            col_type = dev_columns[col][2] or "TEXT"
            default_val = dev_columns[col][4]
            default_clause = f"DEFAULT '{default_val}'" if default_val is not None else ""
            alter_stmt = f"ALTER TABLE {table_name} ADD COLUMN `{col}` {col_type} {default_clause}".strip()
            try:
                cursor.execute(alter_stmt)
                write_log(f"✅ Colonne ajoutée : {alter_stmt}")
            except Exception as e:
                write_log(f"❌ Erreur lors de l'ajout de {col} : {e}")
        conn.commit()

    write_log("🎉 Mise à jour terminée pour la table associations (PROD).")

if __name__ == "__main__":
    compare_and_update_associations_structure()
