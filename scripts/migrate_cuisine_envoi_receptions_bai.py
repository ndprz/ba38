#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : envoi à la BAI des réceptions cuisine livrées en direct
(module Production Cuisine).

- fournisseurs.livraison_directe_cuisine ('oui'/'non', défaut 'non') + ligne
  field_groups (case oui/non dans la fiche fournisseur) ; cochée pour
  ALPAGEL, BLANC ROSSET et Leclerc Comboire (recherche par nom).
- cuisine_receptions.envoi_bai_le / envoi_bai_par (TEXT) : réception déjà
  transmise à la BAI (jamais envoyée deux fois).
- cuisine_ingredients_carnes.code_vif (TEXT) : code article VIF imprimé sur
  le bordereau ; pré-rempli s'il est vide : viandes et poissons 4620001,
  plats cuisinés 4220031. Groupes mélangés (Surgelé, Divers, Conserves...)
  laissés vides, à renseigner produit par produit dans le référentiel.
- parametres cuisine_mail_envoi_bai : ba380@banquealimentaire.org si aucun
  destinataire n'est encore paramétré.

À lancer sur chaque base (dev, dev_test, prod, prod_test) AVANT le
déploiement du code : la colonne fournisseurs existant déjà en PROD, la
migration automatique DEV → PROD ne la recopiera pas par id, et elle ne
crée jamais de ligne field_groups. Chemins explicites (pas de
load_dotenv). Idempotent.
"""

import argparse
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

COLONNES = [
    ("fournisseurs", "livraison_directe_cuisine", "TEXT DEFAULT 'non'"),
    ("cuisine_receptions", "envoi_bai_le", "TEXT"),
    ("cuisine_receptions", "envoi_bai_par", "TEXT"),
    ("cuisine_ingredients_carnes", "code_vif", "TEXT"),
]

CODES_VIF_PAR_GROUPE = {
    "4620001": ("Agneau", "Boeuf", "Bœuf", "Cheval", "Gibier", "Lapin", "Poisson", "Porc", "Veau", "Volaille"),
    "4220031": ("Plat cuisiné", "Plats cuisinés"),
}
DESTINATAIRE_BAI = "ba380@banquealimentaire.org"

FOURNISSEURS_DIRECTS = ("ALPAGEL", "BLANC ROSSET", "LECLERC COMBOIRE")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=list(DB_PATHS), default="dev")
    args = parser.parse_args()
    db_path = DB_PATHS[args.env]
    if args.env.startswith("prod") and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env.startswith("dev") and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn, conn:
        for table, colonne, type_sql in COLONNES:
            existantes = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not existantes:
                raise RuntimeError(f"❌ Table {table} absente de {db_path}")
            if colonne in existantes:
                print(f"= {table}.{colonne} déjà présente")
            else:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {type_sql}")
                print(f"+ {table}.{colonne} ajoutée")

        if conn.execute(
            "SELECT 1 FROM field_groups WHERE appli = 'fournisseurs' AND field_name = 'livraison_directe_cuisine'"
        ).fetchone():
            print("= field_groups livraison_directe_cuisine déjà présent")
        else:
            conn.execute(
                """INSERT INTO field_groups (field_name, group_name, display_order, is_required, type_champ, appli)
                   VALUES ('livraison_directe_cuisine', 'coordonnees principales', 1067, 0, 'oui_non', 'fournisseurs')"""
            )
            print("+ field_groups livraison_directe_cuisine ajouté (case oui/non de la fiche fournisseur)")

        for nom in FOURNISSEURS_DIRECTS:
            lignes = conn.execute(
                "SELECT id, nom, livraison_directe_cuisine FROM fournisseurs WHERE UPPER(TRIM(nom)) = ?", (nom,)
            ).fetchall()
            if not lignes:
                print(f"⚠️ fournisseur {nom} introuvable")
            for fid, libelle, valeur in lignes:
                if valeur == "oui":
                    print(f"= {libelle} (#{fid}) déjà en livraison directe cuisine")
                else:
                    conn.execute("UPDATE fournisseurs SET livraison_directe_cuisine = 'oui' WHERE id = ?", (fid,))
                    print(f"+ {libelle} (#{fid}) → livraison directe cuisine")
        for code, groupes in CODES_VIF_PAR_GROUPE.items():
            marques = ",".join("?" * len(groupes))
            cur = conn.execute(
                f"UPDATE cuisine_ingredients_carnes SET code_vif = ? WHERE code_vif IS NULL AND groupe IN ({marques})",
                (code, *groupes),
            )
            print(f"+ code VIF {code} posé sur {cur.rowcount} ingrédient(s)")
        sans_code = conn.execute(
            "SELECT groupe, COUNT(*) FROM cuisine_ingredients_carnes WHERE actif = 1 AND code_vif IS NULL GROUP BY groupe"
        ).fetchall()
        if sans_code:
            print("  sans code VIF (à renseigner) : " + ", ".join(f"{g} ({n})" for g, n in sans_code))

        if conn.execute("SELECT 1 FROM parametres WHERE param_name = 'cuisine_mail_envoi_bai'").fetchone():
            print("= destinataire envoi BAI déjà paramétré")
        else:
            conn.execute(
                "INSERT INTO parametres (param_name, param_value, categorie) VALUES ('cuisine_mail_envoi_bai', ?, 'liste')",
                (DESTINATAIRE_BAI,),
            )
            print(f"+ destinataire envoi BAI : {DESTINATAIRE_BAI}")
    print(f"✅ Migration terminée : {db_path}")


if __name__ == "__main__":
    main()
