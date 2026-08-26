#!/usr/bin/env python3
"""Ajoute la colonne is_html à modeles_emails : permet à un modèle de basculer
sur un éditeur riche (Quill, avec coller-image) au lieu du textarea brut."""
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.getenv("BA38_BASE_DIR")


def migrate(db_filename):
    db_path = os.path.join(BASE_DIR, db_filename)

    if not os.path.exists(db_path):
        print(f"⏭️  Base absente, ignorée : {db_path}")
        return

    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(modeles_emails)")}

    if "is_html" in colonnes:
        print("ℹ️ Colonne is_html déjà présente")
    else:
        cursor.execute("ALTER TABLE modeles_emails ADD COLUMN is_html INTEGER DEFAULT 0")
        print("✅ Colonne is_html ajoutée")

    conn.commit()
    conn.close()


def main():
    migrate(os.getenv("SQLITE_DB"))
    migrate(os.getenv("SQLITE_DB_TEST"))


if __name__ == "__main__":
    main()
