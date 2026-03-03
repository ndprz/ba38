from flask import Blueprint, request, jsonify, current_app
from utils import envoyer_mail, get_google_services, write_log
import pandas as pd
import os
import re
import sqlite3
import unicodedata
import subprocess
import time
from gspread.exceptions import APIError

from googleapiclient.discovery import build


# ============================================================================
# Blueprint
# ============================================================================
export_bp = Blueprint("export", __name__, url_prefix="/export_publipostage")

# ============================================================================
# Configuration (FORCÉE PROD)
# ============================================================================
FOLDER_ID_ASSOCIATIONS = os.getenv("FOLDER_ID_ASSOCIATIONS")
FOLDER_ID_BENEVOLES = os.getenv("FOLDER_ID_BENEVOLES")

PUBLIPOSTAGE_DB_PATH = os.getenv("PUBLIPOSTAGE_DB_PATH")
if not PUBLIPOSTAGE_DB_PATH:
    raise RuntimeError("PUBLIPOSTAGE_DB_PATH non défini")

# ============================================================================
# Outils
# ============================================================================
import re

def is_valid_email(email):

    if not isinstance(email, str):
        return False

    email = email.strip()

    if not email:
        return False

    if email.lower() == "none":
        return False

    # Refuser tout caractère non ASCII
    try:
        email.encode("ascii")
    except UnicodeEncodeError:
        return False

    # Regex stricte ASCII
    pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"

    return bool(re.match(pattern, email))

@export_bp.route("/trigger", methods=["POST"])
def trigger_publipostage_cron():
    try:
        cmd = [
            "/srv/ba38/prod/venv/bin/python",
            "/srv/ba38/scripts_taches/export_publipostage_nuit.py"
        ]

        subprocess.Popen(
            cmd,
            stdout=open("/srv/ba38/prod/logs/cron_publipostage.log", "a"),
            stderr=open("/srv/ba38/prod/logs/cron_publipostage.log", "a"),
            start_new_session=True
        )

        return jsonify({
            "message": "⏳ Export publipostage lancé (traitement en arrière-plan).\n"
                       "Les fichiers seront mis à jour dans quelques instants."
        })

    except Exception as e:
        return jsonify({
            "message": f"❌ Erreur lancement export : {e}"
        }), 500

def open_sheet_with_retry(client, file_id, retries=5):
    """
    Ouvre un Google Sheet avec retry automatique
    en cas d'erreur API (502, timeout, etc.).
    """
    for attempt in range(retries):
        try:
            return client.open_by_key(file_id).sheet1
        except APIError as e:
            wait = 5 * (attempt + 1)
            write_log(
                f"⚠️ Google API erreur (tentative {attempt+1}/{retries}) : {e}"
            )

            if attempt < retries - 1:
                write_log(f"⏳ Retry dans {wait}s...")
                time.sleep(wait)
            else:
                write_log("❌ Échec définitif après retries Google.")
                raise


def export_all_publipostage_job():
    """
    Export publipostage complet – version batch / cron (sans Flask).
    Utilise exclusivement la base PROD forcée.
    Retourne une liste de messages (summary).
    """

    import json

    write_log("🚀 Export complet publipostage lancé (job)")

    client, drive_service, _ = get_google_services()
    if not client or not drive_service:
        raise RuntimeError("Erreur de connexion à Google Sheets / Drive")

    summary = []
    emails_invalides = {}

    conn = sqlite3.connect(PUBLIPOSTAGE_DB_PATH)
    conn.row_factory = sqlite3.Row
    write_log(f"🔒 Connexion forcée publipostage : {PUBLIPOSTAGE_DB_PATH}")

    try:
        # ============================================================
        # ASSOCIATIONS — Tous les emails (déduplication robuste)
        # ============================================================

        all_columns = [
            "courriel_association",
            "courriel_president",
            "courriel_resp_operationnel",
            "courriel_distribution",
            "courriel_resp_Hysa",
            "courriel_resp_tresorerie",
            "courriel_resp_IE1",
            "courriel_resp_IE2",
        ]

        df_all = pd.read_sql_query(
            f"""
            SELECT nom_association, {','.join(all_columns)}
            FROM associations
            WHERE validite = 'oui'
            """,
            conn
        )

        emails_uniques = set()
        rows = []

        for _, row in df_all.iterrows():
            nom = str(row["nom_association"])

            for col in all_columns:
                email = row[col]

                if is_valid_email(email):
                    email_clean = str(email).strip().lower()

                    if email_clean not in emails_uniques:
                        emails_uniques.add(email_clean)

                        rows.append({
                            "nom_association": nom,
                            "email": email_clean
                        })

                elif email and str(email).strip().lower() != "none":
                    emails_invalides.setdefault(
                        "Publipostage_Assos_Tous_Les_Mails", []
                    ).append(f"{nom} → {email}")

        df_final = pd.DataFrame(rows)

        summary.append(
            export_dataframe_to_drive(
                df_final,
                "Publipostage_Assos_Tous_Les_Mails",
                client,
                drive_service,
                FOLDER_ID_ASSOCIATIONS
            )
        )

        # ============================================================
        # ASSOCIATIONS — Fichiers par champ
        # ============================================================
        simple_fields = [
            "courriel_association",
            "courriel_president",
            "courriel_resp_operationnel",
            "courriel_distribution",
            "courriel_resp_Hysa",
            "courriel_resp_tresorerie",
        ]

        for champ in simple_fields:
            nom_fichier = f"Publipostage_Assos_{champ}"

            df = pd.read_sql_query(
                f"""
                SELECT nom_association, {champ} AS email
                FROM associations
                WHERE validite = 'oui'
                """,
                conn
            )

            df["email"] = df["email"].astype(str).str.strip()

            for _, row in df.iterrows():
                if not is_valid_email(row["email"]) and row["email"].lower() != "none":
                    emails_invalides.setdefault(nom_fichier, []).append(
                        f"{row['nom_association']} → {row['email']}"
                    )

            df = df[df["email"].apply(is_valid_email)]
            df = df.drop_duplicates(subset=["email"], keep="first")

            summary.append(
                export_dataframe_to_drive(
                    df,
                    nom_fichier,
                    client,
                    drive_service,
                    FOLDER_ID_ASSOCIATIONS
                )
            )

        # ============================================================
        # ASSOCIATIONS — Responsables IE (IE1 + IE2 fusionnés)
        # ============================================================
        nom_fichier_ie = "Publipostage_Assos_courriel_resp_IE"

        df_ie = pd.read_sql_query(
            """
            SELECT nom_association, courriel_resp_IE1, courriel_resp_IE2
            FROM associations
            WHERE validite = 'oui'
            """,
            conn
        )

        rows_ie = []
        for _, row in df_ie.iterrows():
            nom = str(row["nom_association"])
            for champ in ["courriel_resp_IE1", "courriel_resp_IE2"]:
                email = row[champ]
                if is_valid_email(email):
                    rows_ie.append({
                        "nom_association": nom,
                        "email": str(email).strip()
                    })
                elif email and str(email).strip().lower() != "none":
                    emails_invalides.setdefault(nom_fichier_ie, []).append(
                        f"{nom} → {email}"
                    )

        df_ie_final = (
            pd.DataFrame(rows_ie)
            .drop_duplicates(subset=["email"], keep="first")
        )

        summary.append(
            export_dataframe_to_drive(
                df_ie_final,
                nom_fichier_ie,
                client,
                drive_service,
                FOLDER_ID_ASSOCIATIONS
            )
        )

        # ============================================================
        # BÉNÉVOLES
        # ============================================================
        df_b = pd.read_sql_query(
            """
            SELECT civilite, nom, prenom, email
            FROM benevoles
            """,
            conn
        )

        df_b["email"] = df_b["email"].astype(str).str.strip()
        df_b = df_b[df_b["email"].apply(is_valid_email)]
        df_b = df_b.drop_duplicates(subset=["email"], keep="first")

        summary.append(
            export_dataframe_to_drive(
                df_b,
                "Publipostage_Bénévoles",
                client,
                drive_service,
                FOLDER_ID_BENEVOLES
            )
        )

        # ============================================================
        # ASSOCIATIONS — BASE MAIL IE
        # ============================================================

        nom_fichier_ie_base = "BASE_MAIL_IE"

        df_ie_base = pd.read_sql_query("""
            SELECT code_VIF, nom_association, responsable_IE,
                tel_resp_IE, courriel_resp_IE1,
                courriel_resp_IE2, CAR
            FROM associations
            WHERE validite = 'oui'
            ORDER BY nom_association
        """, conn)

        for index, row in df_ie_base.iterrows():
            for champ in ["courriel_resp_IE1", "courriel_resp_IE2"]:
                email = row[champ]

                if email and str(email).strip().lower() != "none":
                    if not is_valid_email(email):
                        emails_invalides.setdefault(
                            nom_fichier_ie_base, []
                        ).append(
                            f"{row['nom_association']} → {champ} : {email}"
                        )
                        df_ie_base.at[index, champ] = ""
                    else:
                        df_ie_base.at[index, champ] = str(email).strip()
                else:
                    df_ie_base.at[index, champ] = ""

        summary.append(
            export_dataframe_to_drive(
                df_ie_base,
                nom_fichier_ie_base,
                client,
                drive_service,
                FOLDER_ID_ASSOCIATIONS
            )
        )


        # ============================================================
        # ASSOCIATIONS — MÉTROPOLE (filtrage codes JSON)
        # ============================================================

        import json
        import unicodedata

        nom_fichier_metropole = "Publipostage_Assos_Metropole"

        json_path = "/srv/ba38/data/codes_postaux_metropole.json"

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                codes_metropole = set(json.load(f)["codes_postaux"])
        except Exception as e:
            write_log(f"❌ Erreur lecture JSON métropole : {e}")
            codes_metropole = set()

        df_metro = pd.read_sql_query(
            """
            SELECT nom_association,
                   CP,
                   courriel_resp_operationnel,
                   courriel_president
            FROM associations
            WHERE validite = 'oui'
            """,
            conn
        )

        rows_metro = []
        emails_uniques_metro = set()

        for _, row in df_metro.iterrows():

            cp = str(row["CP"]).strip()[:5]

            if cp not in codes_metropole:
                continue

            nom = str(row["nom_association"])

            for champ in [
                "courriel_resp_operationnel",
                "courriel_president",
                "couriel_distribution"
            ]:

                email = row[champ]

                if is_valid_email(email):
                    email_clean = str(email).strip().lower()

                    if email_clean not in emails_uniques_metro:
                        emails_uniques_metro.add(email_clean)

                        rows_metro.append({
                            "nom_association": nom,
                            "email": email_clean
                        })

                elif email and str(email).strip().lower() != "none":
                    emails_invalides.setdefault(
                        nom_fichier_metropole, []
                    ).append(f"{nom} → {email}")

        df_metro_final = pd.DataFrame(rows_metro)

        # 🔤 Tri alphabétique sans accents
        if not df_metro_final.empty:

            df_metro_final["_sort"] = (
                df_metro_final["nom_association"]
                .astype(str)
                .apply(lambda x: unicodedata.normalize("NFKD", x)
                       .encode("ascii", "ignore")
                       .decode("ascii")
                       .lower())
            )

            df_metro_final = df_metro_final.sort_values("_sort")
            df_metro_final = df_metro_final.drop(columns=["_sort"])

        summary.append(
            export_dataframe_to_drive(
                df_metro_final,
                nom_fichier_metropole,
                client,
                drive_service,
                FOLDER_ID_ASSOCIATIONS
            )
        )



        # ===============================
        # EMAIL DE SYNTHÈSE
        # ===============================

        if emails_invalides:

            nb_erreurs = sum(len(v) for v in emails_invalides.values())

            corps = (
                "Les adresses suivantes sont invalides et doivent être corrigées "
                "dans la fiche association :\n\n"
            )

            for fichier, lignes in emails_invalides.items():
                corps += f"📂 {fichier} :\n"
                for ligne in lignes:
                    corps += f"  - {ligne}\n"
                corps += "\n"

            envoyer_mail(
                sujet=f"❌ {nb_erreurs} Emails invalides – Publipostage 🚨 [PROD]",
                destinataires=[
                    "ba380.informatique2@banquealimentaire.org",
                    "ba380.secretariat@banquealimentaire.org",
                ],
                texte=corps
            )

            write_log(f"📧 Mail d’alerte envoyé ({nb_erreurs} erreurs)")

        write_log("🏁 Export publipostage terminé")
        return summary

    finally:
        conn.close()


def list_drive_files(drive_service, container_id):
    try:
        container_id = container_id.strip()

        # Racine drive partagé
        if container_id.startswith("0A"):
            results = drive_service.files().list(
                corpora="drive",
                driveId=container_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                q="trashed=false",
                fields="files(id, name)"
            ).execute()
        # Dossier classique
        else:
            results = drive_service.files().list(
                q=f"'{container_id}' in parents and trashed=false",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields="files(id, name)"
            ).execute()

        return results.get("files", [])

    except Exception as e:
        write_log(f"❌ Erreur list_drive_files({container_id}) : {e}")
        return []


def get_existing_spreadsheet_id(drive_service, container_id, filename):
    normalized = filename.strip().replace(" ", "_").lower()
    for file in list_drive_files(drive_service, container_id):
        if file["name"].strip().replace(" ", "_").lower() == normalized:
            return file["id"]
    return None


def create_spreadsheet_in_shared_drive(drive_service, folder_id, sheet_name):
    metadata = {
        "name": sheet_name,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [folder_id]   # ✅ TOUJOURS
    }

    spreadsheet = drive_service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True
    ).execute()

    return spreadsheet["id"]

def export_dataframe_to_drive(df, sheet_name, client, drive_service, container_id):
    try:
        if "email" in df.columns:
            df = df[df["email"].apply(is_valid_email)]

        if df.empty:
            write_log(f"⚠️ Aucun enregistrement à exporter pour {sheet_name}")
            return f"{sheet_name} : vide"

        file_id = get_existing_spreadsheet_id(
            drive_service,
            container_id,
            sheet_name
        )

        if file_id:
            drive_service.files().delete(
                fileId=file_id,
                supportsAllDrives=True
            ).execute()
            write_log(f"🗑️ Ancien fichier supprimé : {sheet_name}")

        file_id = create_spreadsheet_in_shared_drive(
            drive_service,
            container_id,
            sheet_name
        )

        # 🔒 ouverture sécurisée
        sheet = open_sheet_with_retry(client, file_id)

        sheet.clear()
        sheet.insert_rows(
            [df.columns.tolist()] + df.values.tolist()
        )

        write_log(f"✅ Exporté : {sheet_name} ({len(df)} lignes)")
        return f"{sheet_name} : {len(df)} lignes"

    except Exception as e:
        write_log(f"❌ ERREUR export {sheet_name} : {e}")
        return f"❌ {sheet_name} : ERREUR"



# ============================================================================
# ROUTES FLASK
# ============================================================================

@export_bp.route("/all", methods=["POST"])
def export_all_publipostage():
    try:
        summary = export_all_publipostage_job()
        return jsonify({"message": "\n".join(summary)})
    except Exception as e:
        write_log(f"❌ Erreur export global : {e}")
        return jsonify({"message": f"❌ Erreur : {e}"}), 500


@export_bp.route("/last_summary", methods=["GET"])
def last_summary():
    try:
        with open("/srv/ba38/prod/logs/last_publipostage_summary.txt") as f:
            content = f.read()
        return jsonify({"summary": content})
    except:
        return jsonify({"summary": "Aucun rapport disponible."})

