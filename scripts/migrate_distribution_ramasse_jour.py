#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : ramasse journalière (produits ramassés entrés en stock, à répartir sur les bons FRAIS).

Crée (idempotent) :
- ramasses_jour        : une ramasse = un jour + un fournisseur (magasin) ; validée => entrée en stock Basilic
                         et fichier de réception VIF (CSV) conservé avec la ramasse
- ramasses_jour_lignes : lignes article / poids (kg) / DLC, avec le lot calculé à la validation
- produits_ajoutables_frais : produits du stock ajoutables à la ramasse (liste gérée à l'écran des bons FRAIS)
- frais_ajouts_stock   : quantités du stock ajoutées à la ramasse d'un jour

Chemins explicites (pas de load_dotenv). Usage : migrate_distribution_ramasse_jour.py dev [dev_test] [prod] [prod_test]
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
CREATE TABLE IF NOT EXISTS ramasses_jour (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    date_ramasse         TEXT NOT NULL,                  -- AAAA-MM-JJ
    id_fournisseur       INTEGER NOT NULL,
    statut               TEXT NOT NULL DEFAULT 'saisie', -- saisie | validee
    commentaire          TEXT,
    cree_le              TEXT DEFAULT CURRENT_TIMESTAMP, -- UTC (CURRENT_TIMESTAMP)
    cree_par             TEXT,
    valide_le            TEXT,                           -- UTC
    valide_par           TEXT,
    reception_fichier    TEXT,                           -- recepba38_AAAAMMJJ_<codeVIF>_<id>.csv
    reception_csv        TEXT,
    reception_depose_le  TEXT,
    UNIQUE (date_ramasse, id_fournisseur)
);
CREATE INDEX IF NOT EXISTS idx_ramasses_jour_date ON ramasses_jour(date_ramasse);

CREATE TABLE IF NOT EXISTS ramasses_jour_lignes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ramasse_id  INTEGER NOT NULL REFERENCES ramasses_jour(id) ON DELETE CASCADE,
    ordre       INTEGER NOT NULL DEFAULT 0,
    article     TEXT NOT NULL,
    libelle     TEXT,
    quantite_kg REAL NOT NULL,
    dlc         TEXT,                                    -- jj/mm/aaaa, facultative
    lot         TEXT,                                    -- calculé à la validation (RJ + AAMMJJ + article)
    commentaire TEXT
);
CREATE INDEX IF NOT EXISTS idx_ramasses_jour_lignes ON ramasses_jour_lignes(ramasse_id);

-- Produits du stock que l'on peut ajouter à la ramasse pour atteindre le panier minimal par bénéficiaire
CREATE TABLE IF NOT EXISTS produits_ajoutables_frais (
    article  TEXT PRIMARY KEY,
    ordre    INTEGER NOT NULL DEFAULT 0,
    cree_le  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Quantités du stock ajoutées à la ramasse d'un jour (à répartir sur les bons FRAIS de ce jour)
CREATE TABLE IF NOT EXISTS frais_ajouts_stock (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date_distribution TEXT NOT NULL,
    article           TEXT NOT NULL,
    quantite_kg       REAL NOT NULL,
    cree_par          TEXT,
    cree_le           TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (date_distribution, article)
);
"""


def main():
    for env in sys.argv[1:] or ["dev"]:
        if env not in DB_PATHS:
            sys.exit(f"Cible inconnue : {env}")
        with closing(sqlite3.connect(DB_PATHS[env])) as conn:
            conn.executescript(SQL)
            conn.commit()
        print(f"[{env}] tables ramasses_jour / ramasses_jour_lignes OK")


if __name__ == "__main__":
    main()
