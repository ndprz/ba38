#!/usr/bin/env python3
"""Ajoute à collecte_analyses les colonnes de la génération automatique de la
partie rédactionnelle (rôle des camions VX, verdicts, recommandation) via
l'API Claude — en complément du prompt copiable existant, pas en remplacement."""
import sqlite3
from utils import get_db_path, write_log

COLONNES = {
    "redaction_statut": "TEXT",
    "redaction_texte": "TEXT",
    "redaction_erreur": "TEXT",
    "redaction_genere_le": "TEXT",
}


def main():
    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    existantes = {row[1] for row in cursor.execute("PRAGMA table_info(collecte_analyses)")}

    for nom, type_sql in COLONNES.items():
        if nom in existantes:
            print(f"ℹ️ Colonne {nom} déjà présente")
        else:
            cursor.execute(f"ALTER TABLE collecte_analyses ADD COLUMN {nom} {type_sql}")
            print(f"✅ Colonne {nom} ajoutée")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur add_collecte_analyses_redaction_columns : {e}")
        raise
