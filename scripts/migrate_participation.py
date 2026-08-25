#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : nouveau module "Facturation participation V2"
(ba38_participation.py) — traitement du fichier PARSOL + génération PDF +
envoi intégrés, en complément (pas en remplacement) du flux existant
upload-PDF-EBP (`ba38_tresorerie.py::factures_pdf`, tables `factures_lots`/
`factures_envois`).

Ajoute :
- `participation_campagnes` (un par trimestre traité) : annee, trimestre,
  fichier_source, numero_facture_depart, date_creation, cree_par,
  dernier_envoi_le, dernier_envoi_par, dernier_envoi_mode_test,
  dernier_envoi_nb_ok, dernier_envoi_nb_erreur.
- `participation_factures` (une ligne par association détectée dans le
  fichier PARSOL) : campagne_id, association_id, code_vif, numero_facture,
  nom_association, montant_total, detail_json (passages retenus, pour
  régénérer le PDF à la demande), lignes_supprimees_json (passages
  ven/sam/dim retirés, traçabilité), pdf_genere_le, email, + les mêmes
  colonnes de suivi d'envoi que `factures_envois`.

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
        CREATE TABLE IF NOT EXISTS participation_campagnes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annee INTEGER,
            trimestre INTEGER,
            fichier_source TEXT,
            numero_facture_depart INTEGER,
            date_creation TEXT,
            cree_par TEXT,
            dernier_envoi_le TEXT,
            dernier_envoi_par TEXT,
            dernier_envoi_mode_test INTEGER DEFAULT 0,
            dernier_envoi_nb_ok INTEGER,
            dernier_envoi_nb_erreur INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS participation_factures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campagne_id INTEGER NOT NULL,
            association_id INTEGER,
            code_vif TEXT,
            numero_facture INTEGER,
            nom_association TEXT,
            montant_total REAL,
            detail_json TEXT,
            lignes_supprimees_json TEXT,
            pdf_genere_le TEXT,
            email TEXT,
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
            FOREIGN KEY (campagne_id) REFERENCES participation_campagnes(id)
        )
    """)

    factures_cols = table_columns(conn, "participation_factures")
    for col, decl in [
        ("sujet", "TEXT"),
        ("corps", "TEXT"),
        ("date_paiement", "TEXT"),
        ("relance_niveau", "INTEGER DEFAULT 0"),
        ("date_derniere_relance", "TEXT"),
        ("mode_test_relance", "INTEGER DEFAULT 0"),
        ("relance_sujet", "TEXT"),
        ("relance_corps", "TEXT"),
        ("relance_mail_erreur", "TEXT"),
        ("relance_mailjet_status", "TEXT"),
        ("relance_mailjet_message_ids", "TEXT"),
        ("relance_statut_final", "TEXT"),
        ("relance_statut_verifie_le", "TEXT"),
        ("relance_renvoi_gmail_le", "TEXT"),
    ]:
        if col not in factures_cols:
            conn.execute(f"ALTER TABLE participation_factures ADD COLUMN {col} {decl}")
            print(f"✓ participation_factures : colonne ajoutée : {col}")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_participation_factures_campagne "
        "ON participation_factures(campagne_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_participation_campagnes_periode "
        "ON participation_campagnes(annee, trimestre)"
    )

    print("✓ Tables participation_campagnes / participation_factures prêtes.")


def ensure_modeles_relance(conn):
    """Seed idempotent des modèles email de relance participation."""
    modeles = [
        (
            "PARTICIPATION Relance 1",
            "Rappel {numero_relance} – Participation de solidarité T{trimestre} {annee}",
            "Bonjour,\n\n"
            "Nous n'avons pas encore reçu le règlement de votre facture de "
            "participation de solidarité pour le T{trimestre} {annee}, "
            "d'un montant de {montant} €.\n\n"
            "Vous trouverez la facture en pièce jointe.\n\n"
            "Merci de bien vouloir procéder au règlement dans les meilleurs délais.\n\n"
            "Cordialement,\nLa Banque Alimentaire de l'Isère",
        ),
        (
            "PARTICIPATION Relance 2",
            "Relance {numero_relance} – Participation de solidarité T{trimestre} {annee}",
            "Bonjour,\n\n"
            "Malgré notre précédent message, nous n'avons toujours pas reçu le "
            "règlement de votre facture de participation de solidarité pour le "
            "T{trimestre} {annee}, d'un montant de {montant} €.\n\n"
            "Vous trouverez la facture en pièce jointe.\n\n"
            "Merci de bien vouloir régulariser rapidement cette situation.\n\n"
            "Cordialement,\nLa Banque Alimentaire de l'Isère",
        ),
        (
            "PARTICIPATION Relance 3",
            "Relance {numero_relance} – Participation de solidarité T{trimestre} {annee}",
            "Bonjour,\n\n"
            "Sans nouvelle de votre part malgré nos précédentes relances, le "
            "règlement de votre facture de participation de solidarité pour le "
            "T{trimestre} {annee} ({montant} €) reste en attente.\n\n"
            "Vous trouverez la facture en pièce jointe.\n\n"
            "Merci de procéder au règlement dans les plus brefs délais.\n\n"
            "Cordialement,\nLa Banque Alimentaire de l'Isère",
        ),
    ]

    for code_modele, sujet, corps in modeles:
        existe = conn.execute(
            "SELECT 1 FROM modeles_emails WHERE code_modele = ?", (code_modele,)
        ).fetchone()
        if not existe:
            conn.execute(
                "INSERT INTO modeles_emails (code_modele, sujet, corps) VALUES (?, ?, ?)",
                (code_modele, sujet, corps),
            )
            print(f"✓ modeles_emails : modèle créé : {code_modele}")


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

    print(f"➡ Migration participation V2 [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        ensure_tables(conn)
        ensure_modeles_relance(conn)
        conn.commit()

    print("✓ Migration terminée.")


if __name__ == "__main__":
    main()
