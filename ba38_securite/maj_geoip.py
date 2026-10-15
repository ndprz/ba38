"""
Mise à jour mensuelle de la base pays DB-IP "IP to Country Lite"
(gratuite, CC BY 4.0 — https://db-ip.com), partagée DEV/PROD.

    <venv>/bin/python ba38_securite/maj_geoip.py

Remplacement atomique : l'application recharge le fichier d'elle-même
(détection par date de modification). En cas d'échec, l'ancienne base
reste en place.
"""

import gzip
import os
import sys
import tempfile
import urllib.request
from datetime import date, timedelta

DEST = os.getenv("GEOIP_DB", "/srv/ba38/geoip/dbip-country-lite.mmdb")
URL = "https://download.db-ip.com/free/dbip-country-lite-{mois}.mmdb.gz"


def telecharger(mois):
    with urllib.request.urlopen(URL.format(mois=mois), timeout=120) as r:
        return gzip.decompress(r.read())


def main():
    aujourd_hui = date.today()
    mois_prec = (aujourd_hui.replace(day=1) - timedelta(days=1))
    for mois in (aujourd_hui.strftime("%Y-%m"), mois_prec.strftime("%Y-%m")):
        try:
            data = telecharger(mois)
            break
        except Exception as e:
            print(f"⚠️ {mois} indisponible : {e}")
    else:
        sys.exit("❌ Aucune base téléchargée, ancienne base conservée")

    import maxminddb
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DEST), suffix=".mmdb")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        lecteur = maxminddb.open_database(tmp)
        assert (lecteur.get("90.0.0.1") or {}).get("country", {}).get("iso_code") == "FR"
        lecteur.close()
    except Exception as e:
        os.unlink(tmp)
        sys.exit(f"❌ Base téléchargée invalide ({e}), ancienne base conservée")
    os.chmod(tmp, 0o644)
    os.replace(tmp, DEST)
    print(f"✅ Base pays {mois} installée ({len(data) // 1024} Ko)")


if __name__ == "__main__":
    main()
