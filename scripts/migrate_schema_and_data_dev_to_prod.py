#!/usr/bin/env python3
"""
Migration du schéma et des données de DEV vers PROD (SQLite).

Règles :
- Création des tables manquantes en PROD
- Ajout uniquement des colonnes absentes
- Copie des données :
    * intégrale si table absente
    * colonne par colonne si table existante
- Aucune suppression
- Compatible serveur Linux /srv/ba38
"""

import sqlite3
import os
from pathlib import Path

# -------------------------------------------------------------------
# Protection anti-exécution accidentelle en PROD
# -------------------------------------------------------------------
ENV = os.getenv("ENVIRONMENT", "DEV").upper()
ALLOW_PROD = os.getenv("ALLOW_PROD_MIGRATION")

if ENV == "PROD":
    raise RuntimeError("⛔ Ce script ne doit jamais être exécuté en PROD")



# -------------------------------------------------------------------
# Résolution des chemins DEV / PROD
# -------------------------------------------------------------------
BASE_DIR_DEV = Path("/srv/ba38/dev")
BASE_DIR_PROD = Path("/srv/ba38/prod")

DEV_DB_NAME = os.getenv("SQLITE_DB_DEV")
PROD_DB_NAME = os.getenv("SQLITE_DB_PROD")

if not DEV_DB_NAME or not PROD_DB_NAME:
    raise ValueError(
        "❌ Variables SQLITE_DB_DEV / SQLITE_DB_PROD non définies"
    )

DEV_DB = BASE_DIR_DEV / DEV_DB_NAME
PROD_DB = BASE_DIR_PROD / PROD_DB_NAME

if not DEV_DB.exists():
    raise FileNotFoundError(f"❌ Base DEV introuvable : {DEV_DB}")

if not PROD_DB.exists():
    raise FileNotFoundError(f"❌ Base PROD introuvable : {PROD_DB}")

# -------------------------------------------------------------------
# Fonctions utilitaires
# -------------------------------------------------------------------
def get_table_names(conn):
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in cur.fetchall()]

def get_columns(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1]: row[2] for row in cur.fetchall()}

def table_is_empty(conn, table):
    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0] == 0

def copy_table_data(dev_conn, prod_conn, table):
    print(f"📥 Copie intégrale de la table '{table}' (table absente en PROD)")
    rows = dev_conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print("  ⚠️ Aucune donnée à copier")
        return

    placeholders = ",".join("?" * len(rows[0]))
    prod_conn.executemany(
        f"INSERT INTO {table} VALUES ({placeholders})",
        rows
    )
    prod_conn.commit()
    print(f"  ✅ {len(rows)} lignes copiées")

def sync_columns(dev_conn, prod_conn, table):
    dev_cols = get_columns(dev_conn, table)
    prod_cols = get_columns(prod_conn, table)

    new_cols = {
        col: typ for col, typ in dev_cols.items()
        if col not in prod_cols
    }

    if not new_cols:
        print(f"✅ Table '{table}' : structure identique")
        return

    print(f"🧩 Table '{table}' : ajout de {len(new_cols)} colonne(s)")
    for col, typ in new_cols.items():
        prod_conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
        )
        print(f"  ➕ {col} ({typ})")

    # Mise à jour des lignes existantes si besoin
    if not table_is_empty(prod_conn, table):
        for col in new_cols:
            try:
                prod_conn.execute(f"""
                    UPDATE {table}
                    SET {col} = (
                        SELECT dev.{col}
                        FROM dev.{table} AS dev
                        WHERE dev.id = {table}.id
                    )
                    WHERE EXISTS (
                        SELECT 1 FROM dev.{table} AS dev
                        WHERE dev.id = {table}.id
                    );
                """)
                print(f"  🔄 Colonne '{col}' synchronisée")
            except Exception as e:
                print(f"  ⚠️ Erreur sur '{col}': {e}")

    prod_conn.commit()

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("🚀 Migration schéma & données DEV → PROD")
    print(f"📦 DEV  : {DEV_DB}")
    print(f"📦 PROD : {PROD_DB}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    with sqlite3.connect(DEV_DB) as dev_conn, \
         sqlite3.connect(PROD_DB) as prod_conn:

        dev_conn.row_factory = sqlite3.Row
        prod_conn.row_factory = sqlite3.Row

        # ATTACH DEV dans PROD pour accès croisé
        prod_conn.execute(f"ATTACH DATABASE '{DEV_DB}' AS dev")

        dev_tables = get_table_names(dev_conn)
        prod_tables = get_table_names(prod_conn)

        for table in dev_tables:
            if table not in prod_tables:
                print(f"🆕 Table '{table}' absente en PROD : création")
                schema = dev_conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?",
                    (table,)
                ).fetchone()[0]

                prod_conn.execute(schema)
                copy_table_data(dev_conn, prod_conn, table)
            else:
                sync_columns(dev_conn, prod_conn, table)

        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("✅ Migration terminée avec succès")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

if __name__ == "__main__":
    main()
