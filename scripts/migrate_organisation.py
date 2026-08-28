#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : table `organisation`.

Extrait l'identité de l'organisme (nom, adresse, IBAN/BIC, SIREN/SIRET/
NAF/RNA, chemins logo/signature, texte de pied de page partenariat) hors du
code source — jusqu'ici en dur dans ba38_tresorerie/constants.py,
ba38_tresorerie/participation.py et le texte de plusieurs pieds de page PDF
(cotisations, participation, CERFA, fiches partenaires). Première étape
vers une appli redéployable pour une autre banque alimentaire.

N'inclut PAS la couleur du logo réseau (#f27830) : c'est une couleur
nationale FFBA commune à toutes les BA, pas un paramètre d'organisme —
elle reste une constante dans ba38_tresorerie/participation.py.

Une seule ligne (id=1), lue via ba38_utilitaires.organisation.get_organisation().

Idempotent : CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE (ne réécrase pas
une ligne déjà personnalisée). Utilisable sur DEV ou PROD via --env.
"""

import argparse
import os
import sys
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

# Valeurs actuelles de BA38, reprises telles quelles depuis
# ba38_tresorerie/constants.py et ba38_tresorerie/participation.py.
VALEURS_BA38 = {
    "nom": "BANQUE ALIMENTAIRE DE L'ISÈRE",
    "adresse": "11, allée de la Pinéa\n38600 FONTAINE",
    "tel": "04 76 85 92 50",
    "email": "ba380@banquealimentaire.org",
    "iban": "FR76 1027 8089 2200 0598 3594 087",
    "bic": "CMCIFR2A",
    "siren": "388 092 132 00033",
    "naf": "8899B",
    "siret": "38809213200025",
    "rna": "W381001970",
    "logo_path": "static/images/logo.png",
    "logo_complet_path": "static/images/logo_ba_complet.png",
    "signature_path": "static/signatures/signature_chantal_vivier.png",
    "footer_partenariat": "Banque Alimentaire de l'Isère - Service Partenariat",
}


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS organisation (
            id INTEGER PRIMARY KEY,
            nom TEXT NOT NULL,
            adresse TEXT NOT NULL,
            tel TEXT,
            email TEXT,
            iban TEXT,
            bic TEXT,
            siren TEXT,
            naf TEXT,
            siret TEXT,
            rna TEXT,
            logo_path TEXT,
            logo_complet_path TEXT,
            signature_path TEXT,
            footer_partenariat TEXT
        )
    """)

    colonnes = {row[1] for row in conn.execute("PRAGMA table_info(organisation)").fetchall()}

    # Nettoyage : colonne couleur_primaire créée par une version précédente
    # de ce script avant qu'on se rende compte qu'elle n'a rien à faire ici
    # (couleur nationale FFBA, pas un paramètre d'organisme).
    if "couleur_primaire" in colonnes:
        conn.execute("ALTER TABLE organisation DROP COLUMN couleur_primaire")
        print("✓ Colonne obsolète `couleur_primaire` supprimée.")

    # logo_complet_path : ajouté après coup (table déjà créée sans cette
    # colonne sur dev/prod avant qu'on distingue le petit logo du grand
    # logo "letterhead" utilisé sur les factures de cotisation).
    if "logo_complet_path" not in colonnes:
        conn.execute("ALTER TABLE organisation ADD COLUMN logo_complet_path TEXT")
        conn.execute(
            "UPDATE organisation SET logo_complet_path = ? WHERE id = 1",
            (VALEURS_BA38["logo_complet_path"],),
        )
        print("✓ Colonne `logo_complet_path` ajoutée.")

    conn.execute(
        """
        INSERT OR IGNORE INTO organisation (
            id, nom, adresse, tel, email, iban, bic, siren, naf, siret, rna,
            logo_path, logo_complet_path, signature_path, footer_partenariat
        ) VALUES (1, :nom, :adresse, :tel, :email, :iban, :bic, :siren, :naf,
                   :siret, :rna, :logo_path, :logo_complet_path,
                   :signature_path, :footer_partenariat)
        """,
        VALEURS_BA38,
    )

    print("✓ Table `organisation` prête (ligne id=1 créée si absente).")


def ensure_menu_entry(conn):
    conn.execute(
        """
        INSERT OR IGNORE INTO applications (
            appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible
        ) VALUES ('organisation', 'Organisation', 'organisation.organisation_edit',
                   'Admin', 2, '🏢', 6, 1)
        """
    )
    print("✓ Entrée de menu `organisation` prête (créée si absente).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["dev", "prod", "dev_test", "prod_test"], default="dev")
    args = parser.parse_args()

    db_path = DB_PATHS[args.env]

    if args.env == "prod" and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env == "dev" and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")
    if args.env in ("dev_test", "prod_test") and "_test.sqlite" not in db_path:
        raise RuntimeError(f"❌ Chemin TEST invalide : {db_path}")

    if not os.path.exists(db_path):
        print(f"❌ Base introuvable : {db_path}")
        sys.exit(1)

    print(f"➡ Migration table organisation [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        ensure_table(conn)
        ensure_menu_entry(conn)
        conn.commit()

    print("✓ Migration terminée.")


if __name__ == "__main__":
    main()
