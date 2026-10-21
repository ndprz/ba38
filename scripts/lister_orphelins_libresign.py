# lister_orphelins_libresign.py
"""
Liste les documents LibreSign qui ne correspondent plus à aucune demande
Basilic (signatures_electroniques, annexe1bis) dans AUCUNE des bases DEV,
DEV test, PROD, PROD test — l'instance LibreSign est partagée entre DEV et
PROD. Lecture seule : ne supprime rien.

Avant le 2026-10-21, supprimer une demande dans Basilic laissait le document
(et ses PDF) dans LibreSign ; un renvoi d'Annexe 1 bis écrase aussi
libresign_file_id, laissant l'ancien envoi orphelin.

Usage : venv/bin/python scripts/lister_orphelins_libresign.py [fichier.csv]
"""

import csv
import os
import sqlite3
import sys

import requests
from dotenv import load_dotenv

RACINE = "/srv/ba38"
sys.path.insert(0, os.path.join(RACINE, "dev"))
load_dotenv(os.path.join(RACINE, "dev", ".env"))  # LIBRESIGN_* identiques DEV/PROD

from ba38_utilitaires.libresign import _auth, _base_url, _headers  # noqa: E402

# Chemins explicites (cf. piège load_dotenv : ne pas dépendre de get_db_path()).
BASES = {
    "DEV": f"{RACINE}/dev/instance/ba380dev.sqlite",
    "DEV test": f"{RACINE}/dev/instance/ba380dev_test.sqlite",
    "PROD": f"{RACINE}/prod/instance/ba380.sqlite",
    "PROD test": f"{RACINE}/prod/instance/ba380_test.sqlite",
}
TABLES = ("signatures_electroniques", "annexe1bis")


def references_basilic():
    """{(cle, valeur): ["PROD annexe1bis #12", ...]} pour file_id et uuid."""
    refs = {}
    for nom_base, chemin in BASES.items():
        if not os.path.exists(chemin):
            continue
        conn = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
        for table in TABLES:
            try:
                lignes = conn.execute(
                    f"SELECT id, libresign_file_id, libresign_uuid FROM {table} "
                    "WHERE libresign_file_id IS NOT NULL OR libresign_uuid IS NOT NULL"
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for id_ligne, file_id, uuid in lignes:
                etiquette = f"{nom_base} {table} #{id_ligne}"
                if file_id not in (None, ""):
                    refs.setdefault(("id", str(file_id)), []).append(etiquette)
                if uuid:
                    refs.setdefault(("uuid", uuid), []).append(etiquette)
        conn.close()
    return refs


def documents_libresign():
    documents, page = [], 1
    while True:
        resp = requests.get(
            f"{_base_url()}/ocs/v2.php/apps/libresign/api/v1/file/list",
            auth=_auth(), headers=_headers(), params={"page": page, "length": 50}, timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()["ocs"]["data"]
        documents.extend(data["data"])
        if not data["pagination"].get("next"):
            return documents
        page += 1


def main():
    sortie_csv = sys.argv[1] if len(sys.argv) > 1 else None
    refs = references_basilic()
    lignes = []
    for doc in documents_libresign():
        trouve = refs.get(("id", str(doc["id"]))) or refs.get(("uuid", doc["uuid"])) or []
        signataires = ", ".join(
            f"{s.get('displayName') or ''} <{s.get('email') or ''}>" for s in doc.get("signers") or []
        )
        lignes.append({
            "libresign_id": doc["id"],
            "uuid": doc["uuid"],
            "nom": doc.get("name"),
            "cree_le": (doc.get("created_at") or "")[:10],
            "statut": doc.get("statusText"),
            "signataires": signataires,
            "reference_basilic": " ; ".join(sorted(set(trouve))),
            "orphelin": "oui" if not trouve else "non",
        })

    orphelins = [l for l in lignes if l["orphelin"] == "oui"]
    print(f"{len(lignes)} documents LibreSign, {len(lignes) - len(orphelins)} référencés, {len(orphelins)} orphelins\n")
    for l in orphelins:
        print(f"  #{l['libresign_id']:>4}  {l['cree_le']}  {l['statut']:<14} {l['nom'][:45]:<45} {l['signataires'][:60]}")

    if sortie_csv:
        with open(sortie_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(lignes[0].keys()), delimiter=";")
            writer.writeheader()
            writer.writerows(lignes)
        print(f"\nDétail complet : {sortie_csv}")


if __name__ == "__main__":
    main()
