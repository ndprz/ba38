#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Corrige les menus FRAIS d'après le tableau « selections menus frais.xlsx »
(ligne 3 = codes article en colonnes, une ligne par menu : 1 = article présent, 0 = absent).

Règles :
- pour les articles figurant en colonnes du tableau : présence = 1 / absence = 0 ;
- les autres articles d'un menu existant (quantités fixes, produits spécifiques…) ne sont pas touchés ;
- ordre : celui du menu FRAIS (« Tout ») ; un article ajouté reprend unité / quantité / commentaire
  de sa ligne dans FRAIS ;
- menus du tableau absents de la base : créés (type frais), articles hors tableau repris de FRAIS ;
- libellé mis à jour d'après le tableau.
Par défaut : SIMULATION (affiche les écarts). --appliquer pour écrire.

Usage : corriger_menus_frais_excel.py <fichier.xlsx> [--appliquer] [dev] [dev_test] [prod] [prod_test]
"""
import sqlite3
import sys
from contextlib import closing

import openpyxl

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
    "dev_test": "/srv/ba38/dev/instance/ba380dev_test.sqlite",
    "prod_test": "/srv/ba38/prod/instance/ba380_test.sqlite",
}
MENU_REF = "FRAIS"


def lire_tableau(chemin):
    ws = openpyxl.load_workbook(chemin, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    entete = next(i for i, r in enumerate(rows) if r[1] == "Produits")
    codes = {}
    for c, v in enumerate(rows[entete]):
        if c >= 2 and v is not None and str(v).strip():
            codes[c] = str(v).strip()
    menus = {}
    for r in rows[entete + 1:]:
        if not r[0]:
            continue
        menus[str(r[0]).strip()] = {
            "libelle": str(r[1] or "").strip(),
            "presents": {codes[c] for c in codes if r[c] in (1, "1", 1.0)},
            "absents": {codes[c] for c in codes if r[c] in (0, "0", 0.0)},
        }
        inconnus = [codes[c] for c in codes if r[c] not in (0, 1, "0", "1", 0.0, 1.0)]
        if inconnus:
            raise SystemExit(f"{r[0]} : valeur ni 0 ni 1 pour {inconnus}")
    return set(codes.values()), menus


def lignes_menu(conn, code):
    return [dict(article=a, quantite=q, unite=u, commentaire=c) for a, q, u, c in conn.execute(
        "SELECT article, quantite, unite, commentaire FROM menus_articles WHERE menu_code=? ORDER BY ordre", (code,))]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    appliquer = "--appliquer" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    colonnes, menus_x = lire_tableau(args[0])
    print(f"📄 {len(menus_x)} menus frais, {len(colonnes)} articles en colonnes")
    for env in args[1:] or ["dev"]:
        with closing(sqlite3.connect(DB_PATHS[env])) as conn:
            ref = lignes_menu(conn, MENU_REF)
            ref_par_art = {l["article"]: l for l in ref}
            manquants = colonnes - set(ref_par_art)
            if manquants:
                raise SystemExit(f"[{env}] articles du tableau absents du menu {MENU_REF} : {sorted(manquants)}")
            connus = {a for (a,) in conn.execute("SELECT article FROM articles")}
            print(f"\n=== [{env}] {'APPLICATION' if appliquer else 'simulation'} ===")
            for code, x in menus_x.items():
                existe = conn.execute("SELECT 1 FROM menus WHERE code=?", (code,)).fetchone()
                actuel = lignes_menu(conn, code) if existe else []
                actuel_par_art = {l["article"]: l for l in actuel}
                final = []
                for l in ref:                                   # ordre du menu de référence
                    a = l["article"]
                    if a in colonnes:
                        if a in x["presents"]:
                            final.append(actuel_par_art.get(a) or dict(l))
                    elif existe:
                        if a in actuel_par_art:
                            final.append(actuel_par_art[a])
                    else:                                       # nouveau menu : hors tableau repris de FRAIS
                        final.append(dict(l))
                for l in actuel:                                # spécifiques à ce menu, absents de la référence
                    if l["article"] not in ref_par_art:
                        final.append(l)
                avant, apres = {l["article"] for l in actuel}, {l["article"] for l in final}
                ajoutes, retires = sorted(apres - avant), sorted(avant - apres)
                etat = "CRÉÉ" if not existe else ("inchangé" if not ajoutes and not retires else "modifié")
                print(f"{code:10} {etat:8} {len(avant):2} → {len(final):2} articles"
                      + (f" | + {', '.join(ajoutes)}" if ajoutes else "") + (f" | − {', '.join(retires)}" if retires else ""))
                if appliquer:
                    if not existe:
                        conn.execute("INSERT INTO menus (code, libelle, type_menu) VALUES (?,?, 'frais')", (code, x["libelle"] or None))
                    else:
                        conn.execute("UPDATE menus SET libelle=?, type_menu='frais', maj_le=datetime('now','localtime') WHERE code=?",
                                     (x["libelle"] or None, code))
                        conn.execute("DELETE FROM menus_articles WHERE menu_code=?", (code,))
                    conn.executemany(
                        "INSERT INTO menus_articles (menu_code, ordre, article, quantite, unite, commentaire) VALUES (?,?,?,?,?,?)",
                        [(code, i + 1, l["article"], l["quantite"], l["unite"], l["commentaire"]) for i, l in enumerate(final)])
            if appliquer:
                conn.commit()
                print("✅ appliqué")


if __name__ == "__main__":
    main()
