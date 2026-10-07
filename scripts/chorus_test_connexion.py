#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test de connexion à l'API Chorus Pro (via PISTE) — LECTURE SEULE.

1. Obtient un jeton OAuth2 (client_credentials) auprès de PISTE.
2. Recherche une structure destinataire par SIRET (API Structures).
3. Consulte ses paramètres : code service / n° d'engagement obligatoires ?

Aucune facture n'est déposée, rien n'est modifié côté Chorus Pro.

Identifiants lus dans dev/.env :
    CHORUS_ENV            prod | sandbox (défaut : sandbox), écrasable par --env
    CHORUS_CLIENT_ID      application PISTE
    CHORUS_CLIENT_SECRET  application PISTE
    CHORUS_TECH_LOGIN     compte technique Chorus Pro (TECH_1_...@cpro.fr)
    CHORUS_TECH_PASSWORD  mot de passe du compte technique
En sandbox (qualification), mêmes noms préfixés CHORUS_SANDBOX_
(CHORUS_SANDBOX_CLIENT_ID, ...) : application APP_SANDBOX_ + compte
technique créé sur le portail de qualification.

Usage :
    venv/bin/python scripts/chorus_test_connexion.py [--env sandbox] [--siret 26381036800062]
"""

import argparse
import base64
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv("/srv/ba38/dev/.env")

URLS = {
    "prod": {
        "oauth": "https://oauth.piste.gouv.fr/api/oauth/token",
        "api": "https://api.piste.gouv.fr/cpro",
    },
    "sandbox": {
        "oauth": "https://sandbox-oauth.piste.gouv.fr/api/oauth/token",
        "api": "https://sandbox-api.piste.gouv.fr/cpro",
    },
}


def lire_config(env=None):
    env = env or os.getenv("CHORUS_ENV", "sandbox")
    prefixe = "CHORUS_SANDBOX_" if env == "sandbox" else "CHORUS_"
    config = {
        "env": env,
        "client_id": os.getenv(prefixe + "CLIENT_ID", ""),
        "client_secret": os.getenv(prefixe + "CLIENT_SECRET", ""),
        "login": os.getenv(prefixe + "TECH_LOGIN", ""),
        "password": os.getenv(prefixe + "TECH_PASSWORD", ""),
    }
    manquants = [k for k, v in config.items() if not v]
    if manquants:
        sys.exit(f"❌ Variables manquantes dans .env : {', '.join(manquants)}")
    if config["env"] not in URLS:
        sys.exit(f"❌ CHORUS_ENV invalide : {config['env']} (prod ou sandbox)")
    return config


def obtenir_jeton(config):
    rep = requests.post(
        URLS[config["env"]]["oauth"],
        data={
            "grant_type": "client_credentials",
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "scope": "openid",
        },
        timeout=30,
    )
    if rep.status_code != 200:
        sys.exit(f"❌ Jeton PISTE refusé ({rep.status_code}) : {rep.text[:500]}")
    print("✅ Jeton OAuth PISTE obtenu.")
    return rep.json()["access_token"]


def appel(config, jeton, chemin, corps):
    cpro_account = base64.b64encode(
        f"{config['login']}:{config['password']}".encode()
    ).decode()
    rep = requests.post(
        URLS[config["env"]]["api"] + chemin,
        headers={
            "Authorization": f"Bearer {jeton}",
            "cpro-account": cpro_account,
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json;charset=utf-8",
        },
        json=corps,
        timeout=30,
    )
    if rep.status_code != 200:
        sys.exit(f"❌ {chemin} ({rep.status_code}) : {rep.text[:800]}")
    return rep.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--siret", default="26381036800062",
                        help="SIRET destinataire à rechercher (défaut : CCAS Échirolles)")
    parser.add_argument("--env", choices=list(URLS), help="écrase CHORUS_ENV")
    args = parser.parse_args()

    config = lire_config(args.env)
    print(f"Environnement : {config['env']}")
    jeton = obtenir_jeton(config)

    resultat = appel(config, jeton, "/structures/v1/rechercher", {
        "structure": {
            "identifiantStructure": args.siret,
            "typeIdentifiantStructure": "SIRET",
        }
    })
    structures = resultat.get("listeStructures") or []
    if not structures:
        print(f"⚠️  Aucune structure Chorus Pro pour le SIRET {args.siret}")
        print(json.dumps(resultat, indent=2, ensure_ascii=False))
        return
    print(f"✅ Compte technique accepté — {len(structures)} structure(s) trouvée(s).")

    for s in structures:
        print(f"\n— {s.get('designationStructure')} (idStructureCPP={s.get('idStructureCPP')})")
        detail = appel(config, jeton, "/structures/v1/consulter", {
            "idStructureCPP": s.get("idStructureCPP"),
            "codeLangue": "fr",
        })
        parametres = detail.get("parametres") or {}
        print("   Code service obligatoire        :", parametres.get("codeServiceDoitEtreRenseigne"))
        print("   N° engagement (EJ) obligatoire  :", parametres.get("numeroEJDoitEtreRenseigne"))
        print("   EJ OU code service obligatoire  :", parametres.get("gestionNumeroEJOuCodeService"))


if __name__ == "__main__":
    main()
