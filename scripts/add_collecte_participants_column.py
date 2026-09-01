#!/usr/bin/env python3
"""Ajoute les colonnes fichier_participants(_le/_par) à collecte_campagnes :
export go-on-web "liste des participants" (contacts/référents par groupe),
utilisé pour retrouver le référent (nom/email/téléphone) de chaque
association du sous-projet Associations gardant."""
import sqlite3
from ba38_utilitaires.core import get_db_path, write_log


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    colonnes = {row[1] for row in cursor.execute("PRAGMA table_info(collecte_campagnes)")}

    for nom, type_sql in [
        ("fichier_participants", "TEXT"),
        ("fichier_participants_le", "TEXT"),
        ("fichier_participants_par", "TEXT"),
    ]:
        if nom in colonnes:
            print(f"ℹ️ Colonne {nom} déjà présente")
        else:
            cursor.execute(f"ALTER TABLE collecte_campagnes ADD COLUMN {nom} {type_sql}")
            print(f"✅ Colonne {nom} ajoutée")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur add_collecte_participants_column : {e}")
        raise
