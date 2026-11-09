#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : planning_deplacements.sens accepte aussi 'annule'
(passage annulé un jour de fermeture de la BA38, ni déplacé avant ni après).

SQLite ne modifie pas un CHECK : la table est recréée (données conservées). Idempotent.
Chemins explicites (pas de load_dotenv).
Usage : migrate_distribution_deplacement_annule.py dev [dev_test] [prod] [prod_test]
"""
import sqlite3
import sys
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

NOUVELLE = """
CREATE TABLE planning_deplacements (
    ligne_id     INTEGER NOT NULL,    -- associations_menus.id
    date_origine TEXT NOT NULL,       -- jour de passage normal (fermé)
    sens         TEXT NOT NULL CHECK (sens IN ('suivant', 'precedent', 'annule')),
    PRIMARY KEY (ligne_id, date_origine)
)"""


def main():
    for env in sys.argv[1:] or ["dev"]:
        if env not in DB_PATHS:
            sys.exit(f"Cible inconnue : {env}")
        with closing(sqlite3.connect(DB_PATHS[env])) as conn:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE name='planning_deplacements'").fetchone()
            if row is None:
                print(f"[{env}] table absente : lancer d'abord migrate_distribution_fermetures.py")
                continue
            if "annule" in row[0]:
                print(f"[{env}] déjà à jour")
                continue
            conn.execute("BEGIN")
            conn.execute("ALTER TABLE planning_deplacements RENAME TO planning_deplacements_old")
            conn.execute(NOUVELLE)
            conn.execute("INSERT INTO planning_deplacements SELECT ligne_id, date_origine, sens FROM planning_deplacements_old")
            conn.execute("DROP TABLE planning_deplacements_old")
            conn.commit()
            print(f"[{env}] planning_deplacements mise à jour")


if __name__ == "__main__":
    main()
