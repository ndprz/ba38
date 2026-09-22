#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ajout du champ "partenaire_cuisine" (oui/non, défaut "non") sur la table
associations, dans la famille de champs "Planification" (display_order 100,
juste après ignorer_differences_planif_vif à 95).

Idempotent : ALTER TABLE ADD COLUMN seulement si la colonne est absente,
INSERT field_groups seulement si la ligne est absente.

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

FIELD_NAME = "partenaire_cuisine"
GROUP_NAME = "Planification"
DISPLAY_ORDER = 100


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
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(associations)")}
        if FIELD_NAME in colonnes:
            print(f"ℹ️  Colonne {FIELD_NAME} déjà présente.")
        else:
            conn.execute(f"ALTER TABLE associations ADD COLUMN {FIELD_NAME} TEXT DEFAULT 'non'")
            print(f"✅ Colonne {FIELD_NAME} ajoutée (défaut 'non', rétro-appliqué aux lignes existantes).")

        existe = conn.execute(
            "SELECT 1 FROM field_groups WHERE appli = 'associations' AND field_name = ?",
            (FIELD_NAME,),
        ).fetchone()
        if existe:
            print("ℹ️  Ligne field_groups déjà présente.")
        else:
            conn.execute(
                """INSERT INTO field_groups
                   (field_name, group_name, display_order, is_required, type_champ, appli)
                   VALUES (?, ?, ?, 0, 'oui_non', 'associations')""",
                (FIELD_NAME, GROUP_NAME, DISPLAY_ORDER),
            )
            print("✅ Ligne field_groups créée (groupe Planification, ordre 100).")

        conn.commit()

    print(f"🎉 Terminé sur [{args.env}] : {db_path}")


if __name__ == "__main__":
    main()
