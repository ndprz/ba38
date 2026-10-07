#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vérifie, pour chaque association chorus_pro='oui', ce que Chorus Pro exige
(code service / n° d'engagement juridique) et le compare aux champs
chorus_code_service / chorus_numero_engagement saisis dans la base.

LECTURE SEULE des deux côtés (base ouverte en mode ro, API en consultation).
Identifiants Chorus : voir chorus_test_connexion.py.

Usage :
    venv/bin/python scripts/chorus_verifier_associations.py [--env prod|dev]
"""

import argparse
import sqlite3
import time

import chorus_test_connexion as cpro

DB_PATHS = {
    "dev": "/srv/ba38/dev/instance/ba380dev.sqlite",
    "prod": "/srv/ba38/prod/instance/ba380.sqlite",
}


def appel_avec_reprise(config, etat, chemin, corps):
    """Appel API avec pause + nouveau jeton si refus (quota PISTE)."""
    for tentative in range(4):
        try:
            return cpro.appel(config, etat["jeton"], chemin, corps)
        except SystemExit as e:
            if tentative == 3:
                raise
            print(f"   … refus ({str(e)[:60]}), nouvelle tentative dans {2 ** (tentative + 1)} s")
            time.sleep(2 ** (tentative + 1))
            etat["jeton"] = cpro.obtenir_jeton(config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=list(DB_PATHS), default="prod")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATHS[args.env]}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    assos = conn.execute(
        """SELECT nom_association, code_SIRET, chorus_code_service, chorus_numero_engagement
           FROM associations WHERE chorus_pro = 'oui' ORDER BY nom_association"""
    ).fetchall()
    conn.close()

    config = cpro.lire_config()
    etat = {"jeton": cpro.obtenir_jeton(config)}
    nb_alertes = 0

    for a in assos:
        siret = (a["code_SIRET"] or "").replace(" ", "")
        service = (a["chorus_code_service"] or "").strip()
        ej = (a["chorus_numero_engagement"] or "").strip()
        nom = a["nom_association"].strip()

        time.sleep(1)
        res = appel_avec_reprise(config, etat, "/structures/v1/rechercher", {
            "structure": {"identifiantStructure": siret, "typeIdentifiantStructure": "SIRET"}
        })
        structures = res.get("listeStructures") or []
        if not structures:
            print(f"❌ {nom:32} {siret}  introuvable dans Chorus Pro")
            nb_alertes += 1
            continue

        detail = appel_avec_reprise(config, etat, "/structures/v1/consulter", {
            "idStructureCPP": structures[0]["idStructureCPP"], "codeLangue": "fr",
        })
        p = detail.get("parametres") or {}
        exige = []
        manque = []
        if p.get("codeServiceDoitEtreRenseigne"):
            exige.append("service")
            if not service:
                manque.append("code service")
        if p.get("numeroEJDoitEtreRenseigne"):
            exige.append("EJ")
            if not ej:
                manque.append("n° engagement")
        if p.get("gestionNumeroEJOuCodeService") and not (p.get("codeServiceDoitEtreRenseigne") or p.get("numeroEJDoitEtreRenseigne")):
            exige.append("EJ ou service")
            if not (service or ej):
                manque.append("EJ ou code service")

        designation = structures[0].get("designationStructure", "")
        statut = "⚠️ " if manque else "✅"
        if manque:
            nb_alertes += 1
        print(f"{statut} {nom:32} {siret}  [{designation}]  exige: {', '.join(exige) or 'rien'}"
              + (f"  → MANQUE {', '.join(manque)}" if manque else ""))

    print(f"\n{len(assos)} association(s) vérifiée(s), {nb_alertes} à corriger.")


if __name__ == "__main__":
    main()
