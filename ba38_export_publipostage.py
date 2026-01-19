from utils import envoyer_mail, get_google_services, write_log, get_db_path
from flask import Blueprint, request, jsonify, current_app
import pandas as pd
import os
import re
import sqlite3
from googleapiclient.discovery import build

export_bp = Blueprint("export", __name__, url_prefix="/export_publipostage")

FOLDER_ID_ASSOCIATIONS = os.getenv("FOLDER_ID_ASSOCIATIONS")
FOLDER_ID_BENEVOLES = os.getenv("FOLDER_ID_BENEVOLES")


def is_valid_email(email):
    if not isinstance(email, str):
        return False
    email = email.strip()
    if not email or email.lower() == "none":
        return False
    if email.endswith("."):
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", email))


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


def create_spreadsheet_in_shared_drive(drive_service, folder_id, sheet_name):
    spreadsheet_metadata = {
        "name": sheet_name,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [folder_id]
    }
    spreadsheet = drive_service.files().create(
        body=spreadsheet_metadata,
        fields="id",
        supportsAllDrives=True
    ).execute()
    return spreadsheet["id"]


def export_dataframe_to_drive(df, sheet_name, client, drive_service, folder_id):
    if "email" in df.columns:
        df = df[df["email"].apply(is_valid_email)]
    if df.empty:
        write_log(f"⚠️ Aucun enregistrement à exporter pour {sheet_name}")
        return f"{sheet_name} : vide"
    file_id = get_existing_spreadsheet_id(drive_service, folder_id, sheet_name)
    if file_id:
        drive_service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        write_log(f"🗑️ Ancien fichier supprimé : {sheet_name}")
    file_id = create_spreadsheet_in_shared_drive(drive_service, folder_id, sheet_name)
    sheet = client.open_by_key(file_id).sheet1
    sheet.clear()
    sheet.insert_rows([df.columns.tolist()] + df.values.tolist())
    write_log(f"✅ Exporté : {sheet_name} ({len(df)} lignes)")
    return f"{sheet_name} : {len(df)} lignes"


@export_bp.route("/export_mail_ie", methods=["POST"])
def export_mail_ie_route():
    try:
        client, drive_service, creds = get_google_services()
        if not client or not drive_service:
            return jsonify({"message": "❌ Connexion Google échouée."}), 500

        FILENAME = "BASE_MAIL_IE"
        folder_id = FOLDER_ID_ASSOCIATIONS

        def get_existing_id():
            normalized = FILENAME.strip().replace(" ", "_").lower()
            for file in list_drive_files(drive_service, folder_id):
                if file['name'].strip().replace(" ", "_").lower() == normalized:
                    return file['id']
            return None

        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql_query("""
                SELECT code_VIF, nom_association, responsable_IE, tel_resp_IE,
                       courriel_resp_IE1, courriel_resp_IE2, CAR
                FROM associations
                WHERE validite = "oui"
                ORDER BY nom_association
            """, conn)

        file_id = get_existing_id()
        if file_id:
            sheet = client.open_by_key(file_id).sheet1
            sheet.clear()
        else:
            spreadsheet = drive_service.files().create(
                body={
                    "name": FILENAME,
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "parents": [folder_id]
                },
                fields="id",
                supportsAllDrives=True
            ).execute()
            file_id = spreadsheet["id"]
            sheet = client.open_by_key(file_id).sheet1

        sheet.insert_rows([df.columns.tolist()] + df.values.tolist())

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

        return jsonify({"message": f"✅ {FILENAME} mis à jour avec succès."})

    except Exception as e:
        return jsonify({"message": f"❌ Erreur : {str(e)}"}), 500


@export_bp.route("/all", methods=["POST"])
def export_all_publipostage():
    write_log("🚀 Export complet publipostage lancé")
    client, drive_service, _ = get_google_services()
    if not client or not drive_service:
        return jsonify({"message": "Erreur de connexion à Google."}), 500

    summary = []
    emails_invalides = {}

    try:
        conn = sqlite3.connect("/home/ndprz/ba380/ba380.sqlite")
        conn.row_factory = sqlite3.Row
        write_log("🔒 Connexion forcée à la base de PROD : /home/ndprz/ba380/ba380.sqlite")

        all_columns = [
            "courriel_association", "courriel_president", "courriel_resp_operationnel",
            "courriel_distribution", "courriel_resp_Hysa", "courriel_resp_tresorerie",
            "courriel_resp_IE1", "courriel_resp_IE2"
        ]
        df_all = pd.read_sql_query(
            f"SELECT nom_association, {','.join(all_columns)} FROM associations WHERE validite = 'oui'",
            conn
        )
        rows = []
        for _, row in df_all.iterrows():
            for col in all_columns:
                email = row.get(col, "")
                if is_valid_email(email):
                    rows.append({
                        "nom_association": str(row["nom_association"]),
                        "email": str(email).strip()
                    })
                elif email and email.strip().lower() != "none":
                    emails_invalides.setdefault("Publipostage_Assos_Tous_Les_Mails", []).append(
                        f"{row['nom_association']} → {email}"
                    )
        df_final = pd.DataFrame(rows).drop_duplicates(subset=['email'], keep='first')
        summary.append(export_dataframe_to_drive(df_final, "Publipostage_Assos_Tous_Les_Mails", client, drive_service, FOLDER_ID_ASSOCIATIONS))

        simple_fields = [
            "courriel_association", "courriel_president", "courriel_resp_operationnel",
            "courriel_distribution", "courriel_resp_Hysa", "courriel_resp_tresorerie"
        ]
        for champ in simple_fields:
            nom_fichier = f"Publipostage_Assos_{champ}"
            df = pd.read_sql_query(
                f"SELECT nom_association, {champ} as email FROM associations WHERE validite = 'oui'",
                conn
            )
            df["email"] = df["email"].astype(str).str.strip()
            for _, row in df.iterrows():
                if not is_valid_email(row["email"]) and row["email"].strip().lower() != "none":
                    emails_invalides.setdefault(nom_fichier, []).append(f"{row['nom_association']} → {row['email']}")
            df = df[df["email"].apply(is_valid_email)]
            df = df.drop_duplicates(subset=["email"], keep="first")
            summary.append(export_dataframe_to_drive(df, nom_fichier, client, drive_service, FOLDER_ID_ASSOCIATIONS))

        nom_fichier = "Publipostage_Assos_courriel_resp_IE"
        df_ie = pd.read_sql_query(
            "SELECT nom_association, courriel_resp_IE1, courriel_resp_IE2 FROM associations WHERE validite = 'oui'",
            conn
        )
        rows_ie = []
        for _, row in df_ie.iterrows():
            nom = str(row["nom_association"])
            for champ in ["courriel_resp_IE1", "courriel_resp_IE2"]:
                email = row.get(champ, "")
                if is_valid_email(email):
                    rows_ie.append({"nom_association": nom, "email": str(email).strip()})
                elif email and email.strip().lower() != "none":
                    emails_invalides.setdefault(nom_fichier, []).append(f"{nom} → {email}")
        df_ie_final = pd.DataFrame(rows_ie).drop_duplicates(subset=['email'], keep='first')
        summary.append(export_dataframe_to_drive(df_ie_final, nom_fichier, client, drive_service, FOLDER_ID_ASSOCIATIONS))

        df_b = pd.read_sql_query("SELECT civilite, nom, prenom, email FROM benevoles", conn)
        df_b["email"] = df_b["email"].astype(str).str.strip()
        df_b = df_b[df_b["email"].apply(is_valid_email)]
        df_b = df_b.drop_duplicates(subset=["email"], keep="first")
        summary.append(export_dataframe_to_drive(df_b, "Publipostage_Bénévoles", client, drive_service, FOLDER_ID_BENEVOLES))

        conn.close()

        if emails_invalides:
            corps = "Les adresses suivantes sont invalides et doivent être corrigées dans la fiche association :\n\n"
            for fichier, lignes in emails_invalides.items():
                corps += f"📂 {fichier} :\n"
                for ligne in lignes:
                    corps += f"   - {ligne}\n"
                corps += "\n"

            envoyer_mail(
                sujet="❌ Emails invalides dans les fichiers publipostage",
                destinataires=[
                    "ba380.informatique2@banquealimentaire.org",
                    "ba380.secretariat@banquealimentaire.org"
                ],
                texte=corps
            )

        # ✅ Enchaîne avec export_mail_ie_route
        try:
            with current_app.app_context():
                resp = export_mail_ie_route()
                if isinstance(resp, tuple):
                    summary.append(resp[0].json.get("message", "❌ Erreur export_mail_ie"))
                else:
                    summary.append(resp.json.get("message", "❌ Erreur export_mail_ie"))
        except Exception as e:
            summary.append(f"❌ Erreur appel export_mail_ie_route : {e}")

        return jsonify({"message": "\n".join(summary)}), 200

    except Exception as e:
        write_log(f"❌ Erreur export global : {e}")
        return jsonify({"message": f"❌ Erreur : {str(e)}"}), 500
