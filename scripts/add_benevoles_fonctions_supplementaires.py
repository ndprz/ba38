#!/usr/bin/env python3
"""Ajoute 9 nouvelles fonctions bénévoles (colonnes benevoles + field_groups) :
finances, informatique, presidence, ressources_alimentaires,
ressources_humaines, services_generaux, logistique, prospection_mecenat,
environnement_energie."""
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.getenv("BA38_BASE_DIR")

NOUVELLES_FONCTIONS = [
    "finances",
    "informatique",
    "presidence",
    "ressources_alimentaires",
    "ressources_humaines",
    "services_generaux",
    "logistique",
    "prospection_mecenat",
    "environnement_energie",
]


def migrate(db_filename):
    db_path = os.path.join(BASE_DIR, db_filename)

    if not os.path.exists(db_path):
        print(f"⏭️  Base absente, ignorée : {db_path}")
        return

    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(benevoles)")}

    display_order = 171

    for field_name in NOUVELLES_FONCTIONS:

        if field_name in colonnes:
            print(f"ℹ️ Colonne {field_name} déjà présente")
        else:
            cursor.execute(f"ALTER TABLE benevoles ADD COLUMN {field_name} TEXT")
            print(f"✅ Colonne {field_name} ajoutée")

        existe = cursor.execute(
            "SELECT 1 FROM field_groups WHERE appli = 'benevoles' AND field_name = ?",
            (field_name,)
        ).fetchone()

        if existe:
            print(f"ℹ️ field_groups déjà présent pour {field_name}")
        else:
            cursor.execute(
                """
                INSERT INTO field_groups
                    (field_name, group_name, display_order, is_required, type_champ, appli)
                VALUES (?, 'fonction', ?, 0, 'oui_non', 'benevoles')
                """,
                (field_name, display_order)
            )
            print(f"✅ field_groups ajouté pour {field_name} (display_order={display_order})")

        display_order += 1

    conn.commit()
    conn.close()


def main():
    migrate(os.getenv("SQLITE_DB"))
    migrate(os.getenv("SQLITE_DB_TEST"))


if __name__ == "__main__":
    main()
