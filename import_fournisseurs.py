#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Import de fournisseurs à partir d'un fichier Excel dans la base PROD.
Base cible : /home/ndprz/ba380/ba380.sqlite
Table : fournisseurs

Si un fournisseur avec le même nom existe déjà, il est ignoré.
"""

import sqlite3
import os
import pandas as pd

DB_PROD = "/home/ndprz/dev/ba380dev.sqlite"
EXCEL_FILE = "frs_a_creer.xlsx"   # chemin à adapter si besoin

# Champs supportés pour insertion
FIELDS = [
    "nom", "adresse", "cp", "ville", "tel", "mail",
    "enseigne", "societe", "tel_mobile", "adresse2",
    "notes", "actif", "ramasse", "type_frs", "code_vif"
]

def main():
    if not os.path.exists(DB_PROD):
        print(f"❌ Base PROD introuvable : {DB_PROD}")
        return
    if not os.path.exists(EXCEL_FILE):
        print(f"❌ Fichier Excel introuvable : {EXCEL_FILE}")
        return

    # Charger Excel
    df = pd.read_excel(EXCEL_FILE)
    df = df.fillna("")  # remplacer NaN par vide

    conn = sqlite3.connect(DB_PROD)
    cur = conn.cursor()

    inserted, skipped = 0, 0

    for _, row in df.iterrows():
        nom = str(row.get("nom", "")).strip()
        if not nom:
            print("⚠️ Ligne ignorée (nom vide)")
            continue

        # Vérifier si déjà existant
        cur.execute("SELECT id FROM fournisseurs WHERE nom = ?", (nom,))
        exists = cur.fetchone()
        if exists:
            print(f"⏩ {nom} déjà présent, ignoré.")
            skipped += 1
            continue

        # Préparer les champs présents
        values = []
        columns = []
        placeholders = []
        for field in FIELDS:
            if field in df.columns:
                val = str(row[field]).strip() if row[field] != "" else None
                columns.append(field)
                values.append(val)
                placeholders.append("?")

        if not columns:
            print(f"⚠️ {nom} ignoré (aucune donnée exploitable).")
            continue

        sql = f"INSERT INTO fournisseurs ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
        cur.execute(sql, values)
        inserted += 1
        print(f"✅ Ajouté : {nom}")

    conn.commit()
    conn.close()

    print("\n=== Résumé ===")
    print(f"✔️ {inserted} fournisseurs ajoutés")
    print(f"⏩ {skipped} déjà présents")


if __name__ == "__main__":
    main()
