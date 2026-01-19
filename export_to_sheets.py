# import os
# import sqlite3
# import gspread
# from googleapiclient.discovery import build
# from google.oauth2.service_account import Credentials
# import pandas as pd

# from utils import get_db_path, write_log

# # ✅ Configuration Google
# SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
# basedir = os.path.abspath(os.path.dirname(__file__))
# SERVICE_ACCOUNT_FILE = os.path.join(basedir, "service_account.json")

# # 📁 ID des dossiers Google Drive
# FOLDER_ID_ASSOCIATIONS = "18UHHGeGn7kepW7YjOF0YBO0XcEPG_D4L"
# FOLDER_ID_BENEVOLES = "1Nog8k-r9xDuBvLbb8Fu-RKDghiKh4WSE"

# # ✅ Connexion Google
# def get_google_services():
#     if not os.path.exists(SERVICE_ACCOUNT_FILE):
#         raise FileNotFoundError(f"Fichier manquant : {SERVICE_ACCOUNT_FILE}")
#     try:
#         creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
#         client = gspread.authorize(creds)
#         drive_service = build("drive", "v3", credentials=creds)
#         write_log("✅ Connexion à Google Sheets et Drive réussie.")
#         return client, drive_service
#     except Exception as e:
#         write_log(f"❌ Erreur de connexion Google Sheets/Drive : {e}")
#         return None, None

# # ✅ Lecture des colonnes email (pour associations uniquement)
# def get_email_columns():
#     try:
#         with sqlite3.connect(get_db_path()) as conn:
#             cursor = conn.cursor()
#             query = "SELECT field_name FROM field_groups WHERE type_champ = 'email' AND appli = 'associations'"
#             cursor.execute(query)
#             return [row[0] for row in cursor.fetchall()]
#     except Exception as e:
#         write_log(f"❌ Erreur SQL lors de la récupération des emails : {e}")
#         return []

# # ✅ Liste les fichiers d’un dossier Drive
# def list_drive_files(drive_service, folder_id):
#     try:
#         results = drive_service.files().list(
#             q=f"'{folder_id}' in parents and trashed=false",
#             fields="files(id, name)",
#             includeItemsFromAllDrives=True,
#             supportsAllDrives=True
#         ).execute()
#         return results.get("files", [])
#     except Exception as e:
#         write_log(f"❌ Erreur list_drive_files({folder_id}) : {e}")
#         return []

# # ✅ Cherche un fichier existant
# def get_existing_spreadsheet_id(drive_service, folder_id, filename):
#     filename_normalized = filename.strip().replace(" ", "_").lower()
#     for file in list_drive_files(drive_service, folder_id):
#         if file['name'].strip().replace(" ", "_").lower() == filename_normalized:
#             return file["id"]
#     return None

# # ✅ Crée un fichier Google Sheets
# def create_spreadsheet_in_shared_drive(drive_service, folder_id, filename):
#     try:
#         metadata = {
#             "name": filename,
#             "mimeType": "application/vnd.google-apps.spreadsheet",
#             "parents": [folder_id]
#         }
#         file = drive_service.files().create(
#             body=metadata,
#             supportsAllDrives=True,
#             fields="id"
#         ).execute()
#         write_log(f"✅ Fichier `{filename}` créé avec ID: {file.get('id')}")
#         return file.get("id")
#     except Exception as e:
#         write_log(f"❌ Erreur create_spreadsheet({filename}) : {e}")
#         return None

# def export_to_google_sheets_assos():
#     write_log("📤 Export publipostage pour associations (multi-fichiers)")
#     client, drive_service = get_google_services()
#     if not client or not drive_service:
#         return "❌ Échec de la connexion Google"

#     try:
#         summary = []
#         db_path = get_db_path()
#         with sqlite3.connect(db_path) as conn:
#             def clean_and_filter(df, email_col):
#                 initial = len(df)
#                 df = df.copy()
#                 df[email_col] = df[email_col].astype(str).str.strip()

#                 vides = df[df[email_col] == '']
#                 df = df[df[email_col] != '']

#                 df_before_dupes = df.copy()
#                 df = df.drop_duplicates(subset=email_col)
#                 doublons = len(df_before_dupes) - len(df)

#                 mask_valid = df[email_col].str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", na=False)
#                 invalides = df[~mask_valid]
#                 df = df[mask_valid]

#                 cleaned = len(df)
#                 return df, initial, cleaned, len(vides), doublons, len(invalides)

#             full_fields = [
#                 "nom_association", "courriel_association", "courriel_president",
#                 "courriel_resp_operationnel", "courriel_resp_IE1", "courriel_resp_IE2",
#                 "courriel_distribution", "courriel_resp_Hysa", "courriel_resp_tresorerie"
#             ]
#             df_all = pd.read_sql_query(
#                 f"SELECT {', '.join(full_fields)} FROM associations", conn)
#             export_dataframe_to_drive(df_all, "Publipostage_Assos_Tous_Les_Mails", client, drive_service, FOLDER_ID_ASSOCIATIONS)
#             summary.append(f"TLM: {len(df_all)} lignes")

#             df_code = pd.read_sql_query(
#                 "SELECT nom_association, code_comptable FROM associations WHERE code_comptable IS NOT NULL", conn)
#             export_dataframe_to_drive(df_code, "Publipostage_Assos_Code_Comptable", client, drive_service, FOLDER_ID_ASSOCIATIONS)
#             summary.append(f"Code comptable: {len(df_code)} lignes")

#             champs_emails = [
#                 "courriel_association", "courriel_distribution", "courriel_president",
#                 "courriel_resp_Hysa", "courriel_resp_operationnel", "courriel_resp_tresorerie"
#             ]
#             for champ in champs_emails:
#                 df = pd.read_sql_query(
#                     f"SELECT nom_association, {champ} FROM associations",
#                     conn
#                 )
#                 df_clean, init, final, vides, doublons, invalides = clean_and_filter(df, champ)
#                 export_dataframe_to_drive(df_clean, f"Publipostage_Assos_{champ}", client, drive_service, FOLDER_ID_ASSOCIATIONS)
#                 summary.append(f"{champ}: {final}/{init} valides ({vides} vides, {doublons} doublons, {invalides} invalides)")

#             df_ie = pd.read_sql_query(
#                 "SELECT nom_association, courriel_resp_IE1, courriel_resp_IE2 FROM associations",
#                 conn
#             )
#             df_ie["courriel_IE"] = df_ie[["courriel_resp_IE1", "courriel_resp_IE2"]].fillna('').agg(';'.join, axis=1)
#             df_ie["courriel_IE"] = df_ie["courriel_IE"].str.replace(';;', ';').str.strip(';').str.strip()
#             df_ie_filtered = df_ie.loc[df_ie["courriel_IE"] != "", ["nom_association", "courriel_IE"]]
#             df_ie_clean, init, final, vides, doublons, invalides = clean_and_filter(df_ie_filtered, "courriel_IE")
#             export_dataframe_to_drive(df_ie_clean, "Publipostage_Assos_courriel_resp_IE", client, drive_service, FOLDER_ID_ASSOCIATIONS)
#             summary.append(f"courriel_resp_IE: {final}/{init} valides ({vides} vides, {doublons} doublons, {invalides} invalides)")

#         write_log("✅ Tous les fichiers publipostage associations ont été générés.")
#         return "\n".join(["✅ Exports réussis:"] + summary)

#     except Exception as e:
#         write_log(f"❌ Erreur dans export_to_google_sheets_assos : {e}")
#         return f"❌ Erreur : {str(e)}"


# def export_to_google_sheets_bene():
#     import re
#     write_log("📤 Export publipostage pour bénévoles")
#     client, drive_service = get_google_services()
#     if not client or not drive_service:
#         return "❌ Échec de la connexion Google"

#     try:
#         db_path = get_db_path()
#         with sqlite3.connect(db_path) as conn:
#             df = pd.read_sql_query(
#                 "SELECT civilite, nom, prenom, email FROM benevoles WHERE email IS NOT NULL AND email != ''",
#                 conn
#             )
#             initial = len(df)
#             df["email"] = df["email"].astype(str).str.strip()

#             vides = df[df["email"] == '']
#             df = df[df["email"] != '']

#             df_before_dupes = df.copy()
#             df = df.drop_duplicates(subset="email")
#             doublons = len(df_before_dupes) - len(df)

#             mask_valid = df["email"].str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", na=False)
#             invalides = df[~mask_valid]
#             df = df[mask_valid]

#             final = len(df)

#         sheet_name = "Publipostage_Bénévoles"
#         folder_id = FOLDER_ID_BENEVOLES

#         file_id = get_existing_spreadsheet_id(drive_service, folder_id, sheet_name)
#         if not file_id:
#             file_id = create_spreadsheet_in_shared_drive(drive_service, folder_id, sheet_name)

#         sheet = client.open_by_key(file_id).sheet1
#         sheet.clear()
#         sheet.insert_rows([df.columns.tolist()] + df.values.tolist())

#         message = f"✅ Bénévoles: {final}/{initial} valides ({len(vides)} vides, {doublons} doublons, {len(invalides)} invalides)"
#         write_log(message)
#         return message

#     except Exception as e:
#         write_log(f"❌ Erreur dans export_to_google_sheets_bene : {e}")
#         return f"❌ Erreur : {str(e)}"


# def export_dataframe_to_drive(df, sheet_name, client, drive_service, folder_id):
#     if df.empty:
#         write_log(f"⚠️ Aucun enregistrement pour {sheet_name}.")
#         return
#     file_id = get_existing_spreadsheet_id(drive_service, folder_id, sheet_name)
#     if not file_id:
#         file_id = create_spreadsheet_in_shared_drive(drive_service, folder_id, sheet_name)
#     sheet = client.open_by_key(file_id).sheet1
#     sheet.clear()
#     sheet.insert_rows([df.columns.tolist()] + df.values.tolist())
#     write_log(f"✅ Export terminé : {sheet_name}")

import os
import gspread
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import pandas as pd

from utils import write_log, get_db_connection

# ✅ Configuration Google
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
basedir = os.path.abspath(os.path.dirname(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(basedir, "service_account.json")

# 📁 ID des dossiers Google Drive
FOLDER_ID_ASSOCIATIONS = "18UHHGeGn7kepW7YjOF0YBO0XcEPG_D4L"
FOLDER_ID_BENEVOLES = "1Nog8k-r9xDuBvLbb8Fu-RKDghiKh4WSE"

# ✅ Connexion Google
def get_google_services():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Fichier manquant : {SERVICE_ACCOUNT_FILE}")
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        client = gspread.authorize(creds)
        drive_service = build("drive", "v3", credentials=creds)
        write_log("✅ Connexion à Google Sheets et Drive réussie.")
        return client, drive_service
    except Exception as e:
        write_log(f"❌ Erreur de connexion Google Sheets/Drive : {e}")
        return None, None

# ✅ Lecture des colonnes email (pour associations uniquement)
def get_email_columns():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT field_name FROM field_groups WHERE type_champ = 'email' AND appli = 'associations'"
            cursor.execute(query)
            return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        write_log(f"❌ Erreur SQL lors de la récupération des emails : {e}")
        return []

# ✅ Liste les fichiers d’un dossier Drive
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

# ✅ Cherche un fichier existant
def get_existing_spreadsheet_id(drive_service, folder_id, filename):
    filename_normalized = filename.strip().replace(" ", "_").lower()
    for file in list_drive_files(drive_service, folder_id):
        if file['name'].strip().replace(" ", "_").lower() == filename_normalized:
            return file["id"]
    return None

# ✅ Crée un fichier Google Sheets
def create_spreadsheet_in_shared_drive(drive_service, folder_id, filename):
    try:
        metadata = {
            "name": filename,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [folder_id]
        }
        file = drive_service.files().create(
            body=metadata,
            supportsAllDrives=True,
            fields="id"
        ).execute()
        write_log(f"✅ Fichier `{filename}` créé avec ID: {file.get('id')}")
        return file.get("id")
    except Exception as e:
        write_log(f"❌ Erreur create_spreadsheet({filename}) : {e}")
        return None

def export_to_google_sheets_assos():
    write_log("📤 Export publipostage pour associations (multi-fichiers)")
    client, drive_service = get_google_services()
    if not client or not drive_service:
        return "❌ Échec de la connexion Google"

    try:
        summary = []
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            def clean_and_filter(df, email_col):
                initial = len(df)
                df = df.copy()
                df[email_col] = df[email_col].astype(str).str.strip()

                vides = df[df[email_col] == '']
                df = df[df[email_col] != '']

                df_before_dupes = df.copy()
                df = df.drop_duplicates(subset=email_col)
                doublons = len(df_before_dupes) - len(df)

                mask_valid = df[email_col].str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", na=False)
                invalides = df[~mask_valid]
                df = df[mask_valid]

                cleaned = len(df)
                return df, initial, cleaned, len(vides), doublons, len(invalides)

            full_fields = [
                "nom_association", "courriel_association", "courriel_president",
                "courriel_resp_operationnel", "courriel_resp_IE1", "courriel_resp_IE2",
                "courriel_distribution", "courriel_resp_Hysa", "courriel_resp_tresorerie"
            ]
            df_all = pd.read_sql_query(
                f"SELECT {', '.join(full_fields)} FROM associations", conn)
            export_dataframe_to_drive(df_all, "Publipostage_Assos_Tous_Les_Mails", client, drive_service, FOLDER_ID_ASSOCIATIONS)
            summary.append(f"TLM: {len(df_all)} lignes")

            df_code = pd.read_sql_query(
                "SELECT nom_association, code_comptable FROM associations WHERE code_comptable IS NOT NULL", conn)
            export_dataframe_to_drive(df_code, "Publipostage_Assos_Code_Comptable", client, drive_service, FOLDER_ID_ASSOCIATIONS)
            summary.append(f"Code comptable: {len(df_code)} lignes")

            champs_emails = [
                "courriel_association", "courriel_distribution", "courriel_president",
                "courriel_resp_Hysa", "courriel_resp_operationnel", "courriel_resp_tresorerie"
            ]
            for champ in champs_emails:
                df = pd.read_sql_query(
                    f"SELECT nom_association, {champ} FROM associations",
                    conn
                )
                df_clean, init, final, vides, doublons, invalides = clean_and_filter(df, champ)
                export_dataframe_to_drive(df_clean, f"Publipostage_Assos_{champ}", client, drive_service, FOLDER_ID_ASSOCIATIONS)
                summary.append(f"{champ}: {final}/{init} valides ({vides} vides, {doublons} doublons, {invalides} invalides)")

            df_ie = pd.read_sql_query(
                "SELECT nom_association, courriel_resp_IE1, courriel_resp_IE2 FROM associations",
                conn
            )
            df_ie["courriel_IE"] = df_ie[["courriel_resp_IE1", "courriel_resp_IE2"]].fillna('').agg(';'.join, axis=1)
            df_ie["courriel_IE"] = df_ie["courriel_IE"].str.replace(';;', ';').str.strip(';').str.strip()
            df_ie_filtered = df_ie.loc[df_ie["courriel_IE"] != "", ["nom_association", "courriel_IE"]]
            df_ie_clean, init, final, vides, doublons, invalides = clean_and_filter(df_ie_filtered, "courriel_IE")
            export_dataframe_to_drive(df_ie_clean, "Publipostage_Assos_courriel_resp_IE", client, drive_service, FOLDER_ID_ASSOCIATIONS)
            summary.append(f"courriel_resp_IE: {final}/{init} valides ({vides} vides, {doublons} doublons, {invalides} invalides)")

        write_log("✅ Tous les fichiers publipostage associations ont été générés.")
        return "\n".join(["✅ Exports réussis:"] + summary)

    except Exception as e:
        write_log(f"❌ Erreur dans export_to_google_sheets_assos : {e}")
        return f"❌ Erreur : {str(e)}"


def export_to_google_sheets_bene():
    import re
    write_log("📤 Export publipostage pour bénévoles")
    client, drive_service = get_google_services()
    if not client or not drive_service:
        return "❌ Échec de la connexion Google"

    try:
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql_query(
                "SELECT civilite, nom, prenom, email FROM benevoles WHERE email IS NOT NULL AND email != ''",
                conn
            )
            initial = len(df)
            df["email"] = df["email"].astype(str).str.strip()

            vides = df[df["email"] == '']
            df = df[df["email"] != '']

            df_before_dupes = df.copy()
            df = df.drop_duplicates(subset="email")
            doublons = len(df_before_dupes) - len(df)

            mask_valid = df["email"].str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", na=False)
            invalides = df[~mask_valid]
            df = df[mask_valid]

            final = len(df)

        sheet_name = "Publipostage_Bénévoles"
        folder_id = FOLDER_ID_BENEVOLES

        file_id = get_existing_spreadsheet_id(drive_service, folder_id, sheet_name)
        if not file_id:
            file_id = create_spreadsheet_in_shared_drive(drive_service, folder_id, sheet_name)

        sheet = client.open_by_key(file_id).sheet1
        sheet.clear()
        sheet.insert_rows([df.columns.tolist()] + df.values.tolist())

        message = f"✅ Bénévoles: {final}/{initial} valides ({len(vides)} vides, {doublons} doublons, {len(invalides)} invalides)"
        write_log(message)
        return message

    except Exception as e:
        write_log(f"❌ Erreur dans export_to_google_sheets_bene : {e}")
        return f"❌ Erreur : {str(e)}"


def export_dataframe_to_drive(df, sheet_name, client, drive_service, folder_id):
    if df.empty:
        write_log(f"⚠️ Aucun enregistrement pour {sheet_name}.")
        return
    file_id = get_existing_spreadsheet_id(drive_service, folder_id, sheet_name)
    if not file_id:
        file_id = create_spreadsheet_in_shared_drive(drive_service, folder_id, sheet_name)
    sheet = client.open_by_key(file_id).sheet1
    sheet.clear()
    sheet.insert_rows([df.columns.tolist()] + df.values.tolist())
    write_log(f"✅ Export terminé : {sheet_name}")

