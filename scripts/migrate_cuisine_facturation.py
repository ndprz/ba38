#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : facturation mensuelle cuisine (ba38_cuisine/routes_facturation.py
+ routes_facturation_relance.py), sur le modèle de Cotisations V2.

Une facture par client et par mois, qui regroupe ses bons de livraison
validés du mois non encore facturés (les BL passent au statut 'facture').
PDF régénéré à la demande depuis la base (aucun fichier conservé).

Ajoute :
- `cuisine_factures_campagnes` : une ligne par mois facturé (annee, mois) +
  bandeau du dernier envoi.
- `cuisine_factures` : numéro FC-AAAA-NNNN, client, dates, portions,
  montant, statut (emise / annulee) + suivi envoi Mailjet, paiement,
  relances (mêmes colonnes que cotisations_v2_factures).
- `cuisine_factures_bl` : BL inclus dans chaque facture.
- `cuisine_factures_reglements` : règlements (partiels possibles) ; la
  facture est soldée (date_paiement) quand leur total atteint son montant.
- Modèles de mail : "CUISINE Facture" (envoi, variables <<xxx>>) et
  "CUISINE Relance 1/2/3" (variables {xxx}, comme Cotisations V2).

Les 4 tables référencent associations.id (qui diverge DEV/PROD) : elles sont
dans EXCLUDE_TABLES de migrate_schema_and_data_dev_to_prod.py. Les modèles de
mail sont de nouvelles LIGNES d'une table existante, que la synchro ne copie
jamais : lancer ce script sur chaque base (dev, dev_test, prod, prod_test)
AVANT le déploiement du code.

Chemins explicites (pas de load_dotenv, cf. piège dev/.env). Idempotent.
"""

import argparse
import os
import sqlite3
import sys
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cuisine_factures_campagnes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  annee INTEGER NOT NULL,
  mois INTEGER NOT NULL,
  date_creation TEXT,
  cree_par TEXT,
  dernier_envoi_le TEXT,
  dernier_envoi_par TEXT,
  dernier_envoi_mode_test INTEGER DEFAULT 0,
  dernier_envoi_nb_ok INTEGER,
  dernier_envoi_nb_erreur INTEGER,
  UNIQUE (annee, mois)
);

CREATE TABLE IF NOT EXISTS cuisine_factures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campagne_id INTEGER NOT NULL REFERENCES cuisine_factures_campagnes(id),
  numero TEXT NOT NULL UNIQUE,
  association_id INTEGER NOT NULL,
  nom_association TEXT NOT NULL,
  date_facture TEXT NOT NULL,
  date_echeance TEXT,
  portions_carne INTEGER DEFAULT 0,
  portions_legumes INTEGER DEFAULT 0,
  montant REAL DEFAULT 0,
  statut TEXT NOT NULL DEFAULT 'emise' CHECK (statut IN ('emise','annulee')),
  date_creation TEXT,
  user_creation TEXT,
  date_annulation TEXT,
  user_annulation TEXT,
  motif_annulation TEXT,
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
  relance_renvoi_gmail_le TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_factures_campagne ON cuisine_factures(campagne_id);

CREATE TABLE IF NOT EXISTS cuisine_factures_bl (
  facture_id INTEGER NOT NULL REFERENCES cuisine_factures(id),
  bon_id INTEGER NOT NULL REFERENCES cuisine_bons_livraison(id),
  PRIMARY KEY (facture_id, bon_id)
);
CREATE INDEX IF NOT EXISTS idx_cuisine_factures_bl_bon ON cuisine_factures_bl(bon_id);

CREATE TABLE IF NOT EXISTS cuisine_factures_reglements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  facture_id INTEGER NOT NULL REFERENCES cuisine_factures(id),
  date_reglement TEXT NOT NULL,
  montant REAL NOT NULL,
  mode TEXT,
  reference TEXT,
  commentaire TEXT,
  date_saisie TEXT,
  user_saisie TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_factures_reglements_facture ON cuisine_factures_reglements(facture_id);
"""

SIGNATURE = "Cordialement,\nLa Trésorerie de la Banque Alimentaire de l'Isère"

MODELE_ENVOI = (
    "CUISINE Facture",
    "Facture <<numero>> – Repas livrés en <<periode>> – <<nom_association>>",
    "Bonjour,\n\n"
    "Vous trouverez ci-joint la facture <<numero>> correspondant aux repas livrés "
    "par la cuisine de la Banque Alimentaire de l'Isère en <<periode>> "
    "(<<nb_bl>> bon(s) de livraison), d'un montant de <<montant>> €.\n\n"
    "Cette facture est payable à réception.\n\n" + SIGNATURE,
)

MODELES_RELANCE = [
    (
        "CUISINE Relance 1",
        "Rappel {numero_relance} – Facture {numero} – Repas livrés en {periode}",
        "Bonjour,\n\n"
        "Sauf erreur de notre part, nous n'avons pas encore reçu le règlement complet de la facture "
        "{numero} du {echeance} (repas livrés en {periode}, montant {montant_facture} €), "
        "payable à réception.\n\nReste à régler : {montant} €.\n\nVous trouverez la facture en pièce jointe.\n\n"
        "Merci de bien vouloir procéder au règlement dans les meilleurs délais.\n\n" + SIGNATURE,
    ),
    (
        "CUISINE Relance 2",
        "Relance {numero_relance} – Facture {numero} – Repas livrés en {periode}",
        "Bonjour,\n\n"
        "Malgré notre précédent message, le règlement de la facture {numero} du {echeance} "
        "(repas livrés en {periode}, montant {montant_facture} €) n'est toujours pas complet.\n\n"
        "Reste à régler : {montant} €.\n\nVous trouverez la facture en pièce jointe.\n\n"
        "Merci de bien vouloir régulariser rapidement cette situation.\n\n" + SIGNATURE,
    ),
    (
        "CUISINE Relance 3",
        "Relance {numero_relance} – Facture {numero} – Repas livrés en {periode}",
        "Bonjour,\n\n"
        "Sans nouvelle de votre part malgré nos précédentes relances, le règlement de la facture "
        "{numero} du {echeance} (repas livrés en {periode}, montant {montant_facture} €) reste en attente.\n\n"
        "Reste à régler : {montant} €.\n\nVous trouverez la facture en pièce jointe.\n\n"
        "Merci de procéder au règlement dans les plus brefs délais.\n\n" + SIGNATURE,
    ),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=list(DB_PATHS), default="dev")
    args = parser.parse_args()
    db_path = DB_PATHS[args.env]
    if args.env.startswith("prod") and "/prod/" not in db_path:
        raise RuntimeError(f"❌ Chemin PROD invalide : {db_path}")
    if args.env.startswith("dev") and "/dev/" not in db_path:
        raise RuntimeError(f"❌ Chemin DEV invalide : {db_path}")
    if not os.path.exists(db_path):
        print(f"❌ Base introuvable : {db_path}")
        sys.exit(1)

    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        print("✅ Tables cuisine_factures_campagnes / cuisine_factures / cuisine_factures_bl / cuisine_factures_reglements prêtes")

        code, sujet, corps = MODELE_ENVOI
        if conn.execute("SELECT 1 FROM modeles_emails WHERE code_modele = ?", (code,)).fetchone():
            print(f"ℹ️  Modèle déjà présent : {code}")
        else:
            conn.execute(
                "INSERT INTO modeles_emails (code_modele, sujet, corps, type_periode) VALUES (?, ?, ?, 'facture_cuisine')",
                (code, sujet, corps),
            )
            print(f"✅ Modèle créé : {code}")
        for code, sujet, corps in MODELES_RELANCE:
            if conn.execute("SELECT 1 FROM modeles_emails WHERE code_modele = ?", (code,)).fetchone():
                print(f"ℹ️  Modèle déjà présent : {code}")
            else:
                conn.execute("INSERT INTO modeles_emails (code_modele, sujet, corps) VALUES (?, ?, ?)", (code, sujet, corps))
                print(f"✅ Modèle créé : {code}")
        conn.commit()

    print(f"🎉 Terminé sur [{args.env}] : {db_path}")


if __name__ == "__main__":
    main()
