#!/usr/bin/env python3
"""Crée la table collecte_cagettes : saisie manuelle du nombre de cagettes
collectées par magasin et demi-journée, relevé sur la fiche camion
demi-journée au retour des camions. Clé par (année, code VIF, demi-journée) —
pas par génération de tournées ni par camion : le référentiel magasins de
l'année (colonne Créneaux) suffit à savoir quelles demi-journées saisir pour
chaque magasin du périmètre bai."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS collecte_cagettes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        annee INTEGER NOT NULL,
        code_vif TEXT NOT NULL,
        demi_journee TEXT NOT NULL,
        nb_cagettes INTEGER,
        saisi_le TEXT,
        saisi_par TEXT,
        UNIQUE(annee, code_vif, demi_journee)
    );
    CREATE INDEX IF NOT EXISTS idx_collecte_cagettes_annee ON collecte_cagettes(annee);
    """)
    conn.commit()
    conn.close()
    print("✅ Table collecte_cagettes créée ou déjà existante")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur create_collecte_cagettes_table : {e}")
        raise
