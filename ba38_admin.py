from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g,current_app, abort
from flask_login import login_required
from utils import (
    get_db_connection, upload_database, write_log, get_version,
    get_db_info, get_all_users, has_access, get_db_info_display
)
from forms import RegistrationForm, RegistrationForm
from werkzeug.security import generate_password_hash

import sqlite3
import markdown
import os

admin_bp = Blueprint("admin", __name__)

DOC_BASE_PATH = "/srv/ba38/documentation_technique"

# ============================================================
# GESTION DES DROITS UTILISATEUR – MATRICE (OPTION B)
# ============================================================

APPLICATIONS = {
    "planning": "Plannings",
    "benevoles": "Bénévoles",
    "associations": "Associations",
    "distribution": "Distribution",   # ← doit être ici
    "fournisseurs": "Fournisseurs",
    "evenements": "Événements",
    "facturation": "Facturation",
    "parametres": "Paramètres",
    "utilisateurs": "Utilisateurs",
    "engagements": "Engagements",
    "engagement_parametres": "Engagement Paramètres",
    "tresorerie": "Trésorerie",
    "droit_image": "Droit à l'image",
}

@admin_bp.route("/roles/<email>", methods=["GET", "POST"])
@login_required
def gestion_roles_matrice(email):
    """
    Gestion matricielle des droits pour un utilisateur.
    """
    if g.user_role != "admin":
        flash("Accès réservé aux administrateurs.", "danger")
        return redirect(url_for("index"))

    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ---- Charger utilisateur ----
        cur.execute("SELECT email, username, role, actif FROM users WHERE email = ?", (email,))
        user = cur.fetchone()
        if not user:
            flash("Utilisateur introuvable.", "danger")
            return redirect(url_for("admin.gestion_utilisateurs"))

        # ---- POST : mise à jour complète ----
        if request.method == "POST":
            admin_global = request.form.get("admin_global") == "on"

            # 1️⃣ Mise à jour admin global (users.role)
            cur.execute(
                "UPDATE users SET role = ? WHERE email = ?",
                ("admin" if admin_global else "user", email),
            )

            # 2️⃣ Suppression de TOUS les rôles existants
            cur.execute(
                "DELETE FROM roles_utilisateurs WHERE user_email = ?",
                (email,),
            )

            # 3️⃣ Réinsertion selon la matrice
            for appli in APPLICATIONS:
                droit = request.form.get(f"droit_{appli}", "")
                if droit:
                    cur.execute(
                        """
                        INSERT INTO roles_utilisateurs (user_email, appli, droit)
                        VALUES (?, ?, ?)
                        """,
                        (email, appli, droit),
                    )

            conn.commit()
            flash("Droits mis à jour avec succès.", "success")
            return redirect(url_for("admin.gestion_roles_matrice", email=email))

        # ---- GET : affichage ----
        cur.execute(
            "SELECT appli, droit FROM roles_utilisateurs WHERE user_email = ?",
            (email,),
        )
        rows = cur.fetchall()

        droits_existants = {
            row["appli"]: row["droit"]
            for row in rows
        }

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
def test_role():
    return f"Session user_role: {session.get('user_role')} | g.user_role: {getattr(g, 'user_role', 'Non défini')}"

@admin_bp.route('/test_session')
def test_session():
    return f"Session Flask : {session} | Rôle dans session : {session.get('user_role')}"

# ===========================
#   INJECTEUR DE CONTEXTE
# ===========================
@admin_bp.app_context_processor
def inject_globals():
    return {
        "version": get_version(),
        "db_info": get_db_info_display(),   # affichage UI
        "db_info_full": get_db_info(),       # debug/admin si besoin
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
def gestion_roles():
    if g.get("user_role") != "admin":
        flash("Accès refusé", "danger")
        return redirect(url_for("index"))

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
                    flash("✅ Rôle ajouté avec succès.", "success")

            elif action.startswith("supprimer_"):
                role_id = int(action.replace("supprimer_", ""))
                cursor.execute("DELETE FROM roles_utilisateurs WHERE id = ?", (role_id,))
                conn.commit()
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

    # --------------------------------------------------
    # Sécurité : admin global uniquement
    # --------------------------------------------------
    if g.user_role != "admin":
        flash("⛔ Accès réservé aux administrateurs.", "danger")
        return redirect(url_for("index"))

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
                actif
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

        # current_app.logger.info(
        #     "ROLES PAR USER (normalisés) = %s",
        #     roles_par_user
        # )

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
def update_user():
    if g.user_role != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

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

    return redirect(url_for("admin.gestion_utilisateurs"))

# --- Suppression utilisateur (avec rôles) ---
@admin_bp.route('/supprimer_utilisateur/<int:user_id>', methods=['POST'])
@login_required
def supprimer_utilisateur(user_id):
    if g.user_role != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

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

    upload_database()
    flash("🗑️ Utilisateur et rôles associés supprimés.", "success")
    return redirect(url_for('admin.gestion_utilisateurs'))

# --- Ajout utilisateur ---
# @admin_bp.route('/ajouter_utilisateur', methods=['POST'])
# @login_required
# def ajouter_utilisateur():
#     form = RegistrationForm()
#     if form.validate_on_submit():
#         with get_db_connection() as conn:
#             hashed_password = generate_password_hash(form.password.data)
#             conn.execute(
#                 "INSERT INTO users (username, email, password_hash, role, actif) VALUES (?, ?, ?, ?, ?)",
#                 (form.username.data, form.email.data, hashed_password, form.role.data, form.actif.data)
#             )
#             conn.commit()

#         upload_database()
#         flash("Utilisateur ajouté avec succès.", "success")
#         return redirect(url_for('admin.gestion_utilisateurs'))

#     flash("Erreur lors de l'ajout de l'utilisateur.", "danger")
#     return redirect(url_for('admin.gestion_utilisateurs'))

@admin_bp.route('/ajouter_utilisateur', methods=['POST'])
@login_required
def ajouter_utilisateur():

    if g.user_role != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    email = request.form.get("email", "").strip().lower()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    actif = request.form.get("actif", "Oui")

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
                INSERT INTO users (username, email, password_hash, role, actif)
                VALUES (?, ?, ?, ?, ?)
            """, (username, email, hashed_password, role, actif_db))

            conn.commit()

        upload_database()

        flash("✅ Utilisateur ajouté avec succès.", "success")

    except Exception as e:
        current_app.logger.exception("Erreur ajout utilisateur")
        flash("❌ Erreur technique lors de l'ajout.", "danger")

    return redirect(url_for('admin.gestion_utilisateurs'))


@admin_bp.route("/update_users_batch", methods=["POST"])
@login_required
def update_users_batch():
    """
    Enregistrement en masse des utilisateurs depuis la page de gestion.
    """

    if g.user_role != "admin":
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    users_data = request.form.to_dict(flat=False)

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

            if not user_id:
                continue

            # Actif → bool
            actif_db = 1 if actif == "Oui" else 0

            # Mise à jour des champs standards
            cur.execute("""
                UPDATE users
                SET username = ?, role = ?, actif = ?
                WHERE id = ?
            """, (username, role, actif_db, user_id))

            # Mot de passe (si fourni)
            if new_password:
                password_hash = generate_password_hash(new_password)
                cur.execute("""
                    UPDATE users
                    SET password_hash = ?
                    WHERE id = ?
                """, (password_hash, user_id))

        conn.commit()

    flash("✅ Modifications enregistrées.", "success")
    return redirect(url_for("admin.gestion_utilisateurs"))


@admin_bp.route("/documentation")
@login_required
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
    html = markdown.markdown(
        content_md,
        extensions=["fenced_code", "tables"]
    )

    return render_template(
        "admin/documentation_view.html",
        content=html,
        file=file
    )

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
