#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration one-off : extraction des champs "Annexe 1 bis" de la table `associations`
vers une nouvelle table historisée `annexe1bis`.

Étapes :
1. Création de la table `annexe1bis` (id, partenaire_id, workflow de signature,
   + les 84 champs métier de l'annexe).
2. Backfill : pour chaque association ayant au moins une valeur renseignée parmi
   ces 84 champs, création d'une ligne `annexe1bis` de baseline (considérée comme
   déjà "signée" puisqu'elle correspond à des données antérieures à ce chantier).
3. Retag des 84 lignes `field_groups` (appli='associations' -> 'annexe1bis').
4. Suppression des 84 colonnes de `associations` (ALTER TABLE ... DROP COLUMN,
   supporté nativement depuis SQLite 3.35).

Utilisable sur DEV ou PROD via l'argument --env (défaut : dev). Le backfill part
toujours des données de la base ciblée elle-même (jamais une copie depuis l'autre
environnement) : IMPORTANT de lancer ce script sur PROD *avant* le déploiement
habituel (deploy_to_prod.sh / migrate_schema_and_data_dev_to_prod.py), sans quoi
ce dernier traiterait `annexe1bis` comme une table manquante et copierait telles
quelles les lignes DEV (mauvais partenaire_id) dans PROD.

Idempotent : peut être rejoué sans casse (CREATE TABLE IF NOT EXISTS, backfill
uniquement si la table associations a encore les colonnes, drop uniquement si
la colonne existe encore).
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
# Les 84 champs "Annexe 1 bis" (extraits de field_groups, group_name='annexe 1 bis')
# ---------------------------------------------------------------------
ANNEXE1BIS_FIELDS = [
    'secteur_geographique', 'logiciel_Ticadi_utilise', 'combien_de_benevoles',
    'combien_de_salaries', 'statut', 'statut_autre', 'presence_travailleur_social',
    'appartient_grand_reseau_habilitation_nationale', 'reseau_national',
    'habilitation_regionale', 'date_agrement_regional', 'date_FIN_habilitation',
    'habilitation_regionale_encours', 'habilitation_regionale_en_cours_prochaine_session',
    'categorie', 'mode_distrib_colis', 'mode_distrib_maraude', 'mode_distrib_repas',
    'mode_distrib_petit_dejeuner', 'particularite_hebergement_longue_duree',
    'particularite_hebergement_urgence', 'particularite_dispositif_itinerant',
    'particularite_livraison_domicile', 'activite_principale_aide_alimentaire',
    'public_accueilli_enfants_bas_age', 'public_accueilli_mineurs_isoles',
    'public_accueilli_jeunes_travailleurs_etudiants',
    'public_accueilli_femmes_victimes_violence', 'produits_souhaites',
    'produits_souhaites_commentaire', 'autres_approvisionnements',
    'partenaire_souhaite_FSE', 'partenaire_souhaite_convention_delegation_retrait',
    'partenaire_souhaite_convention_PROXIDON', 'distribution_toute_annee',
    'alternative_fermeture', 'livraison_par_bai', 'piece_accueil',
    'piece_accueil_nbre', 'piece_accueil_volume_surface', 'cuisine', 'cuisine_nbre',
    'cuisine_volume_surface', 'local_de_distribution', 'local_de_distribution_nbre',
    'local_de_distribution_volume_surface', 'local_entreposage',
    'local_entreposage_nbre', 'local_entreposage_volume_surface',
    'chambre_froide_positive', 'chambre_froide_positive_nbre',
    'chambre_froide_positive_volume_surface', 'chambre_froide_negative',
    'chambre_froide_negative_nbre', 'chambre_froide_negative_volume_surface',
    'congelateur', 'congelateur_nbre', 'congelateur_volume_surface',
    'refrigerateur', 'refrigerateur_nbre', 'refrigerateur_volume_surface',
    'container_isotherme_agree', 'container_isotherme_agree_nbre',
    'container_isotherme_agree_volume_surface', 'glaciere', 'glaciere_nbre',
    'glaciere_volume_surface', 'plaques_eutectiques', 'plaques_eutectiques_nbre',
    'plaques_eutectiques_volume_surface', 'vehicule_frigorifique',
    'vehicule_frigorifique_nbre', 'vehicule_frigorifique_volume_surface',
    'vehicule_isotherme', 'vehicule_isotherme_nbre',
    'vehicule_isotherme_volume_surface', 'autre_vehicule', 'autre_vehicule_nbre',
    'autre_vehicule_volume_surface', 'Logiciel_autre', 'Logiciel_autre_lequel',
    'nbre_beneficiaires_annuel_previsionnel',
    'nbre_beneficiaires_trimestriels_previsionnel', 'nbre_foyers',
]

assert len(ANNEXE1BIS_FIELDS) == len(set(ANNEXE1BIS_FIELDS)) == 84


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_annexe1bis_table(conn):
    cols_sql = ",\n".join(f"    `{f}` TEXT" for f in ANNEXE1BIS_FIELDS)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS annexe1bis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partenaire_id INTEGER NOT NULL,
            date_creation TEXT NOT NULL,
            user_creation TEXT,
            statut_signature TEXT NOT NULL DEFAULT 'brouillon',
            date_envoi_signature TEXT,
            date_signature TEXT,
{cols_sql}
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_annexe1bis_partenaire "
        "ON annexe1bis(partenaire_id)"
    )
    print("✓ Table 'annexe1bis' prête.")


def backfill_from_associations(conn):
    assoc_cols = table_columns(conn, "associations")
    fields_present = [f for f in ANNEXE1BIS_FIELDS if f in assoc_cols]
    if not fields_present:
        print("✓ Aucune colonne annexe1bis restante dans 'associations' — backfill déjà fait.")
        return

    select_cols = ", ".join(f"`{f}`" for f in fields_present)
    rows = conn.execute(
        f"SELECT Id, date_modif, `{'`, `'.join(fields_present)}` "
        f"FROM associations"
    ).fetchall()

    n_inserted = 0
    for row in rows:
        row = dict(row)
        partner_id = row["Id"]
        values = {f: row.get(f) for f in fields_present}
        if not any((v or "").strip() for v in values.values() if v is not None):
            continue

        already = conn.execute(
            "SELECT 1 FROM annexe1bis WHERE partenaire_id = ? LIMIT 1", (partner_id,)
        ).fetchone()
        if already:
            continue

        date_creation = (row.get("date_modif") or datetime.now().strftime("%Y-%m-%d"))

        cols = ["partenaire_id", "date_creation", "statut_signature", "date_signature"] + fields_present
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f"`{c}`" for c in cols)
        params = [partner_id, date_creation, "signee", date_creation] + [values[f] for f in fields_present]

        conn.execute(
            f"INSERT INTO annexe1bis ({col_list}) VALUES ({placeholders})",
            params,
        )
        n_inserted += 1

    print(f"✓ Backfill : {n_inserted} annexe(s) baseline créée(s) à partir de 'associations'.")


def retag_field_groups(conn):
    cur = conn.execute(
        "UPDATE field_groups SET appli = 'annexe1bis' "
        "WHERE appli = 'associations' AND LOWER(group_name) = 'annexe 1 bis'"
    )
    print(f"✓ field_groups : {cur.rowcount} ligne(s) retaguée(s) vers appli='annexe1bis'.")


def drop_columns_from_associations(conn):
    assoc_cols = table_columns(conn, "associations")
    to_drop = [f for f in ANNEXE1BIS_FIELDS if f in assoc_cols]
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

    print(f"➡ Migration annexe1bis [{args.env}] sur : {db_path}")

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        ensure_annexe1bis_table(conn)
        conn.commit()

        conn.execute("BEGIN")
        try:
            backfill_from_associations(conn)
            retag_field_groups(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # DROP COLUMN implique un commit implicite par colonne en SQLite ;
        # on l'exécute après le reste pour garder les étapes précédentes atomiques.
        drop_columns_from_associations(conn)
        conn.commit()

    print("✅ Migration 'annexe1bis' terminée avec succès.")


if __name__ == "__main__":
    main()
