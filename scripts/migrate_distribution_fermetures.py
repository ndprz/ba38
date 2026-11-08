#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : planning du jour de distribution — fermetures de la BA38.

Crée (idempotent) :
- ba_fermetures          : jours de fermeture de la BA38 (fériés, ponts…)
- planning_deplacements  : choix individuel quand le jour de passage d'une
                           association tombe un jour de fermeture
                           (sens = 'suivant' (défaut, absent de la table) | 'precedent')

Étape 2 (planification par couple association/menu) :
- associations.planification ('oui' défaut / 'non') : exclut l'association du planning
  (+ ligne field_groups, groupe Planification) ;
- associations_menus     : une ligne = un couple association/UN menu avec SA planification
                           (jours, heure, emplacement, fréquence, bénéficiaires, besoins).
                           Amorcée une seule fois depuis les champs « Planification » de
                           associations (qui restent en place, pour information) ;
- associations_fermetures: périodes de fermeture d'une association (date_fin vide = sans limite) ;
- planning_deplacements  : reclé sur la ligne de menu (ligne_id) au lieu de l'association.

Chemins explicites (pas de load_dotenv, cf. piège dev/.env).
Usage : migrate_distribution_fermetures.py dev [dev_test] [prod] [prod_test]
"""
import sqlite3
import sys
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

SQL = """
CREATE TABLE IF NOT EXISTS ba_fermetures (
    date  TEXT PRIMARY KEY,          -- AAAA-MM-JJ
    motif TEXT
);
"""

SQL2 = """
CREATE TABLE IF NOT EXISTS associations_menus (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    association_id         INTEGER NOT NULL,
    menu                   TEXT,      -- code du menu (UN seul menu par ligne)
    type_menu              TEXT CHECK (type_menu IN ('sec', 'frais')),
    jours                  TEXT,      -- « Lundi, Mercredi » (même format que la fiche)
    heure                  TEXT,      -- « 13:30 », « Matin », « Après-midi »
    emplacement            TEXT,      -- P1 / P6 / Livraison / À la demande
    frequence              TEXT,
    nbre_beneficiaires     INTEGER,
    besoins_particuliers   TEXT,
    actif                  INTEGER NOT NULL DEFAULT 1,
    cree_le                TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_assoc_menus_assoc ON associations_menus(association_id);

CREATE TABLE IF NOT EXISTS associations_fermetures (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    association_id INTEGER NOT NULL,
    date_debut     TEXT NOT NULL,
    date_fin       TEXT,              -- NULL = jusqu'à nouvel ordre
    motif          TEXT
);
CREATE INDEX IF NOT EXISTS idx_assoc_ferm_assoc ON associations_fermetures(association_id);

CREATE TABLE IF NOT EXISTS planning_deplacements (
    ligne_id     INTEGER NOT NULL,    -- associations_menus.id
    date_origine TEXT NOT NULL,       -- jour de passage normal (fermé)
    sens         TEXT NOT NULL CHECK (sens IN ('suivant', 'precedent')),
    PRIMARY KEY (ligne_id, date_origine)
);
"""


def _etape2(conn):
    # ancienne version de planning_deplacements (clé association_id) : vide en pratique
    cols = {r[1] for r in conn.execute("PRAGMA table_info(planning_deplacements)")}
    if cols and "ligne_id" not in cols:
        if conn.execute("SELECT COUNT(*) FROM planning_deplacements").fetchone()[0]:
            raise SystemExit("planning_deplacements (ancienne version) contient des lignes : à traiter à la main")
        conn.execute("DROP TABLE planning_deplacements")
    conn.executescript(SQL2)

    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(associations)")}
    if "planification" not in colonnes:
        conn.execute("ALTER TABLE associations ADD COLUMN planification TEXT DEFAULT 'oui'")
        conn.execute("UPDATE associations SET planification = 'oui' WHERE planification IS NULL")
        print("   + colonne associations.planification (défaut 'oui')")
    if not conn.execute("SELECT 1 FROM field_groups WHERE appli='associations' AND field_name='planification'").fetchone():
        conn.execute("""INSERT INTO field_groups (field_name, group_name, display_order, is_required, type_champ, appli)
                        VALUES ('planification', 'Planification', 5, 0, 'oui_non', 'associations')""")
        print("   + field_groups : planification")

    # --- ancienne structure (menu_sec + menu_frais sur la même ligne) -> un menu par ligne
    cols = {r[1] for r in conn.execute("PRAGMA table_info(associations_menus)")}
    if "menu_sec" in cols:
        for c in ("menu", "type_menu"):
            if c not in cols:
                conn.execute(f"ALTER TABLE associations_menus ADD COLUMN {c} TEXT")
        conn.execute("""INSERT INTO associations_menus
              (association_id, menu, type_menu, jours, heure, emplacement, frequence,
               nbre_beneficiaires, besoins_particuliers, actif, cree_le)
            SELECT association_id, menu_frais, 'frais', jours, heure, emplacement, frequence,
                   nbre_beneficiaires, besoins_particuliers, actif, cree_le
            FROM associations_menus
            WHERE COALESCE(menu_frais, '') <> '' AND COALESCE(menu_sec, '') <> ''""")
        n_split = conn.execute("SELECT changes()").fetchone()[0]
        # la ligne d'origine garde le menu sec (ou, à défaut, le menu frais) ;
        # la copie « frais » insérée ci-dessus a déjà menu = frais
        conn.execute("""UPDATE associations_menus SET menu = menu_sec, type_menu = 'sec'
                        WHERE menu IS NULL AND COALESCE(menu_sec, '') <> ''""")
        conn.execute("""UPDATE associations_menus SET menu = menu_frais, type_menu = 'frais'
                        WHERE menu IS NULL AND COALESCE(menu_frais, '') <> ''""")
        conn.execute("ALTER TABLE associations_menus DROP COLUMN menu_sec")
        conn.execute("ALTER TABLE associations_menus DROP COLUMN menu_frais")
        print(f"   + associations_menus : un menu par ligne ({n_split} ligne(s) dédoublée(s) sec/frais)")

    if conn.execute("SELECT COUNT(*) FROM associations_menus").fetchone()[0] == 0:
        base = """FROM associations a
            WHERE {cond}"""
        commun = """association_id, menu, type_menu, jours, heure, emplacement, frequence,
               nbre_beneficiaires, besoins_particuliers"""
        sel = """SELECT id, {menu}, {type}, NULLIF(TRIM(jour_de_passage_a_la_BAI), ''), NULLIF(TRIM(heure_de_passage), ''),
                   NULLIF(TRIM(Emplacement), ''), NULLIF(TRIM(frequence), ''),
                   CAST(NULLIF(TRIM(nbre_beneficiaires_vif), '') AS INTEGER),
                   NULLIF(TRIM(besoins_particuliers), '')
            FROM associations
            WHERE (validite IS NULL OR LOWER(TRIM(validite)) != 'non') AND ({cond})"""
        total = 0
        for menu, type_, cond in (
            ("TRIM(menu_sec)", "'sec'", "COALESCE(TRIM(menu_sec), '') <> ''"),
            ("TRIM(menu_frais)", "'frais'", "COALESCE(TRIM(menu_frais), '') <> ''"),
            ("NULL", "NULL", "COALESCE(TRIM(menu_sec), '') = '' AND COALESCE(TRIM(menu_frais), '') = '' "
                             "AND COALESCE(TRIM(jour_de_passage_a_la_BAI), '') <> ''"),
        ):
            cur = conn.execute(f"INSERT INTO associations_menus ({commun}) " + sel.format(menu=menu, type=type_, cond=cond))
            total += cur.rowcount
        print(f"   + associations_menus amorcée : {total} ligne(s) (un menu par ligne)")


def main():
    cibles = sys.argv[1:] or ["dev"]
    for env in cibles:
        if env not in DB_PATHS:
            raise SystemExit(f"Environnement inconnu : {env}")
        with closing(sqlite3.connect(DB_PATHS[env])) as conn:
            conn.executescript(SQL)
            _etape2(conn)
            conn.commit()
        print(f"✅ [{env}] tables fermetures/déplacements OK")


if __name__ == "__main__":
    main()
