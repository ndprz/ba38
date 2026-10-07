#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : dépôt Chorus Pro + export PDF Drive du module Participation V2.

participation_factures (suivi du dépôt Chorus Pro, une facture) :
  chorus_env               'prod' | 'sandbox' (environnement du dernier dépôt)
  chorus_id_facture        identifiantFactureCPP renvoyé par Chorus Pro
  chorus_numero            n° facture tel que déposé (préfixé Q... en sandbox)
  chorus_statut            DEPOSEE, MISE_A_DISPOSITION, REJETEE, ...
  chorus_depose_le / chorus_depose_par
  chorus_erreur            dernier refus (vide si dépôt réussi)
  chorus_statut_verifie_le

participation_campagnes (dernier export des PDF vers Drive) :
  pdf_export_le / pdf_export_par / pdf_export_nb / pdf_export_nb_erreur
  pdf_export_dossier_id / pdf_export_dossier_chemin / pdf_export_mode_test

Idempotent (ADD COLUMN seulement si absente). Chemins explicites (pas de
load_dotenv, voir mémoire "Piège load_dotenv() ignore cwd").
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

COLONNES = {
    "participation_factures": [
        ("chorus_env", "TEXT"),
        ("chorus_id_facture", "INTEGER"),
        ("chorus_numero", "TEXT"),
        ("chorus_statut", "TEXT"),
        ("chorus_depose_le", "TEXT"),
        ("chorus_depose_par", "TEXT"),
        ("chorus_erreur", "TEXT"),
        ("chorus_statut_verifie_le", "TEXT"),
    ],
    "participation_campagnes": [
        ("pdf_export_le", "TEXT"),
        ("pdf_export_par", "TEXT"),
        ("pdf_export_nb", "INTEGER"),
        ("pdf_export_nb_erreur", "INTEGER"),
        ("pdf_export_dossier_id", "TEXT"),
        ("pdf_export_dossier_chemin", "TEXT"),
        ("pdf_export_mode_test", "INTEGER DEFAULT 0"),
    ],
}


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
        for table, colonnes in COLONNES.items():
            existantes = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existantes:
                raise RuntimeError(f"❌ Table {table} absente de {db_path} (lancer migrate_participation.py)")
            for nom, type_sql in colonnes:
                if nom in existantes:
                    print(f"ℹ️  {table}.{nom} déjà présente.")
                else:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {nom} {type_sql}")
                    print(f"✅ {table}.{nom} ajoutée.")
        conn.commit()

    print(f"🎉 Terminé sur [{args.env}] : {db_path}")


if __name__ == "__main__":
    main()
