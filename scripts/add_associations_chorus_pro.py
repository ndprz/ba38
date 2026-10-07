#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ajout du champ "chorus_pro" (oui/non, défaut "non") sur la table
associations : "oui" = la facture de cette association doit être déposée sur
la plateforme Chorus Pro (partenaires qui sont des entités publiques :
CCAS, communes...). Famille "coordonnées principales", display_order 1159 (juste
après exclusion_mails_indicateurs à 1158, bloc trésorerie/comptable).

Ajoute aussi les deux références que certains destinataires publics exigent
sur leurs factures Chorus Pro (texte libre, vide par défaut) :
- chorus_code_service      : code service destinataire (ordre 1160)
- chorus_numero_engagement : n° d'engagement juridique / bon de commande (ordre 1161)

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

GROUP_NAME = "coordonnées principales"
# (field_name, display_order, type_champ, défaut SQL)
CHAMPS = [
    ("chorus_pro", 1159, "oui_non", "'non'"),
    ("chorus_code_service", 1160, "text", None),
    ("chorus_numero_engagement", 1161, "text", None),
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

    with closing(sqlite3.connect(db_path)) as conn:
        colonnes = {row[1] for row in conn.execute("PRAGMA table_info(associations)")}
        for field_name, display_order, type_champ, defaut in CHAMPS:
            if field_name in colonnes:
                print(f"ℹ️  Colonne {field_name} déjà présente.")
            else:
                clause_defaut = f" DEFAULT {defaut}" if defaut else ""
                conn.execute(f"ALTER TABLE associations ADD COLUMN {field_name} TEXT{clause_defaut}")
                print(f"✅ Colonne {field_name} ajoutée (défaut {defaut or 'NULL'}).")

            existe = conn.execute(
                "SELECT 1 FROM field_groups WHERE appli = 'associations' AND field_name = ?",
                (field_name,),
            ).fetchone()
            if existe:
                print(f"ℹ️  Ligne field_groups {field_name} déjà présente.")
            else:
                conn.execute(
                    """INSERT INTO field_groups
                       (field_name, group_name, display_order, is_required, type_champ, appli)
                       VALUES (?, ?, ?, 0, ?, 'associations')""",
                    (field_name, GROUP_NAME, display_order, type_champ),
                )
                print(f"✅ Ligne field_groups {field_name} créée (groupe {GROUP_NAME}, ordre {display_order}).")

        conn.commit()

    print(f"🎉 Terminé sur [{args.env}] : {db_path}")


if __name__ == "__main__":
    main()
