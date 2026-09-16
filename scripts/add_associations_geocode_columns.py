#!/usr/bin/env python3
"""Ajoute latitude/longitude/geocode_le à associations — pour la carte
« Localisation magasins et associations » (collecte annuelle) : les
associations n'ont aucune coordonnée GPS, seulement des adresses postales,
donc un géocodage (via Nominatim/OpenStreetMap) est nécessaire une fois puis
persisté ici plutôt que refait à chaque affichage de la carte."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(associations)")}
    for col, type_sql in [("latitude", "REAL"), ("longitude", "REAL"), ("geocode_le", "TEXT")]:
        if col in colonnes:
            print(f"ℹ️ Colonne {col} déjà présente")
        else:
            cursor.execute(f"ALTER TABLE associations ADD COLUMN {col} {type_sql}")
            conn.commit()
            print(f"✅ Colonne {col} ajoutée")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur add_associations_geocode_columns : {e}")
        raise
