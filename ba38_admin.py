from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g
from flask_login import login_required
from utils import (
    get_db_connection, upload_database, write_log, get_version,
    get_db_info, get_all_users, has_access
)
from forms import RegistrationForm
from werkzeug.security import generate_password_hash

admin_bp = Blueprint("admin", __name__)


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
    role = session.get("user_role", "").lower()
    return dict(
        user_role=role,
        version=get_version(),
        db_info=get_db_info() if role == "admin" else None
    )

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

    return render_template("gestion_roles.html", roles=roles, users=[u["email"] for u in users], filtre=filtre)

# ===========================
#  GESTION DES UTILISATEURS
# ===========================
@admin_bp.route('/gestion_utilisateurs')
@login_required
def gestion_utilisateurs():
    if g.get("user_role") != "admin":
        flash("Accès refusé.", "danger")
        return redirect(url_for("index"))

    users = [dict(u) for u in get_all_users()]
    for user in users:
        user['app_bene'] = int(user.get('app_bene') or 0)
        user['app_assos'] = int(user.get('app_assos') or 0)

    form = RegistrationForm()
    return render_template('gestion_utilisateurs.html', users=users, form=form)

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
    actif = int(request.form.get("actif", 1))
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
@admin_bp.route('/ajouter_utilisateur', methods=['POST'])
@login_required
def ajouter_utilisateur():
    form = RegistrationForm()
    if form.validate_on_submit():
        with get_db_connection() as conn:
            hashed_password = generate_password_hash(form.password.data)
            conn.execute(
                "INSERT INTO users (username, email, password_hash, role, actif) VALUES (?, ?, ?, ?, ?)",
                (form.username.data, form.email.data, hashed_password, form.role.data, form.actif.data)
            )
            conn.commit()

        upload_database()
        flash("Utilisateur ajouté avec succès.", "success")
        return redirect(url_for('admin.gestion_utilisateurs'))

    flash("Erreur lors de l'ajout de l'utilisateur.", "danger")
    return redirect(url_for('admin.gestion_utilisateurs'))
