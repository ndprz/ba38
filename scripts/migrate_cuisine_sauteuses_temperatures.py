#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : améliorations module Cuisine d'octobre 2026.

Schéma + données de départ (option --schema, à lancer AVANT le code) :
- cuisine_productions.numero_sauteuse (TEXT) : n° de sauteuse demandé pour
  les modes de cuisson « Cuisson de nuit » et « Sauteuse ».
- parametres cuisine_sauteuse : Sauteuse 1..4 si aucune sauteuse n'existe.
- cuisine_hygiene_zones_temperature.groupe (TEXT) : 'positive' / 'negative'
  / 'dirigee' — pré-rempli d'après le libellé si vide (modifiable ensuite
  dans le référentiel).
- cuisine_hygiene_releves_temperature.moment (TEXT) : 'matin' / 'soir'.

Tuiles de la page d'accueil (option --applications, à lancer APRÈS le
code : l'endpoint cuisine_hygiene.autres doit exister, sinon BuildError) :
- production_cuisine → « Production », cuisine_hygiene → « Températures »,
  + cuisine_hygiene:nettoyage et cuisine_hygiene:autres (droits hérités de
  cuisine_hygiene, cf. build_menu dans ba38_utilitaires/core.py).
  Inutile en PROD si déploiement par deploy_to_prod.sh (table applications
  recopiée depuis DEV).

Tables dans EXCLUDE_TABLES de migrate_schema_and_data_dev_to_prod.py : à
lancer sur chaque base (dev, dev_test, prod, prod_test). Chemins explicites
(pas de load_dotenv). Idempotent.
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
    ("cuisine_productions", "numero_sauteuse", "TEXT"),
    ("cuisine_hygiene_zones_temperature", "groupe", "TEXT"),
    ("cuisine_hygiene_releves_temperature", "moment", "TEXT"),
]

SAUTEUSES = ["Sauteuse 1", "Sauteuse 2", "Sauteuse 3", "Sauteuse 4"]

# appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible
APPLICATIONS = [
    ("production_cuisine", "Production", "production_cuisine.liste_productions", "Cuisine", 1, "🍳", 7, 1),
    ("cuisine_hygiene", "Températures", "cuisine_hygiene.temperatures", "Cuisine", 2, "🌡️", 7, 1),
    ("cuisine_hygiene:nettoyage", "Hygiène nettoyage", "cuisine_hygiene.nettoyage", "Cuisine", 3, "🧽", 7, 1),
    ("cuisine_hygiene:autres", "Autres (étalonnage, référentiels)", "cuisine_hygiene.autres", "Cuisine", 4, "🔧", 7, 1),
]


def groupe_depuis_libelle(libelle):
    txt = (libelle or "").upper()
    if "NEGATIVE" in txt or "NÉGATIVE" in txt:
        return "negative"
    if txt.startswith("CT ") or "DIRIG" in txt:
        return "dirigee"
    return "positive"


def migrer_schema(conn):
    for table, colonne, type_sql in COLONNES:
        existantes = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existantes:
            raise RuntimeError(f"❌ Table {table} absente")
        if colonne in existantes:
            print(f"= {table}.{colonne} déjà présente")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {type_sql}")
            print(f"+ {table}.{colonne} ajoutée")

    for zone_id, libelle in conn.execute(
        "SELECT id, libelle FROM cuisine_hygiene_zones_temperature WHERE groupe IS NULL"
    ).fetchall():
        groupe = groupe_depuis_libelle(libelle)
        conn.execute("UPDATE cuisine_hygiene_zones_temperature SET groupe = ? WHERE id = ?", (groupe, zone_id))
        print(f"  zone {libelle} → {groupe}")

    if conn.execute("SELECT 1 FROM parametres WHERE param_name = 'cuisine_sauteuse'").fetchone():
        print("= sauteuses déjà paramétrées")
    else:
        for nom in SAUTEUSES:
            conn.execute(
                "INSERT INTO parametres (param_name, param_value, categorie) VALUES ('cuisine_sauteuse', ?, 'liste')",
                (nom,),
            )
        print(f"+ {len(SAUTEUSES)} sauteuses ajoutées")


def migrer_applications(conn):
    for appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible in APPLICATIONS:
        if conn.execute("SELECT 1 FROM applications WHERE appli = ?", (appli,)).fetchone():
            conn.execute(
                "UPDATE applications SET label = ?, endpoint = ?, groupe = ?, ordre = ?, icon = ?, "
                "ordre_groupe = ?, menu_visible = ? WHERE appli = ?",
                (label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible, appli),
            )
            print(f"~ tuile {appli} mise à jour")
        else:
            conn.execute(
                "INSERT INTO applications (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible),
            )
            print(f"+ tuile {appli} ajoutée")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=list(DB_PATHS), default="dev")
    parser.add_argument("--schema", action="store_true", help="colonnes + données de départ (avant le code)")
    parser.add_argument("--applications", action="store_true", help="tuiles de la page d'accueil (après le code)")
    args = parser.parse_args()
    if not (args.schema or args.applications):
        parser.error("préciser --schema et/ou --applications")
    db_path = DB_PATHS[args.env]
    if args.env.startswith("prod") and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env.startswith("dev") and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            if args.schema:
                migrer_schema(conn)
            if args.applications:
                migrer_applications(conn)
    print(f"✅ Migration terminée : {db_path}")


if __name__ == "__main__":
    main()
