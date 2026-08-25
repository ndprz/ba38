#!/usr/bin/env python3
"""
Synchronisation des bases TEST à partir de la base DEV.

- DEV        → DEV_TEST
- DEV        → PROD_TEST
- Ajout des tables / colonnes manquantes
- Copie optionnelle des données
- Compatible serveur Linux /srv/ba38
"""

import os
import sqlite3
from pathlib import Path
from ba38_utilitaires.core import migrate_schema_and_data, write_log

# -------------------------------------------------------------------
# Résolution des chemins
# -------------------------------------------------------------------
BASE_DEV = Path("/srv/ba38/dev")
BASE_PROD = Path("/srv/ba38/prod")

DEV_DB_NAME = os.getenv("SQLITE_DB_DEV")
DEV_TEST_DB_NAME = os.getenv("SQLITE_DB_DEV_TEST")
PROD_TEST_DB_NAME = os.getenv("SQLITE_DB_PROD_TEST")

if not DEV_DB_NAME or not DEV_TEST_DB_NAME or not PROD_TEST_DB_NAME:
    raise ValueError(
        "❌ Variables SQLITE_DB_DEV / SQLITE_DB_DEV_TEST / SQLITE_DB_PROD_TEST manquantes"
    )

DEV_DB = BASE_DEV / DEV_DB_NAME
DEV_TEST_DB = BASE_DEV / DEV_TEST_DB_NAME
PROD_TEST_DB = BASE_PROD / PROD_TEST_DB_NAME


# -------------------------------------------------------------------
# Protection anti-erreur PROD
# -------------------------------------------------------------------
ENV = os.getenv("ENVIRONMENT", "DEV").upper()
ALLOW_TEST_SYNC = os.getenv("ALLOW_TEST_SYNC")

if ENV == "PROD" and ALLOW_TEST_SYNC != "YES":
    raise RuntimeError(
        "⛔ Synchronisation TEST bloquée en PROD.\n"
        "Pour autoriser explicitement :\n"
        "  ALLOW_TEST_SYNC=YES python3 sync_test_schemas.py"
    )

# -------------------------------------------------------------------
# users / roles_utilisateurs : remplacement intégral depuis DEV (réel)
# -------------------------------------------------------------------
def sync_users_and_roles(dest_db_path: str):
    """Remplace entièrement `users` et `roles_utilisateurs` dans `dest_db_path`
    par une copie fidèle de la base DEV réelle, pour que TOUS les comptes
    (pas seulement les comptes test_only) aient en base TEST les mêmes droits
    qu'en réel."""
    source_conn = sqlite3.connect(str(DEV_DB))
    dest_conn = sqlite3.connect(dest_db_path)
    try:
        for table in ("users", "roles_utilisateurs"):
            source_cols = [r[1] for r in source_conn.execute(f"PRAGMA table_info({table})")]
            dest_cols = {r[1] for r in dest_conn.execute(f"PRAGMA table_info({table})")}
            common_cols = [c for c in source_cols if c in dest_cols]

            rows = source_conn.execute(
                f"SELECT {', '.join(common_cols)} FROM {table}"
            ).fetchall()

            dest_conn.execute(f"DELETE FROM {table}")
            if rows:
                placeholders = ", ".join(["?"] * len(common_cols))
                dest_conn.executemany(
                    f"INSERT INTO {table} ({', '.join(common_cols)}) VALUES ({placeholders})",
                    rows,
                )
            dest_conn.commit()
            write_log(f"🔁 {table} remplacée dans {dest_db_path} ({len(rows)} lignes)")
    finally:
        source_conn.close()
        dest_conn.close()


# -------------------------------------------------------------------
# API appelée par Flask
# -------------------------------------------------------------------
def sync_test_databases(copy_data: bool = False):
    missing = []

    if not DEV_DB.exists():
        missing.append(f"Base DEV manquante : {DEV_DB}")
    if not DEV_TEST_DB.exists():
        missing.append(f"Base DEV_TEST manquante : {DEV_TEST_DB}")
    if not PROD_TEST_DB.exists():
        missing.append(f"Base PROD_TEST manquante : {PROD_TEST_DB}")

    if missing:
        raise RuntimeError(
            "⚠️ Bases TEST absentes.\n"
            "Veuillez d’abord lancer : « Créer les bases TEST anonymisées ».\n\n"
            + "\n".join(missing)
        )

    print("🔁 Synchronisation DEV → DEV_TEST")
    migrate_schema_and_data(str(DEV_DB), str(DEV_TEST_DB), copy_data)

    print("🔁 Synchronisation DEV → PROD_TEST")
    migrate_schema_and_data(str(DEV_DB), str(PROD_TEST_DB), copy_data)
