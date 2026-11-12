#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration : cotisations bénévoles dynamiques.

Crée `benevoles_cotisations` (id_benevole, annee) et y copie chaque 'oui' des
colonnes `cotisation_AAAA` de `benevoles` et `benevoles_inactifs`. Les colonnes
sont CONSERVÉES (filet de sécurité) ; seules les entrées `field_groups` sont
retirées avec --retirer-champs (à lancer après validation).

Idempotent. Usage : migrate_benevoles_cotisations.py --env dev [--retirer-champs]
"""
import argparse
import re
import sqlite3
import sys

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev", choices=DB_PATHS)
    ap.add_argument("--retirer-champs", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATHS[args.env], timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS benevoles_cotisations (
        id_benevole INTEGER NOT NULL, annee INTEGER NOT NULL, date_maj TEXT, user_maj TEXT,
        date_paiement TEXT, PRIMARY KEY (id_benevole, annee))""")
    if "date_paiement" not in {r[1] for r in conn.execute("PRAGMA table_info(benevoles_cotisations)")}:
        conn.execute("ALTER TABLE benevoles_cotisations ADD COLUMN date_paiement TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bc_annee ON benevoles_cotisations(annee)")

    total = 0
    for table in ("benevoles", "benevoles_inactifs"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        for c in cols:
            m = re.match(r"^cotisation_(\d{4})$", c)
            if not m:
                continue
            cur = conn.execute(
                f"INSERT OR IGNORE INTO benevoles_cotisations (id_benevole, annee, date_maj, user_maj) "
                f"SELECT id, {int(m.group(1))}, datetime('now','localtime'), 'migration' "
                f"FROM {table} WHERE LOWER(TRIM(COALESCE({c},''))) = 'oui'")
            total += cur.rowcount
    print(f"{total} cotisations copiées")

    if args.retirer_champs:
        cur = conn.execute("DELETE FROM field_groups WHERE appli='benevoles' AND field_name GLOB 'cotisation_[0-9][0-9][0-9][0-9]'")
        print(f"{cur.rowcount} champs retirés de field_groups")
    conn.commit()
    for a, n in conn.execute("SELECT annee, COUNT(*) FROM benevoles_cotisations GROUP BY annee ORDER BY annee"):
        print(f"  {a} : {n}")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
