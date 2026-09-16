#!/usr/bin/env python3
"""Crée les tables de saisie des poids par magasin gardé et des quantités
par produit (par association) — même principe que collecte_cagettes mais
sans notion de demi-journée : une valeur en kg par ligne."""
import sqlite3

from ba38_utilitaires.core import get_db_path


def main():
    with sqlite3.connect(get_db_path()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS collecte_poids_magasins_gardee (
                annee INTEGER NOT NULL,
                code_vif TEXT NOT NULL,
                poids_kg REAL,
                saisi_le TEXT,
                saisi_par TEXT,
                PRIMARY KEY (annee, code_vif)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS collecte_quantites_produits (
                annee INTEGER NOT NULL,
                association TEXT NOT NULL,
                code_produit TEXT NOT NULL,
                poids_kg REAL,
                saisi_le TEXT,
                saisi_par TEXT,
                PRIMARY KEY (annee, association, code_produit)
            )
        """)
        conn.commit()
    print("✅ Tables collecte_poids_magasins_gardee et collecte_quantites_produits disponibles")


if __name__ == "__main__":
    main()
