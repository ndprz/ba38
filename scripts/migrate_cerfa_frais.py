#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : module CERFA (package ba38_cerfa/), première option
« CERFA abandon de frais bénévoles ».

Campagne annuelle : les bénévoles à jour de cotisation de l'année des frais
reçoivent un lien personnel vers un questionnaire en ligne (sans compte) ;
ils indiquent s'ils sont imposables, saisissent leurs journées par lieu et
par mois, déposent leurs pièces (carte grise / avis de non-imposition).
La trésorerie valide, puis génère le CERFA 11580 (imposables) ou le
courrier de remboursement forfaitaire en tickets TAG (non-imposables).

Ajoute :
- `cerfa_frais_lieux` : lieux d'activité paramétrables (adresse géocodée
  pour le calcul automatique des km) ; « Autres » sans adresse = km saisis
  par le bénévole + commentaire.
- `cerfa_frais_campagnes` : une ligne par année de frais (barème km,
  prix ticket TAG, date limite, textes des mails/courrier, bandeaux envoi).
- `cerfa_frais_declarations` : une ligne par bénévole et par campagne
  (jeton du lien, réponses, montants figés à la validation, suivi envois).
- `cerfa_frais_pieces` : pièces justificatives déposées.
- ligne `applications` « cerfa » (groupe Finance) + droits `cerfa` recopiés
  depuis les droits `tresorerie` existants.

Les déclarations référencent benevoles.id et les pièces des fichiers locaux
→ tables à mettre dans EXCLUDE_TABLES de migrate_schema_and_data_dev_to_prod.py.
Les lignes `applications` / `roles_utilisateurs` ne sont jamais copiées par
la synchro : lancer ce script sur chaque base (dev, dev_test, prod,
prod_test) AVANT le déploiement du code.

Chemins explicites (pas de load_dotenv, cf. piège dev/.env). Idempotent.

Usage :
    python3 scripts/migrate_cerfa_frais.py --env dev
    python3 scripts/migrate_cerfa_frais.py --env dev_test
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
CREATE TABLE IF NOT EXISTS cerfa_frais_lieux (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nom TEXT NOT NULL,
  adresse TEXT,
  latitude REAL,
  longitude REAL,
  geocode_label TEXT,
  est_autre INTEGER DEFAULT 0,
  ordre INTEGER DEFAULT 0,
  actif INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cerfa_frais_campagnes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  annee_frais INTEGER NOT NULL UNIQUE,
  annee_emission INTEGER NOT NULL,
  date_limite TEXT,
  prix_ticket_tag REAL,
  nb_tickets_par_journee INTEGER DEFAULT 2,
  majoration_electrique REAL DEFAULT 0.20,
  bareme_json TEXT,
  bareme_source TEXT,
  expediteur_email TEXT,
  reply_to_email TEXT,
  signataire_nom TEXT,
  signataire_qualite TEXT,
  mail_invitation_sujet TEXT,
  mail_invitation_corps TEXT,
  mail_relance_sujet TEXT,
  mail_relance_corps TEXT,
  mail_cerfa_sujet TEXT,
  mail_cerfa_corps TEXT,
  mail_remboursement_sujet TEXT,
  mail_remboursement_corps TEXT,
  courrier_remboursement_texte TEXT,
  date_creation TEXT,
  cree_par TEXT,
  dernier_envoi_type TEXT,
  dernier_envoi_le TEXT,
  dernier_envoi_par TEXT,
  dernier_envoi_mode_test INTEGER DEFAULT 0,
  dernier_envoi_nb_ok INTEGER,
  dernier_envoi_nb_erreur INTEGER
);

CREATE TABLE IF NOT EXISTS cerfa_frais_declarations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campagne_id INTEGER NOT NULL REFERENCES cerfa_frais_campagnes(id),
  benevole_id INTEGER NOT NULL,
  token TEXT NOT NULL UNIQUE,
  statut TEXT NOT NULL DEFAULT 'a_inviter'
    CHECK (statut IN ('a_inviter','invite','en_cours','soumis','valide','termine',
                      'courrier_envoye','rembourse','decline')),
  civilite TEXT,
  nom TEXT,
  prenom TEXT,
  rue TEXT,
  complement_adresse TEXT,
  code_postal TEXT,
  ville TEXT,
  email TEXT,
  telephone TEXT,
  imposable INTEGER,
  vehicule_type TEXT,
  vehicule_marque TEXT,
  vehicule_immatriculation TEXT,
  vehicule_cv INTEGER,
  vehicule_electrique INTEGER DEFAULT 0,
  journees_json TEXT,
  km_json TEXT,
  km_auto_json TEXT,
  adresse_geocode_label TEXT,
  adresse_geocode_score REAL,
  commentaire_autres TEXT,
  commentaire_benevole TEXT,
  premiere_ouverture_le TEXT,
  derniere_modif_le TEXT,
  certifie_le TEXT,
  soumis_le TEXT,
  decline_le TEXT,
  total_journees INTEGER,
  distance_totale REAL,
  montant REAL,
  montant_arrondi INTEGER,
  numero_document TEXT,
  valide_le TEXT,
  valide_par TEXT,
  commentaire_tresorerie TEXT,
  invitation_envoyee_le TEXT,
  invitation_mode_test INTEGER DEFAULT 0,
  invitation_erreur TEXT,
  invitation_mailjet_status TEXT,
  invitation_mailjet_ids TEXT,
  invitation_statut_final TEXT,
  nb_relances INTEGER DEFAULT 0,
  derniere_relance_le TEXT,
  relance_mode_test INTEGER DEFAULT 0,
  relance_erreur TEXT,
  document_envoye_le TEXT,
  document_mode_test INTEGER DEFAULT 0,
  document_erreur TEXT,
  document_mailjet_status TEXT,
  document_mailjet_ids TEXT,
  document_statut_final TEXT,
  document_remis_le TEXT,
  document_remis_par TEXT,
  rembourse_le TEXT,
  rembourse_par TEXT,
  UNIQUE (campagne_id, benevole_id)
);
CREATE INDEX IF NOT EXISTS idx_cerfa_frais_decl_campagne ON cerfa_frais_declarations(campagne_id);

CREATE TABLE IF NOT EXISTS cerfa_frais_pieces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  declaration_id INTEGER NOT NULL REFERENCES cerfa_frais_declarations(id),
  type_piece TEXT NOT NULL CHECK (type_piece IN ('carte_grise','avis_non_imposition','autre')),
  chemin TEXT NOT NULL,
  nom_original TEXT,
  taille INTEGER,
  depose_le TEXT
);
CREATE INDEX IF NOT EXISTS idx_cerfa_frais_pieces_decl ON cerfa_frais_pieces(declaration_id);
"""

# Adresses inconnues laissées vides : à compléter dans Paramètres → Lieux
# (sans adresse, le bénévole saisit lui-même ses km pour ce lieu).
LIEUX_DEFAUT = [
    ("La Pinéa", "11 allée de la Pinéa 38600 Fontaine", 0, 1),
    ("Ramasse", "11 allée de la Pinéa 38600 Fontaine", 0, 2),
    ("Cuisine 3 étoiles", None, 0, 3),
    ("Épicerie Esope", None, 0, 4),
    ("Autres", None, 1, 99),
]


def _ddl_declarations(nom_table):
    debut = SCHEMA.index("CREATE TABLE IF NOT EXISTS cerfa_frais_declarations")
    fin = SCHEMA.index(");", debut) + 2
    return SCHEMA[debut:fin].replace("IF NOT EXISTS cerfa_frais_declarations", nom_table)


def _reconstruire_declarations(conn):
    """v2 : statuts de fin de dossier (termine / courrier_envoye / rembourse)
    + colonnes remise/remboursement. SQLite ne sait pas modifier un CHECK :
    reconstruction selon la procédure officielle (nouvelle table, copie,
    suppression, renommage) — les FK de cerfa_frais_pieces restent valides."""
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cerfa_frais_declarations'"
    ).fetchone()
    if not ddl or "courrier_envoye" in ddl[0]:
        return
    anciennes = [r[1] for r in conn.execute("PRAGMA table_info(cerfa_frais_declarations)")]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    conn.execute(_ddl_declarations("cerfa_frais_declarations_v2"))
    nouvelles = {r[1] for r in conn.execute("PRAGMA table_info(cerfa_frais_declarations_v2)")}
    colonnes = ", ".join(c for c in anciennes if c in nouvelles)
    conn.execute(f"INSERT INTO cerfa_frais_declarations_v2 ({colonnes}) "
                 f"SELECT {colonnes} FROM cerfa_frais_declarations")
    nb = conn.execute("SELECT COUNT(*) FROM cerfa_frais_declarations_v2").fetchone()[0]
    conn.execute("DROP TABLE cerfa_frais_declarations")
    conn.execute("ALTER TABLE cerfa_frais_declarations_v2 RENAME TO cerfa_frais_declarations")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cerfa_frais_decl_campagne ON cerfa_frais_declarations(campagne_id)")
    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys = ON")
    print(f"  ✅ cerfa_frais_declarations reconstruite (nouveaux statuts) : {nb} ligne(s) conservée(s)")


def migrer(db_path):
    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        _reconstruire_declarations(conn)
        conn.executescript(SCHEMA)

        if conn.execute("SELECT COUNT(*) FROM cerfa_frais_lieux").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO cerfa_frais_lieux (nom, adresse, est_autre, ordre) VALUES (?, ?, ?, ?)",
                LIEUX_DEFAUT,
            )
            print("  ✅ lieux par défaut créés")

        conn.execute("""
            INSERT OR IGNORE INTO applications
                (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible)
            VALUES ('cerfa', 'CERFA', 'cerfa.cerfa_menu', 'Finance', 3, '📜', 3, 1)
        """)

        nb = conn.execute("""
            INSERT INTO roles_utilisateurs (user_email, appli, droit)
            SELECT r.user_email, 'cerfa', r.droit
            FROM roles_utilisateurs r
            WHERE r.appli = 'tresorerie'
              AND NOT EXISTS (
                  SELECT 1 FROM roles_utilisateurs c
                  WHERE LOWER(c.user_email) = LOWER(r.user_email) AND c.appli = 'cerfa'
              )
        """).rowcount
        print(f"  ✅ droits cerfa recopiés depuis tresorerie : {nb} ligne(s)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, choices=sorted(DB_PATHS))
    args = parser.parse_args()

    db_path = DB_PATHS[args.env]
    if not os.path.exists(db_path):
        print(f"❌ Base introuvable : {db_path}")
        sys.exit(1)

    print(f"🔧 Migration CERFA abandon de frais → {db_path}")
    migrer(db_path)
    print("✅ Terminé")


if __name__ == "__main__":
    main()
