#!/usr/bin/env python3
import os
from pathlib import Path
from dotenv import load_dotenv

# ============================================================
# 📁 Détermination de la racine BA38
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent          # /srv/ba38/dev ou /srv/ba38/prod
ENV_FILE = BASE_DIR / ".env"

if not ENV_FILE.exists():
    print(f"❌ Fichier .env introuvable : {ENV_FILE}")
    exit(1)

load_dotenv(ENV_FILE)

# ============================================================
# 🔍 Vérification des variables
# ============================================================

def check_var(name):
    value = os.getenv(name)
    if value:
        print(f"✅ {name} = {value}")
    else:
        print(f"❌ {name} manquante ou vide")


vars_to_check = [
    # Environnement
    "ENVIRONMENT",
    "FLASK_ENV",

    # Bases SQLite
    "SQLITE_DB_DEV",
    "SQLITE_DB_DEV_TEST",
    "SQLITE_DB_PROD",
    "SQLITE_DB_PROD_TEST",

    # Google Drive
    "SERVICE_ACCOUNT_FILE",
    "GDRIVE_DB_FOLDER_ID",
    "GDRIVE_DB_FILE_ID_DEV",
    "GDRIVE_DB_FILE_ID_TEST",
    "GDRIVE_DB_FILE_ID_PROD",

    # Mail / sécurité
    "SMTP_PASSWORD",
    "FLASK_SECRET_KEY",
    "MAILJET_API_KEY",
    "MAILJET_API_SECRET",
]

env = os.getenv("ENVIRONMENT", "NON DÉFINI")

print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print("🔍 Vérification des variables d'environnement")
print(f"📦 Environnement détecté : {env}")
print(f"📄 Fichier .env           : {ENV_FILE}")
print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

for var in vars_to_check:
    check_var(var)
