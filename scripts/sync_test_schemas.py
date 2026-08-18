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
import random
import sqlite3
import string
from pathlib import Path
from werkzeug.security import generate_password_hash
from utils import migrate_schema_and_data, write_log

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
# Générateurs pour l'anonymisation des comptes users
# -------------------------------------------------------------------
def _rnd_txt(prefix="TXT"):
    return f"{prefix}_{''.join(random.choices(string.ascii_uppercase, k=5))}"

def _rnd_email():
    return ''.join(random.choices(string.ascii_lowercase, k=8)) + "@example.org"


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


def anonymize_users_table(dest_db_path: str):
    """Anonymise les données personnelles de `users` (email, nom, mot de
    passe) dans la base de test, après une synchronisation fidèle des droits
    par sync_users_and_roles(). L'identité réelle n'y sert à rien : le login
    interroge toujours la base réelle (get_real_db_connection), jamais la
    base TEST. `roles_utilisateurs.user_email` est mis à jour en cohérence
    pour ne pas casser le lien users/roles."""
    with sqlite3.connect(dest_db_path) as conn:
        rows = conn.execute("SELECT id, email FROM users").fetchall()
        for user_id, real_email in rows:
            new_email = _rnd_email()
            conn.execute(
                "UPDATE users SET email=?, username=?, password_hash=? WHERE id=?",
                (new_email, _rnd_txt("User"), generate_password_hash(_rnd_txt("PWD")), user_id),
            )
            conn.execute(
                "UPDATE roles_utilisateurs SET user_email=? WHERE user_email=?",
                (new_email, real_email),
            )
        conn.commit()
    write_log(f"🕶️ Table users anonymisée dans {dest_db_path} ({len(rows)} comptes)")


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
