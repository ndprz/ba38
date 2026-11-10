#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Création des tables `menus` / `menus_articles` à partir du PDF « Menu type » de VIF
(une page ou plus par client, chaque client liste ses « Commande type » = menus).

- Un menu (code) a le même contenu chez tous les clients : on le vérifie, on garde un seul exemplaire.
- Tables : menus (code, libellé, type sec|frais, actif)
           menus_articles (menu_code, ordre, article, quantité fixe éventuelle, unité, commentaire)
- Type sec/frais : repris de associations_menus quand le code y est utilisé, sinon libellé
  contenant « frais » ou code commençant par F → frais, sinon sec (à vérifier à l'écran).
- Par défaut n'importe rien si `menus` contient déjà des lignes ; --remplacer pour tout recharger.
- pdftotext est lancé avec un délai maximum (consigne mémoire serveur).

Usage : import_menus_vif_pdf.py <menus.pdf> [--remplacer] [dev] [dev_test] [prod] [prod_test]
"""
import collections
import json
import re
import sqlite3
import subprocess
import sys
from contextlib import closing

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}

SQL = """
CREATE TABLE IF NOT EXISTS menus (
    code      TEXT PRIMARY KEY,
    libelle   TEXT,
    type_menu TEXT NOT NULL CHECK (type_menu IN ('sec', 'frais')),
    actif     INTEGER NOT NULL DEFAULT 1,
    maj_le    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS menus_articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_code   TEXT NOT NULL,
    ordre       INTEGER NOT NULL,
    article     TEXT NOT NULL,
    quantite    REAL,            -- quantité fixe prévue par le menu (vide = selon portions)
    unite       TEXT,            -- Kg, kgn, COL, P…
    commentaire TEXT
);
CREATE INDEX IF NOT EXISTS idx_menus_articles_menu ON menus_articles(menu_code);
"""

RE_CLIENT = re.compile(r"Code client\s*:\s*(\S+)")
RE_MENU = re.compile(r"Commande type\s*:\s*(\S+)\s*(.*)$")
RE_ART = re.compile(r"^(\S{3,12})\s+(.+?)\s+(?:(\d+(?:[.,]\d+)?)\s+)?(kgn|kg|col|p|u|pal)\s*$", re.I)
BRUIT = re.compile(r"BA\. de l|Allée de la Pin|38600 Fontaine|TEL : 04 76 85|FAX : 04 76 85|EMAIL:|^MENU TYPE"
                   r"|^Adresse de Livraison|^Dates\b|^Commande\s+__|^Livraison\s+__|^Article\s+Désignation|^\d{5}\s|^Tél")
UNITES = {"kg": "Kg", "kgn": "kgn", "col": "COL", "p": "P", "u": "U", "pal": "PAL"}


def lire_pdf(chemin):
    texte = subprocess.run(["pdftotext", "-layout", chemin, "-"], capture_output=True, text=True,
                           timeout=120, check=True).stdout
    menus, menu = [], None
    for brut in texte.split("\n"):
        l = brut.strip()
        if not l:
            continue
        if RE_CLIENT.search(l):
            menu = None
            continue
        m = RE_MENU.search(l)
        if m:
            menu = {"code": m.group(1), "libelle": m.group(2).strip(), "articles": []}
            menus.append(menu)
            continue
        if menu is None or BRUIT.search(l):
            continue
        a = RE_ART.match(l)
        if a:
            q = a.group(3)
            menu["articles"].append({"article": a.group(1), "unite": UNITES[a.group(4).lower()],
                                     "quantite": float(q.replace(",", ".")) if q else None, "commentaire": []})
        elif menu["articles"]:
            menu["articles"][-1]["commentaire"].append(l)   # ligne de texte libre rattachée à l'article du dessus
    uniques, vus = {}, {}
    for m in menus:
        cle = json.dumps([(a["article"], a["quantite"], a["unite"], a["commentaire"]) for a in m["articles"]])
        if m["code"] in uniques:
            if vus[m["code"]] != cle:
                raise SystemExit(f"Menu {m['code']} : contenu différent selon les clients, à traiter à la main.")
            continue
        uniques[m["code"]], vus[m["code"]] = m, cle
    return list(uniques.values()), len(menus)


def deviner_type(m, types_connus):
    if m["code"] in types_connus:
        return types_connus[m["code"]]
    if "frais" in m["libelle"].lower() or m["code"].upper().startswith("F"):
        return "frais"
    return "sec"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    remplacer = "--remplacer" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    menus, n_brut = lire_pdf(args[0])
    print(f"📄 {n_brut} commandes type lues → {len(menus)} menus distincts, "
          f"{sum(len(m['articles']) for m in menus)} lignes d'articles")
    for env in args[1:] or ["dev"]:
        if env not in DB_PATHS:
            raise SystemExit(f"Environnement inconnu : {env}")
        with closing(sqlite3.connect(DB_PATHS[env])) as conn:
            conn.executescript(SQL)
            if conn.execute("SELECT COUNT(*) FROM menus").fetchone()[0] and not remplacer:
                print(f"ℹ️  [{env}] menus déjà remplie : rien importé (--remplacer pour recharger)")
                continue
            if remplacer:
                conn.execute("DELETE FROM menus_articles")
                conn.execute("DELETE FROM menus")
            try:
                types = {r[0]: r[1] for r in conn.execute(
                    "SELECT menu, type_menu FROM associations_menus WHERE menu IS NOT NULL AND type_menu IS NOT NULL GROUP BY menu")}
            except sqlite3.OperationalError:
                types = {}
            devines = []
            for m in menus:
                t = deviner_type(m, types)
                if m["code"] not in types:
                    devines.append(f"{m['code']}={t}")
                conn.execute("INSERT INTO menus (code, libelle, type_menu) VALUES (?,?,?)", (m["code"], m["libelle"] or None, t))
                conn.executemany(
                    "INSERT INTO menus_articles (menu_code, ordre, article, quantite, unite, commentaire) VALUES (?,?,?,?,?,?)",
                    [(m["code"], i + 1, a["article"], a["quantite"], a["unite"], " / ".join(a["commentaire"]) or None)
                     for i, a in enumerate(m["articles"])])
            conn.commit()
            print(f"✅ [{env}] {len(menus)} menus importés. Type deviné (code absent de associations_menus) : {', '.join(devines)}")


if __name__ == "__main__":
    main()
