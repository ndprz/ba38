#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : paiement partiel / solde d'un engagement (comptabilisation).

engagements_depenses :
  montant_paye_cumule    somme déjà payée/comptabilisée (0 = aucun paiement)
  montant_prevu_initial  montant prévu avant correction à la comptabilisation

Reste à payer = montant_total - montant_paye_cumule.
Idempotent. Chemins explicites (pas de load_dotenv).
Usage : --env dev | dev_test | prod | prod_test (défaut : dev)
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
    ("montant_paye_cumule", "REAL DEFAULT 0"),
    ("montant_prevu_initial", "REAL"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=list(DB_PATHS), default="dev")
    args = parser.parse_args()
    db_path = DB_PATHS[args.env]
    with closing(sqlite3.connect(db_path)) as conn:
        existantes = {r[1] for r in conn.execute("PRAGMA table_info(engagements_depenses)")}
        if not existantes:
            raise RuntimeError(f"❌ engagements_depenses absente de {db_path}")
        for nom, type_sql in COLONNES:
            if nom in existantes:
                print(f"ℹ️  {nom} déjà présente.")
            else:
                conn.execute(f"ALTER TABLE engagements_depenses ADD COLUMN {nom} {type_sql}")
                print(f"✅ {nom} ajoutée.")
        conn.commit()


if __name__ == "__main__":
    main()
