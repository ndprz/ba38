#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : module Mécénat (tables mecenat, mecenat_dossiers, mecenat_demandes).

Idempotent (CREATE TABLE IF NOT EXISTS) : ne touche à aucune donnée existante.
Ajoute aussi la brique « Mécénat » (applications.appli = 'mecenat', groupe Finance) ; aucun droit
n'est attribué automatiquement (droits spécifiques, à donner dans Utilisateurs ; admin = accès total).
Le schéma vient de ba38_tresorerie/mecenat_schema.py (chargé par chemin, sans
importer le package Flask). Chemins explicites (pas de load_dotenv).
Usage : --env dev | dev_test | prod | prod_test (défaut : dev)
"""

import argparse
import importlib.util
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

TABLES = ("mecenat", "mecenat_dossiers", "mecenat_demandes", "mecenat_interactions", "mecenat_pieces")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=list(DB_PATHS), default="dev")
    args = parser.parse_args()

    db_path = DB_PATHS[args.env]
    if args.env.startswith("prod") and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env.startswith("dev") and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")

    # Schéma lu dans DEV dans tous les cas : la migration PROD passe AVANT le déploiement du code.
    spec = importlib.util.spec_from_file_location(
        "mecenat_schema", "/srv/ba38/dev/ba38_tresorerie/mecenat_schema.py")
    schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)

    with closing(sqlite3.connect(db_path)) as conn:
        avant = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        schema.ensure_schema(conn)
        conn.commit()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='applications'").fetchone():
            if conn.execute("SELECT 1 FROM applications WHERE appli='mecenat'").fetchone() is None:
                conn.execute(
                    "INSERT INTO applications (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible) "
                    "VALUES ('mecenat', 'Mécénat', 'tresorerie.mecenat_dossiers', 'Finance', 4, '🤝', 3, 1)")
                conn.commit()
                print("✅ Ligne applications 'mecenat' ajoutée (groupe Finance). Droits à attribuer dans Utilisateurs.")
            else:
                print("ℹ️  Ligne applications 'mecenat' déjà présente.")
        for t in TABLES:
            print(("ℹ️  " if t in avant else "✅ ") + t + (" déjà présente." if t in avant else " créée."))


if __name__ == "__main__":
    main()
