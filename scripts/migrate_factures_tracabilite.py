#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : traçabilité des envois "Facture participation"
(ba38_tresorerie.py::factures_pdf). Contrairement aux indicateurs état, ce
flux n'avait aucune persistance en base (tout en session + fichier temp) —
cette migration crée les deux tables nécessaires plutôt que d'ajouter des
colonnes à une table existante.

Ajoute :
- `factures_lots` (un par upload/envoi confirmé) : periode, annee, trimestre
  (ajoutés le 2026-07-17 pour l'écran de sélection par trimestre — periode
  reste un simple libellé dérivé, ex "T2 2026"), sender, modele_id, pdf_path
  (PDF combiné source), date_creation, cree_par, dernier_envoi_le,
  dernier_envoi_par, dernier_envoi_mode_test, dernier_envoi_nb_ok,
  dernier_envoi_nb_erreur.
- `factures_envois` (une ligne par facture détectée dans le PDF) : lot_id,
  nom, email, pages (ex "3,4,5", pour régénérer le PDF depuis le PDF
  combiné), sujet/corps rendus et figés au moment de l'envoi (pour que le
  renvoi Gmail ne dépende pas d'un modèle qui pourrait changer entre temps),
  mail_envoye_le, mail_mode_test, mail_erreur, mail_mailjet_status,
  mail_mailjet_message_ids, mail_statut_final, mail_statut_verifie_le,
  mail_modele_id, mail_renvoi_gmail_le.

Idempotent (CREATE TABLE IF NOT EXISTS). Utilisable sur DEV ou PROD via
l'argument --env (défaut : dev).
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


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factures_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            periode TEXT,
            sender TEXT,
            modele_id INTEGER,
            pdf_path TEXT,
            date_creation TEXT,
            cree_par TEXT,
            dernier_envoi_le TEXT,
            dernier_envoi_par TEXT,
            dernier_envoi_mode_test INTEGER DEFAULT 0,
            dernier_envoi_nb_ok INTEGER,
            dernier_envoi_nb_erreur INTEGER
        )
    """)

    lots_cols = table_columns(conn, "factures_lots")
    for col, decl in [("annee", "INTEGER"), ("trimestre", "INTEGER")]:
        if col not in lots_cols:
            conn.execute(f"ALTER TABLE factures_lots ADD COLUMN {col} {decl}")
            print(f"✓ factures_lots : colonne ajoutée : {col}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS factures_envois (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id INTEGER NOT NULL,
            nom TEXT,
            email TEXT,
            pages TEXT,
            sujet TEXT,
            corps TEXT,
            mail_envoye_le TEXT,
            mail_mode_test INTEGER DEFAULT 0,
            mail_erreur TEXT,
            mail_mailjet_status TEXT,
            mail_mailjet_message_ids TEXT,
            mail_statut_final TEXT,
            mail_statut_verifie_le TEXT,
            mail_modele_id INTEGER,
            mail_renvoi_gmail_le TEXT,
            FOREIGN KEY (lot_id) REFERENCES factures_lots(id)
        )
    """)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factures_envois_lot "
        "ON factures_envois(lot_id)"
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factures_lots_periode "
        "ON factures_lots(annee, trimestre)"
    )

    print("✓ Tables factures_lots / factures_envois prêtes.")


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

    print(f"➡ Migration traçabilité factures [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        ensure_tables(conn)
        conn.commit()

    print("✓ Migration terminée.")


if __name__ == "__main__":
    main()
