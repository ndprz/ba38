#!/usr/bin/env python3
"""
🔧 Synchronisation du schéma de la table `associations` (DEV → PROD)

Objectif :
- Comparer la structure de la table `associations` entre la base DEV et la base PROD
- Ajouter en PROD les colonnes présentes en DEV mais absentes en PROD
- Ne JAMAIS modifier la base DEV
- Fonctionner via admin_scripts, sans dépendre de ENVIRONMENT
"""

from pathlib import Path
import sys
import sqlite3

# ============================================================
# 📁 Rendre utils.py importable (racine BA38)
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from utils import write_log, get_db_path_by_env


# ============================================================
# 🔊 Helper : log + affichage admin_scripts
# ============================================================
def log_and_print(msg: str):
    print(msg)
    write_log(msg)


# ============================================================
# 🔍 Lecture des colonnes SQLite
# ============================================================
def get_columns(db_path: str, table_name: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1]: row for row in cursor.fetchall()}


# ============================================================
# 🔧 Synchronisation DEV → PROD
# ============================================================
def compare_and_update_associations_structure():
    table_name = "associations"

    # --- Résolution explicite des bases ---
    dev_path = get_db_path_by_env(
        "dev",
        force_base_dir="/srv/ba38/dev"
    )

    prod_path = get_db_path_by_env(
        "prod",
        force_base_dir="/srv/ba38/prod"
    )

    # --------------------------------------------------------
    # 🛡️ GARDE-FOU CRITIQUE
    # --------------------------------------------------------
    if "/dev/" in prod_path:
        raise RuntimeError(
            f"❌ ERREUR CRITIQUE : base PROD incorrecte détectée : {prod_path}"
        )

    log_and_print("🧪 Début comparaison schéma table associations DEV → PROD")
    log_and_print(f"📄 DEV  : {dev_path}")
    log_and_print(f"📄 PROD : {prod_path}")

    dev_columns = get_columns(dev_path, table_name)
    prod_columns = get_columns(prod_path, table_name)

    missing_columns = [
        col for col in dev_columns
        if col not in prod_columns
    ]

    if not missing_columns:
        log_and_print("✅ Table associations à jour : aucune colonne manquante.")
        return

    log_and_print(
        f"🔍 Colonnes manquantes dans associations (PROD) : {missing_columns}"
    )

    # --- Mise à jour PROD ---
    with sqlite3.connect(prod_path) as conn:
        cursor = conn.cursor()

        for col in missing_columns:
            col_type = dev_columns[col][2] or "TEXT"
            default_val = dev_columns[col][4]

            default_clause = (
                f"DEFAULT '{default_val}'"
                if default_val is not None else ""
            )

            alter_stmt = (
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN `{col}` {col_type} {default_clause}"
            )

            try:
                cursor.execute(alter_stmt)
                log_and_print(f"✅ Colonne ajoutée : {alter_stmt}")
            except Exception as e:
                log_and_print(f"❌ Erreur lors de l'ajout de {col} : {e}")

        conn.commit()

    log_and_print("🎉 Mise à jour terminée pour la table associations (PROD).")


# ============================================================
# ▶️ Point d’entrée
# ============================================================
if __name__ == "__main__":
    compare_and_update_associations_structure()
