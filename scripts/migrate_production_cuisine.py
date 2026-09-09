#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : module Cuisine — Étape 1 : traçabilité production + hygiène site.

⚠️ Nommage : un module `ba38_planning/cuisine.py` (blueprint
`planning_cuisine_bp`, templates `templates/planning/cuisine/`) existe déjà
et gère le planning des équipes (horaires bénévoles) — sans rapport avec ce
module. Ce script crée un ensemble de tables totalement séparé, préfixé
`cuisine_*`, pour le nouveau module `ba38_production_cuisine/`
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

CREATE TABLE IF NOT EXISTS cuisine_productions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date_production TEXT NOT NULL,
  recette_id INTEGER REFERENCES cuisine_recettes(id),
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
    ("decongelation", "Décongélation", 7, 1, 1, 0, 0, 0, 1, 0),
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
        ])
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cuisine_receptions_production ON cuisine_receptions(production_id)")
        add_missing_columns(conn, "cuisine_etapes_ref", [
            ("saisie_temperature_debut", "INTEGER DEFAULT 0"),
        ])
        add_missing_columns(conn, "cuisine_production_etapes", [
            ("temperature_debut", "REAL"),
            ("non_applicable", "INTEGER DEFAULT 0"),
        ])
        seed(conn)
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
