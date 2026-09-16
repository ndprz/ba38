#!/usr/bin/env python3
"""Ajoute poids_cagette_estime/poids_cagette_pese/poids_total_pese à
collecte_campagnes — pour la page de saisie des cagettes : un poids de
cagette estimé (avant collecte), un poids total pesé en entrepôt (saisie
manuelle) et le poids de cagette après pesée qui en est déduit
(poids_total_pese / total cagettes), tous valables pour toute la campagne
(pas par magasin)."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(collecte_campagnes)")}
    for col in ("poids_cagette_estime", "poids_cagette_pese", "poids_total_pese"):
        if col in colonnes:
            print(f"ℹ️ Colonne {col} déjà présente")
        else:
            cursor.execute(f"ALTER TABLE collecte_campagnes ADD COLUMN {col} REAL")
            conn.commit()
            print(f"✅ Colonne {col} ajoutée")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur add_collecte_poids_cagette_columns : {e}")
        raise
