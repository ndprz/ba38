#!/usr/bin/env python3

import os
import sys
import sqlite3
import pandas as pd
from dotenv import load_dotenv

# 🔧 Accès aux fonctions utilitaires
sys.path.insert(0, "/home/ndprz/ba380")
from utils import get_google_services, get_db_path, write_log

# ✅ Charge le .env de production
load_dotenv("/home/ndprz/ba380/.env")

FILENAME = "BASE_MAIL_IE"
FOLDER_ID = os.getenv("FOLDER_ID_ASSOCIATIONS")

def list_drive_files(drive_service, folder_id):
    try:
        results = drive_service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id, name)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True
        ).execute()
        return results.get("files", [])
    except Exception as e:
        write_log(f"❌ Erreur list_drive_files({folder_id}) : {e}")
        return []

def get_existing_spreadsheet_id(drive_service, folder_id, filename):
    filename_normalized = filename.strip().replace(" ", "_").lower()
    for file in list_drive_files(drive_service, folder_id):
        if file['name'].strip().replace(" ", "_").lower() == filename_normalized:
            return file["id"]
    return None

def export_mail_ie():
    try:
        write_log("🚀 Lancement export_mail_ie.py")
        client, drive_service, creds = get_google_services()
        if not client or not drive_service:
            write_log("❌ Connexion à Google Sheets/Drive échouée")
            return

        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            query = """
                SELECT code_VIF, nom_association, responsable_IE, tel_resp_IE,
                       courriel_resp_IE1, courriel_resp_IE2, CAR
                FROM associations
                WHERE validite ="oui"
                ORDER BY nom_association
            """
            df = pd.read_sql_query(query, conn)

        # 🔍 Recherche d’un fichier existant
        file_id = get_existing_spreadsheet_id(drive_service, FOLDER_ID, FILENAME)

        if file_id:
            sheet = client.open_by_key(file_id).sheet1
            sheet.clear()
            write_log(f"♻️ Fichier existant trouvé : {FILENAME} (ID={file_id}), contenu vidé.")
        else:
            spreadsheet = drive_service.files().create(
                body={
                    "name": FILENAME,
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "parents": [FOLDER_ID]
                },
                fields="id",
                supportsAllDrives=True
            ).execute()
            file_id = spreadsheet["id"]
            sheet = client.open_by_key(file_id).sheet1
            write_log(f"🆕 Nouveau fichier créé : {FILENAME} (ID={file_id})")

        # ✏️ Insertion des données
        sheet.insert_rows([df.columns.tolist()] + df.values.tolist())
        write_log(f"✅ Données insérées dans {FILENAME}")

        # 📐 Ajustement des colonnes
        from googleapiclient.discovery import build
        sheets_service = build("sheets", "v4", credentials=creds)
        requests = [{
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": 0,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": len(df.columns)
                }
            }
        }]
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=file_id,
            body={"requests": requests}
        ).execute()
        write_log("📐 Largeurs de colonnes ajustées automatiquement.")

    except Exception as e:
        write_log(f"❌ Erreur export_mail_ie : {e}")

if __name__ == "__main__":
    export_mail_ie()

