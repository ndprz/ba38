#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : utilisation partielle des réceptions (module Production Cuisine).

Un arrivage important peut alimenter plusieurs recettes, sur plusieurs jours :
on choisit le poids utilisé par recette, la réception reste disponible avec
son solde (poids reçu − poids utilisés − poids purgé) tant que la DLC le
permet.

- Table cuisine_reception_utilisations (réception → production, poids
  utilisé), qui remplace cuisine_receptions.production_id (colonne gardée
  pour l'historique, plus lue ni écrite par l'appli). Reprise : chaque
  réception déjà affectée devient une utilisation de tout son poids.
- cuisine_receptions : colonnes livraison_id (regroupe les lignes saisies
  ensemble = une "réception" au sens du cuisinier, pour la purger d'un coup ;
  reprise = plus petit id du groupe date/heure/fournisseur/camion/
  réceptionnaire/horodatage de création), date_purge, user_purge,
  motif_purge, poids_purge (solde retiré du stock au moment de la purge).
  La DLC est saisie dans la colonne dlc_ddm, déjà présente.

cuisine_receptions et cuisine_reception_utilisations sont dans EXCLUDE_TABLES
de migrate_schema_and_data_dev_to_prod.py (ids divergents DEV/PROD) : ce
script doit être lancé sur chaque base (dev, dev_test, prod, prod_test),
AVANT le déploiement du code.

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

NOUVELLES_COLONNES = [
    ("livraison_id", "INTEGER"),
    ("date_purge", "TEXT"),
    ("user_purge", "TEXT"),
    ("motif_purge", "TEXT"),
    ("poids_purge", "REAL"),
]

SCHEMA_UTILISATIONS = """
CREATE TABLE IF NOT EXISTS cuisine_reception_utilisations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reception_id INTEGER NOT NULL REFERENCES cuisine_receptions(id),
  production_id INTEGER NOT NULL REFERENCES cuisine_productions(id),
  poids_kg REAL NOT NULL CHECK (poids_kg > 0),
  date_creation TEXT DEFAULT (datetime('now','utc')),
  user_creation TEXT
)
"""

INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_cuisine_recep_util_reception ON cuisine_reception_utilisations(reception_id)",
    "CREATE INDEX IF NOT EXISTS idx_cuisine_recep_util_production ON cuisine_reception_utilisations(production_id)",
    "CREATE INDEX IF NOT EXISTS idx_cuisine_receptions_livraison ON cuisine_receptions(livraison_id)",
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
        colonnes = {r[1] for r in conn.execute("PRAGMA table_info(cuisine_receptions)")}
        if not colonnes:
            raise RuntimeError(f"❌ Table cuisine_receptions absente de {db_path}")
        table_existait = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cuisine_reception_utilisations'"
        ).fetchone() is not None

        conn.execute("BEGIN IMMEDIATE")
        try:
            for nom, type_sql in NOUVELLES_COLONNES:
                if nom not in colonnes:
                    conn.execute(f"ALTER TABLE cuisine_receptions ADD COLUMN {nom} {type_sql}")
                    print(f"✅ Colonne cuisine_receptions.{nom} ajoutée")

            conn.execute(SCHEMA_UTILISATIONS)
            for sql in INDEX:
                conn.execute(sql)

            if not table_existait:
                n = conn.execute(
                    """INSERT INTO cuisine_reception_utilisations
                       (reception_id, production_id, poids_kg, date_creation, user_creation)
                       SELECT r.id, r.production_id, r.poids_kg, COALESCE(r.date_modif, r.date_creation),
                              'reprise migration'
                       FROM cuisine_receptions r
                       WHERE r.production_id IS NOT NULL AND r.poids_kg > 0"""
                ).rowcount
                print(f"✅ Table cuisine_reception_utilisations créée, {n} affectation(s) reprise(s)")
            else:
                print("ℹ️  Table cuisine_reception_utilisations déjà présente : pas de reprise")

            # Le trigger trg_cuisine_receptions_datemodif écraserait date_modif
            # (date de dernière correction affichée) sur toutes les lignes :
            # retiré le temps du backfill puis recréé à l'identique.
            trigger_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_cuisine_receptions_datemodif'"
            ).fetchone()
            if trigger_sql:
                conn.execute("DROP TRIGGER trg_cuisine_receptions_datemodif")
            n = conn.execute(
                """UPDATE cuisine_receptions AS r
                   SET livraison_id = (
                       SELECT MIN(r2.id) FROM cuisine_receptions r2
                       WHERE r2.date_reception = r.date_reception
                         AND r2.heure_arrivee IS r.heure_arrivee
                         AND r2.fournisseur_id IS r.fournisseur_id
                         AND r2.camion_libelle IS r.camion_libelle
                         AND r2.user_creation IS r.user_creation
                         AND r2.date_creation IS r.date_creation
                   )
                   WHERE livraison_id IS NULL"""
            ).rowcount
            if trigger_sql:
                conn.execute(trigger_sql[0])
            print(f"✅ livraison_id renseigné sur {n} ligne(s)")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    print(f"🎉 Terminé sur [{args.env}] : {db_path}")


if __name__ == "__main__":
    main()
