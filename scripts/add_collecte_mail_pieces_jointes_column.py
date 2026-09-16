#!/usr/bin/env python3
"""Ajoute la colonne stockant la liste des pièces jointes PDF persistantes
du mail chauffeurs/équipiers (JSON, liste de noms de fichiers)."""
import sqlite3

from ba38_utilitaires.core import get_db_path


def main():
    with sqlite3.connect(get_db_path()) as conn:
        colonnes = {ligne[1] for ligne in conn.execute("PRAGMA table_info(collecte_campagnes)")}
        if "mail_pieces_jointes" not in colonnes:
            conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_pieces_jointes TEXT")
        conn.commit()
    print("✅ Colonne mail_pieces_jointes disponible")


if __name__ == "__main__":
    main()
