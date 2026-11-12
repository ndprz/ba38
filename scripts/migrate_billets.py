#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : module "Billets spectacle" (ba38_billets/), groupe Média/Événements.
Tables : billets_campagnes, billets_participants. Ligne `applications` + droits
(les utilisateurs ayant un droit « evenements » reçoivent le même niveau sur « billets »).
Idempotent.
Usage : migrate_billets.py --env dev|prod|dev_test|prod_test
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
        CREATE TABLE IF NOT EXISTS billets_campagnes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            donateur TEXT NOT NULL,
            lieu TEXT,
            spectacle TEXT NOT NULL,
            date_spectacle TEXT,
            heure_spectacle TEXT,
            mode_retrait TEXT,
            nb_billets INTEGER NOT NULL DEFAULT 1,
            date_limite TEXT,
            sujet TEXT,
            corps TEXT,
            expediteur TEXT,
            expediteur_nom TEXT,
            reply_to TEXT,
            statut TEXT NOT NULL DEFAULT 'ouverte',
            tirage_le TEXT,
            tirage_par TEXT,
            resultats_envoyes_le TEXT,
            cree_le TEXT,
            cree_par TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billets_participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campagne_id INTEGER NOT NULL,
            benevole_id INTEGER,
            prenom TEXT,
            nom TEXT,
            email TEXT,
            categorie TEXT NOT NULL DEFAULT 'benevole',   -- benevole | salarie
            selected INTEGER DEFAULT 1,
            token TEXT,
            invite_le TEXT,
            invite_mode_test INTEGER DEFAULT 0,
            invite_erreur TEXT,
            nb_invitations INTEGER DEFAULT 0,
            etat TEXT NOT NULL DEFAULT 'invite',           -- invite | inscrit | desiste
            inscrit_le TEXT,
            inscrit_par TEXT,                              -- NULL = lui-même, sinon gestionnaire
            confirmation_le TEXT,
            resultat TEXT,                                 -- NULL | gagnant | non_retenu
            origine_place TEXT,                            -- inscrit | salarie (pour les gagnants)
            ordre_tirage INTEGER,
            resultat_envoye_le TEXT,
            resultat_erreur TEXT,
            FOREIGN KEY (campagne_id) REFERENCES billets_campagnes(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_billets_part_campagne ON billets_participants(campagne_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_billets_part_token ON billets_participants(token)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(billets_campagnes)")}
    if "textes_mails" not in cols:
        conn.execute("ALTER TABLE billets_campagnes ADD COLUMN textes_mails TEXT")  # JSON des textes éditables
    print("✓ Tables billets_campagnes / billets_participants prêtes.")

    if conn.execute("SELECT 1 FROM applications WHERE appli='billets'").fetchone() is None:
        conn.execute(
            "INSERT INTO applications (appli, label, endpoint, groupe, ordre, icon, ordre_groupe, menu_visible) "
            "VALUES ('billets', 'Billets spectacle', 'billets.liste', 'Média', 4, '🎟️', 5, 1)")
        print("✓ Ligne applications 'billets' ajoutée (groupe Média).")
    n = 0
    for r in conn.execute("SELECT user_email, droit FROM roles_utilisateurs WHERE appli='evenements' "
                          "AND droit IN ('lecture','ecriture','admin')").fetchall():
        if conn.execute("SELECT 1 FROM roles_utilisateurs WHERE user_email=? AND appli='billets'",
                        (r[0],)).fetchone() is None:
            conn.execute("INSERT INTO roles_utilisateurs (user_email, appli, droit) VALUES (?, 'billets', ?)",
                         (r[0], r[1]))
            n += 1
    print(f"✓ Droits 'billets' copiés depuis 'evenements' : {n} utilisateur(s).")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--env", default="dev", choices=DB_PATHS)
    a = p.parse_args()
    with closing(sqlite3.connect(DB_PATHS[a.env])) as conn:
        ensure(conn)
        conn.commit()
    print(f"Migration billets terminée ({a.env}).")


if __name__ == "__main__":
    sys.exit(main())
