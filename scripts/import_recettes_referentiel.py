#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Import one-off : référentiel des recettes C3ES (module Production Cuisine).

Source : fichier Excel fourni par le responsable cuisine
"/srv/ba38/uploads/cuisine/Liste de recettes C3ES.xlsx", onglet "05"
(la liste la plus complète — 282 recettes, toutes avec un code renseigné ;
l'onglet "03" n'ajoute qu'une seule recette de plus et n'est pas repris ici).

⚠️ Piège repéré à la lecture du fichier : la colonne "Code" affiche
"1001-05" à l'écran dans Excel, mais ce n'est qu'un format d'affichage
personnalisé (`###"-05"`) — la valeur réellement stockée dans la cellule
est l'entier 1001. openpyxl (et donc ce script) lit déjà la valeur brute,
sans le suffixe : rien à découper manuellement.

Le suffixe -05 / -03 n'est PAS stocké en base : il est ajouté uniquement
à l'export Excel pour l'étiqueteuse automatique, selon la DLC choisie à ce
moment-là (une même recette peut être étiquetée en 3 ou 5 jours).

Idempotent (UPSERT par `code`, ré-exécutable sans créer de doublons).
Utilisable sur DEV ou PROD via --env (défaut : dev).
"""

import argparse
import os
import sys
import sqlite3
from contextlib import closing

import openpyxl

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

FICHIER_SOURCE = "/srv/ba38/uploads/cuisine/Liste de recettes C3ES.xlsx"
ONGLET = "05"


def lire_recettes(fichier):
    wb = openpyxl.load_workbook(fichier, data_only=True)
    ws = wb[ONGLET]

    recettes = []
    for row in ws.iter_rows(min_row=2, max_col=6, values_only=True):
        code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson = row
        nom = (nom or "").strip()
        if not nom:
            continue
        recettes.append((code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson))
    return recettes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["dev", "prod", "dev_test", "prod_test"], default="dev")
    parser.add_argument("--fichier", default=FICHIER_SOURCE)
    args = parser.parse_args()

    db_path = DB_PATHS[args.env]

    if args.env == "prod" and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env == "dev" and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")

    if not os.path.exists(db_path):
        print(f"❌ Base introuvable : {db_path}")
        sys.exit(1)
    if not os.path.exists(args.fichier):
        print(f"❌ Fichier Excel introuvable : {args.fichier}")
        sys.exit(1)

    recettes = lire_recettes(args.fichier)
    print(f"➡ {len(recettes)} recette(s) lues dans l'onglet « {ONGLET} » de {args.fichier}")

    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.cursor()
        crees, mises_a_jour = 0, 0

        for code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson in recettes:
            if code is not None:
                existant = cur.execute(
                    "SELECT id FROM cuisine_recettes_referentiel WHERE code = ?", (code,)
                ).fetchone()
            else:
                existant = cur.execute(
                    "SELECT id FROM cuisine_recettes_referentiel WHERE code IS NULL AND nom = ?",
                    (nom,),
                ).fetchone()

            if existant:
                cur.execute(
                    """UPDATE cuisine_recettes_referentiel
                       SET nom = ?, famille = ?, sous_famille_1 = ?, sous_famille_2 = ?,
                           type_cuisson = ?
                       WHERE id = ?""",
                    (nom, famille, sous_famille_1, sous_famille_2, type_cuisson, existant[0]),
                )
                mises_a_jour += 1
            else:
                cur.execute(
                    """INSERT INTO cuisine_recettes_referentiel
                       (code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson),
                )
                crees += 1

        conn.commit()

        total = cur.execute("SELECT COUNT(*) FROM cuisine_recettes_referentiel").fetchone()[0]

    print(f"✓ {crees} créée(s), {mises_a_jour} mise(s) à jour.")
    print(f"📋 Total en base [{args.env}] : {total} recette(s).")


if __name__ == "__main__":
    main()
