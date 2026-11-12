#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : formulaire de réponse en ligne « Indicateurs état ».

Ajoute :
- indicateurs_suivi.token (lien personnel, unique) ;
- indicateurs_suivi.reponse_mode (1 Non reçu, 2 Réponse mail, 3 Formulaire, 4 Courrier ; défaut 1) ;
- table indicateurs_reponses (une ligne par suivi : valeurs saisies, date de
  validation, état du mail de confirmation, réouverture éventuelle).

Idempotent. Usage : migrate_indicateurs_formulaire.py [--env dev|prod|dev_test|prod_test]
"""
import argparse
import sqlite3
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

DDL = """
CREATE TABLE IF NOT EXISTS indicateurs_reponses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suivi_id INTEGER NOT NULL UNIQUE,
    responsable_ie TEXT,
    tel_resp_ie TEXT,
    courriel_ie TEXT,
    foyers_trim INTEGER,
    beneficiaires_trim INTEGER,
    passages_trim INTEGER,
    foyers_annee INTEGER,
    personnes_annee INTEGER,
    passages_annee INTEGER,
    remarques TEXT,
    soumis_le TEXT,
    soumis_ip TEXT,
    mail_confirmation_le TEXT,
    mail_confirmation_erreur TEXT,
    rouvert_le TEXT,
    rouvert_par TEXT,
    FOREIGN KEY (suivi_id) REFERENCES indicateurs_suivi(id)
)
"""


def migrer(path):
    with closing(sqlite3.connect(path)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(indicateurs_suivi)")]
        if "token" not in cols:
            conn.execute("ALTER TABLE indicateurs_suivi ADD COLUMN token TEXT")
            print("  + indicateurs_suivi.token")
        if "reponse_mode" not in cols:
            # 1 Non reçu, 2 Réponse mail, 3 Réponse formulaire, 4 Courrier
            conn.execute("ALTER TABLE indicateurs_suivi ADD COLUMN reponse_mode INTEGER DEFAULT 1")
            print("  + indicateurs_suivi.reponse_mode")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_indicateurs_suivi_token ON indicateurs_suivi(token)")
        conn.execute(DDL)
        # Réponses déjà validées en ligne -> « Réponse formulaire »
        conn.execute("""UPDATE indicateurs_suivi SET reponse_mode = 3
                        WHERE id IN (SELECT suivi_id FROM indicateurs_reponses WHERE soumis_le IS NOT NULL)""")
        conn.commit()
        print("  OK", path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev", choices=DB_PATHS)
    migrer(DB_PATHS[ap.parse_args().env])
