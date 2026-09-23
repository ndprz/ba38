#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : module Cuisine — Étape 1 : traçabilité production + hygiène site.

⚠️ Nommage : un module `ba38_planning/cuisine.py` (blueprint
`planning_cuisine_bp`, templates `templates/planning/cuisine/`) existe déjà
et gère le planning des équipes (horaires bénévoles) — sans rapport avec ce
module. Ce script crée un ensemble de tables totalement séparé, préfixé
`cuisine_*`, pour le nouveau module `ba38_cuisine/`
(blueprints `production_cuisine_bp` et `cuisine_hygiene_bp`).

Crée (idempotent, CREATE TABLE IF NOT EXISTS) :
- cuisine_receptions / cuisine_reception_photos (réception marchandises)
- cuisine_recettes / cuisine_productions / cuisine_productions_renommages
- cuisine_traca_lots / cuisine_traca_photos (traçabilité fournisseur par lot)
- cuisine_etapes_ref / cuisine_production_etapes (étapes de fabrication)
- cuisine_production_quantites / cuisine_production_validations
- cuisine_hygiene_zones_temperature / cuisine_hygiene_releves_temperature
- cuisine_hygiene_zones_nettoyage / cuisine_hygiene_nettoyages
- cuisine_hygiene_thermometres / cuisine_hygiene_etalonnages
- cuisine_consignes_clients (consignes de livraison des partenaires cuisine)
- cuisine_bons_livraison / cuisine_bons_livraison_lignes (sorties de stock)

Seed (INSERT OR IGNORE, idempotent) :
- cuisine_etapes_ref (7 étapes)
- cuisine_hygiene_zones_temperature (13 zones)
- cuisine_hygiene_zones_nettoyage (27 zones)
- cuisine_hygiene_thermometres (15 thermomètres)
- 2 lignes dans `applications` (menu) : production_cuisine, cuisine_hygiene

⚠️ Ne crée AUCUN compte `roles_utilisateurs` (pas d'email en dur) : c'est à
l'administrateur d'attribuer les droits "production_cuisine" /
"cuisine_hygiene" aux bénévoles/postes concernés via l'interface existante
(gestion des utilisateurs).

Réutilise la table `fournisseurs` existante (FK fournisseur_id) : aucune
nouvelle table fournisseur n'est créée.

Idempotent : CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS /
CREATE TRIGGER IF NOT EXISTS / INSERT OR IGNORE.
Utilisable sur DEV ou PROD via l'argument --env (défaut : dev) — mais ce
script ne doit être exécuté QUE avec --env dev pour l'instant (module non
encore validé pour la prod).
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

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cuisine_receptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date_reception TEXT NOT NULL,
  heure_arrivee TEXT,
  fournisseur_id INTEGER REFERENCES fournisseurs(id),
  camion_libelle TEXT,
  temperature_mesuree REAL,
  poids_kg REAL,
  aspect_conforme TEXT CHECK (aspect_conforme IN ('conforme','non_conforme')),
  emballage_conforme TEXT CHECK (emballage_conforme IN ('conforme','non_conforme')),
  etiquetage_conforme TEXT CHECK (etiquetage_conforme IN ('conforme','non_conforme')),
  dlc_ddm TEXT,
  numero_lot TEXT,
  commentaire TEXT,
  libelle_produit TEXT,
  production_id INTEGER REFERENCES cuisine_productions(id),
  actif INTEGER DEFAULT 1,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  date_modif TEXT, user_creation TEXT, user_modif TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_receptions_date ON cuisine_receptions(date_reception);

CREATE TABLE IF NOT EXISTS cuisine_reception_photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reception_id INTEGER NOT NULL REFERENCES cuisine_receptions(id) ON DELETE CASCADE,
  type_photo TEXT CHECK (type_photo IN ('produit','etiquette')),
  chemin_fichier TEXT NOT NULL,
  ordre INTEGER DEFAULT 0,
  date_creation TEXT DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS cuisine_recettes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nom TEXT NOT NULL,
  actif INTEGER DEFAULT 1,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  date_modif TEXT, user_modif TEXT
);

-- Référentiel officiel des recettes C3ES (import du fichier Excel du
-- responsable cuisine, onglet "05"). Table distincte de cuisine_recettes
-- (catalogue léger utilisé au fil de l'eau en production) : celle-ci porte
-- le code interne, la famille/sous-familles et le mode de cuisson, sert de
-- base aux exports pour l'étiqueteuse automatique (code suffixé -05 ou -03
-- selon la DLC choisie à l'export, pas stocké ici).
CREATE TABLE IF NOT EXISTS cuisine_recettes_referentiel (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code INTEGER UNIQUE,
  nom TEXT NOT NULL,
  famille TEXT,
  sous_famille_1 TEXT,
  sous_famille_2 TEXT,
  type_cuisson TEXT,
  actif INTEGER DEFAULT 1,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  date_modif TEXT, user_creation TEXT, user_modif TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_recettes_ref_code ON cuisine_recettes_referentiel(code);
CREATE INDEX IF NOT EXISTS idx_cuisine_recettes_ref_famille ON cuisine_recettes_referentiel(famille);

CREATE TRIGGER IF NOT EXISTS trg_cuisine_recettes_referentiel_datemodif
AFTER UPDATE ON cuisine_recettes_referentiel
FOR EACH ROW
BEGIN
  UPDATE cuisine_recettes_referentiel SET date_modif = datetime('now','utc') WHERE id = NEW.id;
END;

-- Référentiel des ingrédients carnés (import du fichier Excel du
-- responsable cuisine "documents Nicolas.xlsx", onglet "liste produit ").
-- Utilisé par l'écran de réception marchandises : on choisit d'abord le
-- groupe (Boeuf, Agneau, Poisson, Gibier, Divers...), puis le produit dans
-- ce groupe (ex. "Boeuf" / "Langue").
CREATE TABLE IF NOT EXISTS cuisine_ingredients_carnes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  groupe TEXT NOT NULL,
  produit TEXT NOT NULL,
  actif INTEGER DEFAULT 1,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  UNIQUE(groupe, produit)
);
CREATE INDEX IF NOT EXISTS idx_cuisine_ingredients_carnes_groupe ON cuisine_ingredients_carnes(groupe);

CREATE TABLE IF NOT EXISTS cuisine_productions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date_production TEXT NOT NULL,
  recette_id INTEGER REFERENCES cuisine_recettes(id),
  recette_referentiel_id INTEGER REFERENCES cuisine_recettes_referentiel(id),
  nom_recette TEXT NOT NULL,
  nom_recette_initial TEXT NOT NULL,
  espece TEXT,
  mode_cuisson TEXT,
  statut TEXT DEFAULT 'en_cours' CHECK (statut IN ('en_cours','terminee','annulee')),
  actif INTEGER DEFAULT 1,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  date_modif TEXT, user_creation TEXT, user_modif TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_productions_date ON cuisine_productions(date_production);

CREATE TABLE IF NOT EXISTS cuisine_productions_renommages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL REFERENCES cuisine_productions(id) ON DELETE CASCADE,
  ancien_nom TEXT NOT NULL,
  nouveau_nom TEXT NOT NULL,
  date_renommage TEXT DEFAULT (datetime('now','utc')),
  user_creation TEXT
);

CREATE TABLE IF NOT EXISTS cuisine_traca_lots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL REFERENCES cuisine_productions(id) ON DELETE CASCADE,
  fournisseur_id INTEGER REFERENCES fournisseurs(id),
  horodatage TEXT DEFAULT (datetime('now','utc')),
  commentaire TEXT, user_creation TEXT,
  date_creation TEXT DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_cuisine_traca_lots_production ON cuisine_traca_lots(production_id);

CREATE TABLE IF NOT EXISTS cuisine_traca_photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id INTEGER NOT NULL REFERENCES cuisine_traca_lots(id) ON DELETE CASCADE,
  chemin_fichier TEXT NOT NULL,
  ordre INTEGER DEFAULT 0,
  date_creation TEXT DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS cuisine_etapes_ref (
  code TEXT PRIMARY KEY,
  libelle TEXT NOT NULL,
  ordre INTEGER NOT NULL,
  saisie_debut INTEGER DEFAULT 1,
  saisie_fin INTEGER DEFAULT 1,
  saisie_temperature INTEGER DEFAULT 1,
  saisie_temperature_debut INTEGER DEFAULT 0,
  saisie_conformite INTEGER DEFAULT 1,
  saisie_cellule INTEGER DEFAULT 0,
  optionnelle INTEGER DEFAULT 0,
  actif INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cuisine_production_etapes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL REFERENCES cuisine_productions(id) ON DELETE CASCADE,
  etape_code TEXT NOT NULL REFERENCES cuisine_etapes_ref(code),
  heure_debut TEXT, heure_fin TEXT,
  temperature REAL,
  temperature_debut REAL,
  cellule_numero INTEGER,
  conforme TEXT CHECK (conforme IN ('conforme','non_conforme')),
  non_applicable INTEGER DEFAULT 0,
  commentaire TEXT,
  user_creation TEXT, date_creation TEXT DEFAULT (datetime('now','utc')),
  user_modif TEXT, date_modif TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_prod_etapes_production ON cuisine_production_etapes(production_id);

CREATE TABLE IF NOT EXISTS cuisine_production_quantites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL REFERENCES cuisine_productions(id) ON DELETE CASCADE,
  taille TEXT NOT NULL CHECK (taille IN ('1/2','1/4','1/8')),
  quantite INTEGER NOT NULL DEFAULT 0,
  UNIQUE(production_id, taille)
);

-- Référentiel des articles barquettes (module Stock & Ventes barquettes) :
-- une ligne par taille de barquette, avec son nombre de portions.
CREATE TABLE IF NOT EXISTS cuisine_articles_barquettes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  taille TEXT NOT NULL UNIQUE CHECK (taille IN ('1/2','1/4','1/8')),
  code_article TEXT,
  libelle TEXT NOT NULL,
  nb_portions INTEGER NOT NULL,
  actif INTEGER DEFAULT 1,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  date_modif TEXT, user_creation TEXT, user_modif TEXT
);

-- Stock barquettes : une ligne par (production, taille) une fois la
-- recette conditionnée — alimenté depuis "Quantités conditionnées" sur la
-- fiche production (bouton "Ajouter au stock"), jamais saisi à la main.
-- Sert de base aux futurs bons de livraison / factures (pas encore
-- développés — pour l'instant uniquement un stock entrant, sans sortie).
CREATE TABLE IF NOT EXISTS cuisine_stock_barquettes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL REFERENCES cuisine_articles_barquettes(id),
  production_id INTEGER NOT NULL REFERENCES cuisine_productions(id) ON DELETE CASCADE,
  libelle_recette TEXT NOT NULL,
  date_fin_recette TEXT,
  cellule_numero INTEGER,
  quantite INTEGER NOT NULL,
  actif INTEGER DEFAULT 1,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  user_creation TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_stock_barquettes_production ON cuisine_stock_barquettes(production_id);
CREATE INDEX IF NOT EXISTS idx_cuisine_stock_barquettes_article ON cuisine_stock_barquettes(article_id);

-- Consignes de livraison des "clients" cuisine : une ligne par association
-- partenaire (associations.partenaire_cuisine = 'oui'). Référence
-- associations.id, qui DIVERGE entre DEV et PROD → table exclue de la
-- synchro dev→prod (EXCLUDE_TABLES de migrate_schema_and_data_dev_to_prod.py).
--   periodicite : 'hebdomadaire' (jours_semaine = "1,2,3,4", 1 = lundi),
--                 'mensuelle' (jours_semaine + semaine_du_mois 1..4, 5 = dernier),
--                 'a_la_demande' (jamais pré-sélectionné dans la simulation).
--   tailles_barquettes : tailles acceptées, CSV ("1/2,1/4").
--   pourcentage_legumes : portions légumes = nb_portions_carne × % / 100.
--   prix_portion_carne : inclut les légumes (livrés gratuitement).
--   recoit_reliquat : reçoit le reste du stock après les autres clients du jour.
CREATE TABLE IF NOT EXISTS cuisine_consignes_clients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  association_id INTEGER NOT NULL UNIQUE REFERENCES associations(id),
  tailles_barquettes TEXT,
  periodicite TEXT NOT NULL DEFAULT 'hebdomadaire'
    CHECK (periodicite IN ('hebdomadaire','mensuelle','a_la_demande')),
  jours_semaine TEXT,
  semaine_du_mois INTEGER,
  nb_portions_carne INTEGER,
  pourcentage_legumes INTEGER DEFAULT 100,
  prix_portion_carne REAL,
  recoit_reliquat INTEGER DEFAULT 0,
  commentaire TEXT,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  date_modif TEXT, user_creation TEXT, user_modif TEXT
);

-- Bons de livraison cuisine : générés un par un (un client, une date)
-- depuis la simulation de répartition. Le stock disponible d'une ligne de
-- cuisine_stock_barquettes = quantite − SUM(lignes de BL non annulés) sur
-- (production_id, article_id) : les lignes d'entrée ne sont jamais
-- décrémentées (elles sont recréées à chaque "Ajouter au stock"), et annuler
-- un BL (statut 'annule') remet donc le stock sans autre écriture.
-- Annulation interdite une fois statut = 'facture'.
-- Références associations.id / cuisine_productions.id (divergent DEV/PROD)
-- → tables exclues de la synchro dev→prod (EXCLUDE_TABLES).
CREATE TABLE IF NOT EXISTS cuisine_bons_livraison (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  numero TEXT NOT NULL UNIQUE,
  association_id INTEGER NOT NULL REFERENCES associations(id),
  nom_association TEXT NOT NULL,
  date_livraison TEXT NOT NULL,
  statut TEXT NOT NULL DEFAULT 'valide' CHECK (statut IN ('valide','annule','facture')),
  portions_carne INTEGER DEFAULT 0,
  portions_legumes INTEGER DEFAULT 0,
  prix_portion_carne REAL,
  montant REAL DEFAULT 0,
  commentaire TEXT,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  user_creation TEXT,
  date_annulation TEXT,
  user_annulation TEXT,
  motif_annulation TEXT,
  date_facturation TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_bl_date ON cuisine_bons_livraison(date_livraison);
CREATE INDEX IF NOT EXISTS idx_cuisine_bl_association ON cuisine_bons_livraison(association_id);

CREATE TABLE IF NOT EXISTS cuisine_bons_livraison_lignes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bon_id INTEGER NOT NULL REFERENCES cuisine_bons_livraison(id) ON DELETE CASCADE,
  production_id INTEGER NOT NULL REFERENCES cuisine_productions(id),
  article_id INTEGER NOT NULL REFERENCES cuisine_articles_barquettes(id),
  libelle_recette TEXT NOT NULL,
  categorie_produit TEXT,
  taille TEXT NOT NULL,
  nb_portions_barquette INTEGER NOT NULL,
  quantite INTEGER NOT NULL,
  date_fin_recette TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_bl_lignes_bon ON cuisine_bons_livraison_lignes(bon_id);
CREATE INDEX IF NOT EXISTS idx_cuisine_bl_lignes_stock ON cuisine_bons_livraison_lignes(production_id, article_id);

CREATE TABLE IF NOT EXISTS cuisine_production_validations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  production_id INTEGER NOT NULL UNIQUE REFERENCES cuisine_productions(id) ON DELETE CASCADE,
  plat_temoin TEXT NOT NULL DEFAULT 'non' CHECK (plat_temoin IN ('oui','non')),
  plat_temoin_poids_g REAL,
  valide_par TEXT,
  date_validation TEXT DEFAULT (datetime('now','utc')),
  commentaire TEXT
);

CREATE TABLE IF NOT EXISTS cuisine_hygiene_zones_temperature (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, libelle TEXT NOT NULL,
  ordre INTEGER DEFAULT 0, actif INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cuisine_hygiene_releves_temperature (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_id INTEGER NOT NULL REFERENCES cuisine_hygiene_zones_temperature(id),
  date_releve TEXT DEFAULT (datetime('now','utc')),
  temperature REAL NOT NULL,
  conforme TEXT CHECK (conforme IN ('conforme','non_conforme')),
  user_creation TEXT, commentaire TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_hyg_temp_date ON cuisine_hygiene_releves_temperature(date_releve);

CREATE TABLE IF NOT EXISTS cuisine_hygiene_zones_nettoyage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, libelle TEXT NOT NULL,
  ordre INTEGER DEFAULT 0, actif INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cuisine_hygiene_nettoyages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_id INTEGER NOT NULL REFERENCES cuisine_hygiene_zones_nettoyage(id),
  date_nettoyage TEXT DEFAULT (datetime('now','utc')),
  conforme TEXT CHECK (conforme IN ('conforme','non_conforme')) DEFAULT 'conforme',
  user_creation TEXT, commentaire TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_hyg_nettoyage_date ON cuisine_hygiene_nettoyages(date_nettoyage);

-- Remplacement du nettoyage "zone unique" par le vrai Plan de Nettoyage et
-- Désinfection (PND) du site, importé depuis "PND C3ES.xlsx" (14 onglets =
-- 14 zones, chacune avec plusieurs surfaces à nettoyer, fréquence/produit/
-- point clef propres à chaque surface). cuisine_hygiene_zones_nettoyage /
-- cuisine_hygiene_nettoyages ci-dessus restent en base (historique) mais
-- ne sont plus utilisées par l'écran "Nettoyage" — remplacées par les
-- tables suivantes.
CREATE TABLE IF NOT EXISTS cuisine_pnd_zones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  libelle TEXT NOT NULL,
  description TEXT,
  ordre INTEGER DEFAULT 0,
  actif INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cuisine_pnd_surfaces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_id INTEGER NOT NULL REFERENCES cuisine_pnd_zones(id),
  nom_surface TEXT NOT NULL,
  frequence TEXT,
  produit_dose TEXT,
  point_clef TEXT,
  ordre INTEGER DEFAULT 0,
  actif INTEGER DEFAULT 1,
  date_creation TEXT DEFAULT (datetime('now','utc')),
  date_modif TEXT, user_modif TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_pnd_surfaces_zone ON cuisine_pnd_surfaces(zone_id);

CREATE TABLE IF NOT EXISTS cuisine_pnd_nettoyages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  surface_id INTEGER NOT NULL REFERENCES cuisine_pnd_surfaces(id),
  date_nettoyage TEXT DEFAULT (datetime('now','utc')),
  conforme TEXT CHECK (conforme IN ('conforme','non_conforme')) DEFAULT 'conforme',
  user_creation TEXT, commentaire TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_pnd_nettoyages_date ON cuisine_pnd_nettoyages(date_nettoyage);
CREATE INDEX IF NOT EXISTS idx_cuisine_pnd_nettoyages_surface ON cuisine_pnd_nettoyages(surface_id);

CREATE TABLE IF NOT EXISTS cuisine_hygiene_thermometres (
  id INTEGER PRIMARY KEY AUTOINCREMENT, numero INTEGER UNIQUE NOT NULL,
  libelle TEXT, actif INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cuisine_hygiene_etalonnages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thermometre_id INTEGER NOT NULL REFERENCES cuisine_hygiene_thermometres(id),
  date_test TEXT DEFAULT (datetime('now','utc')),
  test_glace_valeur REAL, test_glace_resultat TEXT CHECK (test_glace_resultat IN ('conforme','non_conforme')),
  test_ebullition_valeur REAL, test_ebullition_resultat TEXT CHECK (test_ebullition_resultat IN ('conforme','non_conforme')),
  user_creation TEXT, commentaire TEXT
);
CREATE INDEX IF NOT EXISTS idx_cuisine_hyg_etalon_date ON cuisine_hygiene_etalonnages(date_test);

CREATE TRIGGER IF NOT EXISTS trg_cuisine_receptions_datemodif
AFTER UPDATE ON cuisine_receptions
FOR EACH ROW
BEGIN
  UPDATE cuisine_receptions SET date_modif = datetime('now','utc') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_cuisine_productions_datemodif
AFTER UPDATE ON cuisine_productions
FOR EACH ROW
BEGIN
  UPDATE cuisine_productions SET date_modif = datetime('now','utc') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_cuisine_production_etapes_datemodif
AFTER UPDATE ON cuisine_production_etapes
FOR EACH ROW
BEGIN
  UPDATE cuisine_production_etapes SET date_modif = datetime('now','utc') WHERE id = NEW.id;
END;
"""

# D'après le fichier Excel "Suivi production jour" (feuille "Suivi des
# données") : chaque étape à double colonne Début/Fin (fusion d'en-tête
# horizontale) demande une température aux DEUX bouts ; les étapes à colonne
# unique (Fin de cuisson, Refroidissement à l'eau) n'ont qu'un seul relevé,
# à la fin.
#
# code, libelle, ordre, saisie_debut, saisie_fin, saisie_temperature (fin),
# saisie_conformite, saisie_cellule, optionnelle, saisie_temperature_debut
ETAPES_REF = [
    ("tranchage_froid", "Tranchage froid", 1, 1, 1, 1, 1, 0, 0, 1),
    ("cuisson", "Cuisson", 2, 1, 1, 1, 1, 0, 0, 0),
    ("tranchage_chaud", "Tranchage chaud", 3, 1, 1, 1, 1, 0, 0, 1),
    ("refroidissement_eau", "Refroidissement à l'eau", 4, 0, 1, 1, 0, 0, 0, 0),
    ("conditionnement", "Conditionnement", 5, 1, 1, 1, 0, 0, 0, 1),
    ("refroidissement_cellule", "Mise en cellule", 6, 1, 1, 1, 0, 1, 0, 1),
    ("decongelation", "Décongélation", 0, 1, 1, 0, 0, 0, 1, 1),
]

# (taille, code_article, libelle, nb_portions)
ARTICLES_BARQUETTES = [
    ("1/2", "1/2", "1/2 10P", 10),
    ("1/4", "1/4", "1/4 4P", 4),
    ("1/8", "1/8", "1/8 1P", 1),
]

ZONES_TEMPERATURE = [
    "CF RECEPTION N°4",
    "CF LEGUMES N°7",
    "CF BOF N°12",
    "CF VIANDES N°8",
    "CF POSITIVE JOUR N°13",
    "CF STOCKAGE PRODUITS FINIS N°22",
    "CF EXPEDITION N°25",
    "CT DECOUPE N°10",
    "CT PREPARATION FROIDE N°11",
    "CHAMBRE NEGATIVE LEGUMES",
    "CHAMBRE NEGATIVE VIANDES",
    "CF NEGATIVE",
    "CF POSITIVE",
]

ZONES_NETTOYAGE = [
    "CF BOF N°12",
    "CF DU JOUR N°13",
    "CF LEGUMES N°7",
    "CF RECEPTION N°4",
    "CF VIANDES N°8",
    "CHAMBRE FROIDE STOCKAGE PF ET ALLOTEMENT N°22",
    "CN LEGUMES N°15",
    "CN LEGUMES N°16",
    "COULOIR DES CHAMBRES FROIDES N°27",
    "COULOIR DES CHAMBRES FROIDES N°29",
    "COULOIR DU PERSONNEL N°28",
    "CUISINE",
    "DECOUPE N°10",
    "LOCAL A DECHETS N°30",
    "LEGUMERIE N°9",
    "PLONGE – BATTERIE N°18",
    "PREPARATION FROIDE N°11",
    "QUAI RECEPTION EMBALLAGE N°38",
    "QUAI RECEPTION EXPEDITION N°40",
    "QUAI RECEPTION PM N°37",
    "RESERVE SECHE N°32",
    "SALLE",
    "SALLE DU PERSONNEL N°31",
    "STOCKAGE EMBALLAGE N°31",
    "VESTIAIRES N°1 ET 2",
    "ZONE DE CONDITIONNEMENT N°22",
    "ZONE DE CUISSON N°17",
]

NB_THERMOMETRES = 15

# appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible
APPLICATIONS = [
    ("production_cuisine", "Production Cuisine", "production_cuisine.liste_productions", "Cuisine", 1, "🍳", 7, 1),
    ("cuisine_hygiene", "Hygiène Cuisine", "cuisine_hygiene.temperatures", "Cuisine", 2, "🧼", 7, 1),
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


def slugify(libelle: str) -> str:
    import re
    import unicodedata
    txt = unicodedata.normalize("NFKD", libelle).encode("ascii", "ignore").decode("ascii")
    txt = re.sub(r"[^a-zA-Z0-9]+", "_", txt).strip("_").lower()
    return txt


def seed(conn):
    cur = conn.cursor()

    # --- étapes de fabrication ---
    # UPSERT (pas juste INSERT OR IGNORE) : ce référentiel est du paramétrage
    # applicatif, pas de la donnée utilisateur — une correction de règle
    # métier (ex. cuisson avec/sans heure de début) doit s'appliquer même
    # après un premier passage du script.
    cur.executemany(
        """INSERT INTO cuisine_etapes_ref
           (code, libelle, ordre, saisie_debut, saisie_fin, saisie_temperature,
            saisie_conformite, saisie_cellule, optionnelle, saisie_temperature_debut)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(code) DO UPDATE SET
             libelle=excluded.libelle, ordre=excluded.ordre,
             saisie_debut=excluded.saisie_debut, saisie_fin=excluded.saisie_fin,
             saisie_temperature=excluded.saisie_temperature,
             saisie_conformite=excluded.saisie_conformite,
             saisie_cellule=excluded.saisie_cellule, optionnelle=excluded.optionnelle,
             saisie_temperature_debut=excluded.saisie_temperature_debut""",
        ETAPES_REF,
    )
    print(f"✓ cuisine_etapes_ref : {len(ETAPES_REF)} ligne(s) synchronisées (upsert)")

    # --- articles barquettes (module Stock & Ventes barquettes) ---
    cur.executemany(
        """INSERT INTO cuisine_articles_barquettes (taille, code_article, libelle, nb_portions)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(taille) DO UPDATE SET
             libelle=excluded.libelle, nb_portions=excluded.nb_portions""",
        ARTICLES_BARQUETTES,
    )
    print(f"✓ cuisine_articles_barquettes : {len(ARTICLES_BARQUETTES)} ligne(s) synchronisées (upsert)")

    # --- zones de relevé température ---
    rows = [(slugify(lib), lib, i + 1) for i, lib in enumerate(ZONES_TEMPERATURE)]
    cur.executemany(
        "INSERT OR IGNORE INTO cuisine_hygiene_zones_temperature (code, libelle, ordre) VALUES (?, ?, ?)",
        rows,
    )
    print(f"✓ cuisine_hygiene_zones_temperature : {len(rows)} ligne(s) seedées (INSERT OR IGNORE)")

    # --- zones de nettoyage ---
    rows = [(slugify(lib), lib, i + 1) for i, lib in enumerate(ZONES_NETTOYAGE)]
    cur.executemany(
        "INSERT OR IGNORE INTO cuisine_hygiene_zones_nettoyage (code, libelle, ordre) VALUES (?, ?, ?)",
        rows,
    )
    print(f"✓ cuisine_hygiene_zones_nettoyage : {len(rows)} ligne(s) seedées (INSERT OR IGNORE)")

    # --- thermomètres ---
    rows = [(n, f"Thermomètre N°{n}") for n in range(1, NB_THERMOMETRES + 1)]
    cur.executemany(
        "INSERT OR IGNORE INTO cuisine_hygiene_thermometres (numero, libelle) VALUES (?, ?)",
        rows,
    )
    print(f"✓ cuisine_hygiene_thermometres : {len(rows)} ligne(s) seedées (INSERT OR IGNORE)")

    # --- menu (table applications) ---
    for appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible in APPLICATIONS:
        cur.execute(
            """INSERT OR IGNORE INTO applications
               (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible),
        )
    print(f"✓ applications : {len(APPLICATIONS)} entrée(s) menu seedées (INSERT OR IGNORE)")
    print(
        "ℹ️  Aucun droit `roles_utilisateurs` n'a été créé : l'administrateur doit "
        "attribuer manuellement les droits 'production_cuisine' / 'cuisine_hygiene' "
        "aux comptes concernés (postes tablette + responsables cuisine) via "
        "l'interface d'administration existante."
    )


# Paramètres cuisine : réutilise la table générique `parametres`
# (param_name/param_value/categorie) déjà utilisée ailleurs dans l'appli
# (ex. ba38_fournisseurs/routes.py::create_fournisseur), plutôt qu'une
# table dédiée. Seedé une fois avec les valeurs déjà présentes dans
# cuisine_recettes_referentiel au moment de l'écriture de ce script ;
# ensuite modifiable via /production-cuisine/parametres.
PARAMETRES_CUISINE = {
    "cuisine_type_cuisson": ["Four", "Sauteuse", "Courte", "Cuisson de nuit"],
    "cuisine_famille": [
        "Féculents", "Gibier", "Légumes", "Légumineuses", "Oeufs", "Poisson", "Viande",
    ],
    "cuisine_sous_famille_1": [
        "01-Agneau", "02-Boeuf", "03-Porc", "04-Veau", "05-Volaille",
        "06-Œufs/gibier", "07-Poisson", "08-Féculents", "09-Légumes/légumineuses",
    ],
    "cuisine_sous_famille_2": [
        "Abats", "Agneau", "Aubergine", "Autruche", "Blanquette", "Cabillaud", "Caille",
        "Calamar", "Canard", "Carottes", "Cerf", "Chevreuil", "Chou fleur", "Chou vert",
        "Choucroute", "Coco", "Coq/coquelet", "Courge", "Courgettes", "Couscous",
        "Crevettes", "Céleri", "Côtes", "Dinde", "Dorade", "Emisole", "Espadon", "Faisan",
        "Filet mignon", "Haricots rouge", "Haricots verts", "Lapin", "Lentilles",
        "Lieu noir", "Lièvre", "Légumes", "Merlan", "Mijoté", "Mixte", "Omelette",
        "Patate douce", "Paupiette", "Petits pois", "Pigeon", "Pintade", "Pièce grillée",
        "Polenta", "Pomme de terre", "Poule", "Poulet", "Pâtes", "Raie", "Requin", "Riz",
        "Roussette", "Rôti", "Sanglier", "Saucisse", "Saucisson cuit", "Saumon", "Sauté",
        "Semoule", "Thon", "Tortillas", "Travers", "Truite", "Tête", "Viande haché",
        "Volaille", "Volaille de fête", "Végétarien", "Œufs",
    ],
    # Valeurs déjà présentes dans le PND C3ES.xlsx (13/5 valeurs distinctes,
    # fautes de frappe/espaces du fichier source normalisés).
    "cuisine_frequence_nettoyage": [
        "chaque jour", "chaque jour ou après utilisation", "après chaque utilisation",
        "chaque semaine", "1 semaine sur 2", "mardi/jeudi", "mercredi matin",
        "avant chaque vacances", "1 fois par mois", "1 fois par semestre",
        "2 fois par an", "En fin de journée", "Après utilisation",
    ],
    "cuisine_produit_nettoyage": [
        "ASTRASURF 1% auto", "Détergent graisses cuites", "FAR 4en1", "Manulav", "aucun",
    ],
}


def seed_parametres_cuisine(conn):
    cur = conn.cursor()
    total = 0
    for param_name, valeurs in PARAMETRES_CUISINE.items():
        for valeur in valeurs:
            existe = cur.execute(
                "SELECT 1 FROM parametres WHERE param_name = ? AND param_value = ?",
                (param_name, valeur),
            ).fetchone()
            if not existe:
                cur.execute(
                    "INSERT INTO parametres (param_name, param_value, categorie) VALUES (?, ?, 'liste')",
                    (param_name, valeur),
                )
                total += 1
    print(f"✓ parametres cuisine : {total} valeur(s) ajoutée(s) (INSERT si absent)")


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

    print(f"➡ Migration module Production Cuisine [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)
        add_missing_columns(conn, "cuisine_receptions", [
            ("production_id", "INTEGER REFERENCES cuisine_productions(id)"),
            ("libelle_produit", "TEXT"),
            ("ingredient_groupe", "TEXT"),
            ("ingredient_produit", "TEXT"),
        ])
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cuisine_receptions_production ON cuisine_receptions(production_id)")
        add_missing_columns(conn, "cuisine_etapes_ref", [
            ("saisie_temperature_debut", "INTEGER DEFAULT 0"),
        ])
        add_missing_columns(conn, "cuisine_production_etapes", [
            ("temperature_debut", "REAL"),
            ("non_applicable", "INTEGER DEFAULT 0"),
        ])
        add_missing_columns(conn, "cuisine_productions", [
            ("recette_referentiel_id", "INTEGER REFERENCES cuisine_recettes_referentiel(id)"),
            ("categorie_produit", "TEXT CHECK (categorie_produit IN ('carne','legumes'))"),
        ])
        add_missing_columns(conn, "cuisine_stock_barquettes", [
            ("categorie_produit", "TEXT"),
        ])
        seed(conn)
        seed_parametres_cuisine(conn)
        conn.commit()

    print("✓ Migration terminée.")

    with closing(sqlite3.connect(db_path)) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cuisine_%' ORDER BY name"
            ).fetchall()
        ]
        print(f"📋 Tables cuisine_* présentes ({len(tables)}) : {', '.join(tables)}")


if __name__ == "__main__":
    main()
