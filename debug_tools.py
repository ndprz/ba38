"""
debug_tools.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Outils d'administration et de diagnostic BA38.


Fonctions principales :
- Consultation des logs applicatifs
- Historique des déploiements
- Comparaison des bases DEV / PROD
- Exécution sécurisée de scripts admin
- Diagnostic environnement
- Gestion des sessions Redis

⚠ Accès réservé aux administrateurs.
"""

from flask import (
    Blueprint, render_template, render_template_string,
    request, abort, redirect, url_for, flash, Response,
    session, g, current_app, jsonify
)
from flask_login import login_required, current_user
from redis import Redis

import os
import re
import subprocess
import sqlite3
import sys
import json

from datetime import datetime, timedelta

from utils import (
    write_log,
    get_db_path,
    get_db_path_by_env,
    get_log_path,
    get_git_commits,
    is_admin_global,
    envoyer_mail,
    render_modele_email,
    copier_modele_email_vers_prod
)

# ============================================================================
# Blueprint
# ============================================================================
debug_bp = Blueprint("debug_bp", __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ============================================================================
# 🪵 HISTORIQUE GIT
# ============================================================================
@debug_bp.route("/git_log", methods=["GET", "POST"])
@login_required
def git_log():
    if session.get("user_role") != "admin":
        return "⛔ Accès interdit", 403

    repo_path = os.getenv("BA38_BASE_DIR")
    commits = get_git_commits(repo_path)

    return render_template_string("""
        <h2>🪵 Historique Git</h2>
        <table class="table table-striped table-sm">
            <thead>
                <tr>
                    <th>Date</th><th>Hash</th><th>Auteur</th><th>Message</th>
                </tr>
            </thead>
            <tbody>
            {% for c in commits %}
                {% if c.error %}
                    <tr>
                        <td colspan="4" class="text-danger">{{ c.error }}</td>
                    </tr>
                {% else %}
                    <tr>
                        <td>{{ c.date }}</td>
                        <td><code>{{ c.hash }}</code></td>
                        <td>{{ c.author }}</td>
                        <td>{{ c.message }}</td>
                    </tr>
                {% endif %}
            {% endfor %}
            </tbody>
        </table>
        <a href="{{ url_for('debug_bp.admin_scripts') }}" class="btn btn-secondary mt-3">
            ⬅️ Retour
        </a>
    """, commits=commits)


# ============================================================================
# 📝 HISTORIQUE DES DÉPLOIEMENTS
# ============================================================================
@debug_bp.route("/deploy_log", methods=["GET", "POST"])
@login_required
def deploy_log():
    """
    📋 Génère l'historique des déploiements
    ➜ injecté ensuite dans admin_scripts via session
    """

    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit", "danger")
        return redirect(url_for("debug_bp.admin_scripts"))

    write_log("📋 Accès historique des déploiements")

    log_path = get_log_path("deploy.log")
    historiques = []

    if os.path.exists(log_path):
        regex = re.compile(r"🚀 Déploiement BA38 DEV → PROD\s*:\s*(.+)")
        current = None

        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                m = regex.search(line)
                if m:
                    if current:
                        historiques.append(current)
                    current = {
                        "date": m.group(1),
                        "version": None,
                        "message": None,
                    }
                elif current and line.startswith("📝 VERSION"):
                    current["version"] = line.split(":", 1)[1].strip()
                elif current and line.startswith("📝 MESSAGE"):
                    current["message"] = line.split(":", 1)[1].strip()

        if current:
            historiques.append(current)

    historiques = historiques[-15:][::-1]
    write_log(f"📊 {len(historiques)} déploiement(s) détecté(s)")

    html = render_template_string("""
        <h4 class="mb-3">📝 Historique des déploiements</h4>

        {% if historiques %}
        <table class="table table-sm table-striped">
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Version</th>
                    <th>Message</th>
                </tr>
            </thead>
            <tbody>
            {% for h in historiques %}
                <tr>
                    <td>{{ h.date }}</td>
                    <td><code>{{ h.version or "-" }}</code></td>
                    <td>{{ h.message or "-" }}</td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
            <p class="text-muted">ℹ️ Aucun déploiement enregistré.</p>
        {% endif %}
    """, historiques=historiques)

    # 👉 Passage propre par la session
    session["admin_output"] = html
    session["admin_script_name"] = "Historique des déploiements"

    return redirect(url_for("debug_bp.admin_scripts"))



@debug_bp.route("/debug/clear_logs", methods=["POST"])
@login_required
def clear_logs():

    from flask_login import current_user

    if not current_user.is_authenticated or current_user.role != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    log_files = get_available_logs()
    selected = request.form.get("log_file")

    # ❌ journalctl non vidable
    if not selected or selected == "cron (journalctl)":
        flash("⚠️ Log non vidable.", "warning")
        return redirect(url_for("debug_bp.debug_console"))

    path = log_files.get(selected)

    if not path or not os.path.exists(path):
        flash("❌ Log introuvable.", "danger")
        return redirect(url_for("debug_bp.debug_console"))

    # 🔒 protection app.log
    if "app.log" in selected:
        flash("⚠️ Vidage de app.log déconseillé.", "warning")
        return redirect(url_for("debug_bp.debug_console"))

    try:
        open(path, "w").close()

        # log uniquement si pas app.log
        write_log(f"🗑️ Log vidé : {selected}")

        flash(f"🗑️ {selected} vidé.", "success")

    except Exception as e:
        flash(f"❌ Erreur : {e}", "danger")

    return redirect(url_for("debug_bp.debug_console", log_file=selected))
# ============================================================================
# 🧮 COMPARAISON COMPLÈTE DES BASES DEV / PROD
# ============================================================================
@debug_bp.route("/compare_db_full", methods=["GET", "POST"])
@login_required
def compare_db_full():
    if session.get("user_role") != "admin":
        return "⛔ Accès interdit", 403

    dev_db = get_db_path_by_env("dev")
    prod_db = get_db_path_by_env("prod")

    def normalize_sql(sql):
        if not sql:
            return ""
        sql = re.sub(r"--.*", "", sql).lower()
        sql = sql.replace('"', '').replace("'", "")
        sql = re.sub(r"\s+", " ", sql)
        return sql.strip()

    def get_schema(path):
        with sqlite3.connect(path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT name, sql
                FROM sqlite_master
                WHERE type IN ('table', 'index')
                AND name NOT LIKE 'sqlite_%'
            """)
            return {r[0]: r[1] for r in cur.fetchall()}

    html = "<h2>🧮 Comparaison DEV ↔ PROD</h2>"

    try:
        schema_dev = get_schema(dev_db)
        schema_prod = get_schema(prod_db)

        all_names = sorted(set(schema_dev) | set(schema_prod))
        for name in all_names:
            sql_dev = schema_dev.get(name)
            sql_prod = schema_prod.get(name)

            if normalize_sql(sql_dev) == normalize_sql(sql_prod):
                status = "🟢 Identique"
            elif not sql_prod:
                status = "🆕 DEV uniquement"
            elif not sql_dev:
                status = "🗑️ PROD uniquement"
            else:
                status = "⚠️ Différent"

            html += f"""
            <div class="card mb-3">
                <div class="card-header"><strong>{name}</strong> — {status}</div>
                <div class="card-body">
                    <div class="row">
                        <div class="col-md-6">
                            <h6>DEV</h6>
                            <pre class="bg-light p-2">{sql_dev or "—"}</pre>
                        </div>
                        <div class="col-md-6">
                            <h6>PROD</h6>
                            <pre class="bg-light p-2">{sql_prod or "—"}</pre>
                        </div>
                    </div>
                </div>
            </div>
            """
    except Exception as e:
        html += f"<div class='alert alert-danger'>{e}</div>"

    return html


# ============================================================================
# 🧾 CONSOLE DE DEBUG (LOGS APPLICATIFS)
# ============================================================================
@debug_bp.route("/debug_console", methods=["GET", "POST"])
@login_required
def debug_console():

    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    header_subtitle = "Console debug"

    ref = request.referrer or ""

    # 🔥 CORRECTION : bloc obligatoire
    if "benevoles" in ref or request.args.get("source") == "benevoles":
        pass

    log_files = get_available_logs()

    selected = request.form.get("log_file") or request.args.get("log_file") or "app.log"

    path = log_files.get(selected)


    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-200:]
        output = "".join(lines)
        error = False
    except Exception as e:
        output = f"❌ Erreur lecture : {e}"
        error = True

    return render_template(
        "admin/debug_console.html",
        output=output,
        error=error,
        log_files=log_files,
        selected_log=selected,
        header_subtitle=header_subtitle,
    )

# ============================================================================
# 📥 EXPORT app.log
# ============================================================================
@debug_bp.route("/export_logs", methods=["GET", "POST"])
@login_required
def export_logs():
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    log_path = get_log_path("app.log")

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()

        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=app.log"}
        )
    except Exception as e:
        flash(f"❌ Erreur export : {e}", "danger")
        return redirect(url_for("debug_bp.debug_console"))


# ============================================================================
# 📍 DIAGNOSTIC DES CHEMINS ET LOGS (serveur Debian / systemd)
# ============================================================================
@debug_bp.route("/where_are_my_logs", methods=["GET"])
@login_required
def where_are_my_logs():
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    def file_info(path):
        return {
            "path": path,
            "exists": os.path.exists(path),
            "writable": os.access(path, os.W_OK) if os.path.exists(path) else False,
        }

    base_dir = os.getenv("BA38_BASE_DIR", "❌ NON DÉFINI")
    env = os.getenv("ENVIRONMENT", "❌ NON DÉFINI")

    infos = {
        "ENVIRONMENT": env,
        "BA38_BASE_DIR": base_dir,
        "DATABASE": file_info(get_db_path()),
        "LOG_APP": file_info(get_log_path("app.log")),
        "LOG_DEPLOY": file_info(get_log_path("deploy.log")),
        "LOG_CONNEXIONS": file_info(get_log_path("connexions.log")),
        "ENV_FILE": file_info(os.path.join(base_dir, ".env")) if base_dir.startswith("/") else None,
    }

    write_log("📍 Diagnostic des chemins et logs")

    return render_template(
        "admin/debug_where_logs.html",
        infos=infos,
    )


@debug_bp.route('/run_sync_test_schemas', methods=["GET", "POST"])
@login_required
def run_sync_test_schemas():
    """
    🔁 Synchronise DEV → TEST avec contrôle fin
    - Schéma complet
    - Données critiques uniquement
    ⚠️ IMPORTANT :
        - copy_data=False → ne touche pas aux données anonymisées
        - seules les tables listées ci-dessous sont remplacées volontairement
    """
    if session.get("test_user"):
        flash("🔒 Action désactivée en mode test.", "warning")
        return redirect(url_for("debug_bp.admin_scripts"))

    from dotenv import load_dotenv
    import os

    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ENV_PATH = os.path.join(BASE_DIR, ".env")

    load_dotenv(ENV_PATH)

    write_log("🔁 Lancement synchronisation DEV → TEST (schéma + données critiques)"    )

    try:
        from scripts.sync_test_schemas import (
            sync_test_databases,
            sync_users_and_roles,
            DEV_TEST_DB,
            PROD_TEST_DB,
        )

        import sqlite3

        # =========================================================
        # 🔍 Vérification existence bases TEST
        # =========================================================
        missing = []
        if not DEV_TEST_DB.exists():
            missing.append("DEV_TEST")
        if not PROD_TEST_DB.exists():
            missing.append("PROD_TEST")

        if missing:
            flash(
                "⚠️ Les bases de test n’existent pas encore.\n"
                "Veuillez d’abord lancer : « Créer les bases TEST anonymisées ».",
                "warning"
            )
            return redirect(url_for("debug_bp.admin_scripts"))

        # =========================================================
        # 🔁 1. Synchronisation du schéma UNIQUEMENT
        # =========================================================
        sync_test_databases(copy_data=False)

        # =========================================================
        # 🔁 2. Copie ciblée des tables critiques
        # =========================================================

        from utils import get_db_connection, get_db_path

        dev_db_path = get_db_path()  # base DEV réelle
        test_dbs = [
            str(DEV_TEST_DB),
            str(PROD_TEST_DB),
        ]

        tables_critique = [
            "parametres",
            "applications",
        ]

        for test_db_path in test_dbs:

            with sqlite3.connect(dev_db_path) as dev_conn, sqlite3.connect(test_db_path) as test_conn:

                dev_conn.row_factory = sqlite3.Row
                test_conn.row_factory = sqlite3.Row

                dev_cur = dev_conn.cursor()
                test_cur = test_conn.cursor()

                for table in tables_critique:

                    test_cur.execute(f"DELETE FROM {table}")

                    rows = dev_cur.execute(f"SELECT * FROM {table}").fetchall()

                    if not rows:
                        continue

                    cols = rows[0].keys()
                    cols_str = ",".join(cols)
                    placeholders = ",".join(["?"] * len(cols))

                    for row in rows:
                        test_cur.execute(
                            f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})",
                            tuple(row)
                        )

                    write_log(f"✅ {test_db_path} → {table} : {len(rows)} lignes")

                test_conn.commit()

        # =========================================================
        # 🔁 3. users + roles_utilisateurs : copie fidèle depuis DEV
        #    (identique au réel, aucune anonymisation)
        # =========================================================
        for test_db_path in test_dbs:
            sync_users_and_roles(test_db_path)

        # =========================================================
        # ✅ FIN
        # =========================================================
        flash("✅ Synchronisation DEV → TEST (schéma + données critiques) réussie.", "success")

    except Exception as e:
        write_log(f"❌ Erreur sync TEST : {e}")
        flash(f"❌ Erreur pendant la synchronisation : {e}", "danger")

    return redirect(url_for("debug_bp.admin_scripts"))


@debug_bp.route("/trigger_error_log", methods=["GET", "POST"])
@login_required
def trigger_error_log():
    """
    🔊 Écrit une ligne de test dans error.log
    """
    if session.get("user_role") != "admin":
        flash("⛔ Action réservée aux administrateurs.", "danger")
        return redirect(url_for("debug_bp.debug_console"))

    print("🔊 Ligne écrite volontairement dans error.log via bouton")
    flash("🔊 Une ligne a été envoyée dans le fichier error.log", "info")
    return redirect(url_for("debug_bp.debug_console"))


# ============================================================================
# 📍 UTILITAIRES
# ============================================================================
@debug_bp.route("/restaurer_version", methods=["GET", "POST"])
@login_required
def restaurer_version():
    """
    Restauration sécurisée d'une version PROD à partir d’un backup.
    """

    import re

    write_log("📥 [restaurer_version] Accès à la restauration")

    if session.get("user_role") != "admin":
        flash("⛔ Accès réservé aux administrateurs", "danger")
        return redirect(url_for("index"))

    if session.get("test_user"):
        flash("🔒 Action désactivée en mode test.", "warning")
        return redirect(url_for("debug_bp.admin_scripts"))

    BASE_DIR = os.getenv("BA38_BASE_DIR", "/srv/ba38")
    PROD_DIR = os.path.join(BASE_DIR, "prod")
    BACKUP_DIR = os.getenv("BASE_PATH", "/srv/ba38") + "/backups"

    # =========================================================================
    # 🧠 Parser nom backup
    # =========================================================================
    def parse_backup_name(filename):
        match = re.match(r"ba380-v(.+)-(\d{8})-(\d{6})\.tar\.gz", filename)
        if not match:
            return filename

        version = match.group(1)
        date = match.group(2)
        heure = match.group(3)

        date_fmt = f"{date[6:8]}/{date[4:6]}"
        heure_fmt = f"{heure[0:2]}:{heure[2:4]}"

        return f"Version {version} — {date_fmt} {heure_fmt}"

    # =========================================================================
    # 📦 Liste backups
    # =========================================================================
    try:
        fichiers_bruts = sorted(
            [
                f for f in os.listdir(BACKUP_DIR)
                if f.startswith("ba380-v") and f.endswith(".tar.gz")
            ],
            reverse=True
        )

        fichiers = [
            {"file": f, "label": parse_backup_name(f)}
            for f in fichiers_bruts
        ]

    except Exception as e:
        flash(f"❌ Erreur lecture backups : {e}", "danger")
        write_log(f"❌ Erreur lecture backups : {e}")
        fichiers = []

    # =========================================================================
    # ▶️ POST = lancer rollback
    # =========================================================================
    if request.method == "POST":

        nom_fichier = request.form.get("backup_file")

        if not nom_fichier:
            flash("❌ Aucun fichier sélectionné", "warning")
            return redirect(url_for("debug_bp.restaurer_version"))

        backup_path = os.path.join(BACKUP_DIR, nom_fichier)

        if not os.path.isfile(backup_path):
            flash(f"❌ Fichier introuvable : {nom_fichier}", "danger")
            return redirect(url_for("debug_bp.restaurer_version"))

        try:
            write_log(f"🔄 Lancement rollback via script externe : {backup_path}")

            result = subprocess.run(
                [os.getenv("BASE_PATH", "/srv/ba38") + "/scripts_taches/rollback_prod.sh", backup_path],
                capture_output=True,
                text=True
            )

            output = result.stdout or ""

            if result.stderr:
                output += "\n⚠️ STDERR:\n" + result.stderr

            # 🔥 AJOUT CRITIQUE
            output += f"\n\n🔎 CODE RETOUR = {result.returncode}"

            session["admin_output"] = output
            session["admin_script_name"] = "Rollback PROD"

            if result.returncode != 0:
                flash("❌ Erreur pendant le rollback", "danger")
            else:
                flash("✅ Rollback terminé", "success")

            return redirect(url_for("debug_bp.admin_scripts"))

        except Exception as e:
            write_log(f"❌ Erreur lancement rollback : {e}")
            flash(f"❌ Erreur : {e}", "danger")


        return redirect(url_for("debug_bp.admin_scripts"))

    return render_template("admin/restaurer_version.html", fichiers=fichiers)

# ============================================================================
# 🔍 Vérification des IDs Google Drive (DEV / PROD)
# ============================================================================
@debug_bp.route("/check_drive_ids", methods=["GET"])
@login_required
def check_drive_ids():
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from dotenv import dotenv_values
    import os

    write_log("🔍 Vérification des IDs Google Drive")

    base_envs = {
        "DEV": os.getenv("BASE_PATH", "/srv/ba38") + "/dev/.env",
        "PROD": os.getenv("BASE_PATH", "/srv/ba38") + "/prod/.env",
    }

    results = []

    for env_name, env_path in base_envs.items():
        # --- Vérification fichier .env ---
        if not os.path.exists(env_path):
            results.append((env_name, ".env", "❌", f"Fichier introuvable : {env_path}"))
            continue

        try:
            config = dotenv_values(env_path)

            service_file = config.get("SERVICE_ACCOUNT_FILE")
            if not service_file:
                results.append((env_name, "SERVICE_ACCOUNT_FILE", "❌", "Non défini"))
                continue

            if not os.path.exists(service_file):
                results.append((env_name, "SERVICE_ACCOUNT_FILE", "❌", f"Fichier absent : {service_file}"))
                continue

            credentials = service_account.Credentials.from_service_account_file(
                service_file,
                scopes=["https://www.googleapis.com/auth/drive"],
            )
            service = build("drive", "v3", credentials=credentials)

            file_ids = {
                "GDRIVE_DB_FILE_ID_PROD": config.get("GDRIVE_DB_FILE_ID_PROD"),
                "GDRIVE_DB_FILE_ID_DEV": config.get("GDRIVE_DB_FILE_ID_DEV"),
                "GDRIVE_DB_FILE_ID_TEST": config.get("GDRIVE_DB_FILE_ID_TEST"),
                "GDRIVE_DB_FILE_ID_DEV_TEST": config.get("GDRIVE_DB_FILE_ID_DEV_TEST"),
            }

            for key, file_id in file_ids.items():
                if not file_id:
                    results.append((env_name, key, "❌", "ID manquant dans .env"))
                    continue
                try:
                    file = service.files().get(
                        fileId=file_id,
                        fields="id, name",
                        supportsAllDrives=True,
                    ).execute()
                    results.append((env_name, key, "✅", file["name"]))
                except Exception as e:
                    results.append((env_name, key, "❌", str(e)))

        except Exception as e:
            results.append((env_name, "Chargement", "❌", f"Erreur : {e}"))

    # --- Rendu HTML intégré admin_scripts ---
    html = """
    <h4 class="mb-3">🗃️ Vérification des fichiers Google Drive</h4>
    <table class="table table-sm table-bordered">
      <thead class="table-light">
        <tr>
          <th>ENV</th>
          <th>Clé</th>
          <th>Status</th>
          <th>Détail</th>
        </tr>
      </thead>
      <tbody>
    """

    for env, key, status, detail in results:
        html += f"""
        <tr>
          <td>{env}</td>
          <td><code>{key}</code></td>
          <td>{status}</td>
          <td>{detail}</td>
        </tr>
        """

    html += """
      </tbody>
    </table>
    """

    return render_template(
        "admin/admin_scripts.html",
        output=html,
        script_name="Vérification Google Drive",
        error=False,
    )


def get_active_sessions(env):
    """
    Retourne les sessions Redis actives (utilisateurs connectés) pour DEV ou PROD.

    Les sessions Flask-Session sont sérialisées en msgpack (pas en JSON), et DEV/PROD
    partagent le même Redis (db=0) : on doit donc décoder avec le serializer de
    l'app et filtrer sur le champ "environment" stocké dans la session à la connexion.

    Le champ "last_activity" est mis à jour à chaque requête authentifiée (voir
    set_user_roles dans ba38.py) : il sert à distinguer une session présente en
    Redis (donc non expirée) d'une session réellement utilisée en ce moment.
    """
    redis_client = Redis(host="127.0.0.1", port=6379)
    serializer = current_app.session_interface.serializer

    sessions = []
    prefix = "session:"

    for key in redis_client.scan_iter(f"{prefix}*"):
        try:
            raw = redis_client.get(key)
            if not raw:
                continue

            data = serializer.decode(raw)

            # On ne garde que les sessions d'utilisateurs connectés
            if not data.get("user_id"):
                continue

            if data.get("environment") != env:
                continue

            ttl = redis_client.ttl(key)
            expire_in = f"{ttl // 3600}h{(ttl % 3600) // 60:02d}" if ttl and ttl > 0 else "?"

            last_activity_raw = data.get("last_activity") or data.get("login_time")
            actif = "non"
            if last_activity_raw:
                try:
                    last_activity_dt = datetime.strptime(last_activity_raw, "%Y-%m-%d %H:%M:%S")
                    if datetime.now() - last_activity_dt < timedelta(minutes=5):
                        actif = "oui"
                except ValueError:
                    pass

            sessions.append({
                "key": key.decode() if isinstance(key, bytes) else key,
                "username": data.get("username", "?"),
                "email": data.get("email", "?"),
                "connexion_time": data.get("login_time", "?"),
                "last_activity": last_activity_raw or "?",
                "expire_in": expire_in,
                "actif": actif,
                "ip": data.get("last_ip") or data.get("login_ip") or "?",
                "user_agent": data.get("user_agent", "?"),
            })

        except Exception:
            continue

    return sessions


@debug_bp.route("/close_session", methods=["POST"])
@login_required
def close_session():
    """
    Ferme (supprime) une session Redis active, depuis la page admin_scripts.
    """
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit", "danger")
        return redirect(url_for("index"))

    if session.get("test_user"):
        flash("🔒 Action désactivée en mode test.", "warning")
        return redirect(url_for("debug_bp.admin_scripts"))

    session_key = request.form.get("session_key", "")

    if not session_key.startswith("session:"):
        flash("⛔ Clé de session invalide", "danger")
        return redirect(url_for("debug_bp.admin_scripts"))

    redis_client = Redis(host="127.0.0.1", port=6379)
    deleted = redis_client.delete(session_key)

    if deleted:
        write_log(f"🔒 Session fermée par {current_user.email} : {session_key}")
        flash("✅ Session fermée.", "success")
    else:
        flash("ℹ️ Session déjà expirée ou introuvable.", "warning")

    return redirect(url_for("debug_bp.admin_scripts"))


# ============================================================================
# 🔐 VÉRIFICATION D'ACCÈS POUR LE REVERSE-PROXY COCKPIT (nginx auth_request)
# ============================================================================
@debug_bp.route("/cockpit_authcheck")
def cockpit_authcheck():
    if not current_user.is_authenticated:
        return "", 401
    if not is_admin_global():
        return "", 403
    return "", 200


# ============================================================================
# 🛠️ ADMIN SCRIPTS
# ============================================================================
@debug_bp.route("/admin_scripts", methods=["GET", "POST"])
@login_required
def admin_scripts():
    write_log("📥 Accès admin_scripts")

    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit", "danger")
        return redirect(url_for("index"))

    # 🔒 Compte en mode test : le menu reste visible mais aucune action
    # n'est exécutée (ces scripts touchent des fichiers/process réels du
    # serveur, indépendamment de la bascule base test/réelle).
    test_locked = bool(session.get("test_user"))

    if test_locked and request.method == "POST":
        flash("🔒 Panneau en lecture seule en mode test : aucune action n'a été exécutée.", "warning")
        return redirect(url_for("debug_bp.admin_scripts"))

    output = session.pop("admin_output", None)
    script_name = session.pop("admin_script_name", None)
    error = False

    scripts_dir = os.path.join(os.getenv("BA38_BASE_DIR"), "scripts")


    allowed_scripts = {
        "status_site.sh": "status_site.sh",
        "backup_prod.sh": "backup_prod.sh",
        "deploy_to_prod.sh": "deploy_to_prod.sh",
        "enable_maintenance.sh": "enable_maintenance.sh",
        "disable_maintenance.sh": "disable_maintenance.sh",
        "sync_type_champ.py": "sync_type_champ.py",
        "check_schema_diff.sh": "check_schema_diff.sh",
        "migrate_schema_and_data_dev_to_prod.py": "migrate_schema_and_data_dev_to_prod.py",
        "check_env.py": "check_env.py",
        "read_env.py": "read_env.py",
        "fix_permissions.sh": "fix_permissions.sh",
        "fix_line_endings.sh": "fix_line_endings.sh",
        "update_benevoles_schema_prod.py": "update_benevoles_schema_prod.py",
        "update_associations_schema_prod.py": "update_associations_schema_prod.py",
        "update_engagements_schema_prod.py": "update_engagements_schema_prod.py",
        "verify_env_consistency.py": "verify_env_consistency.py",
        "restore_prod.sh": "restore_prod.sh",
        "cleanup_backups.py": "cleanup_backups.py",
        "recreer_table_benevoles_inactifs.py": "recreer_table_benevoles_inactifs.py",
        "create_test_databases.py": "create_test_databases.py",
        "sync_dev_from_prod.py": "sync_dev_from_prod.py",
        "git_commit_push.sh": "git_commit_push.sh",
    }
    if request.method == "POST":
        script_name = request.form.get("script_name")

        if script_name not in allowed_scripts:
            output = f"⛔ Script non autorisé : {script_name}"
            error = True
        else:
            path = os.path.join(scripts_dir, allowed_scripts[script_name])
            try:
                if script_name.endswith(".py"):
                    result = subprocess.run(
                        [sys.executable, path],
                        capture_output=True, text=True, timeout=300
                    )

                elif script_name == "deploy_to_prod.sh":

                    version = request.form.get("version", "").strip()
                    message = request.form.get("message", "").strip()

                    # 🔴 contrôle obligatoire
                    if not version:
                        output = "❌ Version obligatoire"
                        error = True

                    else:
                        # 🔥 message par défaut
                        if not message:
                            message = "Update"

                        # 🔥 message final propre
                        full_message = f"v{version} - {message}"

                        result = subprocess.run(
                            ["bash", path, version, message],   # ⚠️ IMPORTANT
                            capture_output=True,
                            text=True,
                            timeout=300
                        )

                        output = result.stdout or ""
                        if result.stderr:
                            output += "\n⚠️ STDERR :\n" + result.stderr

                        if not output.strip():
                            output = "ℹ️ Script exécuté avec succès, aucune sortie."

                        error = result.returncode != 0
                        write_log(f"{'❌' if error else '✅'} Script {script_name} exécuté")

                elif script_name == "git_commit_push.sh":

                    version = request.form.get("version", "").strip()
                    message = request.form.get("message", "").strip()

                    if not message:
                        message = "Update"

                    full_message = f"v{version} - {message}" if version else message

                    result = subprocess.run(
                        ["bash", path, full_message],
                        capture_output=True,
                        text=True,
                        timeout=120
                    )

                    output = result.stdout or ""
                    if result.stderr:
                        output += "\n⚠️ STDERR :\n" + result.stderr

                    if not output.strip():
                        output = "ℹ️ Script exécuté avec succès, aucune sortie."

                    error = result.returncode != 0
                    write_log(f"{'❌' if error else '✅'} Script {script_name} exécuté")

                    message = request.form.get("message") or "Version update"

                    result = subprocess.run(
                        ["bash", path, full_message],
                        capture_output=True,
                        text=True,
                        timeout=120
                    )

                    output = result.stdout or ""
                    if result.stderr:
                        output += "\n⚠️ STDERR :\n" + result.stderr

                    if not output.strip():
                        output = "ℹ️ Script exécuté avec succès, aucune sortie."

                    error = result.returncode != 0
                    write_log(f"{'❌' if error else '✅'} Script {script_name} exécuté")

                else:
                    result = subprocess.run(
                        ["bash", path],
                        capture_output=True, text=True, timeout=300
                    )

                    output = result.stdout or ""
                    if result.stderr:
                        output += "\n⚠️ STDERR :\n" + result.stderr

                    if not output.strip():
                        output = "ℹ️ Script exécuté avec succès, aucune sortie."

                    error = result.returncode != 0
                    write_log(f"{'❌' if error else '✅'} Script {script_name} exécuté")

                output = result.stdout or ""
                if result.stderr:
                    output += "\n⚠️ STDERR :\n" + result.stderr

                if not output.strip():
                    output = "ℹ️ Script exécuté avec succès, aucune sortie."

                error = result.returncode != 0
                write_log(f"{'❌' if error else '✅'} Script {script_name} exécuté")

            except Exception as e:
                output = str(e)
                error = True
                write_log(f"❌ Exception script {script_name} : {e}")

    from utils import get_version_full

    v = get_version_full()

    version = v.get("version", "")
    version_msg = v.get("message", "")

    connexions_dev = get_active_sessions("dev")
    connexions_prod = get_active_sessions("prod")

    return render_template(
        "admin/admin_scripts.html",
        output=output,
        error=error,
        script_name=script_name,
        version_msg=version_msg,
        version=version,
        connexions_dev=connexions_dev,
        connexions_prod=connexions_prod,
        test_locked=test_locked
    )


@debug_bp.route("/_runtime/db", methods=["GET", "POST"])
def runtime_db_info():
    token = request.headers.get("X-Internal-Token")
    if token != os.getenv("INTERNAL_STATUS_TOKEN"):
        abort(403)

    db_path = get_db_path()
    return jsonify(
        db_path=db_path,
        exists=os.path.exists(db_path)
    )



__all__ = ["debug_bp"]


@debug_bp.route("/debug_console_stream", methods=["GET"])
@login_required
def debug_console_stream():

    if not current_user.is_authenticated or current_user.role != "admin":
        return jsonify({"error": "Accès interdit"}), 403

    log_files = get_available_logs()

    selected = request.args.get("log_file", "app.log")
    nb_lines = int(request.args.get("lines", 100))
    level = request.args.get("level")

    try:

        # 🔵 CAS CRON (journalctl)
        if selected == "cron (journalctl)":

            result = subprocess.run(
                ["journalctl", "-u", "cron", "-n", str(nb_lines), "--no-pager", "-o", "short-iso"],
                capture_output=True,
                text=True
            )

            raw_lines = result.stdout.splitlines()

            jobs = []

            for line in raw_lines:

                # ✅ On garde uniquement les vraies commandes cron
                if "CMD (" in line and "/etc/munin" not in line:
                    # ❌ On supprime TEST_CRON
                    if "TEST_CRON" in line:
                        continue

                    try:
                        date_part, rest = line.split(" ", 1)
                        host, rest = rest.split(" CRON[", 1)
                        pid, rest = rest.split("]: ", 1)

                        user = rest.split("(")[1].split(")")[0]
                        command = rest.split("CMD (")[1].rstrip(")")

                        jobs.append({
                            "date": date_part,
                            "user": user,
                            "pid": pid,
                            "command": command
                        })

                    except:
                        continue

            return jsonify({
                "cron_jobs": jobs,
                "count": len(jobs)
            })
        # 🟢 CAS FICHIERS CLASSIQUES
        else:
            path = log_files.get(selected)

            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            lines = lines[-nb_lines:]

        # 🔎 Filtrage niveau
        if level:
            lines = [l for l in lines if level in l]

        return jsonify({
            "content": "".join(lines)
        })

    except Exception as e:
        return jsonify({
            "content": f"❌ Erreur lecture : {e}"
        })


def get_available_logs():

    logs_dir = os.getenv("BASE_PATH", "/srv/ba38") + "/logs"

    logs = {
        "app.log (DEV)": os.getenv("BASE_PATH", "/srv/ba38") + "/dev/logs/app.log",
        "app.log (PROD)": os.getenv("BASE_PATH", "/srv/ba38") + "/prod/logs/app.log",
        "connexions.log": get_log_path("connexions.log"),
        "deploy.log": get_log_path("deploy.log"),

        # 🔵 CRON centralisés
        "backup_db.log": os.path.join(logs_dir, "backup_db.log"),
        "publipostage.log": os.path.join(logs_dir, "publipostage.log"),
        "import_stocks.log": os.path.join(logs_dir, "import_stocks.log"),

        "cron (journalctl)": "journalctl",
    }

    return logs


# ============================================================================
# 🛠️ CONVERSATIONS CHATGPT
# ============================================================================
@debug_bp.route('/admin/conv_chatgpt')
def admin_conv_chatgpt():
    # Chemin vers le fichier JSON des conversations (à adapter selon votre structure)
    conv_file = os.path.join(current_app.root_path, 'data', 'conversations.json')
    try:
        with open(conv_file, 'r') as f:
            conversations = json.load(f)
    except FileNotFoundError:
        conversations = []
    return render_template('admin/admin_conv_chatgpt.html', conversations=conversations)


# ============================================================================
# 📧 Envoi de mail aux utilisateurs
# ============================================================================

@debug_bp.route("/admin/mail_utilisateurs", methods=["GET", "POST"])
@login_required
def mail_utilisateurs():
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit", "danger")
        return redirect(url_for("index"))

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        modeles = conn.execute(
            "SELECT id, code_modele, sujet FROM modeles_emails ORDER BY code_modele"
        ).fetchall()
        users = conn.execute(
            "SELECT email, username FROM users WHERE actif = 1 AND email IS NOT NULL AND email != ''"
        ).fetchall()

    if request.method == "POST":
        modele_id = request.form.get("modele_id")
        mode_test = request.form.get("mode_test") == "on"

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            modele = conn.execute(
                "SELECT * FROM modeles_emails WHERE id = ?", (modele_id,)
            ).fetchone()

        if not modele:
            flash("❌ Modèle introuvable", "danger")
            return redirect(url_for("debug_bp.mail_utilisateurs"))

        destinataires = [u["email"] for u in users if u["email"] and "@" in u["email"]]

        if mode_test:
            destinataires = ["ba380.informatique2@banquealimentaire.org"]

        envoyes = 0
        erreurs = []

        for email in destinataires:
            user_row = next((u for u in users if u["email"] == email), None)
            contexte = {
                "username": user_row["username"] if user_row else "",
                "email": email,
            }
            sujet = render_modele_email(modele["sujet"], contexte)
            corps = render_modele_email(modele["corps"], contexte)

            try:
                envoyer_mail(
                    sujet=sujet,
                    destinataires=[email],
                    texte=corps,
                    is_html=False
                )
                envoyes += 1
            except Exception as e:
                write_log(f"❌ Erreur envoi mail à {email} : {e}")
                erreurs.append(email)

        if mode_test:
            flash(f"🧪 TEST : mail envoyé à ba380.informatique2@banquealimentaire.org", "warning")
        elif erreurs:
            flash(f"⚠️ {envoyes} mails envoyés, {len(erreurs)} erreur(s) : {', '.join(erreurs)}", "warning")
        else:
            flash(f"✅ {envoyes} mails envoyés aux utilisateurs actifs", "success")

        return redirect(url_for("debug_bp.mail_utilisateurs"))

    return render_template(
        "admin/mail_utilisateurs.html",
        modeles=modeles,
        users=users
    )


@debug_bp.route("/admin/modeles/new", methods=["GET", "POST"])
@debug_bp.route("/admin/modeles/<int:modele_id>/edit", methods=["GET", "POST"])
@login_required
def edit_modele_admin(modele_id=None):
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit", "danger")
        return redirect(url_for("index"))

    db_path = get_db_path()

    if request.method == "POST":
        code = request.form.get("code_modele", "").strip()
        sujet = request.form.get("sujet", "").strip()
        corps = request.form.get("corps", "").strip()
        type_periode = request.form.get("type_periode", "").strip() or None
        action = request.form.get("action", "save")

        with sqlite3.connect(db_path) as conn:
            if modele_id:
                conn.execute(
                    "UPDATE modeles_emails SET code_modele = ?, sujet = ?, corps = ?, type_periode = ?, date_modification = ? WHERE id = ?",
                    (code, sujet, corps, type_periode, datetime.now().isoformat(), modele_id)
                )
                flash("✅ Modèle mis à jour.", "success")
            else:
                cur = conn.execute(
                    "INSERT INTO modeles_emails (code_modele, sujet, corps, type_periode, date_modification) VALUES (?, ?, ?, ?, ?)",
                    (code, sujet, corps, type_periode, datetime.now().isoformat())
                )
                modele_id = cur.lastrowid
                flash("✅ Modèle créé.", "success")
            conn.commit()

        if action == "save_both" and os.getenv("ENVIRONMENT", "DEV").upper() == "DEV":
            ok, err = copier_modele_email_vers_prod(code, sujet, corps, type_periode)
            if ok:
                flash("Modèle également enregistré en PROD", "success")
            else:
                flash(f"⚠️ Échec de la copie vers PROD : {err}", "danger")

        return redirect(url_for("debug_bp.edit_modele_admin", modele_id=modele_id))

    modele = None
    if modele_id:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            modele = conn.execute(
                "SELECT * FROM modeles_emails WHERE id = ?", (modele_id,)
            ).fetchone()

        if not modele:
            flash("❌ Modèle introuvable.", "danger")
            return redirect(url_for("debug_bp.mail_utilisateurs"))

    return render_template("admin/edit_modele_admin.html", modele=modele)
