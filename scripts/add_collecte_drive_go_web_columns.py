#!/usr/bin/env python3
"""Ajoute les liens Drive des exports groupes, participants et mailing."""
import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ba38_utilitaires.core import get_db_path


def main():
    conn = sqlite3.connect(get_db_path())
    colonnes = {row[1] for row in conn.execute("PRAGMA table_info(collecte_campagnes)")}
    for nom in ("drive_groupes", "drive_participants", "drive_participants_mailing"):
        if nom not in colonnes:
            conn.execute(f"ALTER TABLE collecte_campagnes ADD COLUMN {nom} TEXT")
            print(f"✅ Colonne {nom} ajoutée")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()