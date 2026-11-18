#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Suivi des dépôts SFTP pour VIF : colonnes ajoutées à exports_vif (historique des
exports réceptions / BL des ramasses partenaires).

    fichier            nom du fichier déposé (NULL pour un simple téléchargement)
    dossier            dossier SFTP où il a été déposé (sert à vérifier plus tard
                       s'il a été retiré = pris en charge par VIF)
    statut             NULL = téléchargé à la main (lignes historiques)
                       'depose' | 'pris_en_charge'
    depose_le          horodatage (UTC, comme date_export)
    pris_en_charge_le  horodatage (UTC) du constat « fichier retiré du dossier »

Paramètres de configuration (table parametres, categorie 'config') : créés SEULEMENT
avec --dossier-bl / --dossier-receptions. Vides = bouton de dépôt désactivé.
Ne PAS les renseigner sur DEV avec un chemin sous /srv/sftp/vif (refusé par
ba38_vif/depot.py de toute façon).

Idempotent : ALTER TABLE ADD COLUMN seulement si la colonne est absente.

Chemins de base explicites (pas de load_dotenv) : piège connu, un script sous
dev/scripts/ charge toujours dev/.env même si on se place dans /srv/ba38/prod.
Utilisable sur DEV ou PROD (+ bases test) via --env (défaut : dev).
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
    ("fichier", "TEXT"),
    ("dossier", "TEXT"),
    ("statut", "TEXT"),
    ("depose_le", "TEXT"),
    ("pris_en_charge_le", "TEXT"),
]


def set_param(conn, nom, valeur):
    if not valeur:
        return
    existe = conn.execute("SELECT id FROM parametres WHERE param_name = ?", (nom,)).fetchone()
    if existe:
        conn.execute("UPDATE parametres SET param_value = ? WHERE id = ?", (valeur, existe[0]))
        print(f"  paramètre {nom} mis à jour")
    else:
        conn.execute(
            "INSERT INTO parametres (param_name, param_value, categorie) VALUES (?, ?, 'config')",
            (nom, valeur),
        )
        print(f"  paramètre {nom} créé")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=sorted(DB_PATHS), default="dev")
    parser.add_argument("--dossier-bl", help="dossier de dépôt des BL (paramètre vif_depot_dossier_bl)")
    parser.add_argument("--dossier-receptions", help="dossier de dépôt des réceptions (vif_depot_dossier_receptions)")
    args = parser.parse_args()

    db = DB_PATHS[args.env]
    print(f"Base : {db}")
    with closing(sqlite3.connect(db, timeout=30)) as conn:
        existantes = {r[1] for r in conn.execute("PRAGMA table_info(exports_vif)")}
        if not existantes:
            raise SystemExit("Table exports_vif introuvable.")
        for nom, typ in COLONNES:
            if nom in existantes:
                print(f"  colonne {nom} déjà présente")
            else:
                conn.execute(f"ALTER TABLE exports_vif ADD COLUMN {nom} {typ}")
                print(f"  colonne {nom} ajoutée")
        set_param(conn, "vif_depot_dossier_bl", args.dossier_bl)
        set_param(conn, "vif_depot_dossier_receptions", args.dossier_receptions)
        conn.commit()
    print("OK")


if __name__ == "__main__":
    main()
