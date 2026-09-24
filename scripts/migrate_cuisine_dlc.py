#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : DLC des barquettes (module Production Cuisine).

- Paramètre `cuisine_dlc_jours` (table `parametres`, catégorie 'config') :
  DLC = date de production + N jours (J+3 à la création, J+5 prévu après
  l'agrément DDETS). Créé s'il est absent, jamais écrasé.
- Colonne `dlc` (AAAA-MM-JJ) sur cuisine_stock_barquettes et
  cuisine_bons_livraison_lignes, figée à l'entrée en stock.
- Rattrapage des lignes existantes sans DLC avec la règle du paramètre.

Script dédié plutôt que migrate_production_cuisine.py : ce dernier ré-upserte
les référentiels (étapes, articles barquettes), à ne pas relancer en PROD.
Les deux tables sont dans EXCLUDE_TABLES de migrate_schema_and_data_dev_to_prod.py
(qui n'y ajoute donc AUCUNE colonne en PROD) : ce script doit être lancé sur
chaque base, AVANT le déploiement du code.

Chemins explicites (pas de load_dotenv, cf. piège dev/.env). Idempotent.
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

PARAM_NAME = "cuisine_dlc_jours"
DLC_JOURS_DEFAUT = 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=list(DB_PATHS), default="dev")
    args = parser.parse_args()
    db_path = DB_PATHS[args.env]
    if args.env.startswith("prod") and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env.startswith("dev") and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        for table in ("cuisine_stock_barquettes", "cuisine_bons_livraison_lignes"):
            colonnes = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not colonnes:
                raise RuntimeError(f"❌ Table {table} absente de {db_path}")
            if "dlc" in colonnes:
                print(f"ℹ️  {table}.dlc déjà présente")
            else:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN dlc TEXT")
                print(f"✅ {table}.dlc ajoutée")

        row = conn.execute("SELECT param_value FROM parametres WHERE param_name = ?", (PARAM_NAME,)).fetchone()
        if row:
            jours = int(row[0])
            print(f"ℹ️  Paramètre {PARAM_NAME} déjà présent : J+{jours}")
        else:
            jours = DLC_JOURS_DEFAUT
            conn.execute(
                "INSERT INTO parametres (param_name, param_value, categorie) VALUES (?, ?, 'config')",
                (PARAM_NAME, str(jours)),
            )
            print(f"✅ Paramètre {PARAM_NAME} créé : J+{jours}")

        n = conn.execute(
            """UPDATE cuisine_stock_barquettes
               SET dlc = (SELECT date(p.date_production, ?) FROM cuisine_productions p
                          WHERE p.id = cuisine_stock_barquettes.production_id)
               WHERE dlc IS NULL""",
            (f"+{jours} days",),
        ).rowcount
        print(f"✅ DLC renseignée sur {n} ligne(s) de stock existante(s) (J+{jours})")
        n = conn.execute(
            """UPDATE cuisine_bons_livraison_lignes
               SET dlc = (SELECT date(p.date_production, ?) FROM cuisine_productions p
                          WHERE p.id = cuisine_bons_livraison_lignes.production_id)
               WHERE dlc IS NULL""",
            (f"+{jours} days",),
        ).rowcount
        print(f"✅ DLC renseignée sur {n} ligne(s) de bon de livraison existante(s)")
        conn.commit()

    print(f"🎉 Terminé sur [{args.env}] : {db_path}")


if __name__ == "__main__":
    main()
