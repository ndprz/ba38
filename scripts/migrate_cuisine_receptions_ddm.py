#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : DDM (date de durabilité minimale) en réception + forçage tracé
(module Production Cuisine).

- cuisine_receptions.ddm (TEXT, AAAA-MM-JJ) : à côté de la DLC (colonne
  dlc_ddm, nom historique, qui ne contient que la DLC). Une des deux dates
  doit être saisie.
- cuisine_reception_utilisations.forcage (TEXT) : avertissement accepté au
  moment de l'utilisation (ex. "DLC dépassée (28/09/2026)"), NULL si aucun.

Tables dans EXCLUDE_TABLES de migrate_schema_and_data_dev_to_prod.py : à
lancer sur chaque base (dev, dev_test, prod, prod_test) AVANT le
déploiement du code. Chemins explicites (pas de load_dotenv). Idempotent.
"""

import argparse
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

COLONNES = [
    ("cuisine_receptions", "ddm", "TEXT"),
    ("cuisine_reception_utilisations", "forcage", "TEXT"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=list(DB_PATHS), default="dev")
    args = parser.parse_args()
    db_path = DB_PATHS[args.env]
    if args.env.startswith("prod") and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env.startswith("dev") and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")

    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        for table, colonne, type_sql in COLONNES:
            existantes = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not existantes:
                raise RuntimeError(f"❌ Table {table} absente de {db_path} (migrer d'abord migrate_cuisine_receptions_solde.py)")
            if colonne in existantes:
                print(f"= {table}.{colonne} déjà présente")
            else:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {type_sql}")
                print(f"+ {table}.{colonne} ajoutée")
    print(f"✅ Migration terminée : {db_path}")


if __name__ == "__main__":
    main()
