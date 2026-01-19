#!/usr/bin/env python3
import os
from dotenv import load_dotenv

load_dotenv()

def check_var(name):
    value = os.getenv(name)
    if value:
        print(f"✅ {name} = {value}")
    else:
        print(f"❌ {name} manquante ou vide")

vars_to_check = [
    "ENVIRONMENT", "FLASK_ENV", "SQLITE_DB_DEV", "SQLITE_DB_DEV_TEST", "SQLITE_DB_PROD", "SQLITE_DB_PROD_TEST",
    "SERVICE_ACCOUNT_FILE", "GDRIVE_DB_FOLDER_ID", "GDRIVE_DB_FILE_ID_PROD",
    "GDRIVE_DB_FILE_ID_DEV", "GDRIVE_DB_FILE_ID_TEST", "SMTP_PASSWORD", "FLASK_SECRET_KEY",
    "MAILJET_API_KEY", "MAILJET_API_SECRET", "MAILJET_SENDER",
    "GDRIVE_DB_FOLDER_ID"

]

print("🔍 Vérification des variables d'environnement...\n")
for var in vars_to_check:
    check_var(var)
