#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reprise de l'historique dans le journal des mouvements de stock (table mouvements_stock) :
ramasses journalières validées (entrées RA) et bons de livraison validés (sorties BL).

Idempotent : un document déjà présent au journal n'est pas recréé. La table est créée si besoin.
Chemins de base explicites (pas de load_dotenv : piège connu). --env dev (défaut) | prod | dev_test | prod_test."""

import argparse
import os
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ba38_distribution.journal_stock import reprendre_historique  # noqa: E402

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=sorted(DB_PATHS), default="dev")
    a = p.parse_args()
    print(f"Base : {DB_PATHS[a.env]}")
    with closing(sqlite3.connect(DB_PATHS[a.env], timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        r = reprendre_historique(conn)
    print(f"Reprise : {r['ramasses']} ramasse(s), {r['bl']} BL, {r['lignes']} ligne(s) de mouvement")


if __name__ == "__main__":
    main()
