#!/usr/bin/env python3
"""Remplace le slot unique de génération sur collecte_campagnes par une table
collecte_generations (une ligne par génération) — on veut pouvoir garder
plusieurs versions par année, chacune avec ses propres paramètres."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS collecte_generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        annee INTEGER NOT NULL,
        fichier_excel TEXT NOT NULL,
        parametres TEXT NOT NULL,
        genere_le TEXT,
        genere_par TEXT,
        nb_tournees INTEGER,
        nb_magasins INTEGER,
        nb_nouveaux_magasins INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_collecte_generations_annee ON collecte_generations(annee);
    """)
    conn.commit()
    print("✅ Table collecte_generations créée ou déjà existante")

    colonnes_campagnes = {row[1] for row in cursor.execute("PRAGMA table_info(collecte_campagnes)")}

    if "fichier_resultat_excel" in colonnes_campagnes:
        a_migrer = cursor.execute("""
            SELECT annee, fichier_resultat_excel, parametres_generation,
                   derniere_generation_le, derniere_generation_par,
                   nb_tournees, nb_magasins, nb_nouveaux_magasins
            FROM collecte_campagnes
            WHERE fichier_resultat_excel IS NOT NULL
        """).fetchall()

        for row in a_migrer:
            deja = cursor.execute(
                "SELECT id FROM collecte_generations WHERE annee = ? AND fichier_excel = ?",
                (row["annee"], row["fichier_resultat_excel"])
            ).fetchone()
            if deja:
                continue
            cursor.execute("""
                INSERT INTO collecte_generations
                    (annee, fichier_excel, parametres, genere_le, genere_par,
                     nb_tournees, nb_magasins, nb_nouveaux_magasins)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["annee"], row["fichier_resultat_excel"],
                row["parametres_generation"] or "{}",
                row["derniere_generation_le"], row["derniere_generation_par"],
                row["nb_tournees"], row["nb_magasins"], row["nb_nouveaux_magasins"],
            ))
            print(f"✅ Génération migrée : année {row['annee']}, fichier {row['fichier_resultat_excel']}")

        conn.commit()

        for col in ["parametres_generation", "derniere_generation_le", "derniere_generation_par",
                    "fichier_resultat_excel", "nb_tournees", "nb_magasins", "nb_nouveaux_magasins"]:
            cursor.execute(f"ALTER TABLE collecte_campagnes DROP COLUMN {col}")
            print(f"✅ Colonne collecte_campagnes.{col} supprimée (déplacée vers collecte_generations)")

        conn.commit()
    else:
        print("ℹ️ collecte_campagnes déjà nettoyée (colonnes de génération absentes)")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur create_collecte_generations_table : {e}")
        raise
