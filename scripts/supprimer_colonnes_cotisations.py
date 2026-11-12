#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supprime les anciennes colonnes cotisation_AAAA de `benevoles` et `benevoles_inactifs`
(remplacées par la table benevoles_cotisations).

Garde-fous : refuse d'agir si, pour une colonne, un bénévole à « oui » n'a pas sa ligne
dans benevoles_cotisations (contrôle par id et par année). Sauvegarde la base avant.
Usage : supprimer_colonnes_cotisations.py --env dev|dev_test|prod|prod_test [--simulation]
"""
import argparse, os, re, sqlite3, sys
from datetime import datetime

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}
BACKUPS = "/srv/ba38/backups/fichiers"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=DB_PATHS)
    ap.add_argument("--simulation", action="store_true")
    a = ap.parse_args()
    path = DB_PATHS[a.env]
    conn = sqlite3.connect(path, timeout=30)

    if not conn.execute("select 1 from sqlite_master where name='benevoles_cotisations'").fetchone():
        sys.exit("❌ table benevoles_cotisations absente")

    cibles = []  # (table, colonne, annee)
    for t in ("benevoles", "benevoles_inactifs"):
        for (c,) in conn.execute(f"select name from pragma_table_info('{t}')"):
            m = re.match(r"^cotisation_(\d{4})$", c)
            if m:
                cibles.append((t, c, int(m.group(1))))
    if not cibles:
        print("ℹ️ aucune colonne à supprimer"); return

    for t, c, an in cibles:  # contrôle : chaque 'oui' a sa ligne
        manquants = conn.execute(
            f"select count(*) from {t} where lower(trim(coalesce({c},'')))='oui' and id not in "
            f"(select id_benevole from benevoles_cotisations where annee=?)", (an,)).fetchone()[0]
        if manquants:
            sys.exit(f"❌ {t}.{c} : {manquants} 'oui' absents de benevoles_cotisations — arrêt, rien supprimé")
    print(f"✅ contrôle OK : {len(cibles)} colonnes, tous les 'oui' sont dans benevoles_cotisations")

    if a.simulation:
        for t, c, _ in cibles: print(f"   (simulation) DROP {t}.{c}")
        return

    dest = os.path.join(BACKUPS, f"drop_cotisations_{a.env}_{datetime.now():%Y%m%d-%H%M%S}")
    os.makedirs(dest, exist_ok=True)
    sauv = sqlite3.connect(os.path.join(dest, os.path.basename(path)))
    conn.backup(sauv); sauv.close()
    print(f"💾 {dest}")

    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("BEGIN IMMEDIATE")
    for t, c, _ in cibles:
        conn.execute(f"ALTER TABLE {t} DROP COLUMN {c}")
        print(f"🗑️  {t}.{c}")
    conn.commit()
    restant = conn.execute("select count(*) from pragma_table_info('benevoles') where name glob 'cotisation_[0-9][0-9][0-9][0-9]'").fetchone()[0]
    print(f"✔ colonnes restantes dans benevoles : {restant} ; lignes benevoles_cotisations : "
          f"{conn.execute('select count(*) from benevoles_cotisations').fetchone()[0]} ; "
          f"benevoles : {conn.execute('select count(*) from benevoles').fetchone()[0]} lignes")
    conn.close()


if __name__ == "__main__":
    main()
