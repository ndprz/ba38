#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : exceptions par ligne association/menu.

Crée (idempotent) associations_menus_exceptions : pour une ligne de menu d'une association,
- type 'exclu' : l'article du menu n'est pas servi à cette association ;
- type 'ajout' : article servi en plus du menu (quantité/unité facultatives : sans quantité, calcul par portion
  si l'article en a une, sinon ligne à saisir sur le bon).

Chemins explicites (pas de load_dotenv). Usage : migrate_distribution_menus_exceptions.py dev [dev_test] [prod] [prod_test]
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

SQL = """
CREATE TABLE IF NOT EXISTS associations_menus_exceptions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ligne_id  INTEGER NOT NULL,               -- associations_menus.id
    article   TEXT NOT NULL,
    type      TEXT NOT NULL CHECK (type IN ('exclu', 'ajout')),
    quantite  REAL,                            -- ajout : quantité fixe facultative
    unite     TEXT,                            -- ajout : Kg | kgn | COL | P ...
    cree_le   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ligne_id, article)
);
CREATE INDEX IF NOT EXISTS idx_assoc_menus_exc_ligne ON associations_menus_exceptions(ligne_id);
"""


def main():
    for env in sys.argv[1:] or ["dev"]:
        if env not in DB_PATHS:
            sys.exit(f"Cible inconnue : {env}")
        with closing(sqlite3.connect(DB_PATHS[env])) as conn:
            conn.executescript(SQL)
            conn.commit()
        print(f"[{env}] table associations_menus_exceptions OK")


if __name__ == "__main__":
    main()
