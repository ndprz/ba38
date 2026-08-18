#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Préparation one-off pour la fonctionnalité "Signature électronique" générique
(upload d'un PDF quelconque, signature via LibreSign par soi-même ou par un
tiers désigné par email) : crée la table `signatures_electroniques`.

Contrairement à `annexe1bis`, une demande n'a pas de statut 'brouillon' :
elle est créée directement au moment de l'envoi à LibreSign.

Usable sur DEV ou PROD via --env (défaut : dev). Idempotent.
"""

import argparse
import os
import sys
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS signatures_electroniques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_creation TEXT NOT NULL,
    date_creation TEXT NOT NULL,
    nom_document TEXT NOT NULL,
    type_signataire TEXT NOT NULL CHECK (type_signataire IN ('moi_meme', 'tiers')),
    destinataire_nom TEXT,
    destinataire_email TEXT,
    statut TEXT NOT NULL DEFAULT 'envoyee',
    date_signature TEXT,
    libresign_uuid TEXT,
    libresign_file_id TEXT,
    libresign_sign_request_id TEXT,
    document_original_path TEXT,
    document_signe_path TEXT
)
"""


def ensure_signatures_electroniques_table(conn):
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signatures_electroniques'"
    ).fetchone()
    conn.execute(CREATE_TABLE_SQL)
    if existing:
        print("✓ Table 'signatures_electroniques' déjà présente.")
    else:
        print("✓ Table 'signatures_electroniques' créée.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    args = parser.parse_args()

    db_path = DB_PATHS[args.env]

    if args.env == "prod" and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env == "dev" and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")

    if not os.path.exists(db_path):
        print(f"❌ Base introuvable : {db_path}")
        sys.exit(1)

    print(f"➡ Préparation signatures_electroniques [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        ensure_signatures_electroniques_table(conn)
        conn.commit()

    print("✅ Préparation terminée avec succès.")


if __name__ == "__main__":
    main()
