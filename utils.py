import os
import sqlite3
import gspread
import re
import logging

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.discovery_cache.base import Cache
from google.oauth2 import service_account
from google.oauth2.service_account import Credentials
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit
from flask import send_file
from pathlib import Path

import sqlite3
_real_connect = sqlite3.connect





FOLDER_ID_ASSOCIATIONS = os.getenv("FOLDER_ID_ASSOCIATIONS")
FOLDER_ID_BENEVOLES = os.getenv("FOLDER_ID_BENEVOLES")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]



VERSION = os.getenv("VERSION", "0.0.0")

def get_version():
    return VERSION


# Constantes extraites du .env
GDRIVE_DB_FOLDER_ID = os.getenv("GDRIVE_DB_FOLDER_ID")
GDRIVE_DB_FILE_ID_PROD = os.getenv("GDRIVE_DB_FILE_ID_PROD")
GDRIVE_DB_FILE_ID_DEV = os.getenv("GDRIVE_DB_FILE_ID_DEV")
GDRIVE_DB_FILE_ID_TEST = os.getenv("GDRIVE_DB_FILE_ID_TEST")
GDRIVE_DB_FILE_ID_DEV_TEST = os.getenv("GDRIVE_DB_FILE_ID_DEV_TEST")
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"
SQLITE_TEST_DB = os.getenv("SQLITE_TEST_DB")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")



logger = logging.getLogger("BA38")

def write_log(message: str):
    logger.info(message)


# def write_log(message, level="INFO"):
#     """
#     Logging robuste compatible PythonAnywhere ET serveur Debian.
#     Ne doit JAMAIS lever d'exception.
#     """
#     print(f"LOG_FILE = {LOG_FILE}")

#     try:
#         timestamp = datetime.now().isoformat(sep=" ", timespec="seconds")
#         log_message = f"[{timestamp}] {message}"
#     except Exception:
#         return

#     # 1️⃣ stdout (PA + journald)
#     try:
#         print(log_message)
#         sys.stdout.flush()
#     except Exception:
#         pass

#     # 2️⃣ fichier app.log (source de vérité)
#     try:
#         with open(LOG_FILE, "a", encoding="utf-8") as f:
#             f.write(log_message + "\n")
#     except Exception:
#         pass

#     # 3️⃣ logging standard (Gunicorn / Cockpit)
#     try:
#         if level == "ERROR":
#             logger.error(message)
#         elif level == "WARNING":
#             logger.warning(message)
#         elif level == "DEBUG":
#             logger.debug(message)
#         else:
#             logger.info(message)
#     except Exception:
#         pass

# Connexion à la base de données SQLite
def get_db_connection():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# Vérification validité des emails
def is_valid_email(email: str) -> bool:
    """
    Vérifie qu'une adresse e-mail est syntaxiquement valide.
    Rejette les espaces internes et utilise une expression régulière stricte.
    """
    email = email.strip()
    if " " in email:
        return False
    # Regex stricte mais raisonnable pour usage courant
    pattern = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
    return re.match(pattern, email) is not None


#Vérification de la validité des téléphones
def is_valid_phone(phone):
    """
    Valide un numéro de téléphone français :
    - 10 chiffres exacts après nettoyage (hors indicatif),
    - autorise les espaces, tirets, parenthèses, +,
    - rejette tout ce qui contient autre chose.
    """
    if not phone:
        return True  # Champ facultatif

    # Supprimer tous les caractères non-chiffres
    digits = re.sub(r"\D", "", phone)

    # ✅ Accepte exactement 10 chiffres (ex: 0612345678, 0476041234)
    return len(digits) == 10





#Path des bases de données
# utils.py

def get_db_path():
    import os

    # 🔒 GARDE-FOU CRITIQUE ENVIRONNEMENT
    env = os.getenv("ENVIRONMENT")
    base_dir = os.getenv("BA38_BASE_DIR")

    if env == "prod" and base_dir and "dev" in base_dir:
        raise RuntimeError(
            "⛔ ERREUR CRITIQUE : ENVIRONMENT=prod mais BA38_BASE_DIR pointe vers /dev"
        )

    if env == "dev" and base_dir and "ba380" in base_dir:
        raise RuntimeError(
            "⛔ ERREUR CRITIQUE : ENVIRONMENT=dev mais BA38_BASE_DIR pointe vers PROD"
        )

    try:
        from flask import session
        test_mode = session.get("test_user", False)
    except RuntimeError:
        test_mode = os.getenv("TEST_MODE") == "1"

    env = os.getenv("ENVIRONMENT", "prod").lower()

    if env == "dev" and test_mode:
        filename = os.getenv("SQLITE_DB_DEV_TEST")
    elif env == "dev":
        filename = os.getenv("SQLITE_DB_DEV")
    elif test_mode:
        filename = os.getenv("SQLITE_DB_PROD_TEST")
    else:
        filename = os.getenv("SQLITE_DB_PROD")

    if not filename:
        raise ValueError("❌ Nom de base SQLite non défini")

    base_dir = os.getenv("BA38_BASE_DIR")
    if not base_dir:
        raise RuntimeError("❌ BA38_BASE_DIR non défini dans .env")

    return os.path.join(base_dir, filename)



# Fonction utilisée pour les connexions actives de admin_scripts
def get_db_path_by_env(env: str) -> str:
    env = env.lower()
    test_mode = os.getenv("TEST_MODE") == "1"

    if env == "dev" and test_mode:
        filename = os.getenv("SQLITE_DB_DEV_TEST")
    elif env == "dev":
        filename = os.getenv("SQLITE_DB_DEV")
    elif test_mode:
        filename = os.getenv("SQLITE_DB_PROD_TEST")
    else:
        filename = os.getenv("SQLITE_DB_PROD")

    if not filename:
        raise ValueError(f"❌ SQLITE_DB non défini pour {env}")

    base_dir = os.getenv("BA38_BASE_DIR")
    if not base_dir:
        raise RuntimeError("❌ BA38_BASE_DIR non défini")

    return os.path.join(base_dir, filename)

def get_drive_folder_id_from_path(service, path, shared_drive_id):
    """
    Résout un chemin Drive dans un Drive partagé
    SANS créer les dossiers manquants.
    Retourne None si le chemin n'existe pas.
    """
    parts = path.strip("/").split("/")
    current_parent = shared_drive_id

    for part in parts:
        query = (
            f"name = '{part}' and "
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"trashed = false and "
            f"'{current_parent}' in parents"
        )

        results = service.files().list(
            q=query,
            corpora="drive",
            driveId=shared_drive_id,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id, name)"
        ).execute()

        files = results.get("files", [])
        if not files:
            return None

        current_parent = files[0]["id"]

    return current_parent


# Authentification PyDrive2 avec service account
def get_drive():
    gauth = GoogleAuth()
    gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return GoogleDrive(gauth)

# Suppression robuste via API Google Drive
def delete_file_directly(file_id):
    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        class NoCache(Cache):
            def get(self, url): return None
            def set(self, url, content): pass

        service = build('drive', 'v3', credentials=credentials, cache=NoCache())
        service.files().delete(fileId=file_id, supportsAllDrives=True).execute()

        write_log(f"🗑️ Suppression directe réussie pour le fichier ID: {file_id}")
    except Exception as e:
        write_log(f"❌ Erreur suppression directe fichier {file_id} : {e}")

# Sauvegarde automatique d'un fichier (ajoute un timestamp au nom)
def backup_file(local_path):
    backup_path = local_path + ".bak." + datetime.now().strftime("%Y%m%d%H%M%S")
    try:
        os.rename(local_path, backup_path)
        write_log(f"📦 Fichier local sauvegardé : {backup_path}")
    except Exception as e:
        write_log(f"❌ Erreur sauvegarde fichier local : {e}")


from googleapiclient.http import MediaFileUpload


def upload_database():
    """
    Upload de la base SQLite vers Google Drive.
    Version verrouillée : impossible d'appeler Drive sans file_id.
    Compatible PythonAnywhere + Debian.
    """
    from utils import write_log
    import os

    write_log("🚨 upload_database VERSION 3 VERROUILLÉE APPELÉE")

    # ============================
    # 1️⃣ Détermination du contexte
    # ============================
    local_path = get_db_path()

    env = os.getenv("ENVIRONMENT", "dev").lower()

    try:
        from flask import session
        test_mode = bool(session.get("test_user", False))
    except Exception:
        test_mode = False

    fid_dev = os.getenv("GDRIVE_DB_FILE_ID_DEV")
    fid_dev_test = os.getenv("GDRIVE_DB_FILE_ID_DEV_TEST")
    fid_test = os.getenv("GDRIVE_DB_FILE_ID_TEST")
    fid_prod = os.getenv("GDRIVE_DB_FILE_ID_PROD")

    if env == "prod":
        file_id = fid_prod
    elif env == "dev" and test_mode:
        file_id = fid_dev_test
    elif env == "dev":
        file_id = fid_dev
    elif test_mode:
        file_id = fid_test
    else:
        file_id = None

    write_log(f"🔍 upload_database debug → env={env}, test_mode={test_mode}, file_id={file_id!r}")

    # ============================
    # 🛑 GARDE-FOU ABSOLU
    # ============================
    if not file_id:
        write_log("⛔ upload_database STOP : file_id manquant → aucun appel Google Drive")
        return

    # ============================
    # 2️⃣ APPEL GOOGLE DRIVE
    # ============================
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.discovery_cache.base import Cache

        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/drive"]
        )

        class NoCache(Cache):
            def get(self, url): return None
            def set(self, url, content): pass

        service = build("drive", "v3", credentials=credentials, cache=NoCache())

        media = MediaFileUpload(
            local_path,
            mimetype="application/x-sqlite3",
            resumable=True
        )

        write_log(
            f"🧪 file_id type={type(file_id)} "
            f"len={len(file_id) if file_id is not None else 'None'} "
            f"repr={file_id!r}"
        )

        updated_file = service.files().update(
            fileId=file_id,
            media_body=media,
            supportsAllDrives=True
        ).execute()

        write_log(f"✅ Base envoyée sur Drive (id={updated_file.get('id')})")

    except Exception as e:
        write_log(f"❌ Erreur Google Drive upload_database() : {e}")


def get_db_info():
    """Retourne un résumé de la base actuellement utilisée"""
    from flask import session
    env = os.getenv("ENVIRONMENT", "prod").upper()
    test = session.get("test_user", False)
    mode = "TEST" if test else "NORMAL"

    path = get_db_path()
    filename = os.path.basename(path)

    return f"{env} — {mode} — {filename}"



# ✅ Helper global pour tous les fichiers de logs
def get_log_path(filename="app.log"):
    """
    Retourne le chemin absolu vers un fichier de log dans le dossier 'logs' du projet actif.

    Le dossier est calculé dynamiquement à partir de l'emplacement du fichier utils.py.
    Exemple :
        get_log_path("connexions.log") → /home/ndprz/dev/logs/connexions.log (en DEV)
        get_log_path("deploy.log")     → /home/ndprz/ba380/logs/deploy.log (en PROD)
    """
    basedir = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(basedir, "logs", filename)

import subprocess

def get_git_commits(repo_path, max_commits=20):
    """
    Récupère les derniers commits Git sous forme de dictionnaires.
    """
    try:
        output = subprocess.check_output(
            ["git", "-C", repo_path, "log", f"--max-count={max_commits}", "--pretty=format:%h|%ad|%an|%s", "--date=short"],
            stderr=subprocess.STDOUT
        ).decode("utf-8")

        commits = []
        for line in output.strip().split("\n"):
            parts = line.strip().split("|", 3)
            if len(parts) == 4:
                hash_, date, author, message = parts
                commits.append({
                    "hash": hash_,
                    "date": date,
                    "author": author,
                    "message": message
                })

        return commits
    except subprocess.CalledProcessError as e:
        return [{"error": f"Erreur Git : {e.output.decode()}"}]
    except Exception as e:
        return [{"error": str(e)}]

import os
import requests
from flask import url_for


def send_reset_email(email, token):
    """Envoie un email de réinitialisation via l’API Mailjet"""
    api_key = os.getenv("MAILJET_API_KEY")
    api_secret = os.getenv("MAILJET_API_SECRET")
    sender = os.getenv("MAILJET_SENDER")
    reset_link = url_for('reset_password', token=token, _external=True)

    write_log(f"📧 Préparation d’un email de réinitialisation vers {email}")
    write_log(f"🔑 Clé API : {'OK' if api_key else '❌'} | Secret : {'OK' if api_secret else '❌'}")

    data = {
        "Messages": [
            {
                "From": {"Email": sender, "Name": "BA380"},
                "To": [{"Email": email}],
                "Subject": "Réinitialisation de votre mot de passe",
                "TextPart": f"""
Bonjour,

Vous avez demandé la réinitialisation de votre mot de passe.

👉 Cliquez ici : {reset_link}

Si vous n’êtes pas à l’origine de cette demande, ignorez simplement cet email.

L’équipe BA380
"""
            }
        ]
    }

    try:
        response = requests.post(
            "https://api.mailjet.com/v3.1/send",
            auth=(api_key, api_secret),
            json=data
        )
        write_log(f"📬 Statut Mailjet : {response.status_code}")
        write_log(f"📨 Réponse Mailjet : {response.text}")
        response.raise_for_status()
    except Exception as e:
        write_log(f"❌ Erreur API Mailjet : {e}")


def get_user_roles(user_email):
    """
    Retourne la liste complète des rôles pour un utilisateur.
    Si l'utilisateur est admin global (table users.role = 'admin'),
    il obtient automatiquement l'accès à toutes les applis.
    """
    roles = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Vérifie le rôle global
        user = cursor.execute(
            "SELECT role FROM users WHERE LOWER(email)=LOWER(?)", (user_email,)
        ).fetchone()

        if user and user["role"].lower() == "admin":
            # Superadmin → accès complet
            applis = [
                "benevoles",
                "associations",
                "distribution",
                "fournisseurs",
                "evenements"
            ]
            roles = [("admin", "global")] + [(a, "ecriture") for a in applis]
            conn.close()
            # write_log(f"👑 Superadmin détecté : {user_email}, accès total")
            return roles

        # Sinon : lecture de la table roles_utilisateurs
        rows = cursor.execute(
            "SELECT appli, droit FROM roles_utilisateurs WHERE LOWER(user_email)=LOWER(?)",
            (user_email,)
        ).fetchall()

        roles = [(r["appli"], r["droit"]) for r in rows]
        conn.close()

    except Exception as e:
        write_log(f"❌ Erreur get_user_roles({user_email}) : {e}")
    return roles



def write_connexion_log(user_id, username, action="login"):
    """
    Écrit un message dans le journal des connexions (login ou logout).
    """
    try:
        from flask import request
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        session_id = request.cookies.get("ba38_session", "unknown")
        log_path = get_log_path("connexions.log")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] {action.upper()} : session_file={session_id}, user_id={user_id}, username={username}, ip={ip}\n")

    except Exception as e:
        write_log(f"❌ Erreur write_connexion_log : {e}")

from flask import session
from utils import write_log

# utils.py
from flask import session

def has_access(appli, level):
    alias = {
        "assos": "associations",
        "association": "associations",
        "frs": "fournisseurs",
        "fournisseur": "fournisseurs",
        "benevole": "benevoles",
        "evenements": "evenements",
    }
    appli = alias.get(str(appli).lower(), str(appli).lower())
    level = str(level).lower()

    # ✅ Si admin global → accès total
    if session.get("user_role") == "admin":
        return True

    droits = session.get("roles_utilisateurs", [])

    # blocage explicite
    if (appli, "aucun") in droits:
        return False

    if level == "lecture":
        return (appli, "lecture") in droits or (appli, "ecriture") in droits
    elif level == "ecriture":
        return (appli, "ecriture") in droits
    return False

# ✅ Connexion Google
def get_google_services():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Fichier manquant : {SERVICE_ACCOUNT_FILE}")
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        client = gspread.authorize(creds)
        drive_service = build("drive", "v3", credentials=creds)
        write_log("✅ Connexion à Google Sheets et Drive réussie.")
        return client, drive_service, creds
    except Exception as e:
        write_log(f"❌ Erreur de connexion Google Sheets/Drive : {e}")
        return None, None, None




import sqlite3
import os
import shutil

def migrate_schema_and_data(source_db_path, dest_db_path, copy_data=False):
    import sqlite3
    from utils import write_log

    write_log(f"📂 Source : {source_db_path}")
    write_log(f"📁 Destination : {dest_db_path}")
    write_log(f"🧪 Mode copie des données : {'OUI' if copy_data else 'NON'}")

    source_conn = sqlite3.connect(source_db_path)
    dest_conn = sqlite3.connect(dest_db_path)
    source_cursor = source_conn.cursor()
    dest_cursor = dest_conn.cursor()

    source_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    source_tables = [row[0] for row in source_cursor.fetchall() if row[0] != 'sqlite_sequence']

    for table in source_tables:
        # Vérifie si la table existe déjà dans la base destination
        dest_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        exists = dest_cursor.fetchone()

        if not exists:
            # Crée la table dans la base destination
            source_cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
            create_table_sql = source_cursor.fetchone()[0]
            dest_cursor.execute(create_table_sql)
            write_log(f"🆕 Table créée : {table}")

        else:
            # Compare les colonnes et ajoute celles manquantes
            source_cursor.execute(f"PRAGMA table_info({table})")
            source_columns = {col[1]: col[2] for col in source_cursor.fetchall()}

            dest_cursor.execute(f"PRAGMA table_info({table})")
            dest_columns = {col[1]: col[2] for col in dest_cursor.fetchall()}

            for column, col_type in source_columns.items():
                if column not in dest_columns:
                    alter_sql = f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                    dest_cursor.execute(alter_sql)
                    write_log(f"➕ Colonne ajoutée à {table} : {column} ({col_type})")

        if copy_data:
            source_cursor.execute(f"SELECT * FROM {table}")
            rows = source_cursor.fetchall()

            if rows:
                source_cursor.execute(f"PRAGMA table_info({table})")
                col_names = [col[1] for col in source_cursor.fetchall()]
                placeholders = ','.join('?' * len(col_names))
                columns = ','.join(col_names)
                insert_sql = f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({placeholders})"
                dest_cursor.executemany(insert_sql, rows)
                write_log(f"📥 Données copiées dans {table} : {len(rows)} lignes")

    dest_conn.commit()
    source_conn.close()
    dest_conn.close()
    write_log("✅ Synchronisation terminée avec succès.")



def envoyer_mail(sujet, destinataires, texte, sender_override=None, attachment_path=None):
    """
    Envoie un email via Mailjet
    - MODE TEST : redirection
    - Pièce jointe PDF optionnelle
    """

    import os
    import base64
    import requests
    from utils import write_log

    api_key = os.getenv("MAILJET_API_KEY")
    api_secret = os.getenv("MAILJET_API_SECRET")

    sender = sender_override or os.getenv(
        "MAILJET_SENDER",
        "ba380.informatique2@banquealimentaire.org"
    )

    # ============================
    # MODE TEST / PROD
    # ============================
    mail_mode = os.getenv("MAIL_MODE", "PROD").upper()
    mail_test_to = os.getenv(
        "MAIL_TEST_TO",
        "ba380.informatique2@banquealimentaire.org"
    )

    destinataires_reels = destinataires

    if mail_mode == "TEST":
        write_log(
            f"🧪 MODE TEST MAIL — redirection {sender} - {destinataires} → [{mail_test_to}]"
        )
        destinataires = [mail_test_to]
        sujet = f"[TEST] {sujet}"

    # ============================
    # PIÈCE JOINTE
    # ============================
    attachments = []

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")

        attachments.append({
            "ContentType": "application/pdf",
            "Filename": os.path.basename(attachment_path),
            "Base64Content": encoded
        })

        write_log(f"📎 Pièce jointe ajoutée : {attachment_path}")

    # ============================
    # PAYLOAD MAILJET
    # ============================
    data = {
        "Messages": [
            {
                "From": {"Email": sender, "Name": "BA380"},
                "To": [{"Email": dest} for dest in destinataires],
                "Subject": sujet,
                "TextPart": texte,
                "Attachments": attachments if attachments else None
            }
        ]
    }

    # Nettoyage si pas de PJ
    if not attachments:
        data["Messages"][0].pop("Attachments")

    # ============================
    # ENVOI
    # ============================
    try:
        response = requests.post(
            "https://api.mailjet.com/v3.1/send",
            auth=(api_key, api_secret),
            json=data,
            timeout=15
        )

        write_log(
            f"📧 Email envoyé via Mailjet "
            f"(status {response.status_code}, mode={mail_mode})"
        )
        write_log(
            f"📤 Sujet : {sujet}\n"
            f"✉️ Destinataires effectifs : {destinataires}\n"
            f"🎯 Destinataires réels : {destinataires_reels}"
        )

        response.raise_for_status()

    except Exception as e:
        write_log(
            f"❌ Erreur lors de l'envoi de l'email via Mailjet "
            f"(mode={mail_mode}) : {e}"
        )




def get_all_users():
    """ Récupère tous les utilisateurs avec les droits d'accès associés """
    with get_db_connection() as conn:
        users = conn.execute("""
            SELECT id, username, email, role, actif, app_bene, app_assos
            FROM users
        """).fetchall()
    return users


def format_tel(value):
    """
    Formate un numéro de téléphone au format 00 00 00 00 00
    si le champ contient exactement 10 chiffres.
    """
    if not value or not isinstance(value, str):
        return value
    tel = re.sub(r"\D", "", value)
    if len(tel) == 10:
        return " ".join(tel[i:i+2] for i in range(0, 10, 2))
    return value


def row_get(row, key, default=None):
    return row[key] if key in row.keys() else default


def get_static_event_dir():
    """
    Retourne le chemin absolu du dossier /static/evenements adapté à l'environnement.
    Exemple :
        DEV  → /home/ndprz/dev/static/evenements
        PROD → /home/ndprz/ba380/static/evenements
    """
    base_dir = os.getenv("BA38_BASE_DIR")
    if not base_dir:
        raise RuntimeError("BA38_BASE_DIR non défini")
    return os.path.join(base_dir, "static", "evenements")


def get_static_factures_dir():
    """
    Retourne le chemin absolu du dossier static/factures
    en fonction de l'environnement courant.
    """
    base_dir = os.getenv("BA38_BASE_DIR")
    if not base_dir:
        raise RuntimeError("Variable BA38_BASE_DIR non définie")

    path = os.path.join(base_dir, "static", "factures")
    os.makedirs(path, exist_ok=True)
    return path



def get_param_value(name):
    """Retourne la valeur d’un paramètre global depuis la table `parametres`."""
    conn = get_db_connection()
    row = conn.execute("SELECT param_value FROM parametres WHERE param_name = ?", (name,)).fetchone()
    conn.close()
    return row["param_value"] if row else ""


def get_user_info(user):
    """Extrait email et rôle de l'utilisateur, quelle que soit la structure (Row, dict, ou objet)."""
    try:
        if hasattr(user, "email"):
            return user.email, getattr(user, "role", None)
        if isinstance(user, dict):
            return user.get("email"), user.get("role")
        if isinstance(user, sqlite3.Row):
            return user["email"], user["role"]
    except Exception:
        pass
    return None, None


from googleapiclient.http import MediaFileUpload
from googleapiclient.discovery_cache.base import Cache




def upload_file_to_drive(local_path, folder_id, filename=None):
    """
    Upload d'un fichier local vers Google Drive (création).
    Compatible Drive partagé.
    """

    from utils import write_log
    import os

    if not os.path.exists(local_path):
        write_log(f"❌ upload_file_to_drive : fichier introuvable : {local_path}")
        return None

    filename = filename or os.path.basename(local_path)

    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/drive"]
        )

        class NoCache(Cache):
            def get(self, url): return None
            def set(self, url, content): pass

        service = build("drive", "v3", credentials=credentials, cache=NoCache())

        file_metadata = {
            "name": filename,
            "parents": [folder_id]
        }

        media = MediaFileUpload(
            local_path,
            resumable=True
        )

        write_log(f"📤 Upload Drive : {filename} → folder_id={folder_id}")

        created = service.files().create(
            body=file_metadata,
            media_body=media,
            supportsAllDrives=True,
            fields="id"
        ).execute()

        file_id = created.get("id")
        write_log(f"✅ Upload terminé : {filename} (id={file_id})")

        return file_id

    except Exception as e:
        write_log(f"❌ Erreur upload_file_to_drive({filename}) : {e}")
        return None


def get_or_create_drive_folder(service, path, shared_drive_id):
    """
    Résout un chemin Drive dans un Drive partagé donné
    """
    parts = path.strip("/").split("/")
    current_parent = shared_drive_id   # 🔥 POINT CLÉ

    for part in parts:
        query = (
            f"name = '{part}' and "
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"trashed = false and "
            f"'{current_parent}' in parents"
        )

        results = service.files().list(
            q=query,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id, name)"
        ).execute()

        files = results.get("files", [])

        if files:
            folder = files[0]
        else:
            metadata = {
                "name": part,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [current_parent]
            }

            folder = service.files().create(
                body=metadata,
                supportsAllDrives=True,
                fields="id"
            ).execute()

        current_parent = folder["id"]

    return current_parent




def upload_file_to_drive_path(local_path, drive_path, filename=None):
    """
    Upload un fichier local vers Google Drive
    en résolvant un chemin logique de dossier Drive.
    Exemple :
      drive_path = 'BA380 - TRESORERIE/COTISATIONS/Cotisations 2026'
    """

    from utils import write_log
    import os

    try:
        # ============================
        # ID du Drive partagé (OBLIGATOIRE)
        # ============================
        BA380_SHARED_DRIVE_ID = os.getenv("BA380_SHARED_DRIVE_ID")
        if not BA380_SHARED_DRIVE_ID:
            raise RuntimeError(
                "BA380_SHARED_DRIVE_ID non défini dans l'environnement"
            )

        # ============================
        # Initialisation Drive
        # ============================
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/drive"]
        )

        class NoCache(Cache):
            def get(self, url): return None
            def set(self, url, content): pass

        service = build("drive", "v3", credentials=credentials, cache=NoCache())

        # ============================
        # Résolution du chemin Drive
        # ============================
        folder_id = get_or_create_drive_folder(
            service,
            drive_path,
            shared_drive_id=BA380_SHARED_DRIVE_ID
        )

        if not folder_id:
            write_log(f"❌ Dossier Drive introuvable : {drive_path}")
            return None

        # ============================
        # Upload via fonction existante
        # ============================
        return upload_file_to_drive(
            local_path=local_path,
            folder_id=folder_id,
            filename=filename
        )

    except Exception as e:
        write_log(f"❌ Erreur upload_file_to_drive_path : {e}")
        return None


def slugify_filename(text):
    """
    Nettoie une chaîne pour un nom de fichier sûr
    """
    text = text.strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text
