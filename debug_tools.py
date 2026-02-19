# debug_tools.py
# Outils de diagnostic et d'administration BA38

__all__ = []

from flask import (
    Blueprint, render_template, render_template_string,
    request, abort, redirect, url_for, flash, Response,
    session, g, current_app, jsonify
)
from flask_login import login_required
from redis import Redis

import os
import logging
import subprocess
import sqlite3
import re
import sys
import json

from datetime import datetime, timedelta

from utils import (
    write_log,
    get_db_path,
    get_db_path_by_env,
    get_log_path,
    get_git_commits,
    is_admin_global
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

# ============================
# 📡 2. Affichage rapide logs PA
# ============================
@debug_bp.route('/get_error_log_tail')
@login_required
def get_error_log_tail():
    """
    📡 Retourne les 100 dernières lignes du fichier error.log (DEV ou PROD).
    Détection par ENVIRONMENT dans .env
    """
    try:
        env = os.getenv("ENVIRONMENT", "prod")
        if env == "dev":
            log_path = "/var/log/ndprz.pythonanywhere.com.error.log"
        else:
            log_path = "/var/log/www_ba380_org.error.log"

        if not os.path.exists(log_path):
            return f"❌ Fichier introuvable : {log_path}", 404
        if not os.access(log_path, os.R_OK):
            return f"❌ Accès refusé à : {log_path}", 403

        result = subprocess.run(['tail', '-n', '100', log_path], capture_output=True, text=True)
        if result.returncode != 0:
            write_log("l'erreur est la")
            return f"Erreur lecture : {result.stderr}", 500

        return result.stdout

    except Exception as e:
        import traceback
        return f"❌ Exception Python :\n{traceback.format_exc()}", 500
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



@debug_bp.route("/debug/clear_logs", methods=["GET", "POST"])
@login_required
def clear_logs():
    for name in ["app.log", "connexions.log", "deploy.log"]:
        path = get_log_path(name)
        try:
            open(path, "w").close()
        except Exception:
            pass
    flash("Logs effacés.", "success")
    return redirect(url_for("debug_bp.debug_console"))


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

    base_template = "base_assos.html"
    ref = request.referrer or ""
    if "benevoles" in ref or request.args.get("source") == "benevoles":
        base_template = "base_bene.html"

    log_files = {
        "app.log": get_log_path("app.log"),
        "connexions.log": get_log_path("connexions.log"),
        "deploy.log": get_log_path("deploy.log"),
    }

    selected = request.form.get("log_file", "app.log")
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
        "debug_console.html",
        output=output,
        error=error,
        log_files=log_files,
        selected_log=selected,
        base_template=base_template
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
        "debug_where_logs.html",
        infos=infos,
    )


@debug_bp.route('/run_sync_test_schemas', methods=["GET", "POST"])
@login_required
def run_sync_test_schemas():
    """
    🔁 Synchronise les schémas et données DEV vers les bases TEST
    """
    try:
        from scripts.sync_test_schemas import (
            sync_test_databases,
            DEV_TEST_DB,
            PROD_TEST_DB,
        )

        # 🔍 Précondition : bases TEST existantes
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

        # ✅ Synchronisation
        sync_test_databases(copy_data=True)
        flash("✅ Synchronisation DEV → TEST réussie.", "success")

    except Exception as e:
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
    Route pour restaurer une version précédente de la PROD à partir d’une archive .tar.gz
    située dans le dossier /home/ndprz/backups.
    - En GET : affiche un formulaire avec une liste déroulante des archives disponibles.
    - En POST : restaure l’archive sélectionnée dans /home/ndprz/ba380 (remplace tous les fichiers).
    """
    write_log("📥 [restaurer_version] Affichage de la page de restauration")

    # Vérification des droits administrateur
    if session.get("user_role") != "admin":
        flash("⛔ Accès réservé aux administrateurs", "danger")
        return redirect(url_for("index"))

    # 📁 Emplacement des sauvegardes
    dossier_backups = "/home/ndprz/backups"

    try:
        fichiers = sorted(
            [
                f for f in os.listdir(dossier_backups)
                if f.startswith("ba380-v") and f.endswith(".tar.gz")
            ],
            reverse=True
        )
    except Exception as e:
        flash(f"❌ Erreur lecture du dossier des backups : {e}", "danger")
        write_log(f"❌ Erreur lecture backups : {e}")
        fichiers = []

    # ▶️ Si l'utilisateur a soumis le formulaire
    if request.method == "POST":
        nom_fichier = request.form.get("backup_file")
        if not nom_fichier:
            flash("❌ Aucun fichier sélectionné", "warning")
            return redirect(url_for("debug_bp.restaurer_version"))

        backup_path = os.path.join(dossier_backups, nom_fichier)

        if not os.path.isfile(backup_path):
            flash(f"❌ Le fichier sélectionné n'existe pas : {nom_fichier}", "danger")
            write_log(f"❌ Fichier introuvable : {backup_path}")
            return redirect(url_for("debug_bp.restaurer_version"))

        try:
            write_log(f"🔄 Début restauration depuis : {backup_path}")
            subprocess.run(["tar", "-xzf", backup_path, "-C", "/"], check=True)
            write_log(f"✅ Restauration terminée depuis : {backup_path}")
            flash(f"✅ Version restaurée depuis : {nom_fichier}", "success")
        except subprocess.CalledProcessError as e:
            flash(f"❌ Erreur lors de la restauration : {e}", "danger")
            write_log(f"❌ Erreur tar : {e}")
        except Exception as e:
            flash(f"❌ Exception inattendue : {e}", "danger")
            write_log(f"❌ Exception restauration : {e}")

        return redirect(url_for("debug_bp.admin_scripts"))

    return render_template("restaurer_version.html", fichiers=fichiers)


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
        "DEV": "/srv/ba38/dev/.env",
        "PROD": "/srv/ba38/prod/.env",
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
        "admin_scripts.html",
        output=html,
        script_name="Vérification Google Drive",
        error=False,
    )


def count_active_sessions():
    r = Redis(host="127.0.0.1", port=6379)
    return sum(1 for _ in r.scan_iter("session:*"))



def get_active_sessions(env):
    """
    Retourne les sessions Redis actives pour DEV ou PROD
    """
    redis_client = Redis(host="127.0.0.1", port=6379, decode_responses=True)

    sessions = []
    prefix = "session:"

    for key in redis_client.scan_iter(f"{prefix}*"):
        try:
            raw = redis_client.get(key)
            if not raw:
                continue

            data = json.loads(raw)

            # Sécurité : on filtre sur l'env si présent
            if data.get("ENVIRONMENT") and data["ENVIRONMENT"] != env:
                continue

            sessions.append({
                "username": data.get("username", "?"),
                "email": data.get("email", "?"),
                "connexion_time": data.get("login_time", "?"),
                "last_seen": data.get("last_seen", "?"),
                "actif": "oui",
            })

        except Exception:
            continue

    return sessions


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
        "verify_env_consistency.py": "verify_env_consistency.py",
        "restore_prod.sh": "restore_prod.sh",
        "cleanup_backups.py": "cleanup_backups.py",
        "recreer_table_benevoles_inactifs.py": "recreer_table_benevoles_inactifs.py",
        "create_test_databases.py": "create_test_databases.py",
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

            except Exception as e:
                output = str(e)
                error = True
                write_log(f"❌ Exception script {script_name} : {e}")

    version_msg = os.getenv("VERSION_MSG", "Version inconnue")

    nb_sessions = count_active_sessions()

    connexions_dev = [{"label": f"{nb_sessions} session(s) active(s)"}] if nb_sessions else []
    connexions_prod = [{"label": f"{nb_sessions} session(s) active(s)"}] if nb_sessions else []

    return render_template(
        "admin_scripts.html",
        output=output,
        error=error,
        script_name=script_name,
        version_msg=version_msg,
        connexions_dev=connexions_dev,
        connexions_prod=connexions_prod,
        connexions_historiques=[],
        connexions_log=[]
    )

__all__ = ["debug_bp"]

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
