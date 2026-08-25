from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g,current_app, abort
from flask_login import login_required
from ba38_utilitaires.core import (
    get_db_connection, upload_database, write_log, get_version,
    get_db_info, get_all_users, has_access, get_db_info_display,
    require_admin_global, get_version_full,
    synchroniser_utilisateur_vers_base_test
)
from ba38_utilitaires.forms import RegistrationForm
from werkzeug.security import generate_password_hash

import sqlite3
import markdown
import os

admin_bp = Blueprint("admin", __name__)

DOC_BASE_PATH = os.getenv("DOC_BASE_PATH", "/srv/ba38/documentation_technique")

# ============================================================
# GESTION DES DROITS UTILISATEUR – MATRICE (OPTION B)
# ============================================================


@admin_bp.route("/gestion_roles_matrice/<email>", methods=["GET", "POST"])
@login_required
@require_admin_global
def gestion_roles_matrice(email):

    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # --------------------------------------------------
        # 1️⃣ Charger utilisateur
        # --------------------------------------------------
        cur.execute("""
            SELECT email, username, role, actif
            FROM users
            WHERE email = ?
        """, (email,))
        user = cur.fetchone()

        if not user:
            flash("Utilisateur introuvable.", "danger")
            return redirect(url_for("admin.gestion_utilisateurs"))

        # --------------------------------------------------
        # 2️⃣ Charger les applications dynamiquement
        # --------------------------------------------------
        cur.execute("""
            SELECT appli, label
            FROM applications
            ORDER BY label
        """)
        applications_rows = cur.fetchall()

        APPLICATIONS = {
            row["appli"]: row["label"]
            for row in applications_rows
        }

        # --------------------------------------------------
        # 3️⃣ POST : mise à jour des droits
        # --------------------------------------------------
        if request.method == "POST":

            action = request.form.get("action", "save")

            admin_global = request.form.get("admin_global") == "on"

            # rôle global
            cur.execute(
                "UPDATE users SET role = ? WHERE email = ?",
                ("admin" if admin_global else "user", email),
            )

            # suppression des droits
            cur.execute(
                "DELETE FROM roles_utilisateurs WHERE user_email = ?",
                (email,),
            )

            # réinsertion
            for appli in APPLICATIONS:
                droit = request.form.get(f"droit_{appli}", "")
                if droit:
                    cur.execute("""
                        INSERT INTO roles_utilisateurs (user_email, appli, droit)
                        VALUES (?, ?, ?)
                    """, (email, appli, droit))

            conn.commit()
            synchroniser_utilisateur_vers_base_test(email)
            flash("Droits mis à jour avec succès.", "success")

            if action == "save_return":
                return redirect(url_for("admin.gestion_utilisateurs"))

            return redirect(url_for("admin.gestion_roles_matrice", email=email))

        # --------------------------------------------------
        # 4️⃣ GET : charger droits existants
        # --------------------------------------------------
        cur.execute("""
            SELECT appli, droit
            FROM roles_utilisateurs
            WHERE user_email = ?
        """, (email,))
        rows = cur.fetchall()

        droits_existants = {
            row["appli"]: row["droit"]
            for row in rows
        }

        # 🔥 important : afficher même sans droits existants
        for appli in APPLICATIONS:
            if appli not in droits_existants:
                droits_existants[appli] = ""

    # --------------------------------------------------
    # 5️⃣ Rendu
    # --------------------------------------------------
    return render_template(
        "admin/gestion_roles_matrice.html",
        user=user,
        applications=APPLICATIONS,
        roles=droits_existants
    )

# ===========================
#
#       ROUTES DE TEST
#
# ===========================
@admin_bp.route('/test_role')
@login_required
@require_admin_global
def test_role():
    return f"Session user_role: {session.get('user_role')} | g.user_role: {getattr(g, 'user_role', 'Non défini')}"

@admin_bp.route('/test_session')
@login_required
@require_admin_global
def test_session():
    return f"Session Flask : {session} | Rôle dans session : {session.get('user_role')}"

# ===========================
#   INJECTEUR DE CONTEXTE
# ===========================
@admin_bp.app_context_processor
def inject_globals():
    v = get_version_full()

    return {
        "version": v["version"],
        "version_message": v["message"],
        "version_date": v["date"],
        "db_info": get_db_info_display(),
        "db_info_full": get_db_info(),
    }


def compute_user_role():
    if session.get("user_role") == "admin":
        return "admin"

    roles = session.get("roles_utilisateurs", [])
    droits = [d for _, d in roles]

    if "admin" in droits:
        return "admin"
    if "ecriture" in droits:
        return "gestionnaire"
    return "user"


# ===========================
#   GESTION DES RÔLES
# ===========================
@admin_bp.route("/gestion_roles", methods=["GET", "POST"])
@login_required
@require_admin_global
def gestion_roles():

    filtre = request.args.get("filtre", "").strip().lower()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        if request.method == "POST":
            action = request.form.get("action")

            if action == "ajouter":
                email = request.form["user_email"].strip().lower()
                appli = request.form["appli"]
                droit = request.form["droit"]

                doublon = cursor.execute("""
                    SELECT 1 FROM roles_utilisateurs
                    WHERE user_email = ? AND appli = ?
                """, (email, appli)).fetchone()

                if doublon:
                    flash("⚠️ Ce rôle existe déjà pour cet utilisateur et cette application.", "warning")
                else:
                    cursor.execute("""
                        INSERT INTO roles_utilisateurs (user_email, appli, droit)
                        VALUES (?, ?, ?)
                    """, (email, appli, droit))
                    conn.commit()
                    synchroniser_utilisateur_vers_base_test(email)
                    flash("✅ Rôle ajouté avec succès.", "success")

            elif action.startswith("supprimer_"):
                role_id = int(action.replace("supprimer_", ""))
                role_row = cursor.execute(
                    "SELECT user_email FROM roles_utilisateurs WHERE id = ?", (role_id,)
                ).fetchone()
                cursor.execute("DELETE FROM roles_utilisateurs WHERE id = ?", (role_id,))
                conn.commit()
                if role_row:
                    synchroniser_utilisateur_vers_base_test(role_row["user_email"])
                flash("🗑️ Rôle supprimé.", "info")

        if filtre:
            roles = cursor.execute(
                "SELECT * FROM roles_utilisateurs WHERE user_email = ? ORDER BY user_email",
                (filtre,)
            ).fetchall()
        else:
            roles = cursor.execute("SELECT * FROM roles_utilisateurs ORDER BY user_email").fetchall()

        users = cursor.execute("SELECT email FROM users ORDER BY email").fetchall()

    return render_template("admin/gestion_roles.html", roles=roles, users=[u["email"] for u in users], filtre=filtre)

# ============================================================
# GESTION DES UTILISATEURS – LISTE + RÉSUMÉ DES DROITS
# ============================================================

from flask import (
    render_template, redirect, url_for,
    flash, g, current_app
)
from flask_login import login_required
import sqlite3

@admin_bp.route("/gestion_utilisateurs", methods=["GET"])
@login_required
@require_admin_global
def gestion_utilisateurs():
    """
    Page d'administration des utilisateurs.

    Fonctionnalités :
    - liste tous les utilisateurs
    - affiche leur statut (actif / inactif)
    - affiche leur rôle global (admin / car / user)
    - affiche un résumé lisible des droits métiers
    - accès à la gestion détaillée des droits (matrice)

    ⚠️ IMPORTANT
    - Aucun champ n’est supprimé en base
    - app_bene / app_assos sont ignorés
    - roles_utilisateurs est la seule source de vérité métier
    """

    # # --------------------------------------------------
    # # Sécurité : admin global uniquement
    # # --------------------------------------------------
    # if g.user_role != "admin":
    #     flash("⛔ Accès réservé aux administrateurs.", "danger")
    #     return redirect(url_for("index"))

    def normalize_email(email: str) -> str:
        return email.strip().lower() if email else ""

    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # --------------------------------------------------
        # 1️⃣ Charger les utilisateurs
        # --------------------------------------------------
        cur.execute("""
            SELECT
                id,
                email,
                username,
                role,
                actif,
                force_2fa,
                test_only
            FROM users
            ORDER BY email
        """)
        users = cur.fetchall()

        # --------------------------------------------------
        # 2️⃣ Charger tous les rôles métiers
        # --------------------------------------------------
        cur.execute("""
            SELECT
                user_email,
                appli,
                droit
            FROM roles_utilisateurs
            ORDER BY appli
        """)
        rows = cur.fetchall()

        # --------------------------------------------------
        # 3️⃣ Regrouper les rôles par utilisateur
        #     → structure : { email: [(appli, droit), ...] }
        # --------------------------------------------------
        roles_par_user: dict[str, list[tuple[str, str]]] = {}

        for row in rows:
            email = normalize_email(row["user_email"])
            roles_par_user.setdefault(email, []).append(
                (row["appli"], row["droit"])
            )


        # Formulaire d’ajout utilisateur
        form = RegistrationForm()

        # --------------------------------------------------
        # 4️⃣ Enrichir les utilisateurs pour l’affichage
        # --------------------------------------------------
        LABELS_APPLI = {
            "benevoles": "Bénévoles",
            "associations": "Associations",
            "planning": "Plannings",
            "facturation": "Facturation",
            "evenements": "Événements",
        }

        users_enrichis = []

        for user in users:
            u = dict(user)
            email = normalize_email(u["email"])

            # ADMIN GLOBAL → résumé figé
            if u["role"] == "admin":
                u["resume_roles"] = [
                    {"label": "Administrateur global", "droit": "admin"}
                ]
            else:
                roles = []
                for appli, droit in roles_par_user.get(email, []):
                    roles.append({
                        "label": LABELS_APPLI.get(appli, appli),
                        "droit": droit
                    })
                u["resume_roles"] = roles

            users_enrichis.append(u)

    # --------------------------------------------------
    # 5️⃣ Rendu
    # --------------------------------------------------
    return render_template(
        "admin/gestion_utilisateurs.html",
        users=users_enrichis,
        form=form
    )




# --- Mise à jour utilisateur ---
@admin_bp.route("/update_user", methods=["POST"])
@login_required
@require_admin_global
def update_user():
    # if g.user_role != "admin":
    #     flash("⛔ Accès interdit.", "danger")
    #     return redirect(url_for("index"))

    email = request.form.get("email")
    username = request.form.get("username")
    role = request.form.get("role")
    actif = 1 if request.form.get("actif") == "Oui" else 0
    app_assos = int(request.form.get("app_assos", 0))
    app_bene = int(request.form.get("app_bene", 0))
    new_password = request.form.get("new_password")

    with get_db_connection() as conn:
        cursor = conn.cursor()

        if new_password:
            hashed_pw = generate_password_hash(new_password, method="pbkdf2:sha256", salt_length=16)
            cursor.execute("""
                UPDATE users
                SET username = ?, role = ?, actif = ?, app_assos = ?, app_bene = ?, password_hash = ?
                WHERE email = ?
            """, (username, role, actif, app_assos, app_bene, hashed_pw, email))
            flash(f"🔐 Utilisateur {email} mis à jour avec mot de passe.", "success")
        else:
            cursor.execute("""
                UPDATE users
                SET username = ?, role = ?, actif = ?, app_assos = ?, app_bene = ?
                WHERE email = ?
            """, (username, role, actif, app_assos, app_bene, email))
            flash(f"✅ Utilisateur {email} mis à jour.", "success")

        conn.commit()

    synchroniser_utilisateur_vers_base_test(email)
    return redirect(url_for("admin.gestion_utilisateurs"))

# --- Suppression utilisateur (avec rôles) ---
@admin_bp.route('/supprimer_utilisateur/<int:user_id>', methods=['POST'])
@login_required
@require_admin_global
def supprimer_utilisateur(user_id):
    # if g.user_role != "admin":
    #     flash("⛔ Accès interdit.", "danger")
    #     return redirect(url_for("index"))

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Récupération de l'email
        email_row = cursor.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        if email_row:
            email = email_row["email"]
            cursor.execute("DELETE FROM roles_utilisateurs WHERE user_email = ?", (email,))
            write_log(f"🗑️ Rôles supprimés pour {email}")

        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

    if email_row:
        synchroniser_utilisateur_vers_base_test(email, supprime=True)

    upload_database()
    flash("🗑️ Utilisateur et rôles associés supprimés.", "success")
    return redirect(url_for('admin.gestion_utilisateurs'))



@admin_bp.route('/ajouter_utilisateur', methods=['POST'])
@login_required
@require_admin_global
def ajouter_utilisateur():

    # if g.user_role != "admin":
    #     flash("⛔ Accès interdit.", "danger")
    #     return redirect(url_for("index"))

    email = request.form.get("email", "").strip().lower()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    actif = request.form.get("actif", "Oui")
    test_only = 1 if request.form.get("test_only") == "1" else 0

    # --- Validation minimale ---
    if not email or not password:
        flash("Email et mot de passe obligatoires.", "danger")
        return redirect(url_for("admin.gestion_utilisateurs"))

    actif_db = 1 if actif == "Oui" else 0

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # Vérifier doublon email
            existing = cur.execute(
                "SELECT id FROM users WHERE email = ?",
                (email,)
            ).fetchone()

            if existing:
                flash("⚠️ Un utilisateur avec cet email existe déjà.", "warning")
                return redirect(url_for("admin.gestion_utilisateurs"))

            hashed_password = generate_password_hash(password)

            cur.execute("""
                INSERT INTO users (username, email, password_hash, role, actif, test_only)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (username, email, hashed_password, role, actif_db, test_only))

            conn.commit()

        synchroniser_utilisateur_vers_base_test(email)
        upload_database()

        flash("✅ Utilisateur ajouté avec succès.", "success")

    except Exception as e:
        current_app.logger.exception("Erreur ajout utilisateur")
        flash("❌ Erreur technique lors de l'ajout.", "danger")

    return redirect(url_for('admin.gestion_utilisateurs'))


@admin_bp.route("/update_users_batch", methods=["POST"])
@login_required
@require_admin_global
def update_users_batch():
    """
    Enregistrement en masse des utilisateurs depuis la page de gestion.
    """

    action = request.form.get("action", "save")

    users_data = request.form.to_dict(flat=False)

    emails_modifies = []

    with get_db_connection() as conn:
        cur = conn.cursor()

        # Déterminer tous les index présents (users[0], users[1], ...)
        indexes = sorted({
            key.split("[")[1].split("]")[0]
            for key in users_data.keys()
            if key.startswith("users[")
        })

        for idx in indexes:
            user_id = request.form.get(f"users[{idx}][id]")
            username = request.form.get(f"users[{idx}][username]", "").strip()
            role = request.form.get(f"users[{idx}][role]", "user")
            actif = request.form.get(f"users[{idx}][actif]", "Oui")
            new_password = request.form.get(f"users[{idx}][new_password]", "").strip()
            force_2fa = request.form.get(f"users[{idx}][force_2fa]") == "1"
            test_only = 1 if request.form.get(f"users[{idx}][test_only]") == "1" else 0


            if not user_id:
                continue

            # Actif → bool
            actif_db = 1 if actif == "Oui" else 0

            # Mise à jour des champs standards
            cur.execute("""
                UPDATE users
                SET username = ?, role = ?, actif = ?, force_2fa = ?, test_only = ?
                WHERE id = ?
            """, (username, role, actif_db, force_2fa, test_only, user_id))

            # Mot de passe (si fourni)
            if new_password:
                password_hash = generate_password_hash(new_password)
                cur.execute("""
                    UPDATE users
                    SET password_hash = ?
                    WHERE id = ?
                """, (password_hash, user_id))

            email_row = cur.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
            if email_row:
                emails_modifies.append(email_row["email"])

        conn.commit()

    for email in emails_modifies:
        synchroniser_utilisateur_vers_base_test(email)

        if action == "save_return":
            return redirect(url_for("admin.gestion_utilisateurs"))

    flash("✅ Modifications enregistrées.", "success")
    return redirect(url_for("admin.gestion_utilisateurs"))


@admin_bp.route("/documentation")
@login_required
@require_admin_global
def documentation():

    modules = {}
    root_docs = []

    for root, dirs, files in os.walk(DOC_BASE_PATH):
        rel_root = os.path.relpath(root, DOC_BASE_PATH)

        for file in files:
            if not file.endswith(".md"):
                continue

            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, DOC_BASE_PATH)  # ✅ AJOUT ICI

            if rel_root == ".":
                root_docs.append(rel_path)
            else:
                module = rel_root.split(os.sep)[0]

                if module not in modules:
                    modules[module] = []

                modules[module].append(rel_path)

    # tri propre
    modules = dict(sorted(modules.items()))
    for m in modules:
        modules[m].sort()

    root_docs.sort()

    return render_template(
        "admin/documentation.html",
        modules=modules,
        root_docs=root_docs
    )

# ==========================================================
# AFFICHAGE D'UN DOCUMENT
# ==========================================================
@admin_bp.route("/documentation/view")
@login_required
def documentation_view():

    file = request.args.get("file")

    if not file:
        abort(400)

    # 🔒 Sécurité : empêcher ../
    safe_path = os.path.normpath(file)

    if safe_path.startswith(".."):
        abort(403)

    full_path = os.path.join(DOC_BASE_PATH, safe_path)

    if not os.path.isfile(full_path):
        abort(404)

    with open(full_path, "r", encoding="utf-8") as f:
        content_md = f.read()

    # Conversion Markdown → HTML
    safe_md = escape_html_outside_code(content_md)


    import markdown
    import bleach

    html = markdown.markdown(
        safe_md,
        extensions=["fenced_code", "tables"]
    )

    # 🔥 nettoyage HTML (à mettre ici)
    html = bleach.clean(
        html,
        tags=[
            "p","ul","li","strong","em",
            "h1","h2","h3","h4","h5",
            "pre","code",
            "table","thead","tbody","tr","td","th"
        ],
        attributes={
            "code": ["class"],
            "th": ["colspan"],
            "td": ["colspan"]
        },
        strip=True
    )

    return render_template(
        "admin/documentation_view.html",
        content=html,
        file=file
    )

import html

def escape_html_in_markdown(text):
    return html.escape(text)

import re
import html

def escape_html_outside_code(md_text):

    parts = re.split(r'(```.*?```)', md_text, flags=re.DOTALL)

    result = ""

    for part in parts:
        if part.startswith("```"):
            result += part  # on garde tel quel
        else:
            result += html.escape(part)

    return result

@admin_bp.route("/documentation/ajax")
@login_required
def documentation_ajax():
    import markdown

    file = request.args.get("file")
    if not file:
        return {"error": "missing file"}, 400

    safe_path = os.path.normpath(file)
    if safe_path.startswith(".."):
        return {"error": "forbidden"}, 403

    full_path = os.path.join(DOC_BASE_PATH, safe_path)

    if not os.path.isfile(full_path):
        return {"error": "not found"}, 404

    with open(full_path, "r", encoding="utf-8") as f:
        content_md = f.read()

    html = markdown.markdown(
        content_md,
        extensions=["fenced_code", "tables"]
    )

    return {"html": html}



@admin_bp.route("/documentation/search")
@login_required
def documentation_search():

    query = request.args.get("q", "").lower().strip()
    if not query:
        return {"results": []}

    mots = query.split()

    results = []

    for root, dirs, files in os.walk(DOC_BASE_PATH):
        rel_root = os.path.relpath(root, DOC_BASE_PATH)
        module = rel_root.split(os.sep)[0] if rel_root != "." else "racine"

        for file in files:
            if not file.endswith(".md"):
                continue

            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, DOC_BASE_PATH)

            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read().lower()

            name = file.replace(".md", "").lower()

            score = 0

            for mot in mots:
                if mot in name:
                    score += 5
                if mot in module:
                    score += 3
                if mot in content:
                    score += 1

            if score > 0:
                results.append({
                    "path": rel_path,
                    "name": name,
                    "module": module,
                    "content": content[:300],
                    "score": score
                })

    # tri par pertinence
    results.sort(key=lambda x: x["score"], reverse=True)

    return {"results": results[:20]}


@admin_bp.route("/reset_2fa_user/<int:user_id>", methods=["POST"])
@login_required
@require_admin_global
def reset_2fa_user(user_id):

    from ba38_utilitaires.core import envoyer_mail

    with get_db_connection() as conn:
        cur = conn.cursor()

        user = cur.execute(
            "SELECT email, username FROM users WHERE id=?",
            (user_id,)
        ).fetchone()

        if not user:
            flash("Utilisateur introuvable.", "danger")
            return redirect(url_for("admin.gestion_utilisateurs"))

        # 🔄 Reset 2FA
        cur.execute("""
            UPDATE users
            SET totp_enabled = 0,
                totp_secret = NULL
            WHERE id = ?
        """, (user_id,))

        conn.commit()

    synchroniser_utilisateur_vers_base_test(user["email"])
    write_log(f"🔄 RESET 2FA user_id={user_id} ({user['email']})")

    # 📧 Envoi mail
    try:
        envoyer_mail(
            sujet="Réinitialisation de votre authentification 2FA",
            destinataires=[user["email"]],
            texte=f"""
Bonjour,

Votre authentification à double facteur (2FA) a été réinitialisée par un administrateur.

👉 Lors de votre prochaine connexion, vous devrez la reconfigurer.

Si vous n’êtes pas à l’origine de cette demande, contactez immédiatement un administrateur.

Cordialement,
BA380
"""
        )
    except Exception as e:
        write_log(f"❌ Erreur envoi mail 2FA : {e}")

    flash("🔄 2FA réinitialisé et utilisateur notifié.", "warning")

    return redirect(url_for("admin.gestion_utilisateurs"))



import pandas as pd
from flask import send_file
from io import BytesIO

@admin_bp.route("/export_utilisateurs_excel")
@login_required
@require_admin_global
def export_utilisateurs_excel():
    """
    Export Excel des utilisateurs avec leurs droits.
    """

    def normalize_email(email: str) -> str:
        return email.strip().lower() if email else ""

    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ---- Utilisateurs
        cur.execute("""
            SELECT id, email, username, role, actif, force_2fa
            FROM users
            ORDER BY email
        """)
        users = cur.fetchall()

        # ---- Rôles
        cur.execute("""
            SELECT user_email, appli, droit
            FROM roles_utilisateurs
        """)
        roles = cur.fetchall()

    # ---- Regroupement des rôles
    roles_par_user = {}
    for r in roles:
        email = normalize_email(r["user_email"])
        roles_par_user.setdefault(email, []).append(
            f"{r['appli']} ({r['droit']})"
        )

    # ---- Construction des données
    data = []
    for u in users:
        email = normalize_email(u["email"])

        droits = roles_par_user.get(email, [])
        droits_str = "\n".join(droits) if droits else ""
        data.append({
            "ID": u["id"],
            "Email": u["email"],
            "Nom": u["username"],
            "Rôle": u["role"],
            "Actif": u["actif"],  # déjà "Oui" / "Non"
            "2FA": "Oui" if u["force_2fa"] == 1 else "Non",
            "Droits": droits_str
        })

    # ---- DataFrame
    df = pd.DataFrame(data)

    # ---- Export Excel en mémoire
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="Utilisateurs")

    output.seek(0)

    return send_file(
        output,
        download_name="utilisateurs.xlsx",
        as_attachment=True
    )


@admin_bp.route("/maintenance_applications", methods=["GET", "POST"])
@login_required
@require_admin_global
def maintenance_applications():

    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 🔍 Applications utilisées dans les rôles
        used_rows = cur.execute("""
            SELECT DISTINCT appli FROM roles_utilisateurs
        """).fetchall()

        used_codes = {row["appli"] for row in used_rows}

        if request.method == "POST":

            action = request.form.get("action")

            # ➕ AJOUT
            if action == "add":
                code = clean_code(request.form.get("code", "")).strip().lower()
                label = request.form.get("label", "").strip()
                ordre = request.form.get("ordre", 0)

                if code:
                    cur.execute("""
                        INSERT INTO applications (code, label, ordre)
                        VALUES (?, ?, ?)
                    """, (code, label, ordre))

            # ✏️ UPDATE
            elif action == "update":

                for key in request.form:
                    if key.startswith("code_"):

                        id_ = key.split("_")[1]

                        new_code = request.form.get(f"code_{id_}").strip().lower()
                        label = request.form.get(f"label_{id_}").strip()
                        ordre = request.form.get(f"ordre_{id_}")

                        # 🔍 récupérer ancien code
                        row = cur.execute(
                            "SELECT code FROM applications WHERE id = ?",
                            (id_,)
                        ).fetchone()

                        if not row:
                            continue

                        old_code = row["code"]

                        # 🔒 SI le code change → vérifier usage
                        if new_code != old_code:

                            count = cur.execute("""
                                SELECT COUNT(*) FROM roles_utilisateurs
                                WHERE appli = ?
                            """, (old_code,)).fetchone()[0]

                            if count > 0:
                                flash(
                                    f"❌ Impossible de modifier '{old_code}' : utilisé dans {count} rôle(s)",
                                    "danger"
                                )
                                return redirect(url_for("admin.maintenance_applications"))

                        # ✅ update autorisé
                        cur.execute("""
                            UPDATE applications
                            SET code = ?, label = ?, ordre = ?
                            WHERE id = ?
                        """, (new_code, label, ordre, id_))

                conn.commit()

            # 🗑️ DELETE
            elif action == "delete":

                id_ = request.form.get("id")

                # 🔍 récupérer le code de l'application
                row = cur.execute(
                    "SELECT code FROM applications WHERE id = ?",
                    (id_,)
                ).fetchone()

                if not row:
                    flash("❌ Application introuvable", "danger")
                    return redirect(url_for("admin.maintenance_applications"))

                code = row["code"]

                # 🚫 vérifier utilisation dans les rôles
                count = cur.execute("""
                    SELECT COUNT(*) FROM roles_utilisateurs
                    WHERE appli = ?
                """, (code,)).fetchone()[0]

                if count > 0:
                    flash("❌ Impossible : application utilisée dans les rôles", "danger")
                    return redirect(url_for("admin.maintenance_applications"))

                # ✅ suppression autorisée
                cur.execute("DELETE FROM applications WHERE id = ?", (id_,))
                conn.commit()

                flash("🗑️ Application supprimée", "success")

            conn.commit()

        rows = cur.execute("""
            SELECT * FROM applications ORDER BY ordre, code
        """).fetchall()

    return render_template("admin/maintenance_applications.html", rows=rows, used_codes=used_codes)


import unicodedata

def clean_code(code):
    code = code.strip().lower()
    code = unicodedata.normalize("NFD", code)
    code = "".join(c for c in code if unicodedata.category(c) != "Mn")
    return code


@admin_bp.route("/admin/convert_docs")
@login_required
def convert_docs():

    if session.get("user_role") != "admin":
        flash("⛔ Accès interdit", "danger")
        return redirect(url_for("index"))

    import subprocess
    import os

    base_dir = os.getenv("BA38_BASE_DIR")
    script_path = os.path.join(base_dir, "scripts", "convert_doc_to_html.py")

    try:
        result = subprocess.run(
            [os.path.join(base_dir, "venv/bin/python"), script_path],
            capture_output=True,
            text=True
        )

        output = result.stdout + "\n" + result.stderr

        write_log(f"📄 Conversion docs exécutée\n{output}")

        flash("✅ Documentation convertie", "success")

    except Exception as e:
        write_log(f"❌ Erreur conversion docs : {e}")
        flash(f"❌ Erreur : {e}", "danger")

    return redirect(url_for("debug_bp.admin_scripts"))

    def load_doc_html(filename):

        base_dir = os.getenv("BA38_BASE_DIR")
        path = os.path.join(base_dir, "doc_html", filename)

        if not os.path.exists(path):
            return "<p>⚠️ Documentation non disponible</p>"

        with open(path, "r", encoding="utf-8") as f:
            return f.read()



# ============================================================
# GESTION DES DROITS PAR APPLICATION
# Vue inversée : on choisit une appli et on affecte
# le droit à tous les utilisateurs en une seule page.
# ============================================================

@admin_bp.route(
    "/gestion_roles_appli",
    methods=["GET", "POST"]
)
@login_required
@require_admin_global
def gestion_roles_par_appli():

    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # --------------------------------------------------
        # Charger les applications disponibles
        # --------------------------------------------------
        cur.execute("""
            SELECT appli, label
            FROM applications
            ORDER BY label
        """)
        applications = [dict(r) for r in cur.fetchall()]

        appli_selectionnee = request.args.get(
            "appli", ""
        ) or (applications[0]["appli"] if applications else "")

        # --------------------------------------------------
        # POST : enregistrer les droits pour cette appli
        # --------------------------------------------------
        if request.method == "POST":

            appli_selectionnee = request.form.get("appli", "")

            emails = request.form.getlist("user_email[]")

            for email in emails:

                droit = request.form.get(
                    f"droit_{email}", "aucun"
                )

                # Supprimer l'entrée existante pour cet
                # utilisateur / cette appli
                cur.execute("""
                    DELETE FROM roles_utilisateurs
                    WHERE user_email = ?
                      AND appli = ?
                """, (email, appli_selectionnee))

                # Réinsérer seulement si droit != aucun
                if droit and droit != "aucun":
                    cur.execute("""
                        INSERT INTO roles_utilisateurs
                            (user_email, appli, droit)
                        VALUES (?, ?, ?)
                    """, (email, appli_selectionnee, droit))

            conn.commit()

            for email in emails:
                synchroniser_utilisateur_vers_base_test(email)

            flash(
                f"✅ Droits mis à jour pour "
                f"« {appli_selectionnee} ».",
                "success"
            )

            return redirect(url_for(
                "admin.gestion_roles_par_appli",
                appli=appli_selectionnee
            ))

        # --------------------------------------------------
        # GET : charger les utilisateurs + leurs droits
        # pour l'application sélectionnée
        # --------------------------------------------------
        cur.execute("""
            SELECT id, email, username, role, actif
            FROM users
            ORDER BY username, email
        """)
        all_users = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT user_email, droit
            FROM roles_utilisateurs
            WHERE appli = ?
        """, (appli_selectionnee,))

        droits_existants = {
            row["user_email"]: row["droit"]
            for row in cur.fetchall()
        }

        # Enrichir chaque utilisateur avec son droit actuel
        for u in all_users:
            u["droit"] = droits_existants.get(
                u["email"], "aucun"
            )

        # Label de l'application sélectionnée
        label_appli = next(
            (a["label"] for a in applications
             if a["appli"] == appli_selectionnee),
            appli_selectionnee
        )

    return render_template(
        "admin/gestion_roles_par_appli.html",
        applications=applications,
        appli_selectionnee=appli_selectionnee,
        label_appli=label_appli,
        users=all_users,
    )
