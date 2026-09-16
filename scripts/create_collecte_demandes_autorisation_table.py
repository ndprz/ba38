#!/usr/bin/env python3
"""Crée la table de suivi des envois de demande d'autorisation de collecter
aux magasins (une ligne par envoi effectif, hors mode test) — indépendante
des colonnes 'Accord'/'Date 1er envoi demande autorisation' du fichier
liste_magasins.xlsx, qui restent gérées à la main par l'équipe collecte à
partir des coupons-réponses reçus."""
import sqlite3

from ba38_utilitaires.core import get_db_path


def main():
    with sqlite3.connect(get_db_path()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS collecte_demandes_autorisation (
                annee INTEGER NOT NULL,
                code_vif TEXT NOT NULL,
                envoye_le TEXT,
                envoye_par TEXT,
                PRIMARY KEY (annee, code_vif)
            )
        """)
        conn.commit()
    print("✅ Table collecte_demandes_autorisation disponible")


if __name__ == "__main__":
    main()
