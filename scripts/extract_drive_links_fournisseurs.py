#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BA38 - Extraction des liens Drive (fournisseurs)
Auteur : ndprz
Date : 2025-11-05
Description :
  - Liste tous les sous-dossiers du dossier partagé "FOURNISSEURS dossiers individuels"
  - Génère un fichier Excel avec nom du dossier + lien Drive complet
"""

import sys, os
sys.path.append("/home/ndprz/dev")  # ou /home/ndprz/ba380 en prod
os.chdir("/home/ndprz/dev")

import pandas as pd
from utils import get_google_services, write_log

# --- CONFIGURATION ---
PARENT_FOLDER_ID = "1SOT2GopTrH4X7kkE6ENCV-h7r79TcT1C"  # BA380 - Public / FOURNISSEURS dossiers individuels
OUTPUT_XLSX = "/home/ndprz/exports/liste_dossiers_fournisseurs new.xlsx"


def list_subfolders(drive_service, parent_id):
    """Retourne la liste de tous les sous-dossiers (nom + id)"""
    results = []
    try:
        page_token = None
        while True:
            response = drive_service.files().list(
                q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="nextPageToken, files(id, name)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                pageToken=page_token
            ).execute()

            for f in response.get("files", []):
                results.append({
                    "nom_dossier": f["name"],
                    "lien_drive": f"https://drive.google.com/drive/folders/" + f["id"]
                })

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return results
    except Exception as e:
        write_log(f"❌ Erreur lors du listing des dossiers Drive : {e}")
        return []


def main():
    write_log("🚀 Extraction des dossiers Drive fournisseurs")

    client, drive_service, creds = get_google_services()
    if not drive_service:
        raise RuntimeError("❌ Connexion Google Drive impossible via get_google_services")

    write_log(f"🔍 Lecture des sous-dossiers du dossier parent {PARENT_FOLDER_ID}...")
    folders = list_subfolders(drive_service, PARENT_FOLDER_ID)
    write_log(f"✅ {len(folders)} sous-dossiers trouvés")

    if not folders:
        print("⚠️ Aucun dossier trouvé dans le dossier Drive parent.")
        return

    df = pd.DataFrame(folders)
    df.sort_values("nom_dossier", inplace=True)
    df.to_excel(OUTPUT_XLSX, index=False)

    write_log(f"✅ Fichier Excel généré : {OUTPUT_XLSX}")
    print(f"✅ Fichier Excel généré : {OUTPUT_XLSX}")


if __name__ == "__main__":
    main()
