#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Import des PORTIONS par article depuis le fichier Excel « portions » (export VIF).

Écrit dans `articles_portions` (persistante : survit à la reconstruction
quotidienne de `articles` par scripts_taches/update_articles_table.py) et
met à jour `articles.portion` immédiatement.

- colonnes lues : « Article » et « PORTION » (virgule décimale, en kg NET — kgn, unité de l'export VIF) ;
  les lignes sans portion sont ignorées (une portion existante n'est pas effacée) ;
  0,000 est une vraie valeur (article non distribué en portion) ;
- le fichier contient plus d'articles que la table `articles` (tous les articles VIF) :
  tous sont conservés pour le jour où ils apparaissent dans l'import quotidien ;
- idempotent (upsert). Chemins explicites (pas de load_dotenv).

Usage : import_portions_articles.py <fichier.xlsx> [dev] [dev_test] [prod] [prod_test]
        (sans environnement : dev)
"""
import sqlite3
import sys
from contextlib import closing
from datetime import datetime

import openpyxl

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}


def lire_portions(chemin):
    ws = openpyxl.load_workbook(chemin, read_only=True, data_only=True)["portions"]
    entete, portions, ignorees = None, {}, 0
    for ligne in ws.iter_rows(values_only=True):
        if entete is None:
            if ligne and str(ligne[0]).strip() == "Article" and any(str(c).strip().upper() == "PORTION" for c in ligne):
                entete = [str(c).strip().upper() for c in ligne]
            continue
        article = str(ligne[0] or "").strip()
        brut = str(ligne[entete.index("PORTION")] or "").strip()
        if not article or not brut:
            continue
        try:
            portions[article] = float(brut.replace(",", "."))
        except ValueError:
            ignorees += 1
            print(f"   ⚠️ portion illisible ignorée : {article} = {brut!r}")
    if entete is None:
        raise SystemExit("En-tête « Article / PORTION » introuvable dans la feuille « portions ».")
    return portions


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    portions = lire_portions(sys.argv[1])
    print(f"📄 {len(portions)} portion(s) lues dans {sys.argv[1]}")
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for env in sys.argv[2:] or ["dev"]:
        if env not in DB_PATHS:
            raise SystemExit(f"Environnement inconnu : {env}")
        with closing(sqlite3.connect(DB_PATHS[env])) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS articles_portions (
                                article TEXT PRIMARY KEY, portion REAL, maj_le TEXT)""")
            if "portion" not in {r[1] for r in conn.execute("PRAGMA table_info(articles)")}:
                conn.execute("ALTER TABLE articles ADD COLUMN portion REAL")
            conn.executemany("""INSERT INTO articles_portions (article, portion, maj_le) VALUES (?, ?, ?)
                                ON CONFLICT(article) DO UPDATE SET portion = excluded.portion, maj_le = excluded.maj_le""",
                             [(a, p, maintenant) for a, p in portions.items()])
            conn.execute("""UPDATE articles SET portion =
                            (SELECT p.portion FROM articles_portions p WHERE p.article = articles.article)""")
            n_art = conn.execute("SELECT COUNT(*) FROM articles WHERE portion IS NOT NULL").fetchone()[0]
            n_tot = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            conn.commit()
        print(f"✅ [{env}] articles_portions : {len(portions)} ; articles avec portion : {n_art}/{n_tot}")


if __name__ == "__main__":
    main()
