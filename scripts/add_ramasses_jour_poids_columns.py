#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ramasse journalière = feuille de ramasse des camions : 3 poids supplémentaires par ligne
(ramasses_jour_lignes). La colonne existante `quantite_kg` devient le poids
« entrée bonne date à distribuer » (c'est elle qui entre en stock et part dans le fichier de
réception VIF, comportement inchangé).

    poids_jour       pas OK pour la distribution, à confirmer par un responsable distribution
    poids_rebut      pas OK ; mouvement de stock spécial (règle à valider)
    poids_3etoiles   déposé directement à la cuisine 3 étoiles (BL à faire, règle à valider)

Ces trois poids sont SAISIS mais n'ont encore aucun effet (stock, VIF, BL) : règles en cours
de validation. NULL = non renseigné. Une ligne peut n'avoir que ces poids (quantite_kg = 0) :
elle n'entre pas en stock.

Idempotent : ALTER TABLE ADD COLUMN seulement si la colonne est absente. Chemins de base
explicites (pas de load_dotenv : piège connu). Utilisable sur DEV ou PROD (+ bases test).
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

COLONNES = ["poids_jour", "poids_rebut", "poids_3etoiles"]


def migrer(conn):
    """Ajoute les colonnes manquantes ; retourne la liste de celles qui viennent d'être ajoutées."""
    existantes = {r[1] for r in conn.execute("PRAGMA table_info(ramasses_jour_lignes)")}
    if not existantes:
        raise SystemExit("Table ramasses_jour_lignes introuvable.")
    ajoutees = []
    for nom in COLONNES:
        if nom not in existantes:
            conn.execute(f"ALTER TABLE ramasses_jour_lignes ADD COLUMN {nom} REAL")
            ajoutees.append(nom)
    conn.commit()
    return ajoutees


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=sorted(DB_PATHS), default="dev")
    args = parser.parse_args()
    db = DB_PATHS[args.env]
    print(f"Base : {db}")
    with closing(sqlite3.connect(db, timeout=30)) as conn:
        ajoutees = migrer(conn)
        for nom in COLONNES:
            print(f"  colonne {nom} {'ajoutée' if nom in ajoutees else 'déjà présente'}")
    print("OK")


if __name__ == "__main__":
    main()
