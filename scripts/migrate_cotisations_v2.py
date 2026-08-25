#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : nouveau module "Cotisations V2"
(ba38_tresorerie/cotisations_v2.py + cotisations_v2_relance.py) — sur le
modèle de Participation V2 (ba38_participation.py) : écran unique
génération + envoi + bouton Payé + relance, PDF régénéré à la demande
(pas de dépendance Google Drive), modèles d'email en base pour l'envoi
initial ET la relance. Coexiste avec l'ancien flux "Cotisations 2026"
(table `cotisations`, ba38_tresorerie/cotisations.py) jusqu'à son retrait
prévu fin 2026 — aucune table/colonne existante n'est touchée.

Ajoute :
- `cotisations_v2_campagnes` (une ligne par année traitée) : annee,
  fichier_source, numero_facture_depart, date_creation, cree_par,
  dernier_envoi_le, dernier_envoi_par, dernier_envoi_mode_test,
  dernier_envoi_nb_ok, dernier_envoi_nb_erreur.
- `cotisations_v2_factures` (une ligne par association facturée, ou
  orpheline si association_id IS NULL) : campagne_id, association_id,
  code_vif, numero_facture, nom_association, nom_association_affichage
  (avec regroupement), beneficiaires, montant, detail_json (codes_vif
  inclus + commentaire de regroupement, pour régénérer le PDF sans
  dépendre d'un JOIN live), + les mêmes colonnes de suivi d'envoi/paiement/
  relance que `participation_factures`.

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


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cotisations_v2_campagnes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annee INTEGER,
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
        CREATE TABLE IF NOT EXISTS cotisations_v2_factures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campagne_id INTEGER NOT NULL,
            association_id INTEGER,
            code_vif TEXT,
            numero_facture INTEGER,
            nom_association TEXT,
            nom_association_affichage TEXT,
            beneficiaires INTEGER,
            montant REAL,
            detail_json TEXT,
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
            date_paiement TEXT,
            relance_niveau INTEGER DEFAULT 0,
            date_derniere_relance TEXT,
            mode_test_relance INTEGER DEFAULT 0,
            relance_sujet TEXT,
            relance_corps TEXT,
            relance_mail_erreur TEXT,
            relance_mailjet_status TEXT,
            relance_mailjet_message_ids TEXT,
            relance_statut_final TEXT,
            relance_statut_verifie_le TEXT,
            relance_renvoi_gmail_le TEXT,
            FOREIGN KEY (campagne_id) REFERENCES cotisations_v2_campagnes(id)
        )
    """)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cotisations_v2_factures_campagne "
        "ON cotisations_v2_factures(campagne_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cotisations_v2_campagnes_annee "
        "ON cotisations_v2_campagnes(annee)"
    )

    print("✓ Tables cotisations_v2_campagnes / cotisations_v2_factures prêtes.")


def ensure_modeles(conn):
    """Seed idempotent des modèles email de Cotisations V2 (envoi initial + relances)."""

    existe = conn.execute(
        "SELECT 1 FROM modeles_emails WHERE code_modele = 'facture_cotisation_v2'"
    ).fetchone()
    if not existe:
        conn.execute("""
            INSERT INTO modeles_emails (code_modele, sujet, corps, type_periode)
            VALUES (?, ?, ?, 'facture')
        """, (
            "facture_cotisation_v2",
            "Cotisation <<annee>> – <<nom_association>>",
            "Bonjour,\n\n"
            "Conformément à la convention signée avec la Banque Alimentaire de "
            "l'Isère, votre cotisation pour l'année <<annee>> s'élève à "
            "<<montant>> €.\n\n"
            "Vous trouverez la facture en pièce jointe. Merci de bien vouloir "
            "procéder au règlement avant le 28 février <<annee>>.\n\n"
            "Cordialement,\nLa Trésorerie de la Banque Alimentaire de l'Isère",
        ))
        print("✓ modeles_emails : modèle créé : facture_cotisation_v2")

    modeles_relance = [
        (
            "COTISATIONS V2 Relance 1",
            "Rappel {numero_relance} – Cotisation {annee}",
            "Bonjour,\n\n"
            "Nous n'avons pas encore reçu le règlement de votre cotisation "
            "{annee}, d'un montant de {montant} €.\n\n"
            "Vous trouverez la facture en pièce jointe.\n\n"
            "Merci de bien vouloir procéder au règlement dans les meilleurs délais.\n\n"
            "Cordialement,\nLa Trésorerie de la Banque Alimentaire de l'Isère",
        ),
        (
            "COTISATIONS V2 Relance 2",
            "Relance {numero_relance} – Cotisation {annee}",
            "Bonjour,\n\n"
            "Malgré notre précédent message, nous n'avons toujours pas reçu le "
            "règlement de votre cotisation {annee}, d'un montant de {montant} €.\n\n"
            "Vous trouverez la facture en pièce jointe.\n\n"
            "Merci de bien vouloir régulariser rapidement cette situation.\n\n"
            "Cordialement,\nLa Trésorerie de la Banque Alimentaire de l'Isère",
        ),
        (
            "COTISATIONS V2 Relance 3",
            "Relance {numero_relance} – Cotisation {annee}",
            "Bonjour,\n\n"
            "Sans nouvelle de votre part malgré nos précédentes relances, le "
            "règlement de votre cotisation {annee} ({montant} €) reste en attente.\n\n"
            "Vous trouverez la facture en pièce jointe.\n\n"
            "Merci de procéder au règlement dans les plus brefs délais.\n\n"
            "Cordialement,\nLa Trésorerie de la Banque Alimentaire de l'Isère",
        ),
    ]

    for code_modele, sujet, corps in modeles_relance:
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

    print(f"➡ Migration Cotisations V2 [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        ensure_tables(conn)
        ensure_modeles(conn)
        conn.commit()

    print("✓ Migration terminée.")


if __name__ == "__main__":
    main()
