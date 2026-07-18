#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : traçabilité des envois de mails "Indicateurs état".

Contexte : l'envoi du 1er juillet 2026 est passé inaperçu (parti en Mode TEST
sans que personne s'en rende compte), et rien n'était persisté en base pour
vérifier après coup si un envoi réel avait eu lieu, association par
association. Voir mémoire "Envoi indicateurs traçabilité".

Ajoute :
- `indicateurs_suivi` : mail_envoye_le, mail_mode_test, mail_erreur,
  mail_mailjet_status, mail_mailjet_message_ids, mail_statut_final,
  mail_statut_verifie_le, mail_modele_id, mail_renvoi_gmail_le
- `indicateurs_campagnes` : dernier_envoi_le, dernier_envoi_par,
  dernier_envoi_mode_test, dernier_envoi_nb_ok, dernier_envoi_nb_erreur

`mail_modele_id`/`mail_renvoi_gmail_le` (ajoutés le 2026-07-16) servent au
bouton "Renvoyer via Gmail" (contournement rebonds Microsoft/mail.ru liés à
l'absence d'authentification SPF/DKIM du domaine sur Mailjet) : on a besoin
de savoir quel modèle a servi pour l'envoi initial afin de le réutiliser
sans redemander à l'utilisateur.

Idempotent : n'ajoute que les colonnes manquantes (PRAGMA table_info).
Utilisable sur DEV ou PROD via l'argument --env (défaut : dev).
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

SUIVI_COLUMNS = [
    ("mail_envoye_le", "TEXT"),
    ("mail_mode_test", "INTEGER DEFAULT 0"),
    ("mail_erreur", "TEXT"),
    ("mail_mailjet_status", "TEXT"),
    ("mail_mailjet_message_ids", "TEXT"),
    ("mail_statut_final", "TEXT"),
    ("mail_statut_verifie_le", "TEXT"),
    ("mail_modele_id", "INTEGER"),
    ("mail_renvoi_gmail_le", "TEXT"),
]

CAMPAGNES_COLUMNS = [
    ("dernier_envoi_le", "TEXT"),
    ("dernier_envoi_par", "TEXT"),
    ("dernier_envoi_mode_test", "INTEGER DEFAULT 0"),
    ("dernier_envoi_nb_ok", "INTEGER"),
    ("dernier_envoi_nb_erreur", "INTEGER"),
]


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_missing_columns(conn, table, columns):
    existing = table_columns(conn, table)
    added = []
    for name, decl in columns:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        added.append(name)
    if added:
        print(f"✓ {table} : colonne(s) ajoutée(s) : {', '.join(added)}")
    else:
        print(f"✓ {table} : déjà à jour, rien à faire.")


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

    print(f"➡ Migration traçabilité indicateurs [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        add_missing_columns(conn, "indicateurs_suivi", SUIVI_COLUMNS)
        add_missing_columns(conn, "indicateurs_campagnes", CAMPAGNES_COLUMNS)

        conn.commit()

    print("✓ Migration terminée.")


if __name__ == "__main__":
    main()
