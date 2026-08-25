#!/usr/bin/env python3
"""Ajoute la colonne fichier_carte_secteurs à collecte_generations : chaque
génération de tournées produit désormais aussi sa carte des secteurs."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(collecte_generations)")}

    if "fichier_carte_secteurs" in colonnes:
        print("ℹ️ Colonne fichier_carte_secteurs déjà présente")
    else:
        cursor.execute("ALTER TABLE collecte_generations ADD COLUMN fichier_carte_secteurs TEXT")
        conn.commit()
        print("✅ Colonne fichier_carte_secteurs ajoutée")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur add_collecte_carte_secteurs_column : {e}")
        raise
