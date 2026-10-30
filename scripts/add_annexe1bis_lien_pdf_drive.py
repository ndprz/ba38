#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ajout de la colonne "lien_pdf_drive" sur la table annexe1bis : lien de
partage Google Drive d'une annexe 1 bis PDF (association qui ne remplit pas
le formulaire en ligne). Une annexe avec lien renseigné n'a que sa date
(date_creation) : le PDF reste sur le Drive, il n'est pas copié sur le
serveur.

Idempotent : ALTER TABLE ADD COLUMN seulement si la colonne est absente.

Chemins de base explicites (pas de load_dotenv) : piège connu, un script
sous dev/scripts/ charge toujours dev/.env même si on se place dans
/srv/ba38/prod avant de le lancer.
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
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(annexe1bis)")}
        if "lien_pdf_drive" in colonnes:
            print("ℹ️  Colonne lien_pdf_drive déjà présente.")
        else:
            conn.execute("ALTER TABLE annexe1bis ADD COLUMN lien_pdf_drive TEXT")
            print("✅ Colonne lien_pdf_drive ajoutée.")
        conn.commit()


if __name__ == "__main__":
    main()
