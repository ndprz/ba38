#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Préparation one-off pour la future intégration Yousign sur l'Annexe 1 bis
(cf. discussion 2026-07-10) : ajoute les colonnes de suivi sur `annexe1bis`,
SANS appeler l'API Yousign (en attente d'un accès développeur côté FFBA).

Signataire unique confirmé par le service partenariat : le président de
l'association (pas de co-signataire côté Banque Alimentaire).

Étapes :
1. Ajout de 4 colonnes sur `annexe1bis` :
   - yousign_signature_request_id : id de la demande de signature Yousign
   - yousign_document_id          : id du document dans cette demande
   - destinataire_signature_nom   : nom de la personne sollicitée pour signer
                                    (normalement le président, figé au moment
                                    de l'envoi même si le contact change ensuite)
   - destinataire_signature_email : email utilisé pour l'envoi

Usable sur DEV ou PROD via --env (défaut : dev). Idempotent.
"""

import argparse
import os
import sys
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
}

NOUVELLES_COLONNES = [
    ("yousign_signature_request_id", "TEXT"),
    ("yousign_document_id", "TEXT"),
    ("destinataire_signature_nom", "TEXT"),
    ("destinataire_signature_email", "TEXT"),
    ("document_signe_path", "TEXT"),
    ("nom_signataire_saisi", "TEXT"),
]


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_annexe1bis_columns(conn):
    existing = table_columns(conn, "annexe1bis")
    added = []
    for col, decl in NOUVELLES_COLONNES:
        if col not in existing:
            conn.execute(f"ALTER TABLE annexe1bis ADD COLUMN `{col}` {decl}")
            added.append(col)
    if added:
        print(f"✓ Colonnes ajoutées sur 'annexe1bis' : {', '.join(added)}")
    else:
        print("✓ Colonnes déjà présentes sur 'annexe1bis'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    args = parser.parse_args()

    db_path = DB_PATHS[args.env]

    if args.env == "prod" and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env == "dev" and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")

    if not os.path.exists(db_path):
        print(f"❌ Base introuvable : {db_path}")
        sys.exit(1)

    print(f"➡ Préparation Yousign annexe1bis [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        ensure_annexe1bis_columns(conn)
        conn.commit()

    print("✅ Préparation terminée avec succès.")


if __name__ == "__main__":
    main()
