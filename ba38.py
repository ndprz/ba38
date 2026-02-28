# =============================
# BOOTSTRAP ENVIRONNEMENT
# =============================
import os
from dotenv import load_dotenv


# --------------------------------------------------
# ENV versionnée et centralisée
# --------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)
else:
    raise RuntimeError(f".env introuvable : {ENV_PATH}")


import sys
import io
import pandas as pd
import sqlite3
import logging

# --------------------------------------------------
# LOGGING UNIFIÉ BA38 (DEV / PROD)
# --------------------------------------------------

LOG_FILE = os.getenv(
    "LOG_FILE",
    os.path.join(BASE_DIR, "app.log")
)

logger = logging.getLogger("BA38")

logger.setLevel(logging.INFO)

# éviter les handlers multiples (reload gunicorn)
if not logger.handlers:
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    stream_handler = logging.StreamHandler()  # stdout → journald

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)



# --------------------------------------------------
# Flask
# --------------------------------------------------

from datetime import datetime, timedelta
from utils import get_db_connection, write_log, send_reset_email, get_user_roles, get_db_path, get_db_info, upload_database, get_version, get_all_users, format_tel, get_param_value
from utils import get_user_info,has_access
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, g, current_app
from flask_login import current_user, LoginManager, UserMixin, login_user, logout_user, login_required
from flask_session import Session
from wtforms import StringField, PasswordField, SubmitField, SelectField
from wtforms.validators import Optional, DataRequired, Email, Length, EqualTo, ValidationError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import MethodNotAllowed
from docx import Document
from fpdf import FPDF
from weasyprint import HTML
from forms import LoginForm, RegistrationForm, ResetPasswordForm
from googleapiclient.discovery import build
from google.oauth2 import service_account
from pathlib import Path
import jwt
import re



from ba38_planning_ramasse import planning_bp  # Ramasse
from ba38_planning_distribution import planning_dist_bp  # Distribution
from ba38_benevoles import benevoles_bp
from ba38_planning_palettes import planning_palettes_bp
from ba38_partenaires import partenaires_bp
from ba38_planning_tournees import planning_tournees_bp
from ba38_planning_pesee import planning_pesee_bp
from ba38_export_publipostage import export_bp
from ba38_planning_vif import planning_vif_bp
from debug_tools import debug_bp
from ba38_planning_utils import planning_utils_bp
from ba38_distribution import distribution_bp
from scripts.rename_field import rename_bp
from ba38_admin import admin_bp
from ba38_export import export_data_bp
from ba38_fournisseurs import fournisseurs_bp
from ba38_traitements import traitements_bp
from ba38_fiches_visite import fiches_visite_bp
from ba38_mail import mail_bp
from ba38_evenements import evenements_bp
from ba38_factures import factures_bp
from ba38_mail_benevoles import mail_bene_bp
from ba38_planning_report import planning_report_bp
from ba38_aide import aide_bp
from ba38_engagements import engagements_bp






# Initialisation Flask
app = Flask(__name__)

import logging
from logging.handlers import RotatingFileHandler
import os

LOG_FILE = os.path.join(BASE_DIR, "logs", "app.log")

if not os.path.exists(os.path.dirname(LOG_FILE)):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

file_handler.setFormatter(formatter)

app.logger.setLevel(logging.INFO)
app.logger.addHandler(file_handler)

app.secret_key = os.getenv("FLASK_SECRET_KEY")
app.jinja_env.filters['format_tel'] = format_tel

# ==================================================
# 🔴 Sessions Flask via Redis
# ==================================================
from flask_session import Session
from redis import Redis
from datetime import timedelta

app.config.update(
    SESSION_TYPE="redis",
    SESSION_REDIS=Redis(host="127.0.0.1", port=6379, db=0),

    SESSION_PERMANENT=True,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24),

    SESSION_USE_SIGNER=True,
)
Session(app)


# ✅ Augmente la taille maximale des requêtes POST à 10 Mo
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024
app.config["LOG_FILE"] = LOG_FILE

@app.route("/__ping")
def __ping():
    return "PING OK"

@app.context_processor
def inject_env():
    return {
        "env_name": os.getenv("ENVIRONMENT", "dev")
    }

@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith(("/static", "/login", "/admin")):
        write_log(f"❌ 404 - URL non trouvée : {request.url}")
    return render_template("404.html"), 404


@app.errorhandler(MethodNotAllowed)
def handle_405(e):
    current_app.logger.warning(
        f"405 {request.method} {request.path} "
        f"user={getattr(current_user,'id','anon')} "
        f"ip={request.remote_addr}"
    )
    return e


# =========================
# Injection globale Jinja
# =========================
from utils import has_access, is_admin_global

@app.context_processor
def inject_access_helpers():
    return dict(
        has_access=has_access,
        is_admin_global=is_admin_global
    )


# --------------------------------------------------
# CONTEXTE UTILISATEUR & DROITS (GLOBAL)
# --------------------------------------------------

from ba38_admin import compute_user_role

@app.after_request
def add_security_headers(response):
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=31536000; includeSubDomains"
    )
    response.headers.setdefault(
        "X-Frame-Options",
        "SAMEORIGIN"
    )
    response.headers.setdefault(
        "X-Content-Type-Options",
        "nosniff"
    )
    response.headers.setdefault(
        "Referrer-Policy",
        "strict-origin-when-cross-origin"
    )
    response.headers.setdefault(
        "X-XSS-Protection",
        "1; mode=block"
    )
    return response


@app.before_request
def load_user_context():
    """
    Construit le contexte utilisateur global pour chaque requête.
    """
    g.user_role = compute_user_role()

@app.context_processor
def inject_user_role():
    """
    Rend user_role disponible dans TOUS les templates.
    """
    return {
        "user_role": g.user_role
    }

EXCLUDED_LOG_PATHS = [
    "/debug_console_stream",
    "/static/",
    "/_runtime/",
]

@app.before_request
def log_requests():

    if request.method == "GET":
        return

    if any(request.path.startswith(p) for p in EXCLUDED_LOG_PATHS):
        return

    user = (
        current_user.id
        if hasattr(current_user, "is_authenticated") and current_user.is_authenticated
        else "anonymous"
    )

    current_app.logger.debug(
        f"REQ {request.method} {request.path} "
        f"user={user} ip={request.remote_addr}"
    )

# Authentification Flask-Login
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)



# ✅ Enregistrement des blueprints
app.register_blueprint(planning_bp)
app.register_blueprint(planning_dist_bp)
app.register_blueprint(rename_bp)
app.register_blueprint(benevoles_bp)
app.register_blueprint(planning_palettes_bp, url_prefix="/")
app.register_blueprint(partenaires_bp)
app.register_blueprint(planning_tournees_bp)
app.register_blueprint(planning_pesee_bp)
app.register_blueprint(export_bp)
app.register_blueprint(debug_bp)
app.register_blueprint(planning_vif_bp)
app.register_blueprint(planning_utils_bp,url_prefix="/")
app.register_blueprint(distribution_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(export_data_bp)
app.register_blueprint(fournisseurs_bp)
app.register_blueprint(traitements_bp)
app.register_blueprint(fiches_visite_bp)
app.register_blueprint(mail_bp)
app.register_blueprint(evenements_bp)
app.register_blueprint(factures_bp)
app.register_blueprint(mail_bene_bp)
app.register_blueprint(planning_report_bp)
app.register_blueprint(aide_bp)
app.register_blueprint(engagements_bp)



# Enregistrement de la fonction has_access dans l’environnement Jinja
app.jinja_env.globals['has_access'] = has_access


@app.context_processor
def inject_benevole_options():
    # Options de type_benevole depuis la table parametres
    type_opts = []
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT param_value FROM parametres WHERE param_name='type_benevole' ORDER BY id"
        ).fetchall()
        type_opts = [r["param_value"] for r in rows] or ["benevole"]
    except Exception:
        type_opts = ["benevole"]
    finally:
        try:
            conn.close()
        except Exception:
            pass

    civilite_opts = ["-- Choisir --", "Mme", "M.", "Mx"]

    return dict(
        type_benevole_options=type_opts,
        civilite_options=civilite_opts
    )


@app.template_filter('format_label')
def format_label(value):
    if not value:
        return ""
    text = value.replace("_", " ")
    return re.sub(r"\b(\w)", lambda m: m.group(1).upper(), text)


def log_connexion(user, action="login"):
    """📋 Enregistre une connexion ou déconnexion dans la table log_connexions."""
    try:
        db_path = get_db_path()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        user_agent = request.headers.get("User-Agent")
        env = os.getenv("ENVIRONMENT", "prod").lower()
        now = datetime.utcnow().isoformat()

        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO log_connexions (email, username, environ, ip, user_agent, timestamp, action)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user.email, user.username, env, ip, user_agent, now, action))
    except Exception as e:
        print(f"⚠️ Erreur log_connexion : {e}")




# Supprimer les logs trop verbeux de oauth2client et googleapiclient
for noisy_logger in ['oauth2client', 'googleapiclient.discovery']:
    logging.getLogger(noisy_logger).setLevel(logging.ERROR)
    # Désactiver les logs verbeux de fontTools et WeasyPrint
    logging.getLogger("fontTools.subset").setLevel(logging.WARNING)
    logging.getLogger("weasyprint").setLevel(logging.WARNING)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)





# Lecture des noms de fichiers de base de données
# DB_NAME = os.getenv("SQLITE_DB")
DB_NAME_TEST = os.getenv("SQLITE_TEST_DB")

# Emplacements Google Drive
GDRIVE_DB_FILE_ID_PROD = os.getenv("GDRIVE_DB_FILE_ID_PROD")
GDRIVE_DB_FILE_ID_TEST = os.getenv("GDRIVE_DB_FILE_ID_TEST")
# write_log(f"GDRIVE_DB_FILE_ID_TEST : {GDRIVE_DB_FILE_ID_TEST}")


if not GDRIVE_DB_FILE_ID_PROD:
    write_log("❌ GDRIVE_DB_FILE_ID_PROD non défini dans .env")
    raise ValueError("Variable GDRIVE_DB_FILE_ID_PROD manquante dans le fichier .env")

if not GDRIVE_DB_FILE_ID_TEST:
    write_log("❌ GDRIVE_DB_FILE_ID_TEST non défini dans .env")
    raise ValueError("Variable GDRIVE_DB_FILE_ID_TEST manquante dans le fichier .env")





# Formulaire d'inscription
username = StringField('Nom d\'utilisateur', validators=[DataRequired(), Length(min=4, max=25)])
email = StringField('Email', validators=[DataRequired(), Email()])

# 🔥 Correction : Le mot de passe devient optionnel
password = PasswordField('Mot de passe', validators=[Optional(), Length(min=6)])
confirm_password = PasswordField('Confirmer le mot de passe', validators=[Optional(), EqualTo('password')])

role = SelectField('Rôle', choices=[
    ('Gestionnaire', 'Gestionnaire'),
    ('car', 'Car'), ('user', 'Utilisateur'),
    ('admin', 'Administrateur')
], validators=[DataRequired()])
actif = SelectField('Actif', choices=[('Oui','Oui'), ('Non','Non')], default='Oui', validators=[DataRequired()])
submit = SubmitField('S\'inscrire')

def validate_email(self, email):
    # Vérifie si on est en mode modification d'utilisateur
    user_id = request.view_args.get('user_id')

    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email.data,)).fetchone()
    conn.close()

    if user and (user_id is None or user['id'] != user_id):
        raise ValidationError('Cet email est déjà utilisé par un autre utilisateur. Veuillez en choisir un autre.')

# ✅ Définition de la classe User manquante
class User(UserMixin):
    def __init__(self, id, username, email, password_hash, role):
        self.id = id
        self.username = username
        self.email = email
        self.password_hash = password_hash
        self.role = role

    def get_id(self):
        return str(self.id)

    def is_admin(self):
        return self.role == 'admin'


@app.route('/test_tri')
def test_tri():
    return render_template('test_tri.html')


@app.errorhandler(Exception)
def handle_exception(e):
    logging.exception("❌ Exception capturée dans Flask :")
    return "Une erreur est survenue", 500


def role_label_filter(value):
    return {
        "admin": "Administrateur",
        "gestionnaire": "Gestionnaire",
        "user": "Utilisateur",
        "car": "CAR"
    }.get(value, value)

# Enregistrement explicite dans l’environnement Jinja
app.jinja_env.filters['role_label'] = role_label_filter



# # Configuration des services Google
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")

SCOPES = ["https://www.googleapis.com/auth/drive"]

try:
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    drive_service = build("drive", "v3", credentials=credentials)
    # write_log(f"✅ Compte authentifié utilisé : {credentials.service_account_email}")

except Exception as e:
    write_log(f"❌ Erreur d'authentification avec Google Drive : {e}")
    drive_service = None

# Vérifier l'accès au dossier Google Drive
FOLDER_ID = "18UHHGeGn7kepW7YjOF0YBO0XcEPG_D4L"




def list_drive_files():
    if drive_service is None:
        write_log("❌ Service Google Drive non disponible.")
        return []
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name)"
        ).execute()
        files = results.get("files", [])
        if not files:
            write_log("⚠️ Aucun fichier trouvé dans le Drive Partagé.")
        else:
            for file in files:
                write_log(f"📄 Fichier trouvé : {file['name']} (ID: {file['id']})")
        return files
    except Exception as e:
        write_log(f"❌ Erreur lors de la récupération des fichiers Drive : {e}")
        return []



def generate_reset_token(email):
    """Génère un token sécurisé valable 30 minutes"""
    payload = {
        "email": email,
        "exp": datetime.utcnow() + timedelta(minutes=30)
    }
    return jwt.encode(payload, app.secret_key, algorithm="HS256")

def verify_reset_token(token):
    """Vérifie la validité du token"""
    try:
        payload = jwt.decode(token, app.secret_key, algorithms=["HS256"])
        return payload["email"]
    except jwt.ExpiredSignatureError:
        return None  # Token expiré
    except jwt.InvalidTokenError:
        return None  # Token invalide




@app.route('/test_email')
def test_email():
    """Test d'envoi d'un email via les paramètres SMTP"""
    from email.message import EmailMessage
    import smtplib

    SMTP_SERVER = os.getenv("SMTP_SERVER")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_EMAIL = os.getenv("SMTP_EMAIL")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

    msg = EmailMessage()
    msg.set_content("✅ Ceci est un test automatique depuis BA380.\n\nSi vous l'avez reçu, votre configuration SMTP est fonctionnelle.\n\nBien cordialement,\nL'équipe BA380")
    msg["Subject"] = "✅ Test SMTP automatique"
    msg["From"] = SMTP_EMAIL
    msg["To"] = SMTP_EMAIL  # s'envoie à soi-même

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.connect(SMTP_SERVER, SMTP_PORT)  # 👈 forcer la connexion
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        flash("✅ Email de test envoyé avec succès à vous-même.", "success")
    except Exception as e:
        flash(f"❌ Erreur lors de l'envoi du mail : {e}", "danger")

    return redirect(url_for("index"))




@app.route('/debug_photos')
def debug_photos():
    conn = get_db_connection()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT * FROM photos_benevoles").fetchall()
    conn.close()

    if not rows:
        return "📷 Aucune photo enregistrée pour l’instant."
    html = "<h3>Photos enregistrées :</h3><ul>"
    for row in rows:
        html += f"<li>Bénévole #{row['benevole_id']} → {row['filename']}</li>"
    html += "</ul>"
    return html








# Route exécutant un script système autorisé et affichant le journal + le résultat
import subprocess
from flask_login import login_required
import os
from collections import defaultdict



ENV = os.getenv("ENVIRONMENT", "DEV").upper()

if ENV == "PROD":
    FLAG_PATH = "/srv/ba38/prod/maintenance.flag"
else:
    FLAG_PATH = "/srv/ba38/dev/maintenance.flag"


@app.before_request
def check_maintenance_mode():
    base_dir = os.getenv("BA38_BASE_DIR")
    if not base_dir:
        return

    flag_path = os.path.join(base_dir, "maintenance.flag")

    if os.path.exists(flag_path):
        return render_template("maintenance.html"), 503

@app.before_request
def set_user_roles():
    if current_user.is_authenticated:
        session["roles_utilisateurs"] = get_user_roles(current_user.email)


@app.route('/check_login_status')
def check_login_status():
    if current_user.is_authenticated:
        return f"✅ Connecté en tant que : {current_user.username} (Rôle : {current_user.role})"
    else:
        return "❌ Non connecté"

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    email = verify_reset_token(token)
    if email is None:
        flash("Le lien de réinitialisation a expiré ou est invalide.", "danger")
        return redirect(url_for('login'))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        conn = get_db_connection()
        hashed_password = generate_password_hash(form.password.data)
        conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (hashed_password, email))
        conn.commit()
        conn.close()

        flash("Votre mot de passe a été mis à jour !", "success")
        return redirect(url_for('login'))

    return render_template(
'reset_password.html',form=form
    )

@app.route('/reset_password_request', methods=['GET', 'POST'])
def reset_password_request():
    from forms import RequestResetForm
    from utils import send_reset_email

    form = RequestResetForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user:
            # ✅ Générer le token et envoyer le mail
            token = generate_reset_token(email)
            send_reset_email(email, token)
            flash("📧 Demande envoyée. Consultez votre messagerie (et vos SPAM).", "success")
            return redirect(url_for('login'))
        else:
            flash("❌ Cette adresse email n’est pas reconnue.", "danger")

    return render_template("reset_password_request.html", form=form)


@app.route('/debug_session')
def debug_session():
    return f"Session complète : {dict(session)}"



@app.route('/test_set_session')
def test_set_session():
    session["user_role"] = "test_admin"
    session.modified = True
    return "Session enregistrée avec user_role = test_admin"

@app.route('/test_session_path')
def test_session_path():
    import tempfile
    return f"Flask stocke ses sessions dans : {tempfile.gettempdir()}"

# ✅ Fonction pour récupérer le rôle de l'utilisateur
def get_user_role():
    if current_user.is_authenticated:
        return getattr(current_user, 'role', 'Utilisateur')
    return session.get('user_role', 'Utilisateur')  # Valeur par défaut

# # ✅ Avant chaque requête, charge le rôle de l'utilisateur
# @app.before_request
# def load_user_role():
#     if not current_user.is_authenticated:
#         g.user_role = None
#         return

    roles = session.get("roles_utilisateurs", [])

    # 👑 Admin global si présent
    if ("admin", "global") in roles:
        g.user_role = "admin"
    else:
        g.user_role = "utilisateur"



@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    if user:
        return User(user['id'], user['username'], user['email'], user['password_hash'], user['role'])

    return None  # 🔥 Important : Retourner None si l'utilisateur n'est pas trouvé


@app.route('/set_test_user', methods=['POST'])
def set_test_user():
    session['test_user'] = True
    flash("Mode test activé : utilisation de la base de test.", "info")
    return redirect(url_for('index'))

@app.route('/unset_test_user', methods=['POST'])
def unset_test_user():
    session.pop('test_user', None)
    flash("Mode test désactivé : retour à la base de production.", "info")
    return redirect(url_for('index'))




# 🔐 Route pour l'inscription d'un utilisateur
@app.route('/register', methods=['GET', 'POST'])
def register():
    write_log("🚀 Route /register appelée")
    form = RegistrationForm()

    if form.validate_on_submit():
        try:
            conn = get_db_connection()

            # Vérifie si l'email est déjà utilisé
            user = conn.execute("SELECT * FROM users WHERE email = ?", (form.email.data,)).fetchone()
            if user:
                flash("Email déjà utilisé.", "danger")
                return redirect(url_for('register'))

            # Mot de passe requis à la création
            if not form.password.data:
                flash("Le mot de passe est requis pour l'inscription.", "danger")
                return redirect(url_for('register'))

            hashed_password = generate_password_hash(form.password.data)

            # ✅ Lecture des champs hors-formulaire
            try:
                app_bene = int(request.form.get("app_bene", 0))
                app_assos = int(request.form.get("app_assos", 0))
            except ValueError:
                flash("Erreur dans les droits d'accès sélectionnés.", "danger")
                return redirect(url_for('register'))

            # Insertion en base
            cursor = conn.execute(
                """
                INSERT INTO users (username, email, password_hash, role, actif, app_bene, app_assos)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    form.username.data,
                    form.email.data,
                    hashed_password,
                    form.role.data,
                    form.actif.data,
                    app_bene,
                    app_assos
                )
            )
            conn.commit()

            if cursor.rowcount == 0:
                write_log("⚠️ Échec de l'insertion SQL")
                flash("Erreur lors de l'inscription.", "danger")
                return redirect(url_for('register'))

            # Vérification post-insertion
            user = conn.execute("SELECT * FROM users WHERE email = ?", (form.email.data,)).fetchone()
            conn.close()

            if not user:
                write_log("❌ Utilisateur non retrouvé après insertion.")
                flash("Erreur après l'insertion : utilisateur non trouvé.", "danger")
                return redirect(url_for('register'))

            write_log(f"✅ Utilisateur enregistré : ID={user['id']}, Email={user['email']}, Rôle={user['role']}")
            flash("Inscription réussie. L'utilisateur peut maintenant se connecter.", "success")

            # Sauvegarde Drive
            upload_database()
            write_log("✅ Base de données sauvegardée sur Google Drive après l'inscription.")

            return redirect(url_for('index'))

        except Exception as e:
            write_log(f"❌ Exception SQL ou autre : {e}")
            flash(f"Erreur lors de l'inscription : {e}", "danger")
            return redirect(url_for('register'))

    else:
        if request.method == "POST":
            write_log(f"❌ Validation échouée : {form.errors}")

    return render_template("register.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    write_log(f"🧪 Tentative login pour {form.email.data}")

    if form.validate_on_submit():
        username = form.email.data.strip().lower()
        password = form.password.data

        write_log(f"🔐 Tentative de connexion pour l'utilisateur : {username} depuis IP {request.remote_addr}")
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE LOWER(email) = ?", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            if str(user["actif"]).strip().lower() not in ("1", "oui", "true"):
                flash("Votre compte n'est pas actif. Veuillez contacter l'administration.", "danger")
                write_log(f"❌ Tentative de connexion refusée pour {user['email']} : compte inactif")
                return redirect(url_for("login"))

            user_obj = User(user["id"], user["username"], user["email"], user["password_hash"], user["role"])
            login_user(user_obj)

            # 🧩 Initialisation session
            email = user["email"]
            role = user["role"]
            session["user_id"] = str(user["id"])
            session["username"] = user["username"]
            session["roles_utilisateurs"] = get_user_roles(email)


            # 👑 Si superadmin : ajoute accès complet + indicateur clair
            if role == "admin":
                # Accès complet
                session["roles_utilisateurs"] = [
                    ("benevoles", "ecriture"),
                    ("associations", "ecriture"),
                    ("fournisseurs", "ecriture"),
                    ("distribution", "ecriture"),
                    ("evenements", "ecriture"),
                ]

                # Indicateur rôle simple (facultatif)
                session["user_role"] = "admin"

            session.modified = True

            # ✅ Logs : fichier + base de données
            from utils import write_connexion_log
            write_connexion_log(user["id"], user["username"])
            log_connexion(user_obj, action="login")

            write_log(
                f"✅ Connexion réussie ! Utilisateur : {session.get('username')} "
                f"(Rôle: {session.get('user_role', 'utilisateur')})"
            )
            return redirect(url_for("index"))

        # 🔴 Erreurs de login
        if user:
            if not check_password_hash(user["password_hash"], password):
                write_log("❌ Mot de passe incorrect")
            else:
                write_log("✅ Mot de passe correct")
        else:
            write_log("❌ Utilisateur non trouvé")

        flash("Email ou mot de passe incorrect.", "danger")
        write_log(f"❌ Échec de connexion pour {username}")

    return render_template("login.html", form=form)



@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    flash("Déconnexion réussie.", "info")
    return redirect(url_for('login'))


# Route protégée
@app.route('/dashboard')
@login_required
def dashboard():
    return render_template(
'dashboard.html',username=current_user.username,role=current_user.role
    )




from flask_login import login_required


# ✅ Route pour la page maj_parametres
@app.route('/maj_parametres', methods=['GET', 'POST'])
@login_required
def maj_parametres():
    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == 'POST':
        action = request.form.get('action')
        param_id = request.form.get('id')

        if action == 'modifier':
            param_name = request.form.get('param_name', '').strip()
            param_value = request.form.get('param_value', '').strip()
            phone = request.form.get('phone', '').strip()
            mail = request.form.get('mail', '').strip()

            cursor.execute("""
                UPDATE parametres
                SET param_name = ?, param_value = ?, phone = ?, mail = ?
                WHERE id = ?
            """, (param_name, param_value, phone, mail, param_id))
            conn.commit()
            flash(f"✅ Paramètre modifié (ID = {param_id})", "success")
            upload_database()  # Sauvegarde automatique sur Google Drive
        elif action == 'supprimer':
            cursor.execute("DELETE FROM parametres WHERE id = ?", (param_id,))
            conn.commit()
            upload_database()  # Sauvegarde automatique sur Google Drive
            flash(f"🗑️ Paramètre supprimé (ID = {param_id})", "warning")

    parametres = cursor.execute("SELECT * FROM parametres ORDER BY id").fetchall()
    conn.close()
    return render_template(
"maj_parametres.html",parametres=parametres
    )


@app.route('/ajouter_parametre', methods=['POST'])
@login_required
def ajouter_parametre():
    param_name = request.form.get('param_name', '').strip()
    param_value = request.form.get('param_value', '').strip()
    phone = request.form.get('phone', '').strip()
    mail = request.form.get('mail', '').strip()

    if not param_name:
        flash("❌ Le champ 'Nom' est requis.", "danger")
        return redirect(url_for('maj_parametres'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO parametres (param_name, param_value, phone, mail)
            VALUES (?, ?, ?, ?)
        """, (param_name, param_value, phone, mail))
        conn.commit()
        flash("✅ Nouveau paramètre ajouté avec succès.", "success")
    except Exception as e:
        flash(f"❌ Erreur lors de l'ajout : {e}", "danger")
    finally:
        upload_database()  # Sauvegarde automatique sur Google Drive
        conn.close()

    return redirect(url_for('maj_parametres'))


def get_car_options():
    try:
        conn = get_db_connection()
        car_options = conn.execute("SELECT param_name, param_value FROM parametres").fetchall()
        conn.close()
        # Filtrer en ignorant la casse (car vs CAR)
        car_list = [row['param_value'] for row in car_options if row['param_name'].lower() == 'car']
        return car_list
    except Exception as e:
        write_log("ERREUR - get_car_options :", str(e))
        return []


def get_valid_columns():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(associations)")
    columns = [row[1] for row in cursor.fetchall()]
    conn.close()
    return columns

def query_db(query, args=(), one=False):
    conn = get_db_connection()
    cur = conn.execute(query, args)
    rv = cur.fetchall()
    cur.close()
    conn.close()
    return (rv[0] if rv else None) if one else rv





class PDF(FPDF):
    def header(self):
        """ Ajout du logo en haut à gauche de chaque page (avec vérification du chemin) """
        logo_path = "static/images/logo.png"  # Assurez-vous que ce fichier existe !
        if os.path.exists(logo_path):
            self.image(logo_path, 10, 8, 12.5)  # Taille réduite du logo
        else:
            write_log(f"⚠ Logo non trouvé : {logo_path}")

        self.set_font("Arial", 'B', 12)
        self.cell(200, 10, "Fiche de Visite", ln=True, align="C")
        self.ln(5)  # Petit espace après le titre

    def footer(self):
        """ Ajout d'un pied de page """
        self.set_y(-15)  # Position à 15 unités du bas
        self.set_font("Arial", size=10)

        # Texte à gauche
        self.cell(0, 10, "Banque Alimentaire de l'Isère - Service Partenariat", align="L")

        # Date à droite
        date_du_jour = datetime.today().strftime('%d/%m/%Y')
        self.cell(0, 10, date_du_jour, align="R")



@app.route('/maj_champs', methods=['GET', 'POST'])
def maj_champs():
    provenance = request.args.get("source", "index")

    if provenance == "benevoles":
        table = "benevoles"
        base_template = "base_bene.html"
    elif provenance in ("assos", "associations"):
        table = "associations"
        base_template = "base_assos.html"
    elif provenance in ("fournisseurs", "frs"):
        table = "fournisseurs"
        base_template = "base_frs.html"
    else:
        # fallback sûr
        table = "associations"
        base_template = "base_assos.html"


    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 🔄 Suppression
            if request.method == "POST" and request.form.get("delete_id", "").strip():
                champ_id = int(request.form["delete_id"])
                cursor.execute("DELETE FROM field_groups WHERE id = ?", (champ_id,))
                conn.commit()
                flash("🗑️ Champ supprimé.", "warning")
                return redirect(url_for("maj_champs", source=provenance))

            # ➕ Ajout d’un nouveau champ
            if request.method == "POST" and "new_field_name" in request.form:
                new_field = request.form["new_field_name"]
                new_group = request.form.get("new_group_name", "").strip() or "Sans"
                new_type = request.form.get("new_type_champ", None)
                display_order = request.form.get("new_display_order", "")
                display_order = int(display_order) if display_order.isdigit() else None

                cursor.execute("""
                    INSERT INTO field_groups (field_name, group_name, display_order, type_champ, appli)
                    VALUES (?, ?, ?, ?, ?)
                """, (new_field, new_group, display_order, new_type, table))
                conn.commit()
                flash("✅ Nouveau champ ajouté.", "success")
                return redirect(url_for("maj_champs", source=provenance))

            # 🔁 Mise à jour des champs existants
            if request.method == "POST":
                for key, value in request.form.items():
                    if key.startswith("id_"):
                        field_id = int(value)
                        new_field_name = request.form.get(f"field_name_{field_id}", "").strip()
                        new_group = request.form.get(f"group_name_{field_id}", "").strip()
                        new_type = request.form.get(f"type_champ_{field_id}", "").strip() or None
                        display_order = request.form.get(f"display_order_{field_id}", "")
                        display_order = int(display_order) if display_order.strip().isdigit() else None

                        cursor.execute("""
                            UPDATE field_groups
                            SET field_name = ?, group_name = ?, display_order = ?, type_champ = ?
                            WHERE id = ?
                        """, (new_field_name, new_group, display_order, new_type, field_id))

                conn.commit()
                flash("✅ Modifications enregistrées.", "success")
                upload_database()
                return redirect(url_for("maj_champs", source=provenance))

            # 🧩 Lecture des champs disponibles
            champs_table = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
            champs_config = {row["field_name"] for row in cursor.execute(
                "SELECT field_name FROM field_groups WHERE appli = ?", (table,)
            ).fetchall()}
            champs_disponibles = sorted(champs_table - champs_config)

            fields = cursor.execute("""
                SELECT * FROM field_groups
                WHERE appli = ?
                ORDER BY display_order
            """, (table,)).fetchall()

            grouped_fields = {}
            for f in fields:
                g = f["group_name"].strip() if f["group_name"] else "Sans"
                grouped_fields.setdefault(g, []).append(f)

            available_groups = [r["group_name"] for r in cursor.execute(
                "SELECT DISTINCT group_name FROM field_groups WHERE appli = ?", (table,)
            ).fetchall()]
            available_groups = [g.strip() if g and g.strip() else "Sans" for g in available_groups]
            if "Sans" not in available_groups:
                available_groups.append("Sans")

            types = cursor.execute("""
                SELECT param_value FROM parametres WHERE param_name = 'type_champ'
            """).fetchall()
            available_types = [r["param_value"] for r in types]

        return render_template("maj_champs.html",
            grouped_fields=grouped_fields,
            available_groups=available_groups,
            available_types=available_types,
            champs_disponibles=champs_disponibles,
            base_template=base_template,
            provenance=provenance,
            form=None  # 🔧 évite l'erreur dans {{ form.hidden_tag() }}
        )

    except Exception as e:
        write_log(f"❌ Erreur dans maj_champs : {e}")
        flash("Erreur interne", "danger")
        return "Erreur", 500


@app.route('/update_field_groups', methods=['POST'])
def update_field_groups():
    """Met à jour les champs dans la base de données."""
    try:
        form_data = request.form
        # write_log(f"🔍 Requête reçue pour mise à jour : {form_data}")

        conn = get_db_connection()
        cursor = conn.cursor()

        for key, value in form_data.items():
            if key.startswith("group_name_"):  # Mise à jour du `group_name`
                field_id = key.replace("group_name_", "")
                cursor.execute("UPDATE field_groups SET group_name = ? WHERE id = ?", (value, field_id))

            elif key.startswith("display_order_"):  # Mise à jour du `display_order`
                field_id = key.replace("display_order_", "")
                cursor.execute("UPDATE field_groups SET display_order = ? WHERE id = ?", (int(value), field_id))

        conn.commit()
        conn.close()

        write_log("✅ Mise à jour de la base confirmée !")
        flash("Mise à jour réussie !", "success")

        return redirect('/maj_champs')

    except Exception as e:
        write_log(f"❌ Erreur dans update_field_groups : {e}")
        return "Erreur lors de la mise à jour", 500


from flask_login import login_required, current_user

@app.route('/')
@login_required
def index():
    return render_template("index.html")





def get_grouped_fields():
    grouped_fields = {}
    rows = query_db("""
    SELECT field_name,
           COALESCE(group_name, 'Autres') AS group_name,
           COALESCE(display_order, 9999) AS display_order,
           appli
    FROM field_groups
    ORDER BY group_name, display_order
    """)

    for row in rows:
        group = row['group_name']
        field_data = {
            "name": row["field_name"],
            "display_order": row["display_order"],
            "appli": row["appli"]  # ✅ nécessaire pour filtrer ensuite
        }
        if group not in grouped_fields:
            grouped_fields[group] = []
        grouped_fields[group].append(field_data)

    return grouped_fields

@app.route('/routes')
def list_routes():
    output = []
    for rule in app.url_map.iter_rules():
        methods = ",".join(rule.methods)
        output.append(f"{rule.rule:50s} | {methods}")
    return "<pre>" + "\n".join(sorted(output)) + "</pre>"


# @app.errorhandler(404)
# def not_found_error(error):
#     write_log(f"❌ 404 - URL non trouvée : {request.url}")
#     return render_template('404.html', url=request.url), 404

@app.route('/test_404_page')
def test_404_page():
    try:
        return render_template('404.html', url="/url/introuvable")
    except Exception as e:
        return f"❌ Erreur lors du rendu du template : {e}", 500

@app.errorhandler(500)
def internal_error(error):
    import traceback
    write_log("🔥 Erreur 500 interceptée :")
    write_log(traceback.format_exc())
    return render_template("500.html", message=str(error)), 500


@app.route("/test_upload")
def test_upload():
    try:
        upload_database()
        return "✅ Upload manuel lancé avec succès."
    except Exception as e:
        return f"❌ Erreur pendant upload : {e}"

# if __name__ == '__main__':
#     FLASK_ENV = os.getenv("FLASK_ENV", "prod")
#     debug_mode = FLASK_ENV == "dev"

#     write_log(f"🚀 Lancement du serveur Flask (debug={debug_mode})")
#     write_log(f"🧪 Environnement FLASK_ENV = {FLASK_ENV}")
#     write_log(app.url_map)

#     app.run(debug=debug_mode)
#     write_log(str(app.url_map))  # Pour lister toutes les routes définies
#     app.run(debug=True)





@app.route('/test_email_api')
def test_email_api():
    """Test d'envoi d'un email via l’API Mailjet avec affichage des valeurs lues"""
    import os
    import requests
    from utils import write_log

    api_key = os.getenv("MAILJET_API_KEY")
    api_secret = os.getenv("MAILJET_API_SECRET")
    sender = os.getenv("MAILJET_SENDER")
    destinataire = sender

    write_log("📤 Test API Mailjet — Début")
    write_log(f"MAILJET_SENDER = {sender}")
    write_log(f"MAILJET_API_KEY = {repr(api_key)[:8]}... ({'✔️' if api_key else '❌'})")
    write_log(f"MAILJET_API_SECRET = {repr(api_secret)[:8]}... ({'✔️' if api_secret else '❌'})")

    data = {
        "Messages": [
            {
                "From": {"Email": sender},
                "To": [{"Email": destinataire}],
                "Subject": "✅ Test API Mailjet depuis BA380",
                "TextPart": (
                    "Ceci est un test automatique via l'API Mailjet.\n\n"
                    "Si vous recevez ce message, tout fonctionne !"
                )
            }
        ]
    }

    try:
        response = requests.post(
            "https://api.mailjet.com/v3.1/send",
            auth=(api_key, api_secret),
            json=data
        )
        write_log(f"📬 Statut réponse Mailjet : {response.status_code}")
        write_log(f"📨 Réponse Mailjet : {response.text}")
        response.raise_for_status()

        flash("✅ Email de test envoyé avec succès via l’API Mailjet.", "success")

    except Exception as e:
        write_log(f"❌ Erreur Mailjet : {e}")
        flash(f"❌ Erreur Mailjet : {e}", "danger")

    return redirect(url_for("debug_bp.admin_scripts"))




@app.route('/reset_password_ui', methods=['GET', 'POST'])
@login_required
def reset_password_ui():
    if g.user_role != 'admin':
        flash("⛔ Accès réservé à l'administrateur.", "danger")
        return redirect(url_for('index'))

    db_path = get_db_path()
    message = None

    if request.method == 'POST':
        email = request.form.get('email')
        new_password = request.form.get('new_password')

        if not email or not new_password:
            flash("Veuillez renseigner un email et un nouveau mot de passe.", "warning")
        else:
            hashed = generate_password_hash(new_password)
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE email = ?",
                    (hashed, email)
                )
                conn.commit()
            flash(f"✅ Mot de passe réinitialisé pour {email}.", "success")

    # Liste des utilisateurs
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT email, username FROM users WHERE actif = 1").fetchall()

    return render_template("reset_password_ui.html", users=users)

@app.route("/debug_database")
def debug_database():
    return f"📂 Base active : {get_db_path()}"


@app.route("/test_flash")
def test_flash():
    from flask import flash, redirect, url_for
    flash("✅ Test de message Flash réussi", "success")
    return redirect(url_for("index"))



if __name__ == "__main__":
    FLASK_ENV = os.getenv("FLASK_ENV", "prod")
    debug_mode = FLASK_ENV == "dev"

    write_log(f"🚀 Lancement du serveur Flask (debug={debug_mode})")
    app.run(host="127.0.0.1", port=5000, debug=debug_mode)



# app = app
