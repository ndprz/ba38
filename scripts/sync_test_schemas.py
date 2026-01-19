import sqlite3
import argparse
import os
from dotenv import load_dotenv

load_dotenv()

# 📁 Chargement des chemins depuis le .env
DEV_SOURCE = os.getenv("SQLITE_DB")
TEST_DBS = [
    os.getenv("SQLITE_TEST_DB"),       # /home/ndprz/dev/ba380dev_test.sqlite
    os.getenv("SQLITE_TEST_DB_PROD")   # /home/ndprz/ba380/ba380_test.sqlite
]

def get_columns(conn, table):
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {col[1]: col[2] for col in cursor.fetchall()}  # {column_name: column_type}

def table_exists(conn, table):
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None

def sync_schema(source_path, target_path, copy_data=False):
    print(f"🔄 Sync {target_path} ⇐ {source_path}")
    with sqlite3.connect(source_path) as src_conn, sqlite3.connect(target_path) as tgt_conn:
        src_cursor = src_conn.cursor()
        src_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in src_cursor.fetchall()]

        for table in tables:
            if not table_exists(tgt_conn, table):
                print(f"➕ Table manquante : {table} → création")
                schema = src_conn.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                tgt_conn.execute(schema)
                if copy_data:
                    tgt_conn.execute(f"INSERT INTO {table} SELECT * FROM main.{table}")
                continue

            src_cols = get_columns(src_conn, table)
            tgt_cols = get_columns(tgt_conn, table)
            missing_cols = {col: typ for col, typ in src_cols.items() if col not in tgt_cols}

            for col, typ in missing_cols.items():
                print(f"🆕 Colonne à ajouter dans {table}: {col} ({typ})")
                tgt_conn.execute(f'ALTER TABLE {table} ADD COLUMN "{col}" {typ}')

                if copy_data:
                    # Copie des valeurs si la colonne est NULL
                    rows = src_conn.execute(f"SELECT rowid, {col} FROM {table}").fetchall()
                    for rowid, value in rows:
                        if value is not None:
                            tgt_conn.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (value, rowid))

        tgt_conn.commit()
        print(f"✅ Terminé : {target_path}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synchronise les schémas des bases de test avec la base principale.")
    parser.add_argument("--copy-data", action="store_true", help="Copier aussi les données pour les nouvelles colonnes")

    args = parser.parse_args()

    for target_path in TEST_DBS:
        if os.path.exists(target_path):
            sync_schema(DEV_SOURCE, target_path, copy_data=args.copy_data)
        else:
            print(f"❌ Base cible introuvable : {target_path}")

from utils import migrate_schema_and_data

def sync_test_databases(copy_data: bool = False):
    """
    Copie les nouveaux champs et éventuellement les données depuis ba380dev.sqlite
    vers ba380dev_test.sqlite et ba380_test.sqlite
    """
    base_dir = '/home/ndprz'

    dev = os.path.join(base_dir, 'dev', 'ba380dev.sqlite')
    dev_test = os.path.join(base_dir, 'dev', 'ba380dev_test.sqlite')
    prod_test = os.path.join(base_dir, 'ba380', 'ba380_test.sqlite')

    print("🔁 Synchronisation DEV → DEV_TEST")
    migrate_schema_and_data(dev, dev_test, copy_data)

    print("🔁 Synchronisation DEV → PROD_TEST")
    migrate_schema_and_data(dev, prod_test, copy_data)