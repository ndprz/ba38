#!/usr/bin/env python3
"""
sync_plannings_from_prod.py

Copie les tables de plannings de PROD vers DEV :

- plannings_ramasse
- plannings_pal
- plannings_distribution
- plannings_pesee
- plannings_vif

⚠ À lancer depuis l'environnement DEV.
"""

import os
import shutil
import sqlite3
from datetime import datetime

# --- À adapter si nécessaire (ou lire depuis .env) -------------------------
PROD_DB = "/home/ndprz/ba380/ba380.sqlite"
DEV_DB = "/home/ndprz/dev/ba380dev.sqlite"

TABLES_A_SYNC = [
    "plannings_ramasse",
    "plannings_pal",
    "plannings_distribution",
    "plannings_pesee",
    "plannings_vif",
]


def backup_dev_db(dev_db_path: str) -> str:
    """Crée une copie de sauvegarde de la base DEV avec timestamp."""
    if not os.path.exists(dev_db_path):
        raise FileNotFoundError(f"Base DEV introuvable : {dev_db_path}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{dev_db_path}.backup_{ts}.sqlite"
    shutil.copy2(dev_db_path, backup_path)
    print(f"[OK] Backup DEV créé : {backup_path}")
    return backup_path


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cur.fetchone() is not None


def copy_table(prod_conn: sqlite3.Connection, dev_conn: sqlite3.Connection, table: str) -> None:
    """Remplace le contenu de table dans DEV par celui de PROD."""
    print(f"\n=== Synchronisation de la table {table} ===")

    if not table_exists(prod_conn, table):
        print(f"  [WARN] Table {table} inexistante en PROD, on saute.")
        return

    if not table_exists(dev_conn, table):
        print(f"  [WARN] Table {table} inexistante en DEV, on saute.")
        return

    prod_cur = prod_conn.cursor()
    dev_cur = dev_conn.cursor()

    # Vérification rapide du schéma (même nombre de colonnes)
    prod_cur.execute(f"PRAGMA table_info({table})")
    prod_cols = prod_cur.fetchall()
    dev_cur.execute(f"PRAGMA table_info({table})")
    dev_cols = dev_cur.fetchall()

    if len(prod_cols) != len(dev_cols):
        print(
            f"  [ERREUR] Schéma différent entre PROD ({len(prod_cols)} col.) et DEV ({len(dev_cols)} col.) "
            f"pour {table}. Synchronisation annulée pour cette table."
        )
        return

    # On vide la table DEV
    dev_cur.execute(f"DELETE FROM {table}")
    dev_conn.commit()

    # On copie toutes les lignes de PROD vers DEV
    prod_cur.execute(f"SELECT * FROM {table}")
    rows = prod_cur.fetchall()
    if not rows:
        print("  [INFO] Aucune ligne en PROD, table vide copiée.")
        return

    placeholders = ",".join(["?"] * len(prod_cols))
    insert_sql = f"INSERT INTO {table} VALUES ({placeholders})"

    dev_cur.executemany(insert_sql, rows)
    dev_conn.commit()

    print(f"  [OK] {len(rows)} lignes copiées de PROD vers DEV pour {table}.")


def main():
    print("=== Synchronisation des plannings PROD → DEV ===")
    print(f"  PROD : {PROD_DB}")
    print(f"  DEV  : {DEV_DB}")

    # Backup DEV
    backup_dev_db(DEV_DB)

    with sqlite3.connect(PROD_DB) as prod_conn, sqlite3.connect(DEV_DB) as dev_conn:
        for table in TABLES_A_SYNC:
            copy_table(prod_conn, dev_conn, table)

    print("\n=== Terminé. Vérifie tes plannings en DEV. ===")


if __name__ == "__main__":
    main()
