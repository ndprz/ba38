#!/usr/bin/env python3
"""Ajoute la nouvelle fonction bénévole 'tri_entrepot' (colonne benevoles + field_groups)."""
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.getenv("BA38_BASE_DIR")

FIELD_NAME = "tri_entrepot"
DISPLAY_ORDER = 45  # entre ramasse_tri_externe (40) et prep_pesee (50)


def migrate(db_filename):
    db_path = os.path.join(BASE_DIR, db_filename)

    if not os.path.exists(db_path):
        print(f"⏭️  Base absente, ignorée : {db_path}")
        return

    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(benevoles)")}

    if FIELD_NAME in colonnes:
        print(f"ℹ️ Colonne {FIELD_NAME} déjà présente")
    else:
        cursor.execute(f"ALTER TABLE benevoles ADD COLUMN {FIELD_NAME} TEXT")
        print(f"✅ Colonne {FIELD_NAME} ajoutée")

    existe = cursor.execute(
        "SELECT 1 FROM field_groups WHERE appli = 'benevoles' AND field_name = ?",
        (FIELD_NAME,)
    ).fetchone()

    if existe:
        print(f"ℹ️ field_groups déjà présent pour {FIELD_NAME}")
    else:
        cursor.execute(
            """
            INSERT INTO field_groups
                (field_name, group_name, display_order, is_required, type_champ, appli)
            VALUES (?, 'fonction', ?, 0, 'oui_non', 'benevoles')
            """,
            (FIELD_NAME, DISPLAY_ORDER)
        )
        print(f"✅ field_groups ajouté pour {FIELD_NAME} (display_order={DISPLAY_ORDER})")

    conn.commit()
    conn.close()


def main():
    migrate(os.getenv("SQLITE_DB"))
    migrate(os.getenv("SQLITE_DB_TEST"))


if __name__ == "__main__":
    main()
