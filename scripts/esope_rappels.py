#!/usr/bin/env python3
"""Rappel ESOPE : mail la veille aux bénévoles attendus le lendemain.

Usage :
  esope_rappels.py --env dev|prod [--base-test] [--simulation] [--date AAAA-MM-JJ]

--date : jour des créneaux (demain par défaut).
--base-test : base de test (mails redirigés vers la personne qui a envoyé le pré-planning).
En PROD, à lancer chaque jour par cron (ex. 10h) depuis scripts_taches.
"""

import argparse
import os
import sys
from datetime import date

from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=("dev", "prod"), required=True)
    parser.add_argument("--base-test", action="store_true")
    parser.add_argument("--simulation", action="store_true")
    parser.add_argument("--date")
    args = parser.parse_args()

    base_dir = f"/srv/ba38/{args.env}"
    # chemin explicite : un load_dotenv() nu chargerait toujours dev/.env
    load_dotenv(os.path.join(base_dir, ".env"), override=True)
    os.environ["BA38_BASE_DIR"] = base_dir
    sys.path.insert(0, base_dir)

    from ba38_planning.esope.rappels import envoyer_rappels
    jour = date.fromisoformat(args.date) if args.date else None
    _, ko = envoyer_rappels(jour, test_db=args.base_test, simulation=args.simulation)
    sys.exit(1 if ko else 0)


if __name__ == "__main__":
    main()
