#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : bons de préparation (module Production Cuisine).

Le bon de livraison passe par une étape "préparation" : la simulation crée
un bon au statut 'preparation' (numéro BP-AAAA-NNNN, stock réservé), modifiable
(quantités, autres barquettes, température de livraison) ; sa validation le
passe au statut 'valide' avec un numéro BL-AAAA-NNNN.

Sur cuisine_bons_livraison :
- contrainte CHECK du statut élargie à 'preparation' → reconstruction de la
  table (SQLite ne sait pas modifier une contrainte), données et id conservés ;
- colonnes temperature_livraison, numero_preparation, date_validation,
  user_validation.

Table dans EXCLUDE_TABLES de migrate_schema_and_data_dev_to_prod.py (qui n'y
touche donc pas en PROD) : ce script doit être lancé sur chaque base
(dev, dev_test, prod, prod_test), AVANT le déploiement du code.

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

TABLE = "cuisine_bons_livraison"

NOUVEAU_SCHEMA = f"""
CREATE TABLE {TABLE}_nouveau (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  numero TEXT NOT NULL UNIQUE,
  association_id INTEGER NOT NULL REFERENCES associations(id),
  nom_association TEXT NOT NULL,
  date_livraison TEXT NOT NULL,
  statut TEXT NOT NULL DEFAULT 'valide' CHECK (statut IN ('preparation','valide','annule','facture')),
  portions_carne INTEGER DEFAULT 0,
  portions_legumes INTEGER DEFAULT 0,
  prix_portion_carne REAL,
  montant REAL DEFAULT 0,
  commentaire TEXT,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  user_creation TEXT,
  date_annulation TEXT,
  user_annulation TEXT,
  motif_annulation TEXT,
  date_facturation TEXT,
  temperature_livraison REAL,
  numero_preparation TEXT,
  date_validation TEXT,
  user_validation TEXT
)
"""

ANCIENNES_COLONNES = (
    "id, numero, association_id, nom_association, date_livraison, statut, portions_carne, "
    "portions_legumes, prix_portion_carne, montant, commentaire, date_creation, user_creation, "
    "date_annulation, user_annulation, motif_annulation, date_facturation"
)

INDEX = [
    f"CREATE INDEX IF NOT EXISTS idx_cuisine_bl_date ON {TABLE}(date_livraison)",
    f"CREATE INDEX IF NOT EXISTS idx_cuisine_bl_association ON {TABLE}(association_id)",
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
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)).fetchone()
        if not row:
            raise RuntimeError(f"❌ Table {TABLE} absente de {db_path}")
        if "'preparation'" in row[0]:
            print(f"ℹ️  {TABLE} accepte déjà le statut 'preparation' : rien à faire")
        else:
            avant = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
            # Procédure SQLite de modification de table : clés étrangères
            # coupées, nouvelle table, copie, suppression, renommage. Les
            # lignes (cuisine_bons_livraison_lignes) référencent la table par
            # son nom : elles pointent sur la nouvelle une fois renommée.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(NOUVEAU_SCHEMA)
                conn.execute(
                    f"INSERT INTO {TABLE}_nouveau ({ANCIENNES_COLONNES}) SELECT {ANCIENNES_COLONNES} FROM {TABLE}"
                )
                conn.execute(f"DROP TABLE {TABLE}")
                conn.execute(f"ALTER TABLE {TABLE}_nouveau RENAME TO {TABLE}")
                for sql in INDEX:
                    conn.execute(sql)
                apres = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
                if apres != avant:
                    raise RuntimeError(f"nombre de lignes différent ({avant} → {apres})")
                # Contrôle limité aux lignes de BL : la table associations n'a
                # pas de clé unique déclarée sur id, un contrôle global (ou de
                # la table elle-même) échoue en "foreign key mismatch".
                erreurs = conn.execute(f"PRAGMA foreign_key_check({TABLE}_lignes)").fetchall()
                orphelines = conn.execute(
                    f"SELECT COUNT(*) FROM {TABLE}_lignes WHERE bon_id NOT IN (SELECT id FROM {TABLE})"
                ).fetchone()[0]
                if erreurs or orphelines:
                    raise RuntimeError(f"lignes de BL orphelines : {orphelines} / {erreurs[:5]}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
            print(f"✅ {TABLE} reconstruite ({apres} ligne(s) conservée(s)) : statut 'preparation' + 4 colonnes")

    print(f"🎉 Terminé sur [{args.env}] : {db_path}")


if __name__ == "__main__":
    main()
