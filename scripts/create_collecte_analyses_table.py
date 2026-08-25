#!/usr/bin/env python3
"""Crée la table collecte_analyses : analyses comparatives multi-scénarios
(ex. 8 configurations camions-supp × max-magasins), lancées en arrière-plan.
Stockage volontairement temporaire — aucun fichier Excel/carte produit, juste
les indicateurs chiffrés en JSON, à la différence de collecte_generations."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS collecte_analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        annee INTEGER NOT NULL,
        statut TEXT NOT NULL DEFAULT 'en_cours',
        parametres_communs TEXT,
        resultat TEXT,
        erreur TEXT,
        lance_le TEXT,
        lance_par TEXT,
        termine_le TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_collecte_analyses_annee ON collecte_analyses(annee);
    """)
    conn.commit()
    conn.close()
    print("✅ Table collecte_analyses créée ou déjà existante")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur create_collecte_analyses_table : {e}")
        raise
