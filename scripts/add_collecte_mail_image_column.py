#!/usr/bin/env python3
"""Ajoute la colonne stockant l'image jointe au mail chauffeurs/équipiers
(insérable dans le texte via le marqueur <<image>>)."""
import sqlite3

from ba38_utilitaires.core import get_db_path


def main():
    with sqlite3.connect(get_db_path()) as conn:
        colonnes = {ligne[1] for ligne in conn.execute("PRAGMA table_info(collecte_campagnes)")}
        if "mail_image" not in colonnes:
            conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_image TEXT")
        conn.commit()
    print("✅ Colonne mail_image disponible")


if __name__ == "__main__":
    main()
