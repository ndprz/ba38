# verify_env_consistency.py
#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, '/home/ndprz/dev')  # ✅ ajoute le dossier où est utils.py

from utils import get_db_path, write_log

def verify_env_consistency():
    db_path = get_db_path()
    report = []

    report.append("🔎 Vérification de la base SQLite utilisée")
    report.append(f"📁 Chemin détecté : {db_path}")

    if not os.path.exists(db_path):
        report.append("❌ ERREUR : La base de données n'existe pas à ce chemin.")
    else:
        size = os.path.getsize(db_path)
        report.append(f"✅ OK : Fichier trouvé ({size} octets)")

    report.append(f"🌍 ENVIRONMENT = {os.getenv('ENVIRONMENT')}")
    report.append(f"🔁 TEST_MODE = {os.getenv('TEST_MODE')}")


    log_output = "\n".join(report)
    write_log(log_output)
    print(log_output)

if __name__ == "__main__":
    verify_env_consistency()
