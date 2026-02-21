# =========================
# Standard library
# =========================
import os
import re
import sqlite3
import logging
import subprocess
import base64
from datetime import datetime
from pathlib import Path

# =========================
# Third-party
# =========================
import requests
import gspread
from dotenv import load_dotenv
from flask import current_app, session, url_for, request
from google.oauth2.service_account import Credentials

# =========================
# Google API 
# =========================
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.discovery_cache.base import Cache


# SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")


# ---------------------------------------------------------------------------
# Chargement robuste du .env (CLI + Flask + gunicorn)
# ---------------------------------------------------------------------------

# 1️⃣ Charger le .env depuis le répertoire courant (CLI, gunicorn, Flask)
load_dotenv(override=False)

# 2️⃣ Si BA38_BASE_DIR est maintenant défini, recharger explicitement depuis là
base_dir = os.getenv("BA38_BASE_DIR")
if base_dir:
    env_path = Path(base_dir) / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)



SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
VERSION = os.getenv("VERSION", "0.0.0")

# ============================================================================
# 🪵 LOGGING
# ============================================================================

def write_log(message: str):
    try:
        current_app.logger.info(message)
    except RuntimeError:
        # Cas script standalone (cron)
        logging.getLogger("BA38").info(message)


def write_connexion_log(user_id, username, action="login"):
    """
    Écrit un message dans le journal des connexions (login / logout).
    """
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        session_id = request.cookies.get("ba38_session", "unknown")

        log_path = get_log_path("connexions.log")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                f"[{timestamp}] {action.upper()} : "
                f"session={session_id}, user_id={user_id}, "
                f"username={username}, ip={ip}\n"
            )

    except Exception as e:
        try:
            write_log(f"❌ Erreur write_connexion_log : {e}")
        except Exception:
            pass


def get_log_path(filename="app.log"):
    """
    Retourne le chemin absolu d’un fichier de log BA38.

    - app.log → logs DEV ou PROD (BA38_BASE_DIR)
    - deploy.log → logs globaux (/srv/ba38/logs)
    """

    # 🔹 Cas particulier : historique des déploiements (global)
    if filename == "deploy.log":
        base_dir = "/srv/ba38"
    else:
        base_dir = os.getenv("BA38_BASE_DIR") or os.getcwd()

    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    return os.path.join(log_dir, filename)




# ============================================================================
# 🗄️ BASE SQLITE
# ============================================================================
def get_db_path():
    try:
        test_mode = session.get("test_user", False)
    except RuntimeError:
        test_mode = os.getenv("TEST_MODE") == "1"

    db_var = "SQLITE_DB_TEST" if test_mode else "SQLITE_DB"
    filename = os.getenv(db_var)


    if not filename:
        raise RuntimeError(f"{db_var} non défini dans le .env")

    base_dir = os.getenv("BA38_BASE_DIR")
    if not base_dir:
        raise RuntimeError("BA38_BASE_DIR non défini")

    path = os.path.join(base_dir, filename)

    if os.getenv("ENVIRONMENT") == "dev" and "ba380.sqlite" in path:
        raise RuntimeError("❌ Base PROD utilisée en DEV")

    if not os.path.exists(path):
        raise RuntimeError(f"Base SQLite inexistante : {path}")

    return path


def get_db_path_by_env(env: str, *, force_base_dir: str | None = None) -> str:
    """
    Retourne le chemin de la base SQLite pour un environnement donné.

    - env : "dev" | "prod"
    - force_base_dir : permet de forcer la racine (scripts admin)
    """

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
    
    base_dir = force_base_dir or os.getenv("BA38_BASE_DIR")


    if not filename:
        raise RuntimeError(f"Nom de base SQLite non défini pour {env}")

    if force_base_dir:
        base_dir = force_base_dir
    else:
        base_dir = os.getenv("BA38_BASE_DIR")

    if not base_dir:
        raise RuntimeError("BA38_BASE_DIR non défini")

    write_log(f"    path final     = {os.path.join(base_dir, filename)}")

    return os.path.join(base_dir, filename)


def get_db_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn

def get_db_info():
    """
    Retourne des informations simples sur la base SQLite courante.
    Utilisé pour debug / affichage admin.
    """
    path = get_db_path()
    info = {
        "path": path,
        "exists": os.path.exists(path),
        "size": None,
        "tables": []
    }

    if not info["exists"]:
        return info

    try:
        info["size"] = os.path.getsize(path)
        with sqlite3.connect(path) as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            info["tables"] = [r[0] for r in rows]
    except Exception as e:
        write_log(f"❌ get_db_info : {e}")

    return info

def get_db_info_display():
    """
    Retourne une chaîne courte pour affichage pied de page.
    Ex : 'DEV — NORMAL — ba380dev.sqlite'
    """
    try:
        path = get_db_path()
        db_name = os.path.basename(path)

        env = os.getenv("ENVIRONMENT", "DEV").upper()
        mode = "TEST" if os.getenv("TEST_MODE") == "1" else "NORMAL"

        return f"{env} — {mode} — {db_name}"

    except Exception as e:
        write_log(f"❌ get_db_info_display : {e}")
        return "Base inconnue"


def get_version():
    """
    Retourne la version applicative courante.
    """
    return os.getenv("VERSION", "0.0.0")

def get_all_users():
    """
    Retourne la liste complète des utilisateurs (table users).
    """
    users = []
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT * FROM users ORDER BY email"
            ).fetchall()
            users = [dict(r) for r in rows]
    except Exception as e:
        write_log(f"❌ get_all_users : {e}")
    return users


def get_user_info(user_email):
    """
    Retourne les informations complètes d'un utilisateur à partir de son email.
    """
    if not user_email:
        return None

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM users WHERE LOWER(email) = LOWER(?)",
                (user_email,)
            ).fetchone()

            if row:
                return dict(row)

    except Exception as e:
        write_log(f"❌ get_user_info({user_email}) : {e}")

    return None

def get_param_value(key, default=None):
    """
    Retourne la valeur d'un paramètre depuis la table parametres.
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT valeur FROM parametres WHERE cle = ?",
                (key,)
            ).fetchone()
            if row:
                return row["valeur"]
    except Exception as e:
        write_log(f"❌ get_param_value({key}) : {e}")
    return default

# ============================================================================
# ✅ VALIDATION
# ============================================================================

def is_valid_email(email: str) -> bool:
    if not email:
        return False
    email = email.strip()
    if " " in email:
        return False
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None


def is_valid_phone(phone: str) -> bool:
    if not phone:
        return True
    digits = re.sub(r"\D", "", phone)
    return len(digits) == 10


# ============================================================================
# 🔐 DROITS & ACCÈS
# ============================================================================

def get_user_roles(user_email):
    roles = []
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        user = cur.execute(
            "SELECT role FROM users WHERE LOWER(email)=LOWER(?)",
            (user_email,)
        ).fetchone()

        if user and user["role"] == "admin":
            applis = ["benevoles", "associations", "distribution", "fournisseurs"]
            return [("admin", "global")] + [(a, "ecriture") for a in applis]

        rows = cur.execute(
            "SELECT appli, droit FROM roles_utilisateurs WHERE LOWER(user_email)=LOWER(?)",
            (user_email,)
        ).fetchall()

        roles = [(r["appli"], r["droit"]) for r in rows]

    except Exception as e:
        write_log(f"❌ get_user_roles({user_email}) : {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return roles





# ============================================================================
# 🌐 GOOGLE DRIVE
# ============================================================================

if Cache is not None:
    class NoCache(Cache):
        def get(self, url): return None
        def set(self, url, content): pass
else:
    NoCache = None




def get_drive_service():
    if not SERVICE_ACCOUNT_FILE or not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError("SERVICE_ACCOUNT_FILE invalide")

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache=NoCache())

def get_google_services():
    """
    Fonction historique BA38.
    Compatible Flask + script standalone (cron)
    """
    service_account_file = os.getenv("SERVICE_ACCOUNT_FILE")

    if not service_account_file or not os.path.exists(service_account_file):
        write_log(f"❌ SERVICE_ACCOUNT_FILE manquant ou invalide : {service_account_file}")
        return None, None, None

    try:
        creds = Credentials.from_service_account_file(
            service_account_file,
            scopes=SCOPES
        )
        client = gspread.authorize(creds)
        drive_service = build("drive", "v3", credentials=creds)

        write_log("✅ Connexion Google Sheets et Drive réussie.")
        return client, drive_service, creds

    except Exception as e:
        write_log(f"❌ Erreur de connexion Google Sheets/Drive : {e}")
        return None, None, None


def upload_database():
    """
    Upload de la base SQLite vers Google Drive (optionnel).
    """

    # 🔒 Désactivation globale via .env
    if os.getenv("UPLOAD_DB_ON_WRITE", "1") != "1":
        return

    local_path = get_db_path()
    env = os.getenv("ENVIRONMENT", "dev").lower()

    try:
        test_mode = session.get("test_user", False)
    except RuntimeError:
        test_mode = False

    file_id = None
    if env == "prod":
        file_id = os.getenv("GDRIVE_DB_FILE_ID_PROD")
    elif env == "dev" and test_mode:
        file_id = os.getenv("GDRIVE_DB_FILE_ID_DEV_TEST")
    elif env == "dev":
        file_id = os.getenv("GDRIVE_DB_FILE_ID_DEV")
    elif test_mode:
        file_id = os.getenv("GDRIVE_DB_FILE_ID_TEST")

    if not file_id:
        write_log("⛔ upload_database annulé : file_id manquant")
        return

    service = get_drive_service()

    media = MediaFileUpload(
        local_path,
        mimetype="application/x-sqlite3",
        resumable=True
    )

    service.files().update(
        fileId=file_id,
        media_body=media,
        supportsAllDrives=True
    ).execute()

    write_log(f"✅ Base SQLite envoyée sur Drive (id={file_id})")


def upload_file_to_drive(local_path, folder_id, filename=None):
    if not os.path.exists(local_path):
        write_log(f"❌ Fichier introuvable : {local_path}")
        return None

    service = get_drive_service()
    filename = filename or os.path.basename(local_path)

    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True)

    result = service.files().create(
        body=metadata,
        media_body=media,
        supportsAllDrives=True,
        fields="id"
    ).execute()

    write_log(f"📤 Upload Drive : {filename}")
    return result.get("id")


def get_or_create_drive_folder(service, path, shared_drive_id):
    parts = path.strip("/").split("/")
    parent = shared_drive_id

    for part in parts:
        q = (
            f"name='{part}' and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false and '{parent}' in parents"
        )
        res = service.files().list(
            q=q,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id,name)"
        ).execute()

        if res["files"]:
            parent = res["files"][0]["id"]
        else:
            folder = service.files().create(
                body={
                    "name": part,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent]
                },
                supportsAllDrives=True,
                fields="id"
            ).execute()
            parent = folder["id"]

    return parent

def upload_file_to_drive_path(local_path, drive_path, shared_drive_id, filename=None):
    """
    Upload un fichier local vers Google Drive en utilisant un chemin logique.
    Le dossier est créé s'il n'existe pas.
    """
    if not os.path.exists(local_path):
        write_log(f"❌ Fichier introuvable : {local_path}")
        return None

    service = get_drive_service()

    folder_id = get_or_create_drive_folder(
        service=service,
        path=drive_path,
        shared_drive_id=shared_drive_id
    )

    return upload_file_to_drive(
        local_path=local_path,
        folder_id=folder_id,
        filename=filename
    )

def get_drive_folder_id_from_path(drive_path, shared_drive_id):
    """
    Retourne l'ID d'un dossier Google Drive à partir d'un chemin logique.
    Le dossier est créé s'il n'existe pas.
    """
    service = get_drive_service()

    return get_or_create_drive_folder(
        service=service,
        path=drive_path,
        shared_drive_id=shared_drive_id
    )

# ============================================================================
# 📧 MAILJET
# ============================================================================

def envoyer_mail(sujet, destinataires, texte, sender_override=None, attachment_path=None):
    api_key = os.getenv("MAILJET_API_KEY")
    api_secret = os.getenv("MAILJET_API_SECRET")
    sender = sender_override or os.getenv("MAILJET_SENDER")

    mail_mode = os.getenv("MAIL_MODE", "PROD").upper()
    mail_test_to = os.getenv("MAIL_TEST_TO")

    if mail_mode == "TEST":
        sujet = f"[TEST] {sujet}"
        destinataires = [mail_test_to]

    attachments = []
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")

        attachments.append({
            "ContentType": "application/pdf",
            "Filename": os.path.basename(attachment_path),
            "Base64Content": encoded
        })

    data = {
        "Messages": [{
            "From": {"Email": sender, "Name": "BA380"},
            "To": [{"Email": d} for d in destinataires],
            "Subject": sujet,
            "TextPart": texte,
            "Attachments": attachments or None
        }]
    }

    data["Messages"][0].pop("Attachments", None)

    response = requests.post(
        "https://api.mailjet.com/v3.1/send",
        auth=(api_key, api_secret),
        json=data,
        timeout=15
    )

    write_log(f"📧 Mail envoyé (status={response.status_code})")
    response.raise_for_status()

def send_reset_email(email, token):
    """
    Envoie un email de réinitialisation de mot de passe via Mailjet.
    """
    api_key = os.getenv("MAILJET_API_KEY")
    api_secret = os.getenv("MAILJET_API_SECRET")
    sender = os.getenv("MAILJET_SENDER")

    if not all([api_key, api_secret, sender]):
        write_log("❌ Mailjet mal configuré (clé/secret/sender manquant)")
        return

    reset_link = url_for("reset_password", token=token, _external=True)

    data = {
        "Messages": [
            {
                "From": {"Email": sender, "Name": "BA380"},
                "To": [{"Email": email}],
                "Subject": "Réinitialisation de votre mot de passe",
                "TextPart": f"""
Bonjour,

Vous avez demandé la réinitialisation de votre mot de passe.

👉 Cliquez ici pour définir un nouveau mot de passe :
{reset_link}

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
            json=data,
            timeout=15
        )
        write_log(f"📧 Email de réinitialisation envoyé à {email} (status={response.status_code})")
        response.raise_for_status()

    except Exception as e:
        write_log(f"❌ Erreur send_reset_email({email}) : {e}")

# ============================================================================
# 🧰 UTILITAIRES
# ============================================================================

def format_tel(value):
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        return " ".join(digits[i:i+2] for i in range(0, 10, 2))
    return value


def slugify_filename(text):
    text = re.sub(r"[^\w\s-]", "", text.strip())
    return re.sub(r"[\s_-]+", "_", text)

def row_get(row, key, default=None):
    """
    Accès sécurisé à une valeur dans un sqlite3.Row ou un dict.
    """
    if row is None:
        return default

    try:
        if isinstance(row, dict):
            return row.get(key, default)
        return row[key]
    except Exception:
        return default

def get_static_event_dir(event_name=None):
    """
    Retourne le chemin absolu vers le dossier static des événements.
    - Sans argument → /static/evenements
    - Avec event_name → /static/evenements/<event_name>
    Le dossier est créé s'il n'existe pas.
    """
    base_dir = os.getenv("BA38_BASE_DIR")
    if not base_dir:
        raise RuntimeError("BA38_BASE_DIR non défini")

    base_path = os.path.join(base_dir, "static", "evenements")

    if event_name:
        path = os.path.join(base_path, event_name)
    else:
        path = base_path

    os.makedirs(path, exist_ok=True)
    return path

def get_static_factures_dir():
    """
    Retourne le chemin absolu vers le dossier static/factures.
    Le dossier est créé s'il n'existe pas.
    """
    base_dir = os.getenv("BA38_BASE_DIR")
    if not base_dir:
        raise RuntimeError("BA38_BASE_DIR non défini")

    path = os.path.join(base_dir, "static", "factures")
    os.makedirs(path, exist_ok=True)
    return path


# ============================================================================
# 🧰 GIT
# ============================================================================
def get_git_commits(repo_path, limit=20):
    """
    Retourne les derniers commits git du dépôt donné.
    """
    commits = []

    if not repo_path or not os.path.isdir(repo_path):
        return [{"error": f"Dépôt Git introuvable : {repo_path}"}]

    try:
        result = subprocess.run(
            [
                "git", "-C", repo_path,
                "log",
                f"-n{limit}",
                "--pretty=format:%h|%an|%ad|%s",
                "--date=short"
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return [{"error": result.stderr.strip()}]

        for line in result.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "hash": parts[0],
                    "author": parts[1],
                    "date": parts[2],
                    "message": parts[3],
                })

    except Exception as e:
        commits.append({"error": str(e)})

    return commits


def get_runtime_db_info():
    """
    Retourne les informations runtime sur la base réellement utilisée.
    """
    db_path = get_db_path()
    return {
        "db_path": db_path,
        "exists": os.path.exists(db_path) if db_path else False,
    }


def migrate_schema_and_data(source_db_path, dest_db_path, copy_data=False):
    """
    Synchronise le schéma et éventuellement les données
    entre deux bases SQLite.

    - Crée les tables manquantes
    - Ajoute les colonnes absentes
    - Copie les données si demandé (INSERT OR IGNORE)
    """


    write_log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    write_log("🔁 Migration schéma & données SQLite")
    write_log(f"📂 Source      : {source_db_path}")
    write_log(f"📁 Destination : {dest_db_path}")
    write_log(f"🧪 Copie data  : {'OUI' if copy_data else 'NON'}")

    source_conn = sqlite3.connect(source_db_path)
    dest_conn = sqlite3.connect(dest_db_path)

    source_cursor = source_conn.cursor()
    dest_cursor = dest_conn.cursor()

    # Liste des tables source
    source_cursor.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name != 'sqlite_sequence'"
    )
    source_tables = [row[0] for row in source_cursor.fetchall()]

    for table in source_tables:
        # Table existe en destination ?
        dest_cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        exists = dest_cursor.fetchone()

        if not exists:
            # Création de la table
            source_cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            )
            create_sql = source_cursor.fetchone()[0]
            dest_cursor.execute(create_sql)
            write_log(f"🆕 Table créée : {table}")

        else:
            # Synchronisation des colonnes
            source_cursor.execute(f"PRAGMA table_info({table})")
            source_columns = {col[1]: col[2] for col in source_cursor.fetchall()}

            dest_cursor.execute(f"PRAGMA table_info({table})")
            dest_columns = {col[1]: col[2] for col in dest_cursor.fetchall()}

            for column, col_type in source_columns.items():
                if column not in dest_columns:
                    alter_sql = f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_type}'
                    dest_cursor.execute(alter_sql)
                    write_log(f"➕ Colonne ajoutée : {table}.{column} ({col_type})")

        # Copie des données si demandé
        if copy_data:
            source_cursor.execute(f'SELECT * FROM "{table}"')
            rows = source_cursor.fetchall()

            if rows:
                source_cursor.execute(f'PRAGMA table_info("{table}")')
                col_names = [col[1] for col in source_cursor.fetchall()]

                placeholders = ",".join("?" * len(col_names))
                columns = ",".join(f'"{c}"' for c in col_names)

                insert_sql = (
                    f'INSERT OR IGNORE INTO "{table}" ({columns}) '
                    f'VALUES ({placeholders})'
                )

                dest_cursor.executemany(insert_sql, rows)
                write_log(f"📥 Données copiées : {table} ({len(rows)} lignes)")

    dest_conn.commit()
    source_conn.close()
    dest_conn.close()

    write_log("✅ Synchronisation terminée avec succès")
    write_log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")



def has_access(appli: str, niveau_requis: str) -> bool:
    """
    Vérifie si l'utilisateur courant a le droit requis sur une application.
    Source de vérité unique.  NICOLAS
    """

    # Admin global
    if session.get("user_role") == "admin":
        return True

    roles = session.get("roles_utilisateurs", [])
    hierarchy = ["lecture", "ecriture", "admin"]

    for app, droit in roles:
        if app != appli:
            continue

        # Aucun droit ou droit explicite "aucun"
        if not droit or droit == "aucun":
            return False

        # Sécurité : valeurs inattendues
        if droit not in hierarchy or niveau_requis not in hierarchy:
            write_log(
                f"⚠️ has_access incohérent : appli={appli}, droit={droit}, requis={niveau_requis}"
            )
            return False

        return hierarchy.index(droit) >= hierarchy.index(niveau_requis)

    return False

def is_admin_global():
    return session.get("user_role") == "admin"
