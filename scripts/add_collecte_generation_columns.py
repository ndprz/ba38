#!/usr/bin/env python3
"""Ajoute à collecte_campagnes les colonnes de suivi de la génération des
tournées (étape 2 du module Collecte)."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log

COLONNES = {
    "parametres_generation": "TEXT",
    "derniere_generation_le": "TEXT",
    "derniere_generation_par": "TEXT",
    "fichier_resultat_excel": "TEXT",
    "nb_tournees": "INTEGER",
    "nb_magasins": "INTEGER",
    "nb_nouveaux_magasins": "INTEGER",
}


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes_existantes = {row[1] for row in cursor.execute("PRAGMA table_info(collecte_campagnes)")}

    for nom, type_sql in COLONNES.items():
        if nom in colonnes_existantes:
            print(f"ℹ️ Colonne {nom} déjà présente")
            continue
        cursor.execute(f"ALTER TABLE collecte_campagnes ADD COLUMN {nom} {type_sql}")
        print(f"✅ Colonne {nom} ajoutée")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur add_collecte_generation_columns : {e}")
        raise
