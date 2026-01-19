#!/usr/bin/env python3
import sqlite3
from utils import get_db_path, write_log

def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # ✅ Création de la table si elle n’existe pas
    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS fournisseurs_contacts (
        id INTEGER PRIMARY KEY,
        fournisseur_id INTEGER NOT NULL,
        prenom TEXT,
        nom TEXT,
        fonction TEXT,
        tel_mobile TEXT,
        tel_fixe TEXT,
        email TEXT,
        adresse1 TEXT,
        adresse2 TEXT,
        cp TEXT,
        ville TEXT,
        est_referent TEXT DEFAULT 'non' CHECK (est_referent IN ('oui','non')),
        actif TEXT DEFAULT 'oui' CHECK (actif IN ('oui','non')),
        notes TEXT,
        date_creation TEXT DEFAULT (datetime('now','utc')),
        date_modif TEXT,
        user_modif TEXT,
        FOREIGN KEY (fournisseur_id) REFERENCES fournisseurs(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_contacts_fournisseur ON fournisseurs_contacts(fournisseur_id);
    CREATE INDEX IF NOT EXISTS idx_contacts_nom ON fournisseurs_contacts(nom);
    CREATE INDEX IF NOT EXISTS idx_contacts_email ON fournisseurs_contacts(email);
    """)

    conn.commit()
    conn.close()
    print("✅ Table fournisseurs_contacts créée ou déjà existante")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur create_fournisseurs_contacts : {e}")
        raise
