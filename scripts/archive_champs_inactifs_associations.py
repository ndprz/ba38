#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : archivage des champs de `associations` hérités de l'ancienne
base "office", identifiés comme n'ayant plus aucune entrée dans `field_groups`
et aucune référence dans le code applicatif (cf. audit du 2026-07-10).

Étapes :
1. Création de la table `associations_champs_inactifs` (id, association_id,
   date_archivage, + les 33 champs concernés).
2. Backfill : pour chaque association ayant au moins une valeur non vide parmi
   ces 33 champs, une ligne d'archive est créée avec l'id de l'association
   d'origine.
3. Suppression des 33 colonnes de `associations` (ALTER TABLE ... DROP COLUMN).

Usable sur DEV ou PROD via --env (défaut : dev). Toujours backfiller depuis les
données de la base ciblée elle-même.

Idempotent : peut être rejoué sans casse (CREATE TABLE IF NOT EXISTS, backfill
sauté si une archive existe déjà pour l'association, drop sauté si la colonne
n'existe plus).
"""

import argparse
import os
import sys
import sqlite3
from contextlib import closing
from datetime import datetime

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

# ---------------------------------------------------------------------
# Les 33 champs identifiés comme inutilisés (aucune entrée field_groups,
# aucune référence dans le code applicatif hors scripts de seed de test)
# ---------------------------------------------------------------------
CHAMPS_INACTIFS = [
    'Modification_par', 'date_AG', 'denrees_destines_a', 'autres_quels',
    'origine_des_denrees_distribuees', 'lesquels', 'produits_distribues',
    'sacs_isothermes', 'combien_de_benevoles_beneficiaires', 'colis',
    'Remarques_sur_les_denrees_BAI', 'les_locaux_appartiennent_a',
    'etat_local_de_distribution', 'etat_local_de_stockage', 'etat_chambre_froide',
    'nbre_refrigerateur', 'etat_refrigerateur', 'nbre_congelateurs',
    'etat_congelateur', 'vehicule_appartient_a',
    'vehicule_adapte_transport_denrees_alimentaires', 'nettoyage_regulier_vehicule',
    'tel_chauffeur', 'distance_aller_en_km',
    'temps_moyen_de_transport_des_marchandises_en_MN',
    'transport_des_produits_frais_dans', 'administratif', 'formation',
    'sante__prevention', 'cuisine_et_nutrition', 'divertissements', 'divers',
    'autre_action',
]

assert len(CHAMPS_INACTIFS) == len(set(CHAMPS_INACTIFS)) == 33


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_archive_table(conn):
    cols_sql = ",\n".join(f"    `{f}` TEXT" for f in CHAMPS_INACTIFS)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS associations_champs_inactifs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            association_id INTEGER NOT NULL,
            date_archivage TEXT NOT NULL,
{cols_sql}
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_champs_inactifs_association "
        "ON associations_champs_inactifs(association_id)"
    )
    print("✓ Table 'associations_champs_inactifs' prête.")


def backfill_from_associations(conn):
    assoc_cols = table_columns(conn, "associations")
    fields_present = [f for f in CHAMPS_INACTIFS if f in assoc_cols]
    if not fields_present:
        print("✓ Aucune colonne inactive restante dans 'associations' — backfill déjà fait.")
        return

    rows = conn.execute(
        f"SELECT Id, `{'`, `'.join(fields_present)}` FROM associations"
    ).fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    n_inserted = 0
    for row in rows:
        row = dict(row)
        association_id = row["Id"]
        values = {f: row.get(f) for f in fields_present}
        if not any((v or "").strip() for v in values.values() if v is not None):
            continue

        already = conn.execute(
            "SELECT 1 FROM associations_champs_inactifs WHERE association_id = ? LIMIT 1",
            (association_id,)
        ).fetchone()
        if already:
            continue

        cols = ["association_id", "date_archivage"] + fields_present
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f"`{c}`" for c in cols)
        params = [association_id, today] + [values[f] for f in fields_present]

        conn.execute(
            f"INSERT INTO associations_champs_inactifs ({col_list}) VALUES ({placeholders})",
            params,
        )
        n_inserted += 1

    print(f"✓ Archivage : {n_inserted} association(s) avec au moins une valeur archivée.")


def drop_columns_from_associations(conn):
    assoc_cols = table_columns(conn, "associations")
    to_drop = [f for f in CHAMPS_INACTIFS if f in assoc_cols]
    if not to_drop:
        print("✓ Aucune colonne à supprimer dans 'associations' (déjà fait).")
        return
    for col in to_drop:
        conn.execute(f"ALTER TABLE associations DROP COLUMN `{col}`")
    print(f"✓ {len(to_drop)} colonne(s) supprimée(s) de 'associations'.")


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

    print(f"➡ Archivage champs inactifs [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        ensure_archive_table(conn)
        conn.commit()

        conn.execute("BEGIN")
        try:
            backfill_from_associations(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # DROP COLUMN implique un commit implicite par colonne en SQLite ;
        # exécuté après le backfill pour garder ce dernier atomique.
        drop_columns_from_associations(conn)
        conn.commit()

    print("✅ Archivage terminé avec succès.")


if __name__ == "__main__":
    main()
