#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Drapeau « fiche à compléter » sur les bénévoles créés en ajout rapide (fenêtre « ➕ Ajouter un
bénévole… » des plannings : seulement nom, prénom, civilité, type et téléphone).

    benevoles.fiche_a_completer           'oui' = bandeau d'avertissement sur la fiche ; défaut 'non'
    benevoles_inactifs.fiche_a_completer  même colonne : l'archivage / la restauration copient les colonnes
                                          communes aux deux tables, le drapeau doit donc exister dans les deux

La colonne n'est volontairement PAS déclarée dans field_groups : l'enregistrement de la fiche ne la
touche pas (seul le bouton « Marquer la fiche comme complétée » la remet à 'non').

Idempotent : ALTER TABLE ADD COLUMN seulement si la colonne est absente. Chemins de base explicites
(pas de load_dotenv : piège connu). À passer sur PROD ET prod_test AVANT le déploiement du code
(le script de migration générique de deploy_to_prod.sh ne doit pas la créer lui-même).
Utilisable sur DEV ou PROD (+ bases test) via --env (défaut : dev).
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
TABLES = ["benevoles", "benevoles_inactifs"]
COLONNE = "fiche_a_completer"


def migrer(conn):
    """Ajoute la colonne là où elle manque ; retourne {table: 'ajoutée' | 'déjà présente'}."""
    etat = {}
    for table in TABLES:
        colonnes = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not colonnes:
            raise SystemExit(f"Table {table} introuvable.")
        if COLONNE in colonnes:
            etat[table] = "déjà présente"
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {COLONNE} TEXT DEFAULT 'non'")
            etat[table] = "ajoutée"
    conn.commit()
    return etat


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=sorted(DB_PATHS), default="dev")
    args = parser.parse_args()
    db = DB_PATHS[args.env]
    print(f"Base : {db}")
    with closing(sqlite3.connect(db, timeout=30)) as conn:
        for table, etat in migrer(conn).items():
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}.{COLONNE} {etat} ({n} ligne(s), valeur 'non' par défaut)")
    print("OK")


if __name__ == "__main__":
    main()
