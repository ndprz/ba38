#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : tonnage trimestre (colis VIF) sur les indicateurs.

Ajoute `indicateurs_suivi.tonnage_kg_net` (REAL), rempli par import d'un
export VIF ("Etat du BL / Client / Livré à / Kg Net / Kg Brut", cp1252,
tabulations) — colonne "Client" = code_VIF exact (avec zéros de tête,
contrairement au CSV AMS qui alimente statut_csv). Affiché en colonne
"Tonnage Trimestre" sur l'écran Résultats indicateurs.

Idempotent : n'ajoute que la colonne si absente (PRAGMA table_info).
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

SUIVI_COLUMNS = [
    ("tonnage_kg_net", "REAL"),
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

    print(f"➡ Migration tonnage indicateurs [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        add_missing_columns(conn, "indicateurs_suivi", SUIVI_COLUMNS)
        conn.commit()

    print("✓ Migration terminée.")


if __name__ == "__main__":
    main()
