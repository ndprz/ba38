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
# Flask
# --------------------------------------------------

from datetime import datetime, timedelta, timezone
from ba38_utilitaires.core import get_db_connection, write_log, send_reset_email, get_user_roles, get_db_path, get_db_info, upload_database, get_version, get_all_users, format_tel, get_param_value, get_real_db_connection
from ba38_utilitaires.core import get_user_info,has_access, is_admin_global, format_date_fr, build_menu, require_admin_global
from ba38_utilitaires.core import is_blocked, record_attempt, reset_attempts, is_suspicious_ip, is_suspicious_ua, get_attempt_count, require_access
from ba38_utilitaires.core import date_fr
from ba38_securite.core import (
    secondes_verrouillage, message_verrouillage, notifier_verrouillage, enregistrer_echec, effacer_echecs,
    pays_refuse, message_pays_refuse, notifier_pays_refuse_mot_de_passe_correct, apres_connexion_reussie,
    appareil_de_confiance, telephones_du_compte, CONFIANCE_JOURS, envoyer_code, verifier_code, masquer_numero, masquer_email,
)
from ba38_utilitaires.organisation import get_organisation
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, g, current_app, send_from_directory, abort, make_response
from flask_login import current_user, LoginManager, UserMixin, login_user, logout_user, login_required
from flask_session import Session
from flask_wtf.csrf import CSRFProtect
from flask_wtf.csrf import CSRFError
from wtforms import StringField, PasswordField, SubmitField, SelectField
from wtforms.validators import Optional, DataRequired, Email, Length, EqualTo, ValidationError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import MethodNotAllowed, HTTPException
from docx import Document
from fpdf import FPDF
from weasyprint import HTML
from ba38_utilitaires.forms import LoginForm, RegistrationForm, ResetPasswordForm
from googleapiclient.discovery import build
from google.oauth2 import service_account
from pathlib import Path
import jwt
import re



# --------------------------------------------------
# BLUEPRINTS (Modules fonctionnels)
# --------------------------------------------------

from ba38_planning import planning_bp  # Ramasse
from ba38_planning import planning_dist_bp  # Distribution
from ba38_benevoles import benevoles_bp
from ba38_planning import planning_palettes_bp
from ba38_planning import planning_cuisine_bp
from ba38_partenaires import partenaires_bp
from ba38_planning import planning_tournees_bp
from ba38_planning import planning_pesee_bp
from ba38_export import export_bp
from ba38_planning import planning_vif_bp
from ba38_utilitaires import debug_bp
from ba38_planning import planning_utils_bp
from ba38_distribution import distribution_bp
from scripts.rename_field import rename_bp
from ba38_admin import admin_bp
from ba38_export import export_data_bp
from ba38_fournisseurs import fournisseurs_bp
from ba38_cuisine import production_cuisine_bp, cuisine_hygiene_bp
from ba38_tresorerie import tresorerie_bp
from ba38_cerfa import cerfa_bp
from ba38_partenaires import fiches_visite_bp
from ba38_partenaires import annexe1bis_bp, webhook_yousign
from ba38_utilitaires import signature_bp
from ba38_evenements import evenements_bp
from ba38_tresorerie import factures_bp
from ba38_planning import planning_report_bp
from ba38_planning import esope_bp  # Planning ESOPE
from ba38_utilitaires import aide_bp
from ba38_engagements import engagements_bp
from ba38_benevoles import droit_image_bp
from ba38_partenaires import indicateurs_bp
from ba38_utilitaires import emails_bp
from ba38_tresorerie import participation_bp
from ba38_admin import applications_bp
from ba38_sms import sms_bp
from ba38_sms.routes import sms_webhook_mo
from ba38_admin import organisation_bp
from ba38_collecte import collecte_bp
from ba38_utilitaires import vif_bp
from ba38_securite import securite_bp




# Initialisation Flask
app = Flask(__name__)

# 🔒 Protection CSRF globale
csrf = CSRFProtect(app)

# Webhook Yousign : appelé par un serveur externe, pas de token CSRF possible
# (authentification propre par signature HMAC, cf. ba38_annexe1bis.webhook_yousign)
csrf.exempt(webhook_yousign)

# Webhook SmsFactor (réponses SMS entrantes) : appelé par un serveur externe,
# pas de token CSRF possible (authentification par allowlist IP, cf. ba38_sms.routes.sms_webhook_mo)
csrf.exempt(sms_webhook_mo)

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    write_log(f"⚠️ CSRF rejeté sur {request.path} : {e.description}")
    flash("Session expirée ou requête invalide. Veuillez réessayer.", "warning")
    return redirect(request.referrer or url_for('login'))

app.jinja_env.filters["date_fr"] = date_fr

# =====================================================
# 🔹 Injection menu global
# =====================================================

from ba38_utilitaires.core import build_menu

@app.context_processor
def inject_menu():
    try:
        return dict(main_menu=build_menu() or {})
    except:
        return dict(main_menu={})


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

app.logger.handlers = []
app.logger.setLevel(logging.INFO)
app.logger.propagate = False
app.logger.addHandler(file_handler)


app.secret_key = os.getenv("FLASK_SECRET_KEY")
app.jinja_env.filters['format_tel'] = format_tel
app.jinja_env.filters['format_date_fr'] = format_date_fr

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

# 📱 Comptes "mode tablette" (postes partagés verrouillés en kiosque) : la
# session dure DUREE_SESSION_TABLETTE au lieu de PERMANENT_SESSION_LIFETIME,
# pour ne pas avoir à se reconnecter chaque matin / après un week-end.
# Seules les sessions marquées session["mode_tablette"] (posé par
# ouvrir_session) sont concernées ; le contrôle du compte à chaque requête
# (actif, jeton de session → déconnexion à distance) reste inchangé.
DUREE_SESSION_TABLETTE = timedelta(days=30)


class _SessionInterfaceTablette(type(app.session_interface)):
    def _upsert_session(self, session_lifetime, session, store_id):
        if session.get("mode_tablette"):
            session_lifetime = DUREE_SESSION_TABLETTE
        return super()._upsert_session(session_lifetime, session, store_id)

    def get_expiration_time(self, app, session):
        if session.permanent and session.get("mode_tablette"):
            return datetime.now(timezone.utc) + DUREE_SESSION_TABLETTE
        return super().get_expiration_time(app, session)


app.session_interface.__class__ = _SessionInterfaceTablette


# ✅ Augmente la taille maximale des requêtes POST à 10 Mo
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024
app.config["LOG_FILE"] = LOG_FILE



@app.route('/documentation-interne/<nom_fichier>')
@login_required
def telecharger_documentation_interne(nom_fichier):
    """Téléchargement de documents de travail internes (notes, comparatifs
    de solutions...) qui ne relèvent pas de la documentation utilisateur
    d'un module — liste blanche fermée, jamais de chemin construit depuis
    l'entrée utilisateur."""
    documents_autorises = {
        "Securisation_Saisie_Pesee_Palox.docx",
    }
    if nom_fichier not in documents_autorises:
        abort(404)
    chemin = os.path.join(app.root_path, "documentation_interne", nom_fichier)
    if not os.path.exists(chemin):
        abort(404)
    return send_file(chemin, as_attachment=True, download_name=nom_fichier)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'static/images'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )

@app.route('/sw_photo_benevole.js')
def sw_photo_benevole():
    return send_from_directory(
        os.path.join(app.root_path, 'static/pwa'),
        'sw_photo_benevole.js',
        mimetype='application/javascript'
    )

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

    ip = get_real_ip()

    current_app.logger.warning(
        f"405 {request.method} {request.path} "
        f"user={getattr(current_user,'id','anon')} "
        f"IP={ip}"
    )
    return e


# =========================
# Injection globale Jinja
# =========================
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

    if not current_app.debug:
        return

    try:
        ip = get_real_ip()
    except Exception:
        ip = request.remote_addr

    ua = request.headers.get("User-Agent", "unknown")[:120]

    if request.method == "GET":
        return

    if any(request.path.startswith(p) for p in EXCLUDED_LOG_PATHS):
        return

    try:
        if current_user and getattr(current_user, "is_authenticated", False):
            user = current_user.id
        else:
            user = "anonymous"
    except Exception:
        user = "unknown"

    # 🔍 détection
    ua_suspect = is_suspicious_ua(ua)
    ip_suspect = is_suspicious_ip(ip)

    flags = []
    if ua_suspect:
        flags.append("UA")
    if ip_suspect:
        flags.append("IP")

    flag_str = f"⚠️[{','.join(flags)}]" if flags else ""

    # write_log(
    #     f"➡️ REQ {request.method} {request.path} | "
    #     f"user={user} | IP={ip} | UA={ua} {flag_str}"
    # )



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
app.register_blueprint(planning_cuisine_bp, url_prefix="/")
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
app.register_blueprint(production_cuisine_bp, url_prefix="/production-cuisine")
app.register_blueprint(cuisine_hygiene_bp, url_prefix="/hygiene-cuisine")
app.register_blueprint(tresorerie_bp)
app.register_blueprint(cerfa_bp)
app.register_blueprint(fiches_visite_bp)
app.register_blueprint(annexe1bis_bp)
app.register_blueprint(signature_bp)
app.register_blueprint(evenements_bp)
app.register_blueprint(factures_bp)
app.register_blueprint(planning_report_bp)
app.register_blueprint(esope_bp)
app.register_blueprint(aide_bp)
app.register_blueprint(engagements_bp)
app.register_blueprint(droit_image_bp)
app.register_blueprint(indicateurs_bp)
app.register_blueprint(emails_bp)
app.register_blueprint(participation_bp)
app.register_blueprint(applications_bp)
app.register_blueprint(organisation_bp)
app.register_blueprint(collecte_bp)
app.register_blueprint(vif_bp)
app.register_blueprint(sms_bp)
app.register_blueprint(securite_bp)


# Migration : ajout colonne email_verifie si absente
def _migrate_email_verifie():
    try:
        conn = get_db_connection()
        conn.execute("ALTER TABLE users ADD COLUMN email_verifie INTEGER DEFAULT 0")
        conn.commit()
        write_log("✅ Migration: colonne email_verifie ajoutée")
    except Exception:
        pass  # colonne déjà présente
    finally:
        conn.close()

_migrate_email_verifie()


# Enregistrement de la fonction has_access dans l’environnement Jinja
app.jinja_env.globals['has_access'] = has_access

# Alerte SMS active (ex: crédit SmsFactor épuisé) affichée en bandeau
from ba38_sms.routes import get_sms_alerte_active, get_nb_sms_reponses_non_lues
app.jinja_env.globals['sms_alerte_active'] = get_sms_alerte_active
app.jinja_env.globals['nb_sms_reponses_non_lues'] = get_nb_sms_reponses_non_lues


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


def log_connexion(user, action="login", via=None):
    """📋 Enregistre une connexion ou déconnexion dans la table log_connexions."""
    try:
        db_path = get_db_path()
        ip = get_real_ip()
        user_agent = request.headers.get("User-Agent")
        env = os.getenv("ENVIRONMENT", "prod").lower()
        now = datetime.utcnow().isoformat()

        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO log_connexions (email, username, environ, ip, user_agent, timestamp, action, via)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user.email, user.username, env, ip, user_agent, now, action, via))
    except Exception as e:
        write_log(f"⚠️ Erreur log_connexion : {e}")




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
    def __init__(self, id, username, email, password_hash, role, test_only=False):
        self.id = id
        self.username = username
        self.email = email
        self.password_hash = password_hash
        self.role = role
        self.test_only = bool(test_only)

    def get_id(self):
        return str(self.id)

    def is_admin(self):
        return self.role == 'admin'


@app.route('/test_tri')
def test_tri():
    return render_template('test_tri.html')


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
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
    FLAG_PATH = os.getenv("BASE_PATH", "/srv/ba38") + "/prod/maintenance.flag"
else:
    FLAG_PATH = os.getenv("BASE_PATH", "/srv/ba38") + "/dev/maintenance.flag"


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
    if not current_user.is_authenticated:
        return

    # 🕒 Horodatage de la dernière interaction réelle (cf. admin_scripts → connexions actives)
    session["last_activity"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    session["last_ip"] = get_real_ip()
    session["user_agent"] = request.headers.get("User-Agent", "")

    # =====================================================
    # 🔒 COMPTE RESTREINT AU MODE TEST → verrouillage permanent
    # =====================================================
    if getattr(current_user, "test_only", False):
        session["test_user"] = True

    # =====================================================
    # 🧪 MODE TEST → bypass des droits DB anonymisée
    # =====================================================
    if session.get("test_user"):
        session["roles_utilisateurs"] = [
            ("benevoles", "ecriture"),
            ("associations", "ecriture"),
            ("fournisseurs", "ecriture"),
            ("distribution", "ecriture"),
            ("evenements", "ecriture"),
            ("planning", "ecriture"),
            ("planning_esope", "ecriture"),
        ]
        return

    # =====================================================
    # 🔐 MODE NORMAL
    # =====================================================
    session["roles_utilisateurs"] = get_user_roles(current_user.email)

@app.route('/health')
def health():
    """
    Sonde de supervision publique (UptimeRobot) : SELECT 1 sur la base, aucune donnée renvoyée.
    Ouverture en lecture seule (mode=ro) pour qu'une base absente donne KO au lieu d'être créée vide.
    """
    try:
        conn = sqlite3.connect(f"file:{get_db_path()}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        resp = make_response("OK", 200)
    except Exception:
        resp = make_response("KO", 503)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp

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
    from ba38_utilitaires.forms import RequestResetForm
    from ba38_utilitaires.core import send_reset_email

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
@login_required
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
    # ⚠️ Toujours la base réelle (pas get_db_connection) : l'identité d'un
    # utilisateur ne doit jamais dépendre de session['test_user'], sous
    # peine de déconnexion automatique si son compte n'existe pas (encore)
    # dans la base de test, ou de blocage au login si le flag reste
    # incohérent après une déconnexion automatique.
    conn = get_real_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    if user:
        # 🔒 Compte désactivé (admin, inactivité) → session coupée tout de suite
        if str(user["actif"]).strip().lower() not in ("1", "oui", "true"):
            return None
        # 🔒 Jeton de session changé ("ce n'est pas moi") → toutes les
        # sessions ouvertes avant le changement tombent
        if user["jeton_session"] and session.get("jeton_session") != user["jeton_session"]:
            return None
        # 📱 Mode tablette retiré par l'admin → la session repasse à 24 h
        if session.get("mode_tablette") and not user["mode_tablette"]:
            session.pop("mode_tablette", None)
        return User(
            user['id'], user['username'], user['email'],
            user['password_hash'], user['role'],
            test_only=user['test_only']
        )

    return None  # 🔥 Important : Retourner None si l'utilisateur n'est pas trouvé


@app.route('/set_test_user', methods=['POST'])
def set_test_user():
    session['test_user'] = True
    flash("Mode test activé : utilisation de la base de test.", "info")
    return redirect(url_for('index'))

@app.route('/unset_test_user', methods=['POST'])
def unset_test_user():
    if current_user.is_authenticated and getattr(current_user, "test_only", False):
        flash("⛔ Ce compte est restreint au mode test, impossible de le désactiver.", "danger")
        return redirect(url_for('index'))

    session.pop('test_user', None)
    flash("Mode test désactivé : retour à la base de production.", "info")
    return redirect(url_for('index'))




# Route d'inscription publique (email ba380*@banquealimentaire.org requis)
@app.route('/register', methods=['GET', 'POST'])
def register():
    from ba38_utilitaires.core import send_verification_email
    write_log("🚀 Route /register appelée")
    form = RegistrationForm()

    if form.validate_on_submit():
        try:
            email = form.email.data.strip().lower()
            conn = get_db_connection()

            existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                conn.close()
                flash("Email déjà utilisé.", "danger")
                return redirect(url_for('register'))

            hashed_password = generate_password_hash(form.password.data)

            cursor = conn.execute(
                """
                INSERT INTO users (username, email, password_hash, role, actif, app_bene, app_assos, email_verifie)
                VALUES (?, ?, ?, 'user', 'Non', 0, 0, 0)
                """,
                (form.username.data, email, hashed_password)
            )
            conn.commit()
            conn.close()

            if cursor.rowcount == 0:
                write_log("⚠️ Échec de l'insertion SQL")
                flash("Erreur lors de l'inscription.", "danger")
                return redirect(url_for('register'))

            write_log(f"✅ Compte créé (inactif) : {email}")

            token = generate_reset_token(email)
            send_verification_email(email, token)

            return redirect(url_for('register_attente', email=email))

        except Exception as e:
            write_log(f"❌ Exception inscription : {e}")
            flash(f"Erreur lors de l'inscription : {e}", "danger")
            return redirect(url_for('register'))

    else:
        if request.method == "POST":
            write_log(f"❌ Validation échouée : {form.errors}")

    return render_template("register.html", form=form)


@app.route('/register/attente')
def register_attente():
    email = request.args.get('email', '')
    return render_template("email_verifie_attente.html", email=email)


@app.route('/verify_email/<token>')
def verify_email(token):
    email = verify_reset_token(token)
    if email is None:
        flash("Ce lien de vérification est invalide ou expiré.", "danger")
        return redirect(url_for('login'))

    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user:
        conn.close()
        flash("Compte introuvable.", "danger")
        return redirect(url_for('login'))

    if user["email_verifie"]:
        conn.close()
        flash("Votre email est déjà vérifié. Vous pouvez vous connecter.", "info")
        return redirect(url_for('login'))

    conn.execute(
        "UPDATE users SET actif = 'Oui', email_verifie = 1 WHERE email = ?",
        (email,)
    )
    conn.commit()
    conn.close()

    upload_database()
    write_log(f"✅ Email vérifié, compte activé : {email}")
    flash("Votre email est confirmé. Votre compte est maintenant actif — vous pouvez vous connecter.", "success")
    return redirect(url_for('login'))

from ba38_utilitaires.core import (
    get_real_ip,
    is_bad_user_agent,
    is_ip_blocked,
    record_login_attempt
)

import time


def ouvrir_session(user, ip, ua, via=None, confiance_jours=0):
    """
    Ouvre la session d'un utilisateur authentifié (mot de passe + éventuelle
    2FA déjà vérifiés). Point unique pour la connexion simple, l'application
    2FA et le code SMS/email, pour que les sessions soient identiques.
    via : moyen de 2FA ("application", "SMS Marie", "email", "appareil de
          confiance"), tracé dans log_connexions (qui, sur un compte partagé).
    """
    user_obj = User(
        user["id"],
        user["username"],
        user["email"],
        user["password_hash"],
        user["role"],
        test_only=user["test_only"]
    )

    login_user(user_obj)

    # 🔒 Compte restreint au mode test → verrouillage immédiat
    if user_obj.test_only:
        session["test_user"] = True

    # 🔴 IMPORTANT : session persistante
    session.permanent = True
    session.modified = True

    # 📱 Poste tablette : session de 30 jours (cf. _SessionInterfaceTablette)
    if user["mode_tablette"]:
        session["mode_tablette"] = True
    else:
        session.pop("mode_tablette", None)

    # session métier
    email = user["email"]
    role = user["role"]

    session["user_id"] = str(user["id"])
    session["username"] = user["username"]
    session["email"] = email
    session["environment"] = os.getenv("ENVIRONMENT", "prod").lower()
    session["login_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    session["login_ip"] = ip
    session["last_ip"] = ip
    session["user_agent"] = ua
    session["roles_utilisateurs"] = get_user_roles(email)
    if via:
        session["login_via"] = via

    # 🔑 Admin → droits complets
    if role == "admin":
        session["roles_utilisateurs"] = [
            ("benevoles", "ecriture"),
            ("associations", "ecriture"),
            ("fournisseurs", "ecriture"),
            ("distribution", "ecriture"),
            ("evenements", "ecriture"),
        ]
        session["user_role"] = "admin"

    # nettoyage sécurité
    session.pop("pre_2fa_user", None)
    session.pop("code_2fa", None)

    # logs
    from ba38_utilitaires.core import write_connexion_log
    write_connexion_log(user["id"], user["username"])
    log_connexion(user_obj, action="login", via=via)
    apres_connexion_reussie(user, ip, ua, confiance_jours=confiance_jours)
    return user_obj


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    ============================================================================
    🔐 ROUTE LOGIN SÉCURISÉE
    ============================================================================
    Protection mise en place :
    - blocage IP temporaire (Flask)
    - blocage bots simples (User-Agent)
    - ralentissement anti brute force
    - comptage des échecs uniquement
    - compatibilité Fail2ban via logs "❌ LOGIN KO"
    - gestion 2FA existante conservée
    ============================================================================
    """

    start = time.time()

    # 🔍 Infos requête
    ip = get_real_ip()
    ua = request.headers.get("User-Agent", "")

    # ============================================================================
    # 🚫 1. Blocage IP temporaire (niveau Flask)
    # ============================================================================
    if is_ip_blocked(ip):
        write_log(f"🚫 IP bloquée (Flask) : {ip}")
        abort(429)

    # ============================================================================
    # 🤖 2. Blocage bots évidents (python-requests, curl, etc.)
    # ============================================================================
    if is_bad_user_agent():
        write_log(f"🤖 Bot bloqué : IP={ip} UA={ua}")
        abort(403)

    # ============================================================================
    # 🐢 3. Ralentissement global anti brute force
    # ============================================================================

    time.sleep(0.5)

    # ============================================================================
    # 🔐 Formulaire
    # ============================================================================
    form = LoginForm()

    # 🔒 Si déjà connecté → redirection
    from flask_login import current_user
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    # ============================================================================
    # 📩 Traitement POST (tentative de login)
    # ============================================================================
    if request.method == "POST" and not form.validate():
        write_log(f"❌ LOGIN FORM INVALID | IP={ip} | errors={form.errors}")
        flash("Email ou mot de passe invalide.", "danger")
        return render_template("login.html", form=form)

    if form.validate_on_submit():

        username = form.email.data.strip().lower()
        password = form.password.data

        write_log(f"🔐 LOGIN tentative {username} | IP={ip}")

        # 🧹 Une tentative de connexion repart toujours de zéro : si un
        # session['test_user'] est resté accroché (ex. déconnexion
        # automatique Flask-Login suite à un compte introuvable en base de
        # test), on ne veut surtout pas vérifier le mot de passe contre la
        # mauvaise base.
        session.pop("test_user", None)

        # 🔎 Recherche utilisateur (toujours la base réelle, cf. load_user)
        conn = get_real_db_connection()
        if "@" in username:
            user = conn.execute(
                "SELECT * FROM users WHERE LOWER(email) = ?",
                (username,)
            ).fetchone()
        else:
            # 📱 Identifiant court (ex. "tab1") : réservé aux comptes mode tablette
            user = conn.execute(
                "SELECT * FROM users WHERE mode_tablette = 1 AND LOWER(identifiant_tablette) = ?",
                (username,)
            ).fetchone()
        conn.close()

        # ============================================================================
        # 🔒 Verrouillage par compte (toutes IP confondues), AVANT de vérifier
        # le mot de passe : sinon une attaque qui insiste finit par entrer.
        # Pas de "LOGIN KO" ici (fail2ban) : rien n'a été essayé.
        # ============================================================================
        reste = secondes_verrouillage(username)
        if reste:
            write_log(f"🔒 COMPTE VERROUILLÉ {username} | IP={ip} | reste={reste}s")
            flash(message_verrouillage(reste), "danger")
            return render_template("login.html", form=form)

        mot_de_passe_ok = bool(user and check_password_hash(user["password_hash"], password))

        # ============================================================================
        # 🌍 Filtre des pays : même message que le mot de passe soit bon ou non
        # (ne rien révéler à un attaquant étranger). Bon mot de passe → alerte
        # titulaire + admin ; mauvais → compté comme un échec normal.
        # ============================================================================
        pays = pays_refuse(ip, user)
        if pays:
            if mot_de_passe_ok:
                write_log(f"🌍 LOGIN PAYS REFUSÉ (mot de passe correct) {username} | IP={ip} ({pays})")
                notifier_pays_refuse_mot_de_passe_correct(user, ip, pays)
            else:
                enregistrer_echec(ip, username)
                write_log(f"❌ LOGIN KO {username} | IP={ip} | pays={pays}")
            flash(message_pays_refuse(), "danger")
            return render_template("login.html", form=form)

        # ============================================================================
        # ✅ CAS 1 : LOGIN OK
        # ============================================================================
        if mot_de_passe_ok:

            # 🔁 Reset compteur brute force
            effacer_echecs(username)

            # ============================================================================
            # 🔓 Vérification compte actif (avant la 2FA : un compte désactivé
            # ne doit même pas recevoir de code)
            # ============================================================================
            if str(user["actif"]).strip().lower() not in ("1", "oui", "true"):
                write_log(f"❌ Compte inactif : {user['email']} | IP={ip}")
                flash("Votre compte n'est pas actif. Veuillez contacter l'administration.", "danger")
                return redirect(url_for("login"))

            # ============================================================================
            # 🔐 Gestion 2FA
            # ============================================================================
            # Le 2FA est demandé si : imposé par un admin (force_2fa), déjà activé
            # par l'utilisateur (totp_enabled, quel que soit force_2fa), ou activé
            # volontairement via la case à cocher de l'écran de connexion.
            # Sauf sur un appareil de confiance (code déjà saisi < CONFIANCE_JOURS jours).
            besoin_2fa = user["force_2fa"] == 1 or user["totp_enabled"] or form.activer_2fa.data
            if besoin_2fa and not (appareil_de_confiance(user) and not form.activer_2fa.data):

                session["pre_2fa_user"] = user["id"]
                session.modified = True

                # Application d'authentification déjà configurée
                if user["totp_enabled"]:
                    return redirect(url_for("login_2fa"))

                # Activation volontaire de l'application (case à cocher)
                if form.activer_2fa.data:
                    return redirect(url_for("setup_2fa"))

                # 2FA imposée sans application → code par SMS ou email
                return redirect(url_for("login_code"))

            via = "appareil de confiance" if besoin_2fa else None
            ouvrir_session(user, ip, ua, via=via)
            write_log(f"✅ LOGIN OK {username} | IP={ip}{' | ' + via if via else ''} | {round(time.time()-start,2)}s")

            return redirect(url_for("index"))

        # ============================================================================
        # ❌ CAS 2 : LOGIN KO
        # ============================================================================
        # 👉 on enregistre uniquement ici (important)
        enregistrer_echec(ip, username)

        write_log(f"❌ LOGIN KO {username} | IP={ip} | COUNT={get_attempt_count(ip, username)}")

        # 🚫 Ce dernier échec vient de verrouiller le compte → titulaire prévenu
        reste = secondes_verrouillage(username)
        if reste:
            write_log(f"🚫 Verrouillage compte {username} | IP={ip}")
            notifier_verrouillage(username, ip)
            flash(message_verrouillage(reste), "danger")
            return render_template("login.html", form=form)

        # 🐢 ralentissement supplémentaire si user inexistant (anti enumeration)
        if not user:
            time.sleep(1)

        flash("Email ou mot de passe incorrect.", "danger")

    # ============================================================================
    # 📄 Affichage page login
    # ============================================================================
    return render_template("login.html", form=form)





# ============================================================================
# 🪵 LOGIN 2FA (pour admins dans un premier temps   )
# ============================================================================
import pyotp
import qrcode
import io
import base64

import pyotp
import qrcode
import io
import base64

@app.route("/login_2fa", methods=["GET", "POST"])
def login_2fa():

    start = time.time()

    from flask_login import current_user

    ip = get_real_ip()
    ua = request.headers.get("User-Agent", "")

    if current_user.is_authenticated:
        from flask import make_response

        response = make_response(redirect(url_for("index")))
        response.headers["Cache-Control"] = "no-store"
        return response

    user_id = session.get("pre_2fa_user")
    if not user_id:
        return redirect(url_for("login"))

    conn = get_real_db_connection()
    user = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,)
    ).fetchone()
    conn.close()

    if not user or not user["totp_secret"]:
        return redirect(url_for("login"))

    if request.method == "POST":

        write_log(f"🔐 LOGIN_2FA user_id={user_id} {user['email']} | IP{ip}")

        # 🔒 Les échecs de code comptent dans le verrouillage du compte
        # (sinon 1 million de codes à essayer sans limite)
        login_cle = user["email"].strip().lower()
        reste = secondes_verrouillage(login_cle)
        if reste:
            session.pop("pre_2fa_user", None)
            flash(message_verrouillage(reste), "danger")
            return redirect(url_for("login"))

        # Le chemin 2FA court-circuite la vérification "actif" du login simple
        if str(user["actif"]).strip().lower() not in ("1", "oui", "true"):
            session.pop("pre_2fa_user", None)
            write_log(f"❌ Compte inactif (2FA) : {user['email']} | IP={ip}")
            flash("Votre compte n'est pas actif. Veuillez contacter l'administration.", "danger")
            return redirect(url_for("login"))

        code = request.form.get("code")
        totp = pyotp.TOTP(user["totp_secret"])

        if totp.verify("".join(ch for ch in (code or "") if ch.isdigit()), valid_window=1):
            effacer_echecs(login_cle)

            ouvrir_session(user, ip, ua, via="application",
                           confiance_jours=CONFIANCE_JOURS if request.form.get("confiance") else 0)

            write_log(f"✅ LOGIN 2FA OK user_id={user_id} {user['email']} | IP={ip} | {round(time.time()-start,2)}s")

            return redirect(url_for("index"))

        enregistrer_echec(ip, login_cle)
        write_log(f"❌ LOGIN 2FA KO user_id={user_id} {user['email']} | IP={ip}")
        if secondes_verrouillage(login_cle):
            notifier_verrouillage(login_cle, ip)
        flash("❌ Code invalide", "danger")

    return render_template("login_2fa.html", confiance_jours=CONFIANCE_JOURS)


# ============================================================================
# 📱 LOGIN PAR CODE SMS / EMAIL (2FA sans application)
# ============================================================================
@app.route("/login_code", methods=["GET", "POST"])
def login_code():
    """
    Étape 2 de la connexion quand la 2FA est imposée sans application :
    1. choix du destinataire (téléphones du compte, masqués + email du compte)
       → un compte partagé a un numéro par personne ;
    2. saisie du code reçu (+ "faire confiance à cet appareil CONFIANCE_JOURS jours").
    Les échecs comptent dans le verrouillage du compte.
    """
    ip = get_real_ip()
    ua = request.headers.get("User-Agent", "")

    if current_user.is_authenticated:
        return redirect(url_for("index"))

    user_id = session.get("pre_2fa_user")
    if not user_id:
        return redirect(url_for("login"))

    conn = get_real_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if not user or str(user["actif"]).strip().lower() not in ("1", "oui", "true"):
        session.pop("pre_2fa_user", None)
        return redirect(url_for("login"))

    login_cle = user["email"].strip().lower()
    reste = secondes_verrouillage(login_cle)
    if reste:
        session.pop("pre_2fa_user", None)
        session.pop("code_2fa", None)
        flash(message_verrouillage(reste), "danger")
        return redirect(url_for("login"))

    telephones = telephones_du_compte(user["id"])
    action = request.form.get("action") if request.method == "POST" else request.args.get("action")

    # 🔁 Changer de destinataire
    if action == "changer":
        session.pop("code_2fa", None)

    # 📨 Envoi du code
    if request.method == "POST" and action == "envoyer":
        choix = request.form.get("choix", "")
        tel = next((t for t in telephones if str(t["id"]) == choix), None)
        if choix != "email" and tel is None:
            flash("Choisissez où recevoir le code.", "warning")
        else:
            ok, erreur = envoyer_code(user, tel)
            if not ok:
                flash(erreur, "danger")
        return redirect(url_for("login_code"))

    # ✅ Vérification du code
    if request.method == "POST" and action == "verifier":
        ok, via = verifier_code(user, request.form.get("code"))
        if ok:
            effacer_echecs(login_cle)
            ouvrir_session(user, ip, ua, via=via,
                           confiance_jours=CONFIANCE_JOURS if request.form.get("confiance") else 0)
            write_log(f"✅ LOGIN CODE OK {user['email']} | {via} | IP={ip}")
            return redirect(url_for("index"))

        enregistrer_echec(ip, login_cle)
        write_log(f"❌ LOGIN CODE KO {user['email']} | IP={ip}")
        reste = secondes_verrouillage(login_cle)
        if reste:
            notifier_verrouillage(login_cle, ip)
            session.pop("pre_2fa_user", None)
            session.pop("code_2fa", None)
            flash(message_verrouillage(reste), "danger")
            return redirect(url_for("login"))
        flash("❌ Code incorrect ou expiré. Vérifiez le dernier code reçu, ou demandez-en un nouveau.", "danger")
        return redirect(url_for("login_code"))

    code_en_cours = session.get("code_2fa")
    if code_en_cours and code_en_cours.get("user_id") != user["id"]:
        code_en_cours = None

    return render_template(
        "login_code.html",
        telephones=[{"id": t["id"], "libelle": t["libelle"], "masque": masquer_numero(t["numero"])} for t in telephones],
        email_masque=masquer_email(user["email"]),
        code_en_cours=code_en_cours,
        a_application=bool(user["totp_enabled"]),
        confiance_jours=CONFIANCE_JOURS,
    )




@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    flash("Déconnexion réussie.", "info")
    return redirect(url_for('login'))





from flask_login import login_required


# ✅ Route pour la page maj_parametres
@app.route('/maj_parametres', methods=['GET', 'POST'])
@login_required
@require_admin_global
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

    parametres = cursor.execute("""
        SELECT * FROM parametres
        ORDER BY categorie, param_name, param_value
    """).fetchall()
    conn.close()
    return render_template(
"maj_parametres.html",parametres=parametres
    )


@app.route('/ajouter_parametre', methods=['POST'])
@login_required
@require_admin_global
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
        logo_path = get_organisation()["logo_path"]
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
        self.cell(0, 10, get_organisation()["footer_partenariat"], align="L")

        # Date à droite
        date_du_jour = datetime.today().strftime('%d/%m/%Y')
        self.cell(0, 10, date_du_jour, align="R")


@app.route('/maj_champs_assos')
@login_required
@require_access("associations", "ecriture")
def maj_champs_assos():
    return redirect(url_for("maj_champs", source="assos"))


@app.route('/maj_champs_benevoles')
@login_required
@require_access("benevoles", "ecriture")
def maj_champs_benevoles():
    return redirect(url_for("maj_champs", source="benevoles"))


@app.route('/maj_champs_fournisseurs')
@login_required
@require_access("fournisseurs", "ecriture")
def maj_champs_fournisseurs():
    return redirect(url_for("maj_champs", source="fournisseurs"))


@app.route('/maj_champs', methods=['GET', 'POST'])
@login_required
@require_access("associations", "ecriture")
def maj_champs():

    provenance = request.args.get("source", "assos")

    if provenance == "benevoles":
        table = "benevoles"
        header_subtitle = "Plateforme bénévoles"

    elif provenance in ("assos", "associations"):
        table = "associations"
        header_subtitle = "Plateforme partenaires"

    elif provenance in ("fournisseurs", "frs"):
        table = "fournisseurs"
        header_subtitle = "Plateforme fournisseurs"

    else:
        table = "associations"
        header_subtitle = "Plateforme partenaires"

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

        return render_template(
            "maj_champs.html",
            grouped_fields=grouped_fields,
            available_groups=available_groups,
            available_types=available_types,
            champs_disponibles=champs_disponibles,
            provenance=provenance,
            header_subtitle=header_subtitle,
            form=None
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






@app.route('/test_email_api')
def test_email_api():
    """Test d'envoi d'un email via l’API Mailjet avec affichage des valeurs lues"""
    import os
    import requests
    from ba38_utilitaires.core import write_log

    api_key = os.getenv("MAILJET_API_KEY")
    api_secret = os.getenv("MAILJET_API_SECRET")
    sender = os.getenv("MAILJET_SENDER")
    destinataire = os.getenv("MAIL_TEST_TO") or sender

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
@login_required
def debug_database():
    return f"📂 Base active : {get_db_path()}"


@app.route("/test_flash")
@login_required
def test_flash():
    from flask import flash, redirect, url_for
    flash("✅ Test de message Flash réussi", "success")
    return redirect(url_for("index"))



if __name__ == "__main__":
    FLASK_ENV = os.getenv("FLASK_ENV", "prod")
    debug_mode = FLASK_ENV == "dev"

    write_log(f"🚀 Lancement du serveur Flask (debug={debug_mode})")
    app.run(host="127.0.0.1", port=5000, debug=debug_mode)




@app.route("/setup_2fa", methods=["GET", "POST"])
def setup_2fa():

    user_id = session.get("pre_2fa_user")
    if not user_id:
        return redirect(url_for("login"))

    conn = get_db_connection()
    user = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,)
    ).fetchone()

    if not user or user["role"] != "admin":
        conn.close()
        return redirect(url_for("login"))

    # 🔐 déjà activé
    if user["totp_enabled"]:
        conn.close()
        return redirect(url_for("login_2fa"), code=303)

    # 🔐 secret
    if not user["totp_secret"]:
        secret = pyotp.random_base32()
        conn.execute(
            "UPDATE users SET totp_secret=? WHERE id=?",
            (secret, user_id)
        )
        conn.commit()
    else:
        secret = user["totp_secret"]

    if request.method == "POST":
        code = request.form.get("code")

        totp = pyotp.TOTP(secret)

        if totp.verify("".join(ch for ch in (code or "") if ch.isdigit()), valid_window=1):
            conn.execute(
                "UPDATE users SET totp_enabled=1 WHERE id=?",
                (user_id,)
            )
            conn.commit()
            conn.close()

            flash("✅ 2FA activé", "success")
            return redirect(url_for("login_2fa"), code=303)

        flash("❌ Code invalide", "danger")



    conn.close()

    # après récupération du secret

    totp = pyotp.TOTP(secret)

    uri = totp.provisioning_uri(
        name=user["email"].split("@")[0],
        # DEV et PROD ont des secrets distincts : libellés distincts dans l'appli
        issuer_name="BA380 DEV" if os.getenv("ENVIRONMENT", "").upper() == "DEV" else "BA380"
    )

    write_log(f"DEBUG URI = {uri}")


    qr_obj = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=10,
        border=4,
    )

    qr_obj.add_data(uri)
    qr_obj.make(fit=True)

    img = qr_obj.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr = base64.b64encode(buf.getvalue()).decode()

    return render_template("setup_2fa.html", qr=qr, uri=uri, secret=secret)







@app.route("/verify_2fa", methods=["GET", "POST"])
@login_required
def verify_2fa():

    if request.method == "POST":
        code = request.form.get("code")

        conn = get_db_connection()
        user = conn.execute(
            "SELECT totp_secret FROM users WHERE id=?",
            (current_user.id,)
        ).fetchone()
        conn.close()

        totp = pyotp.TOTP(user["totp_secret"])

        if totp.verify("".join(ch for ch in (code or "") if ch.isdigit()), valid_window=1):
            conn = get_db_connection()
            conn.execute(
                "UPDATE users SET totp_enabled=1 WHERE id=?",
                (current_user.id,)
            )
            conn.commit()
            conn.close()

            flash("✅ 2FA activé", "success")
            return redirect(url_for("index"))

        flash("❌ Code invalide", "danger")

    return render_template("verify_2fa.html")




# app = app


