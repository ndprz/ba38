#!/usr/bin/env python3
"""Ajoute la « liste définitive » des magasins servant à la saisie des
cagettes : jusqu'ici la page cagettes relisait le fichier magasins (et le
Créneaux → demi-journées) à chaque affichage, donc la liste bougeait si le
fichier était remplacé en cours de campagne. Un bouton « Initialiser »
fige maintenant une fois pour toutes la liste retenue (magasin × demi-journée
applicable) dans collecte_cagettes_magasins ; la page cagettes lit ensuite
cette liste figée, jamais plus le fichier magasins directement — les
cagettes déjà saisies (table collecte_cagettes, clé annee/code_vif/
demi_journee) ne sont pas affectées par une réinitialisation."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS collecte_cagettes_magasins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        annee INTEGER NOT NULL,
        code_vif TEXT NOT NULL,
        nom_magasin TEXT NOT NULL,
        demi_journee TEXT NOT NULL,
        UNIQUE(annee, code_vif, demi_journee)
    );
    CREATE INDEX IF NOT EXISTS idx_collecte_cagettes_magasins_annee ON collecte_cagettes_magasins(annee);
    """)
    conn.commit()
    print("✅ Table collecte_cagettes_magasins créée ou déjà existante")

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(collecte_campagnes)")}
    for col in ("cagettes_initialisee_le", "cagettes_initialisee_par"):
        if col in colonnes:
            print(f"ℹ️ Colonne {col} déjà présente")
        else:
            cursor.execute(f"ALTER TABLE collecte_campagnes ADD COLUMN {col} TEXT")
            conn.commit()
            print(f"✅ Colonne {col} ajoutée")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur create_collecte_cagettes_magasins_table : {e}")
        raise
