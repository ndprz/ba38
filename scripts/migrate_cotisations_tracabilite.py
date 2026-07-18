#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : traçabilité des envois de mails "Relance cotisations".

Contexte : l'envoi des relances (ba38_tresorerie.py::cotisations_relance)
tournait de façon synchrone dans le cycle requête/réponse (risque de timeout
nginx/gunicorn comme l'incident indicateurs du 1er juillet 2026, voir mémoire
ba38_indicateurs_tracabilite_envois), sans try/except par ligne (un échec
Mailjet arrêtait tout le lot) et sans aucune traçabilité persistée. Même
dispositif que pour indicateurs/factures/participation (mémoire
ba38_participation_v2, ba38_renvoi_gmail_rebonds).

Ajoute à `cotisations` :
- relance_sujet, relance_corps (figés à l'envoi, pour un renvoi Gmail fidèle
  même si le modèle de mail change plus tard)
- relance_mail_erreur, relance_mailjet_status, relance_mailjet_message_ids
- relance_statut_final, relance_statut_verifie_le
- relance_renvoi_gmail_le

Idempotent : n'ajoute que les colonnes manquantes (PRAGMA table_info).
Utilisable sur DEV ou PROD via l'argument --env (défaut : dev).
"""

import argparse
import os
import sys
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

COTISATIONS_COLUMNS = [
    ("relance_sujet", "TEXT"),
    ("relance_corps", "TEXT"),
    ("relance_mail_erreur", "TEXT"),
    ("relance_mailjet_status", "TEXT"),
    ("relance_mailjet_message_ids", "TEXT"),
    ("relance_statut_final", "TEXT"),
    ("relance_statut_verifie_le", "TEXT"),
    ("relance_renvoi_gmail_le", "TEXT"),
]


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_missing_columns(conn, table, columns):
    existing = table_columns(conn, table)
    added = []
    for name, decl in columns:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        added.append(name)
    if added:
        print(f"✓ {table} : colonne(s) ajoutée(s) : {', '.join(added)}")
    else:
        print(f"✓ {table} : déjà à jour, rien à faire.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["dev", "prod", "dev_test", "prod_test"], default="dev")
    args = parser.parse_args()

    db_path = DB_PATHS[args.env]

    if args.env == "prod" and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env == "dev" and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")
    if args.env in ("dev_test", "prod_test") and "_test.sqlite" not in db_path:
        raise RuntimeError(f"❌ Chemin TEST invalide : {db_path}")

    if not os.path.exists(db_path):
        print(f"❌ Base introuvable : {db_path}")
        sys.exit(1)

    print(f"➡ Migration traçabilité relance cotisations [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        add_missing_columns(conn, "cotisations", COTISATIONS_COLUMNS)

        conn.commit()

    print("✓ Migration terminée.")


if __name__ == "__main__":
    main()
