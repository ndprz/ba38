#!/usr/bin/env python3
"""
🔧 Synchronisation du schéma de la table `benevoles` (DEV → PROD)

Objectif :
- Comparer la structure de la table `benevoles` entre la base DEV et la base PROD
- Ajouter en PROD les colonnes présentes en DEV mais absentes en PROD
- Ne JAMAIS modifier la base DEV
- Être totalement indépendant de ENVIRONMENT / Flask

Ce script est conçu pour être exécuté via l’interface admin_scripts.
"""

from pathlib import Path
import sys
import sqlite3

# ============================================================
# 📁 Rendre utils.py importable (racine BA38)
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from ba38_utilitaires.core import write_log, get_db_path_by_env

def log_and_print(msg: str):
    print(msg)
    write_log(msg)


# ============================================================
# 🔍 Utilitaire : lecture des colonnes d’une table SQLite
# ============================================================
def get_columns(db_path: str, table_name: str) -> dict:
    """
    Retourne un dictionnaire :
    { nom_colonne : row_pragma }
    """
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1]: row for row in cursor.fetchall()}


# ============================================================
# 🔧 Synchronisation DEV → PROD
# ============================================================
def compare_and_update_benevoles_structure():
    table_name = "benevoles"

    # --- Résolution EXPLICITE des bases ---
    dev_path = "/srv/ba38/dev/instance/ba380dev.sqlite"
    prod_path = "/srv/ba38/prod/instance/ba380.sqlite"

    # --------------------------------------------------------
    # 🛡️ GARDE-FOU CRITIQUE
    # --------------------------------------------------------
    if "/dev/" in prod_path:
        raise RuntimeError(
            f"❌ ERREUR CRITIQUE : base PROD incorrecte détectée : {prod_path}"
        )

    log_and_print("🧪 Début comparaison schéma table benevoles DEV → PROD")
    log_and_print(f"📄 DEV  : {dev_path}")
    log_and_print(f"📄 PROD : {prod_path}")

    # --- Lecture des schémas ---
    dev_columns = get_columns(dev_path, table_name)
    prod_columns = get_columns(prod_path, table_name)

    missing_columns = [
        col for col in dev_columns
        if col not in prod_columns
    ]

    if not missing_columns:
        log_and_print("✅ Table benevoles à jour : aucune colonne manquante.")
        return

    log_and_print(f"🔍 Colonnes manquantes en PROD : {missing_columns}")

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
                log_and_print(f"❌ Erreur ajout colonne {col} : {e}")

        conn.commit()

    log_and_print("🎉 Mise à jour terminée pour la table benevoles (PROD).")


# ============================================================
# ▶️ Point d’entrée
# ============================================================
if __name__ == "__main__":
    compare_and_update_benevoles_structure()
