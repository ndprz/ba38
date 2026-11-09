#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : bons de préparation de la distribution (SEC puis FRAIS).

Crée (idempotent) :
- bons_preparation        : un bon = un passage (date, type, association, heure, emplacement)
- bons_preparation_lignes : lignes d'articles du bon (quantité calculée + quantité retenue modifiable)

Chemins explicites (pas de load_dotenv, cf. piège dev/.env).
Usage : migrate_distribution_bons_prepa.py dev [dev_test] [prod] [prod_test]
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
CREATE TABLE IF NOT EXISTS bons_preparation (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date_distribution  TEXT NOT NULL,                 -- AAAA-MM-JJ
    type_bon           TEXT NOT NULL CHECK (type_bon IN ('sec', 'frais')),
    association_id     INTEGER NOT NULL,
    heure              TEXT NOT NULL DEFAULT '',
    emplacement        TEXT NOT NULL DEFAULT '',
    menus              TEXT,                          -- codes menus du passage (« A, B »)
    nbre_beneficiaires INTEGER,
    besoins_particuliers TEXT,
    statut             TEXT NOT NULL DEFAULT 'preparation',  -- preparation | validee | bl
    commentaire        TEXT,
    cree_le            TEXT DEFAULT CURRENT_TIMESTAMP,
    cree_par           TEXT,
    modifie_le         TEXT,
    UNIQUE (date_distribution, type_bon, association_id, heure, emplacement)
);
CREATE INDEX IF NOT EXISTS idx_bons_prepa_date ON bons_preparation(date_distribution, type_bon);

CREATE TABLE IF NOT EXISTS bons_preparation_lignes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bon_id        INTEGER NOT NULL REFERENCES bons_preparation(id) ON DELETE CASCADE,
    ordre         INTEGER NOT NULL DEFAULT 0,
    menu_code     TEXT,
    article       TEXT NOT NULL,
    libelle       TEXT,
    source        TEXT NOT NULL,                      -- portion | fixe | sans_portion
    portion       REAL,                               -- kgn / personne
    coef          REAL,                               -- net -> brut
    quantite_calc REAL,                               -- quantité calculée à la génération
    quantite      REAL,                               -- quantité retenue (modifiable)
    unite         TEXT,                               -- Kg | kgn | COL | P ...
    commentaire   TEXT
);
CREATE INDEX IF NOT EXISTS idx_bons_prepa_lignes_bon ON bons_preparation_lignes(bon_id);
"""


def main():
    cibles = sys.argv[1:] or ["dev"]
    for env in cibles:
        if env not in DB_PATHS:
            sys.exit(f"Cible inconnue : {env}")
        with closing(sqlite3.connect(DB_PATHS[env])) as conn:
            conn.executescript(SQL)
            conn.commit()
        print(f"[{env}] tables bons_preparation / bons_preparation_lignes OK")


if __name__ == "__main__":
    main()
