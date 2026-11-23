# -*- coding: utf-8 -*-
"""
Schéma du module Mécénat (trésorerie).

Fichier volontairement sans import Flask : il est chargé aussi bien par les
routes (ba38_tresorerie/mecenat.py) que par le script de migration
(scripts/migrate_mecenat.py, via importlib, sans passer par le package).

Tables
------
mecenat           : les mécènes (structure + contact).
mecenat_dossiers  : les dossiers à financer (ex. un camion l'an prochain).
mecenat_demandes  : une ligne par mécène sollicité sur un dossier
                    (montant demandé / obtenu / date de versement).

mecenat_dossiers.total_subvention_obtenues est recalculé (somme des
montant_obtenu des demandes) à chaque modification d'une demande.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS mecenat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    structure TEXT NOT NULL,
    nom TEXT,
    prenom TEXT,
    telephone_fixe TEXT,
    telephone_portable TEXT,
    email TEXT,
    adresse TEXT,
    cp TEXT,
    ville TEXT,
    commentaire TEXT,
    statut TEXT NOT NULL DEFAULT 'prospect',
    date_modif TEXT,
    user_modif TEXT
);

CREATE TABLE IF NOT EXISTS mecenat_dossiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type_dossier TEXT NOT NULL DEFAULT 'investissement',
    famille TEXT,
    objet TEXT NOT NULL,
    date_prevue TEXT,
    montant_ttc REAL DEFAULT 0,
    total_subvention_obtenues REAL DEFAULT 0,
    pilote_projet_id INTEGER,
    pilote_mecenat_id INTEGER,
    pole_id INTEGER,
    date_modif TEXT,
    user_modif TEXT
);

CREATE TABLE IF NOT EXISTS mecenat_demandes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier_id INTEGER NOT NULL,
    mecene_id INTEGER NOT NULL,
    montant_demande REAL DEFAULT 0,
    montant_obtenu REAL DEFAULT 0,
    date_versement TEXT,
    notes TEXT,
    date_modif TEXT,
    user_modif TEXT
);

-- Journal de suivi (CRM) : rendez-vous, appels, mails, événements... d'un mécène ou prospect.
CREATE TABLE IF NOT EXISTS mecenat_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mecene_id INTEGER NOT NULL,
    dossier_id INTEGER,
    type_interaction TEXT NOT NULL DEFAULT 'rendez_vous',
    date_interaction TEXT,
    benevole_id INTEGER,
    compte_rendu TEXT,
    prochaine_action TEXT,
    date_relance TEXT,
    relance_faite INTEGER NOT NULL DEFAULT 0,
    date_creation TEXT,
    user_creation TEXT,
    date_modif TEXT,
    user_modif TEXT
);

CREATE TABLE IF NOT EXISTS mecenat_pieces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interaction_id INTEGER NOT NULL,
    nom_original TEXT NOT NULL,
    fichier TEXT NOT NULL,
    taille INTEGER,
    date_ajout TEXT,
    user_ajout TEXT
);

CREATE INDEX IF NOT EXISTS idx_mecenat_inter_mecene ON mecenat_interactions(mecene_id);
CREATE INDEX IF NOT EXISTS idx_mecenat_inter_relance ON mecenat_interactions(date_relance);
CREATE INDEX IF NOT EXISTS idx_mecenat_pieces_inter ON mecenat_pieces(interaction_id);
CREATE INDEX IF NOT EXISTS idx_mecenat_demandes_dossier ON mecenat_demandes(dossier_id);
CREATE INDEX IF NOT EXISTS idx_mecenat_demandes_mecene ON mecenat_demandes(mecene_id);
"""


def ensure_schema(conn):
    """Crée les tables si besoin (idempotent, sans effet si elles existent)."""
    conn.executescript(SCHEMA)
    # Colonnes ajoutées après la première version (bases déjà créées)
    existantes = {r[1] for r in conn.execute("PRAGMA table_info(mecenat_demandes)")}
    if "notes" not in existantes:
        conn.execute("ALTER TABLE mecenat_demandes ADD COLUMN notes TEXT")
        conn.commit()
    existantes = {r[1] for r in conn.execute("PRAGMA table_info(mecenat)")}
    if "statut" not in existantes:
        conn.execute("ALTER TABLE mecenat ADD COLUMN statut TEXT NOT NULL DEFAULT 'prospect'")
        conn.commit()
    existantes = {r[1] for r in conn.execute("PRAGMA table_info(mecenat_dossiers)")}
    if "pole_id" not in existantes:
        conn.execute("ALTER TABLE mecenat_dossiers ADD COLUMN pole_id INTEGER")  # -> engagement_poles.id
        conn.commit()
    if "type_dossier" not in existantes:
        # dossiers existants = investissements
        conn.execute("ALTER TABLE mecenat_dossiers ADD COLUMN type_dossier TEXT NOT NULL DEFAULT 'investissement'")
        conn.commit()
