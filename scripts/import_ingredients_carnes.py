#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Import one-off : référentiel des ingrédients carnés (module Production Cuisine).

Source : "/srv/ba38/uploads/cuisine/documents Nicolas.xlsx", onglet
"liste produit " (le premier). Ne reprend que les lignes de matières
premières (colonne catégorie = "01-MP (kg)") — les onglets/sections
"Barquettes" (portions) et "Appro viande" (fournisseurs) ne sont pas des
ingrédients et sont ignorés. La colonne "indice" vaut 2 sur des lignes
d'en-tête de famille dupliquées dans le fichier (ex. une ligne "12-Boeuf"
au milieu des vrais produits Boeuf) — également ignorées (indice != 1).

Le fichier ne sépare pas nommément "groupe" et "produit" : c'est le libellé
complet qui sert de nom de produit (ex. "Boeuf – Langue"). On en déduit :
- groupe = la famille, préfixe numérique retiré (ex. "12-Boeuf" → "Boeuf")
- produit = le libellé, préfixe "<groupe> – " retiré s'il est présent
  (ex. "Boeuf – Langue" → "Langue"), sinon le libellé tel quel (ex. pour
  le Gibier, dont les libellés commencent souvent par l'espèce et non par
  "Gibier", ou pour Poisson/Divers/Plat cuisiné dont les libellés ne
  suivent pas ce format).

Pas de conservation du "Code article" du fichier (non réutilisé côté
appli) — un même couple (groupe, produit) présent plusieurs fois dans le
fichier (quelques doublons constatés) n'est importé qu'une fois.

Idempotent (INSERT OR IGNORE sur l'UNIQUE(groupe, produit) — table créée
par scripts/migrate_ingredients_carnes.py, à exécuter avant ce script).
Utilisable sur DEV ou PROD via --env (défaut : dev).
"""

import argparse
import os
import re
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

FICHIER_SOURCE = "/srv/ba38/uploads/cuisine/documents Nicolas.xlsx"
ONGLET = "liste produit "
CATEGORIE_MP = "01-MP (kg)"


def _clean_groupe(famille):
    return re.sub(r"^\d+-\s*", "", (famille or "")).strip()


def _clean_produit(libelle, groupe):
    libelle = (libelle or "").strip()
    for sep in (" – ", " - "):
        prefixe = groupe + sep
        if libelle.lower().startswith(prefixe.lower()):
            return libelle[len(prefixe):].strip()
    return libelle


def lire_ingredients(fichier):
    wb = openpyxl.load_workbook(fichier, data_only=True)
    ws = wb[ONGLET]

    vus = set()
    ingredients = []
    for row in ws.iter_rows(min_row=2, max_col=5, values_only=True):
        code, libelle, famille, categorie, indice = row
        if code is None or indice != 1 or categorie != CATEGORIE_MP:
            continue
        groupe = _clean_groupe(famille)
        produit = _clean_produit(libelle, groupe)
        if not groupe or not produit:
            continue
        cle = (groupe, produit)
        if cle in vus:
            continue
        vus.add(cle)
        ingredients.append(cle)
    return ingredients


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

    ingredients = lire_ingredients(args.fichier)
    print(f"➡ {len(ingredients)} ingrédient(s) lus dans l'onglet « {ONGLET} » de {args.fichier}")

    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.cursor()
        crees = 0
        for groupe, produit in ingredients:
            cur.execute(
                "INSERT OR IGNORE INTO cuisine_ingredients_carnes (groupe, produit) VALUES (?, ?)",
                (groupe, produit),
            )
            if cur.rowcount:
                crees += 1
        conn.commit()

        total = cur.execute("SELECT COUNT(*) FROM cuisine_ingredients_carnes").fetchone()[0]
        nb_groupes = cur.execute("SELECT COUNT(DISTINCT groupe) FROM cuisine_ingredients_carnes").fetchone()[0]

    print(f"✓ {crees} créé(s).")
    print(f"📋 Total en base [{args.env}] : {total} ingrédient(s) dans {nb_groupes} groupe(s).")


if __name__ == "__main__":
    main()
