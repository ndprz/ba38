#!/usr/bin/env python3
"""Ajoute les textes configurables des mails d'affectations de collecte."""
import sqlite3

from ba38_utilitaires.core import get_db_path


def main():
    with sqlite3.connect(get_db_path()) as conn:
        colonnes = {ligne[1] for ligne in conn.execute("PRAGMA table_info(collecte_campagnes)")}
        for colonne in ("mail_debut", "mail_fin"):
            if colonne not in colonnes:
                conn.execute(f"ALTER TABLE collecte_campagnes ADD COLUMN {colonne} TEXT")
        conn.commit()
    print("✅ Colonnes mail_debut et mail_fin disponibles")


if __name__ == "__main__":
    main()