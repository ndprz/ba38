#!/usr/bin/env python3
"""
Migration du schéma et des données de DEV vers PROD (SQLite).

Règles :
- Création des tables manquantes en PROD
- Ajout uniquement des colonnes absentes
- Copie des données :
    * intégrale si table absente
    * colonne par colonne si table existante
- Aucune suppression

⚠️ EXCLUDE_TABLES : tables dont le contenu est maintenu indépendamment en
DEV et en PROD (écran d'admin propre à chaque environnement, ex. un
référentiel corrigé sur le terrain côté PROD). Pour ces tables-là, le
schéma est quand même créé s'il manque côté PROD (sinon l'appli plante),
mais on ne copie JAMAIS les données depuis DEV — ni la copie intégrale à
la création, ni le backfill d'une nouvelle colonne par id. Ce backfill par
id serait dangereux ici : une fois la table éditée séparément dans les
deux bases, les mêmes id ne désignent plus la même ligne des deux côtés
(cf. incident cuisine_ingredients_carnes, 2026-09-21 — la copie intégrale
initiale était sans risque tant que PROD n'avait encore rien édité, mais
laisser le mécanisme actif expose à un backfill corrompu au premier ajout
de colonne futur).

Deuxième cas à exclure, découvert le 2026-09-22 : une table TOUTE NEUVE
peut aussi être dangereuse à copier intégralement si elle référence par id
une autre table qui, elle, diverge déjà entre DEV et PROD (ex.
cuisine_stock_barquettes.production_id → cuisine_productions.id). La copie
intégrale "table absente côté PROD" a bien créé la table mais avec des
production_id qui ne désignaient PAS les mêmes productions réelles côté
PROD (coïncidence sur les tout premiers id, divergence sur les suivants) —
corrompu silencieusement jusqu'à vérification manuelle après coup. Toute
nouvelle table qui référence une table déjà en usage indépendant des deux
côtés doit être exclue dès sa création, pas seulement une fois éditée.
"""

import sqlite3
from pathlib import Path
from dotenv import dotenv_values

EXCLUDE_TABLES = {
    "cuisine_ingredients_carnes",
    "cuisine_stock_barquettes",
}

# -------------------------------------------------------------------
# Chargement EXPLICITE des .env
# -------------------------------------------------------------------
DEV_ENV = Path("/srv/ba38/dev/.env")
PROD_ENV = Path("/srv/ba38/prod/.env")

if not DEV_ENV.exists():
    raise RuntimeError("❌ .env DEV introuvable")

if not PROD_ENV.exists():
    raise RuntimeError("❌ .env PROD introuvable")

dev_cfg = dotenv_values(DEV_ENV)
prod_cfg = dotenv_values(PROD_ENV)

DEV_DB = Path("/srv/ba38/dev") / dev_cfg["SQLITE_DB"]
PROD_DB = Path("/srv/ba38/prod") / prod_cfg["SQLITE_DB"]

# Sécurité absolue
if "dev" in str(PROD_DB).lower():
    raise RuntimeError(f"⛔ ERREUR GRAVE : PROD_DB invalide → {PROD_DB}")

# -------------------------------------------------------------------
# Fonctions utilitaires
# -------------------------------------------------------------------
def get_table_names(conn):
    cur = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
    """)
    return [r[0] for r in cur.fetchall()]

def get_columns(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {r[1]: r[2] for r in cur.fetchall()}

def table_is_empty(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

def copy_table(dev_conn, prod_conn, table):
    rows = dev_conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        return
    placeholders = ",".join("?" * len(rows[0]))
    prod_conn.executemany(
        f"INSERT INTO {table} VALUES ({placeholders})",
        rows
    )

# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------
def main():
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("🚀 Migration schéma & données DEV → PROD")
    print(f"📦 DEV  : {DEV_DB}")
    print(f"📦 PROD : {PROD_DB}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    with sqlite3.connect(DEV_DB) as dev, sqlite3.connect(PROD_DB) as prod:
        dev.row_factory = sqlite3.Row
        prod.row_factory = sqlite3.Row

        prod.execute(f"ATTACH DATABASE '{DEV_DB}' AS dev")

        dev_tables = get_table_names(dev)
        prod_tables = get_table_names(prod)

        for table in dev_tables:
            exclue = table in EXCLUDE_TABLES
            if table not in prod_tables:
                print(f"🆕 Création table {table}")
                schema = dev.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?",
                    (table,)
                ).fetchone()[0]
                prod.execute(schema)
                if exclue:
                    print(f"⏭️  {table} : contenu géré séparément en PROD — schéma créé, données non copiées")
                else:
                    copy_table(dev, prod, table)
            elif exclue:
                print(f"⏭️  {table} : contenu géré séparément en PROD — ignorée (ni colonne, ni donnée)")
            else:
                dev_cols = get_columns(dev, table)
                prod_cols = get_columns(prod, table)
                for col, typ in dev_cols.items():
                    if col not in prod_cols:
                        prod.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
                        )
                        if not table_is_empty(prod, table):
                            prod.execute(f"""
                                UPDATE {table}
                                SET {col} = (
                                    SELECT dev.{col}
                                    FROM dev.{table} AS dev
                                    WHERE dev.id = {table}.id
                                )
                            """)

        prod.commit()

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("✅ Migration terminée avec succès")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

if __name__ == "__main__":
    main()
