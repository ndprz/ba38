#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : module "Emailing associations" (ba38_emailing/).
Tables : emailing_campagnes, emailing_destinataires. Idempotent.
Usage : migrate_emailing.py --env dev|prod|dev_test|prod_test
"""
import argparse
import sqlite3
import sys
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emailing_campagnes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sujet TEXT NOT NULL,
            corps TEXT,
            is_html INTEGER DEFAULT 1,
            modele_id INTEGER,
            expediteur TEXT,
            expediteur_nom TEXT,
            reply_to TEXT,
            champs_email TEXT,
            cree_le TEXT,
            cree_par TEXT,
            dernier_envoi_le TEXT,
            dernier_envoi_par TEXT,
            dernier_envoi_mode_test INTEGER DEFAULT 0,
            dernier_envoi_nb_ok INTEGER,
            dernier_envoi_nb_erreur INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emailing_destinataires (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campagne_id INTEGER NOT NULL,
            association_id INTEGER,
            nom_association TEXT,
            fonctions TEXT,
            email TEXT NOT NULL,
            selected INTEGER DEFAULT 1,
            mail_envoye_le TEXT,
            mail_mode_test INTEGER DEFAULT 0,
            mail_erreur TEXT,
            mail_mailjet_status TEXT,
            mail_mailjet_message_ids TEXT,
            mail_statut_final TEXT,
            mail_statut_verifie_le TEXT,
            mail_renvoi_gmail_le TEXT,
            mail_renvoi_gmail_erreur TEXT,
            FOREIGN KEY (campagne_id) REFERENCES emailing_campagnes(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emailing_dest_campagne ON emailing_destinataires(campagne_id)")
    # --- Formulaire de réponse (v2) ---
    def ajouter(table, colonne, definition):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if colonne not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {definition}")

    # --- Cible bénévoles (v3) ---
    ajouter("emailing_campagnes", "cible", "TEXT DEFAULT 'associations'")
    ajouter("emailing_destinataires", "benevole_id", "INTEGER")
    ajouter("emailing_destinataires", "prenom", "TEXT")
    ajouter("emailing_destinataires", "nom", "TEXT")
    ajouter("emailing_destinataires", "fonctions_codes", "TEXT")
    conn.execute("UPDATE emailing_campagnes SET cible = 'associations' WHERE cible IS NULL")

    ajouter("emailing_destinataires", "nb_envois", "INTEGER DEFAULT 0")
    ajouter("emailing_destinataires", "premier_envoi_le", "TEXT")
    # Envois réels déjà faits avant l'ajout du compteur
    conn.execute("UPDATE emailing_destinataires SET nb_envois = 1, premier_envoi_le = mail_envoye_le "
                 "WHERE nb_envois IS NULL OR (nb_envois = 0 AND mail_envoye_le IS NOT NULL AND mail_mode_test = 0 AND mail_erreur IS NULL)")

    ajouter("emailing_campagnes", "form_actif", "INTEGER DEFAULT 0")
    ajouter("emailing_campagnes", "form_titre", "TEXT")
    ajouter("emailing_campagnes", "form_intro", "TEXT")
    ajouter("emailing_campagnes", "form_merci", "TEXT")
    ajouter("emailing_destinataires", "token", "TEXT")
    ajouter("emailing_destinataires", "repondu_le", "TEXT")
    ajouter("emailing_destinataires", "relance_le", "TEXT")
    ajouter("emailing_destinataires", "nb_relances", "INTEGER DEFAULT 0")
    ajouter("emailing_destinataires", "relance_erreur", "TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_emailing_dest_token ON emailing_destinataires(token)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emailing_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campagne_id INTEGER NOT NULL,
            ordre INTEGER DEFAULT 0,
            libelle TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'oui_non',
            obligatoire INTEGER DEFAULT 0,
            FOREIGN KEY (campagne_id) REFERENCES emailing_campagnes(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emailing_reponses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            destinataire_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            valeur TEXT,
            maj_le TEXT,
            UNIQUE (destinataire_id, question_id)
        )
    """)
    print("✓ Tables emailing_campagnes / emailing_destinataires / questions / réponses prêtes.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--env", default="dev", choices=DB_PATHS)
    args = p.parse_args()
    with closing(sqlite3.connect(DB_PATHS[args.env])) as conn:
        ensure_tables(conn)
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
