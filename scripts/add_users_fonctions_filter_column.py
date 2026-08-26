#!/usr/bin/env python3
"""Ajoute la colonne fonctions_filter à users : mémorise, par compte, les
fonctions bénévoles sélectionnées dans le filtre de la page bénévoles."""
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.getenv("BA38_BASE_DIR")


def add_column(db_filename):
    db_path = os.path.join(BASE_DIR, db_filename)

    if not os.path.exists(db_path):
        print(f"⏭️  Base absente, ignorée : {db_path}")
        return

    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}

    if "fonctions_filter" in colonnes:
        print("ℹ️ Colonne fonctions_filter déjà présente")
    else:
        cursor.execute("ALTER TABLE users ADD COLUMN fonctions_filter TEXT")
        conn.commit()
        print("✅ Colonne fonctions_filter ajoutée")

    conn.close()


def main():
    add_column(os.getenv("SQLITE_DB"))
    add_column(os.getenv("SQLITE_DB_TEST"))


if __name__ == "__main__":
    main()
