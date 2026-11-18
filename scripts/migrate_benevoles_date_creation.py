#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Migration : date de création des fiches bénévoles.

Ajoute `benevoles.date_creation` (AAAA-MM-JJ), un trigger qui la renseigne à
chaque INSERT (quel que soit le chemin : fiche, ajout rapide, import) et le
champ field_groups (date, groupe « vie association »). Les fiches existantes
restent à NULL (date réelle inconnue). Idempotent.
Usage : migrate_benevoles_date_creation.py --env dev|dev_test|prod|prod_test
"""
import argparse
import sqlite3

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

ap = argparse.ArgumentParser()
ap.add_argument("--env", default="dev", choices=DB_PATHS)
args = ap.parse_args()
conn = sqlite3.connect(DB_PATHS[args.env], timeout=30)
if "date_creation" not in {r[1] for r in conn.execute("PRAGMA table_info(benevoles)")}:
    conn.execute("ALTER TABLE benevoles ADD COLUMN date_creation TEXT")
conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_benevoles_date_creation
    AFTER INSERT ON benevoles WHEN NEW.date_creation IS NULL
    BEGIN UPDATE benevoles SET date_creation = date('now','localtime') WHERE id = NEW.id; END""")
if not conn.execute("SELECT 1 FROM field_groups WHERE appli='benevoles' AND field_name='date_creation'").fetchone():
    conn.execute("""INSERT INTO field_groups (field_name, group_name, display_order, is_required, type_champ, appli)
                    VALUES ('date_creation', 'vie association', 1101, 0, 'date', 'benevoles')""")
conn.commit()
print(args.env, "OK")
