#!/usr/bin/env python3
"""Crée les tables du module Collecte (tournées de collecte annuelle) et
l'entrée correspondante dans la table applications (menu Exploitation, à
droite de Plannings)."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS collecte_campagnes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        annee INTEGER NOT NULL UNIQUE,

        fichier_magasins TEXT,
        fichier_magasins_le TEXT,
        fichier_magasins_par TEXT,

        fichier_vehicules TEXT,
        fichier_vehicules_le TEXT,
        fichier_vehicules_par TEXT,

        fichier_pdf_precedent TEXT,
        fichier_pdf_precedent_le TEXT,
        fichier_pdf_precedent_par TEXT,

        date_debut TEXT,
        date_fin TEXT,

        drive_magasins TEXT,
        drive_vehicules TEXT,
        drive_cagettes TEXT,
        drive_groupes TEXT,
        drive_participants TEXT,
        drive_participants_mailing TEXT,

        mail_debut TEXT,
        mail_fin TEXT,

        date_creation TEXT,
        cree_par TEXT
    );
    """)

    conn.commit()
    print("✅ Table collecte_campagnes créée ou déjà existante")

    existante = cursor.execute(
        "SELECT appli FROM applications WHERE appli = 'collecte'"
    ).fetchone()

    if existante:
        print("ℹ️ Entrée applications 'collecte' déjà présente")
    else:
        cursor.execute("""
            INSERT INTO applications (
                appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "collecte", "Collecte", "collecte.collecte_main",
            "Exploitation", 3, "🧺", 2, 1
        ))
        conn.commit()
        print("✅ Entrée applications 'collecte' créée (Exploitation, à droite de Plannings)")

    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur create_collecte_tables : {e}")
        raise
