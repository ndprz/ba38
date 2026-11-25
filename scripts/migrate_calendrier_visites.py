#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : « Calendrier des visites » (ba38_calendrier_visites/).
Table visites_prevues + ligne `applications` (groupe Gestion) + droit « lecture »
donné à tous les utilisateurs qui n'ont pas de ligne (la consultation est ouverte à tous).
Idempotent.  Usage : migrate_calendrier_visites.py --env dev|prod|dev_test|prod_test
"""
import argparse
import sqlite3
import sys
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}


def ensure(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visites_prevues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            association_id INTEGER NOT NULL,
            date_visite TEXT NOT NULL,            -- AAAA-MM-JJ
            heure_debut TEXT,                     -- HH:MM (facultatif)
            heure_fin TEXT,
            visiteur TEXT NOT NULL,               -- nom du CAR / de la personne qui visite
            car_association TEXT,                 -- CAR de l'association au moment de la saisie
            statut TEXT NOT NULL DEFAULT 'prevue',  -- prevue | realisee | annulee
            commentaire TEXT,
            cree_par TEXT,                        -- username du créateur
            cree_le TEXT,
            modifie_par TEXT,
            modifie_le TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visites_prevues_date ON visites_prevues(date_visite)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visites_prevues_asso ON visites_prevues(association_id)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(visites_prevues)")}
    if "visiteur_type" not in cols:      # user | car | benevole | texte (saisie libre d'avant le contrôle)
        conn.execute("ALTER TABLE visites_prevues ADD COLUMN visiteur_type TEXT")
    if "visiteur_benevole_id" not in cols:
        conn.execute("ALTER TABLE visites_prevues ADD COLUMN visiteur_benevole_id INTEGER")
    # visites déjà saisies : le visiteur est un CAR connu, sinon « texte »
    conn.execute("""UPDATE visites_prevues SET visiteur_type =
        CASE WHEN visiteur IN (SELECT param_value FROM parametres WHERE param_name='car') THEN 'car' ELSE 'texte' END
        WHERE visiteur_type IS NULL""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visites_prevues_visiteurs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visite_id INTEGER NOT NULL,
            ordre INTEGER NOT NULL DEFAULT 0,      -- 0 = intervenant principal (couleur du calendrier)
            nom TEXT NOT NULL,
            type TEXT NOT NULL,                    -- user | car | benevole | texte
            benevole_id INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vpv_visite ON visites_prevues_visiteurs(visite_id)")
    n = conn.execute("""INSERT INTO visites_prevues_visiteurs (visite_id, ordre, nom, type, benevole_id)
        SELECT id, 0, visiteur, COALESCE(visiteur_type,'texte'), visiteur_benevole_id FROM visites_prevues
        WHERE id NOT IN (SELECT visite_id FROM visites_prevues_visiteurs)""").rowcount
    print(f"✓ Table visites_prevues prête (intervenants repris pour {n} visite(s)).")

    if conn.execute("SELECT 1 FROM applications WHERE appli='calendrier_visites'").fetchone() is None:
        conn.execute(
            "INSERT INTO applications (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible) "
            "VALUES ('calendrier_visites', 'Calendrier des visites', 'calendrier_visites.calendrier', "
            "'Gestion', 3, '📅', 1, 1)")
        print("✓ Ligne applications 'calendrier_visites' ajoutée (groupe Gestion).")
    n = 0
    for (email,) in conn.execute("SELECT email FROM users WHERE LOWER(COALESCE(actif,'1')) IN ('1','oui')").fetchall():
        if conn.execute("SELECT 1 FROM roles_utilisateurs WHERE user_email=? AND appli='calendrier_visites'",
                        (email,)).fetchone() is None:
            conn.execute("INSERT INTO roles_utilisateurs (user_email, appli, droit) VALUES (?, 'calendrier_visites', 'lecture')",
                         (email,))
            n += 1
    print(f"✓ Droit 'lecture' ajouté à {n} utilisateur(s). "
          "(Donner 'ecriture' aux non-CAR autorisés à saisir une visite.)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--env", default="dev", choices=DB_PATHS)
    a = p.parse_args()
    with closing(sqlite3.connect(DB_PATHS[a.env])) as conn:
        ensure(conn)
        conn.commit()
    print(f"Migration calendrier_visites terminée ({a.env}).")


if __name__ == "__main__":
    sys.exit(main())
