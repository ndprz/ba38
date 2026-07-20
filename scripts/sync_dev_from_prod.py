#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from utils import write_log

# -------------------------------------------------------------------
# CONFIG — chemins absolus : cette opération traverse toujours les deux
# arborescences (PROD → DEV), quel que soit l'environnement qui l'exécute.
# -------------------------------------------------------------------
PROD_DB = Path("/srv/ba38/prod/instance/ba380.sqlite")
DEV_DB = Path("/srv/ba38/dev/instance/ba380dev.sqlite")

SUMMARY = []


def summary(msg):
    SUMMARY.append(msg)
    print(msg)
    write_log(msg)


def sync_dev_from_prod():
    SUMMARY.clear()
    summary("🔄 Synchronisation base DEV ← base PROD")

    if not PROD_DB.exists():
        raise FileNotFoundError(f"❌ Base PROD introuvable : {PROD_DB}")

    DEV_DB.parent.mkdir(parents=True, exist_ok=True)

    # sqlite3.Connection.backup() est sûr sur une base PROD potentiellement
    # en cours d'écriture (contrairement à une simple copie de fichier).
    src = sqlite3.connect(str(PROD_DB))
    dst = sqlite3.connect(str(DEV_DB))
    try:
        dst.execute("PRAGMA busy_timeout = 5000")
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    summary(f"✅ Base DEV remplacée par une copie de PROD : {DEV_DB}")
    summary("🎉 Synchronisation terminée")
    return "\n".join(SUMMARY)


if __name__ == "__main__":
    sync_dev_from_prod()
