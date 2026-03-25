#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Script de mise à jour de la table fournisseurs en PROD
à partir des données de la base DEV.

Champs synchronisés :
    nom, adresse, cp, ville, tel, mail,
    enseigne, societe, tel_mobile, adresse2,
    notes, actif, ramasse, type_frs, code_vif

La mise à jour est basée sur le champ 'id' qui doit être commun
entre DEV et PROD.
"""

import sqlite3
import os

DB_DEV = "/home/ndprz/dev/ba380dev.sqlite"
DB_PROD = "/home/ndprz/ba380/ba380.sqlite"

# Champs à synchroniser
FIELDS = [
    "nom", "adresse", "cp", "ville", "tel", "mail",
    "enseigne", "societe", "tel_mobile", "adresse2",
    "notes", "actif", "ramasse", "type_frs", "code_vif"
]

def main():
    if not os.path.exists(DB_DEV):
        print(f"❌ Base DEV introuvable : {DB_DEV}")
        return
    if not os.path.exists(DB_PROD):
        print(f"❌ Base PROD introuvable : {DB_PROD}")
        return

    # Connexions
    conn_dev = sqlite3.connect(DB_DEV)
    conn_dev.row_factory = sqlite3.Row
    cur_dev = conn_dev.cursor()

    conn_prod = sqlite3.connect(DB_PROD)
    cur_prod = conn_prod.cursor()

    # Lecture fournisseurs DEV
    fournisseurs_dev = cur_dev.execute(
        f"SELECT id, {', '.join(FIELDS)} FROM fournisseurs"
    ).fetchall()

    maj_count = 0
    skip_count = 0

    for frs in fournisseurs_dev:
        # Vérifier existence dans PROD
        frs_prod = cur_prod.execute(
            "SELECT id FROM fournisseurs WHERE id = ?", (frs["id"],)
        ).fetchone()

        if not frs_prod:
            print(f"⏩ Fournisseur id={frs['id']} absent en PROD, ignoré.")
            skip_count += 1
            continue

        # Préparer mise à jour
        set_clause = ", ".join([f"{f} = ?" for f in FIELDS])
        values = [frs[f] for f in FIELDS] + [frs["id"]]

        cur_prod.execute(
            f"UPDATE fournisseurs SET {set_clause} WHERE id = ?", values
        )
        maj_count += 1
        print(f"✅ Mis à jour id={frs['id']} ({frs['nom']})")

    conn_prod.commit()
    conn_dev.close()
    conn_prod.close()

    print("\n=== Résumé ===")
    print(f"✔️ {maj_count} fournisseurs mis à jour en PROD")
    print(f"⏩ {skip_count} fournisseurs absents en PROD (non modifiés)")


if __name__ == "__main__":
    main()
