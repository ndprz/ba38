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
from pathlib import Path
from utils import migrate_schema_and_data

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
