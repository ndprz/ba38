#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Préparation one-off pour l'intégration LibreSign sur l'Annexe 1 bis (remplace
Yousign, abandonné le 2026-07-16 pour raison de coût) : ajoute les colonnes de
suivi sur `annexe1bis`.

Colonnes ajoutées :
- libresign_uuid            : uuid du fichier LibreSign (identifiant public)
- libresign_file_id         : id numérique du fichier LibreSign
- libresign_sign_request_id : id numérique du signataire pour ce fichier

Les colonnes historiques Yousign (yousign_signature_request_id,
yousign_document_id, nom_signataire_saisi) restent en base, inutilisées.

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

NOUVELLES_COLONNES = [
    ("libresign_uuid", "TEXT"),
    ("libresign_file_id", "TEXT"),
    ("libresign_sign_request_id", "TEXT"),
]


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_annexe1bis_columns(conn):
    existing = table_columns(conn, "annexe1bis")
    added = []
    for col, decl in NOUVELLES_COLONNES:
        if col not in existing:
            conn.execute(f"ALTER TABLE annexe1bis ADD COLUMN `{col}` {decl}")
            added.append(col)
    if added:
        print(f"✓ Colonnes ajoutées sur 'annexe1bis' : {', '.join(added)}")
    else:
        print("✓ Colonnes déjà présentes sur 'annexe1bis'.")


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

    print(f"➡ Préparation LibreSign annexe1bis [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        ensure_annexe1bis_columns(conn)
        conn.commit()

    print("✅ Préparation terminée avec succès.")


if __name__ == "__main__":
    main()
