#!/usr/bin/env python3
"""
🔁 Recrée la table `benevoles_inactifs` dans les bases DEV et PROD,
avec la même structure que `benevoles` + champ `motif_inactivite`.
"""

import os
import sqlite3
from dotenv import load_dotenv
import sys
sys.path.insert(0, "/home/ndprz/dev")
from utils import write_log


def recreate_table(db_path, label):
    write_log(f"📂 {label} : {db_path}")
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            write_log(f"🗑 {label} : suppression table benevoles_inactifs (si existante)")
            cursor.execute("DROP TABLE IF EXISTS benevoles_inactifs")

            write_log(f"🛠 {label} : création table depuis benevoles")
            cursor.execute("CREATE TABLE benevoles_inactifs AS SELECT * FROM benevoles WHERE 1=0")

            write_log(f"➕ {label} : ajout colonne motif_inactivite")
            cursor.execute("ALTER TABLE benevoles_inactifs ADD COLUMN motif_inactivite TEXT")

        write_log(f"✅ Table benevoles_inactifs recréée dans {label}")
    except Exception as e:
        write_log(f"❌ Erreur {label} : {e}")
        write_log(f"❌ Erreur {label} : {e}")

if __name__ == "__main__":
    # Charger les variables DEV
    load_dotenv("/home/ndprz/dev/.env")
    dev_db = os.path.join("/home/ndprz/dev", os.getenv("SQLITE_DB_DEV"))
    recreate_table(dev_db, "DEV")

    # Charger les variables PROD
    load_dotenv("/home/ndprz/ba380/.env")
    prod_db = os.path.join("/home/ndprz/ba380", os.getenv("SQLITE_DB_PROD"))
    recreate_table(prod_db, "PROD")
