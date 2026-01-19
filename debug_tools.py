from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, session, render_template_string, g, current_app  
from flask_login import login_required
import os
import logging
import subprocess
import sqlite3, re

from utils import write_log
from datetime import datetime,timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ✅ Déclaration unique et claire du Blueprint
debug_bp = Blueprint("debug_bp", __name__)

# =======================
# 🔍 1. Historique Git
# =======================
@debug_bp.route('/git_log')
@login_required
def git_log():
    if g.user_role != "admin":
        return "⛔ Accès interdit", 403

    from utils import get_git_commits
    repo_path = "/home/ndprz/dev"  # Modifier si nécessaire en PROD
    commits = get_git_commits(repo_path)

    return render_template_string("""
        <h2>🪵 Historique Git</h2>
        <table class="table table-striped">
            <thead><tr><th>Date</th><th>Hash</th><th>Auteur</th><th>Message</th></tr></thead>
            <tbody>
            {% for c in commits %}
                {% if c.error %}
                    <tr><td colspan="4" class="text-danger">{{ c.error }}</td></tr>
                {% else %}
                    <tr><td>{{ c.date }}</td><td><code>{{ c.hash }}</code></td><td>{{ c.author }}</td><td>{{ c.message }}</td></tr>
                {% endif %}
            {% endfor %}
            </tbody>
        </table>
        <a href="/" class="btn btn-secondary mt-3">⬅️ Retour</a>
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

# ============================
# 📝 3. Historique des déploiements (simplifié)
# ============================
@debug_bp.route('/deploy_log')
def deploy_log():
    """
    Affiche les 15 derniers déploiements (date/heure, version et libellé).
    Extrait depuis app.log (ou deploy.log en fallback).
    """
    import re, os
    from flask import render_template_string

    candidates = [
        "/home/ndprz/app.log",
        "/home/ndprz/ba380/logs/deploy.log",
    ]
    log_path = next((p for p in candidates if os.path.exists(p)), None)
    if not log_path:
        return f"❌ Aucun fichier de log trouvé : {', '.join(candidates)}"

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"❌ Impossible de lire {log_path} : {e}"

    # Regex pour attraper date/heure + version + message
    regex_date = re.compile(r"🚀 Déploiement lancé : (.*)")
    regex_version = re.compile(r"📝 VERSION détectée : (.+)")
    regex_msg = re.compile(r"📝 MESSAGE associé : (.+)")

    historiques = []
    current = {}
    for line in lines:
        if match := regex_date.search(line):
            if current:  # push le précédent
                historiques.append(current)
                current = {}
            current["date"] = match.group(1).strip()
        elif match := regex_version.search(line):
            current["version"] = match.group(1).strip()
        elif match := regex_msg.search(line):
            current["message"] = match.group(1).strip()

    if current:
        historiques.append(current)

    # On garde les 15 derniers
    historiques = historiques[-15:][::-1]

    return render_template_string("""
        <h2>📝 Historique des déploiements</h2>
        <table class="table table-sm table-striped">
          <thead><tr><th>Date</th><th>Version</th><th>Message</th></tr></thead>
          <tbody>
          {% for h in historiques %}
            <tr>
              <td>{{ h.date or '-' }}</td>
              <td><code>{{ h.version or '-' }}</code></td>
              <td>{{ h.message or '-' }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        <a href="/" class="btn btn-secondary mt-3">⬅️ Retour</a>
    """, historiques=historiques)

# ============================
# 🔍 4. Comparaison des bases DEV/PROD
# ============================
@debug_bp.route("/compare_db_full")
@login_required
def compare_db_full():
    if g.user_role != "admin":
        return "⛔ Accès interdit", 403

    dev_db = "/home/ndprz/dev/ba380dev.sqlite"
    prod_db = "/home/ndprz/ba380/ba380.sqlite"

    html = "<h2>🧮 Comparaison complète DEV ↔ PROD</h2>"

    def normalize_sql(sql):
        """ Nettoyage de requête SQL pour comparaison """
        if not sql:
            return ""
        sql = re.sub(r'--.*', '', sql).lower()
        sql = sql.replace('"', '').replace("'", "")
        sql = re.sub(r"\s+", " ", sql)
        sql = re.sub(r"\s*\(\s*", "(", sql)
        sql = re.sub(r"\s*\)\s*", ")", sql)
        sql = re.sub(r"\s*,\s*", ",", sql)
        sql = re.sub(r"\s*=\s*", "=", sql)
        return sql.strip()

    def get_schema(path):
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'")
            return {row[0]: row[2] for row in cursor.fetchall()}

    try:
        schema_dev = get_schema(dev_db)
        schema_prod = get_schema(prod_db)
        all_keys = sorted(set(schema_dev) | set(schema_prod))
        rows = []

        for name in all_keys:
            sql_dev = schema_dev.get(name)
            sql_prod = schema_prod.get(name)
            if normalize_sql(sql_dev) == normalize_sql(sql_prod):
                status = "🟢 Identique"
            elif not sql_prod:
                status = "🆕 Uniquement en DEV"
            elif not sql_dev:
                status = "🗑️ Uniquement en PROD"
            else:
                status = "⚠️ Différent"
            rows.append({"name": name, "status": status, "dev": sql_dev or "❌", "prod": sql_prod or "❌"})

        html += "<h4>📦 Schémas SQLite</h4>"
        for r in rows:
            html += f"""
            <div class="card mb-3">
              <div class="card-header"><strong>{r['name']}</strong> – {r['status']}</div>
              <div class="card-body">
                <div class="row">
                  <div class="col-md-6"><h6>DEV</h6><pre class="p-2 bg-light border">{r['dev']}</pre></div>
                  <div class="col-md-6"><h6>PROD</h6><pre class="p-2 bg-{ 'light' if r['status'] == '🟢 Identique' else 'warning' } border">{r['prod']}</pre></div>
                </div>
              </div>
            </div>
            """
    except Exception as e:
        html += f"<div class='alert alert-danger'>❌ Erreur schéma :<pre>{e}</pre></div>"

    # Comparaison des paramètres
    def get_type_champs(path):
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'type_champ'")
            return sorted(set(row[0] for row in cursor.fetchall()))

    try:
        dev_vals = get_type_champs(dev_db)
        prod_vals = get_type_champs(prod_db)
        set_dev, set_prod = set(dev_vals), set(prod_vals)

        only_in_dev = sorted(set_dev - set_prod)
        only_in_prod = sorted(set_prod - set_dev)
        identiques = sorted(set_dev & set_prod)

        html += "<h4>🧬 Paramètres 'type_champ'</h4>"
        if not only_in_dev and not only_in_prod:
            html += "<div class='alert alert-success'>✅ Paramètres identiques.</div>"
        else:
            if only_in_dev:
                html += "<h5>🆕 Uniquement en DEV :</h5><ul>" + "".join(f"<li>{v}</li>" for v in only_in_dev) + "</ul>"
            if only_in_prod:
                html += "<h5>🗑️ Uniquement en PROD :</h5><ul>" + "".join(f"<li>{v}</li>" for v in only_in_prod) + "</ul>"

        html += f"<h5>✅ Valeurs communes ({len(identiques)}) :</h5><ul>" + "".join(f"<li>{v}</li>" for v in identiques) + "</ul>"
    except Exception as e:
        html += f"<div class='alert alert-danger'>❌ Erreur paramètres :<pre>{e}</pre></div>"

    return html


from flask import Blueprint, render_template, request, session, flash, redirect, url_for
from flask_login import login_required
import os
from utils import get_log_path

LOG_FILE = os.path.join(BASE_DIR, "app.log")


@debug_bp.route("/debug_console", methods=["GET", "POST"])
@login_required
def debug_console():
    """
    Console principale de debug.
    Affiche le contenu de app.log ou error.log selon la sélection de l'utilisateur.
    """

    base_template = "base_assos.html"  # par défaut
    ref = request.referrer or ""
    if "benevoles" in ref or request.args.get("source") == "benevoles":
        base_template = "base_bene.html"

    log_files = {
        "app.log": current_app.config["LOG_FILE"],
    }

    error_log_path = "/var/log/ndprz.pythonanywhere.com.error.log"
    if os.path.exists(error_log_path):
        log_files["error.log (PA)"] = error_log_path

    selected_log = request.form.get("log_file", "app.log")
    path = log_files.get(selected_log, LOG_FILE)

    try:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-100:]
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1") as f:
                lines = f.readlines()[-100:]
        output = "".join(lines)
        error = False
    except Exception as e:
        output = f"Erreur lors de la lecture du fichier : {e}"
        error = True

    return render_template(
        "debug_console.html",
        output=output,
        error=error,
        log_files=log_files,
        selected_log=selected_log,
        base_template=base_template
    )



@debug_bp.route('/export_logs')
@login_required
def export_logs():
    """
    📥 Téléchargement du fichier app.log
    """
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    try:
        log_path = current_app.config["LOG_FILE"]

        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        return Response(
            content,
            mimetype="text/plain",
            headers={
                "Content-Disposition": "attachment; filename=app.log"
            }
        )

    except Exception as e:
        flash(f"❌ Erreur d’export : {e}", "danger")
        return redirect(url_for("debug_bp.debug_console"))
@debug_bp.route('/where_are_my_logs')
@login_required
def where_are_my_logs():
    """
    🔍 Diagnostic du système de logs : existence, droits, chemins .env et DB
    """
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    from utils import get_db_path
    db_path = get_db_path()

    log_path = LOG_FILE
    fallback_path = "/home/ndprz/dev/debug_fallback.log"
    env_path = os.path.join(os.path.dirname(__file__), ".env")

    infos = {
        "log_path": log_path,
        "exists": os.path.exists(log_path),
        "writable": os.access(log_path, os.W_OK),
        "fallback_used": os.path.exists(fallback_path),
        "env_path": env_path,
        "env_readable": os.access(env_path, os.R_OK),
        "db_path": db_path
    }

    try:
        with open(fallback_path, "r") as f:
            fallback_tail = f.readlines()[-10:]
    except:
        fallback_tail = ["❌ Impossible de lire fallback log"]

    return render_template("debug_where_logs.html", infos=infos, fallback_tail=fallback_tail)


@debug_bp.route('/debug_env')
@login_required
def debug_env():
    """
    🔍 Affiche les variables d’environnement et chemins critiques utilisés par l’application.
    """
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    current_mode = "TEST" if session.get("test_user") else "PROD"

    log_message = (
        f"🔍 DEBUG_ENV | Mode={current_mode} | DB_NAME={os.getenv('SQLITE_DB')} | "
        f"DB_NAME_TEST={os.getenv('SQLITE_TEST_DB')} | DB utilisé={db_path} | "
        f"PROD_ID={os.getenv('GDRIVE_DB_FILE_ID_PROD')} | TEST_ID={os.getenv('GDRIVE_DB_FILE_ID_TEST')} | "
        f".env présent={'Oui' if os.path.exists('.env') else 'Non'} | "
        f".env lisible={'Oui' if os.access('.env', os.R_OK) else 'Non'}"
    )

    write_log(log_message)

    return {
        "mode_actuel": current_mode,
        "db_path_utilise": db_path,
        "env_trouve": os.path.exists(".env"),
        "env_lisible": os.access(".env", os.R_OK),
    }



@debug_bp.route('/check_drive_ids')
def check_drive_ids():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from dotenv import dotenv_values
    from os.path import join

    base_envs = {
        "DEV": "/home/ndprz/dev/.env",
        "PROD": "/home/ndprz/ba380/.env"
    }

    results = []

    for env_name, env_path in base_envs.items():
        try:
            config = dotenv_values(env_path)
            service_file = config.get("SERVICE_ACCOUNT_FILE")
            folder_id = config.get("GDRIVE_DB_FOLDER_ID")

            credentials = service_account.Credentials.from_service_account_file(
                service_file, scopes=["https://www.googleapis.com/auth/drive"]
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
                        supportsAllDrives=True
                    ).execute()
                    results.append((env_name, key, "✅", file["name"]))
                except Exception as e:
                    results.append((env_name, key, "❌", str(e)))

        except Exception as e:
            results.append((env_name, "Chargement", "❌", f"Erreur générale : {e}"))

    # Rendu HTML
    html = "<h3>🗃️ Vérification des fichiers Google Drive</h3>"
    html += "<table class='table table-bordered'><thead><tr><th>ENV</th><th>Clé</th><th>Status</th><th>Détail</th></tr></thead><tbody>"
    for env, key, status, detail in results:
        html += f"<tr><td>{env}</td><td>{key}</td><td>{status}</td><td>{detail}</td></tr>"
    html += "</tbody></table>"

    return html


@debug_bp.route('/clear_logs', methods=['POST'])
@login_required
def clear_logs():
    """
    🧹 Vide le contenu du fichier app.log (réservé admin).
    """
    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    try:
        log_path = current_app.config["LOG_FILE"]
        with open(log_path, "w") as f:
            f.truncate(0)
        flash("🧹 app.log effacé avec succès.", "success")
    except Exception as e:
        flash(f"❌ Erreur lors de l’effacement : {e}", "danger")

    return redirect(url_for("debug_bp.debug_console"))

@debug_bp.route('/clear_error_log', methods=['POST'])
@login_required
def clear_error_log():
    """
    🧹 Vide le contenu du fichier error.log de PythonAnywhere.
    """
    if session.get("user_role") != "admin":
        flash("⛔ Action réservée aux administrateurs.", "danger")
        return redirect(url_for("debug_bp.debug_console"))

    try:
        subprocess.run(["bash", "-c", "echo '' > /var/log/ndprz.pythonanywhere.com.error.log"], check=True)
        flash("🧹 error.log (PA) vidé avec succès.", "success")
    except Exception as e:
        flash(f"❌ Impossible de vider error.log : {e}", "danger")

    return redirect(url_for("debug_bp.debug_console"))


@debug_bp.route("/trigger_error_log", methods=["POST"])
@login_required
def trigger_error_log():
    """
    🔊 Écrit une ligne de test dans le fichier error.log via print() (pour vérification).
    """
    if session.get("user_role") != "admin":
        flash("⛔ Action réservée aux administrateurs.", "danger")
        return redirect(url_for("debug_bp.debug_console"))

    # Écrit une ligne dans le log PA
    print("🔊 Ligne écrite volontairement dans error.log (PA) via bouton")
    flash("🔊 Une ligne a été envoyée dans le fichier error.log (PA)", "info")
    return redirect(url_for("debug_bp.debug_console"))

from subprocess import run

@debug_bp.route('/admin_scripts', methods=['GET', 'POST'])
@login_required
def admin_scripts():
    write_log("📥 [admin_scripts] Début exécution route admin_scripts")

    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit", "danger")
        return redirect(url_for("index"))

    output = None
    script_name = request.form.get("script_name", "status_site.sh")
    error = False

    allowed_scripts = {
        "status_site.sh": "/home/ndprz/scripts/status_site.sh",
        "backup_prod.sh": "/home/ndprz/scripts/backup_prod.sh",
        "deploy_to_prod.sh": "/home/ndprz/scripts/deploy_to_prod.sh",
        "enable_maintenance.sh": "/home/ndprz/scripts/enable_maintenance.sh",
        "disable_maintenance.sh": "/home/ndprz/scripts/disable_maintenance.sh",
        "sync_type_champ.py": "/home/ndprz/scripts/sync_type_champ.py",
        "check_schema_diff.sh": "/home/ndprz/scripts/check_schema_diff.sh",
        "migrate_schema_and_data_dev_to_prod.py": "/home/ndprz/scripts/migrate_schema_and_data_dev_to_prod.py",
        "check_env.py": "/home/ndprz/scripts/check_env.py",
        "read_env.py": "/home/ndprz/scripts/read_env.py",
        "fix_permissions.sh": "/home/ndprz/scripts/fix_permissions.sh",
        "fix_line_endings.sh": "/home/ndprz/scripts/fix_line_endings.sh",
        "update_benevoles_schema_prod.py": "/home/ndprz/scripts/update_benevoles_schema_prod.py",
        "update_associations_schema_prod.py": "/home/ndprz/scripts/update_associations_schema_prod.py",
        "verify_env_consistency.py": "/home/ndprz/scripts/verify_env_consistency.py",
        "restore_prod.sh": "/home/ndprz/scripts/restore_prod.sh",
        "cleanup_backups.py": "/home/ndprz/scripts/cleanup_backups.py",
        "recreer_table_benevoles_inactifs.py": "/home/ndprz/scripts/recreer_table_benevoles_inactifs.py",
        "create_test_databases.py": "/home/ndprz/scripts/create_test_databases.py"
    }

    if request.method == "POST" and script_name in allowed_scripts:
        if script_name == "deploy_to_prod.sh":
            try:
                path = os.getenv("PROD_DB_PATH", "/home/ndprz/ba380/ba380.sqlite")
                logging.info(f"📁 Tentative de lecture de la base : {path}")
                with sqlite3.connect(path) as conn:
                    conn.row_factory = sqlite3.Row
                    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    logging.info(f"📋 Tables présentes dans la base : {[t['name'] for t in tables]}")
            except Exception as e:
                logging.error(f"❌ Erreur lecture base {path} : {e}")

        try:
            if script_name.endswith(".py"):
                result = subprocess.run(["python3", allowed_scripts[script_name]], capture_output=True, text=True)
            else:
                result = subprocess.run([allowed_scripts[script_name]], capture_output=True, text=True)

            output = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
            write_log(f"{'✅' if result.returncode == 0 else '❌'} Script {script_name} exécuté.")
            error = result.returncode != 0
        except Exception as e:
            output = f"❌ Exception : {e}"
            write_log(output)
            error = True

    elif request.method == "POST":
        output = f"⛔ Script non autorisé : {script_name}"
        write_log(output)
        error = True
    else:
        try:
            result = subprocess.run([allowed_scripts["status_site.sh"]], capture_output=True, text=True)
            output = result.stdout.strip()
            write_log("✅ Script status_site.sh exécuté automatiquement")
        except Exception as e:
            output = f"❌ Exception status_site.sh : {e}"
            write_log(output)
            error = True

    # 👉 ici suit le bloc pour connexions actives + affichage template...


    # 🔍 Connexions actives DEV + PROD, filtrées (actif = < 1h)
    from dotenv import load_dotenv
    from utils import get_db_path_by_env, get_log_path
    load_dotenv()

    write_log("📌 Début connexions actives.")
    connexions_actives = []
    limite = datetime.utcnow() - timedelta(hours=1)

    for env in ["dev", "prod"]:
        try:
            path = get_db_path_by_env(env)
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                # Vérification de la table
                tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                if "log_connexions" not in tables:
                    write_log(f"⚠️ Table 'log_connexions' absente dans {os.path.basename(path)}")
                    continue

                lignes = cur.execute("""
                    SELECT 
                        email,
                        MAX(username) as username,
                        MAX(environ) as environ,
                        MAX(timestamp) as connexion_time,
                        MAX(last_seen) as last_seen
                    FROM log_connexions
                    GROUP BY email
                    ORDER BY MAX(timestamp) DESC
                """).fetchall()

                # 📜 Historique des connexions/déconnexions
                connexions_historiques = []
                try:
                    path_prod = get_db_path_by_env("prod")
                    with sqlite3.connect(path_prod) as conn:
                        conn.row_factory = sqlite3.Row
                        connexions_historiques = conn.execute("""
                            SELECT email, username, timestamp, action, ip
                            FROM log_connexions
                            ORDER BY timestamp DESC
                            LIMIT 100
                        """).fetchall()
                except Exception as e:
                    write_log(f"❌ Erreur lecture historique log_connexions : {e}")
                    connexions_historiques = []


                for ligne in lignes:
                    last_seen = ligne["last_seen"]
                    try:
                        if last_seen:
                            dt = datetime.fromisoformat(last_seen[:19])
                            if dt >= limite:
                                ligne = dict(ligne)
                                ligne["actif"] = "oui"
                                ligne["connexion_time"] = ligne["connexion_time"][:19].replace("T", " ")
                                ligne["last_seen"] = last_seen[:19].replace("T", " ")
                                connexions_actives.append(ligne)
                    except Exception as e:
                        write_log(f"⚠️ Erreur date last_seen : {e}")
        except Exception as e:
            write_log(f"❌ Erreur lecture base {env} : {e}")

    # 📜 Lecture fichier connexions.log
    try:
        with open(get_log_path("connexions.log"), "r") as f:
            connexions_log = f.readlines()[-50:]
    except Exception as e:
        connexions_log = [f"❌ Erreur lecture connexions.log : {e}"]

    version_msg = os.getenv("VERSION_MSG", "Version inconnue")

    return render_template(
        "admin_scripts.html",
        connexions_log=connexions_log,
        connexions_actives=connexions_actives,
        output=output,
        script_name=script_name,
        error=error,
        connexions_historiques=connexions_historiques,
        version_msg=version_msg
    )




@debug_bp.route('/run_sync_test_schemas')
@login_required
def run_sync_test_schemas():
    """
    🔁 Synchronise les schémas de la base TEST avec la base DEV
    """
    try:
        from scripts.sync_test_schemas import sync_test_databases
        sync_test_databases(copy_data=True)
        flash("✅ Synchronisation test DEV → TEST réussie.", "success")
    except Exception as e:
        flash(f"❌ Erreur pendant la synchronisation : {e}", "danger")

    return redirect(url_for('debug_bp.admin_scripts'))



@debug_bp.route('/restaurer_version', methods=['GET', 'POST'])
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
        fichiers = sorted([
            f for f in os.listdir(dossier_backups)
            if f.startswith("ba380-v") and f.endswith(".tar.gz")
        ], reverse=True)
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

        # Vérification que le fichier existe
        if not os.path.isfile(backup_path):
            flash(f"❌ Le fichier sélectionné n'existe pas : {nom_fichier}", "danger")
            write_log(f"❌ Fichier introuvable : {backup_path}")
            return redirect(url_for("debug_bp.restaurer_version"))

        # ✅ Lancement de la restauration
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

    # En GET : on affiche la liste des fichiers disponibles
    return render_template("restaurer_version.html", fichiers=fichiers)

__all__ = ["debug_bp"]
