from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash
)

from flask_login import login_required

from ba38_utilitaires.core import (
    get_db_connection,
    require_admin_global,
    write_log
)

# ============================================================
# BLUEPRINT
# ============================================================

applications_bp = Blueprint(
    "applications",
    __name__,
    url_prefix="/admin/applications"
)

# ============================================================
# LISTE APPLICATIONS
# ============================================================

@applications_bp.route("/")
@login_required
@require_admin_global
def applications_main():

    conn = get_db_connection()

    applications = conn.execute(
        """
        SELECT *
        FROM applications
        ORDER BY
            appli
        """
    ).fetchall()

    conn.close()

    return render_template(
        "admin/applications_list.html",
        applications=applications
    )

# ============================================================
# UPDATE APPLICATION
# ============================================================

@applications_bp.route(
    "/update/<appli>",
    methods=["POST"]
)
@login_required
@require_admin_global
def update_application(appli):

    try:

        conn = get_db_connection()

        conn.execute(
            """
            UPDATE applications
            SET
                label = ?,
                endpoint = ?,
                groupe = ?,
                ordre = ?,
                icon = ?,
                ordre_groupe = ?,
                menu_visible = ?
            WHERE appli = ?
            """,
            (
                request.form.get("label", "").strip(),
                request.form.get("endpoint", "").strip(),
                request.form.get("groupe", "").strip(),
                request.form.get("ordre") or 999,
                request.form.get("icon", "").strip(),
                request.form.get("ordre_groupe") or 999,
                1 if request.form.get("menu_visible") else 0,
                appli
            )
        )

        conn.commit()
        conn.close()

        write_log(
            f"✅ Application mise à jour : {appli}"
        )

        flash(
            "✅ Application mise à jour",
            "success"
        )

    except Exception as e:

        write_log(
            f"❌ Erreur update application {appli} : {e}"
        )

        flash(
            f"❌ Erreur : {e}",
            "danger"
        )

    return redirect(
        url_for("applications.applications_main")
    )

# ============================================================
# CREATE APPLICATION
# ============================================================

@applications_bp.route(
    "/create",
    methods=["POST"]
)
@login_required
@require_admin_global
def create_application():

    try:

        appli = (
            request.form.get("appli", "")
            .strip()
            .lower()
        )

        conn = get_db_connection()

        # ----------------------------------------------------
        # VERIFICATION EXISTENCE
        # ----------------------------------------------------

        existing = conn.execute(
            """
            SELECT appli
            FROM applications
            WHERE appli = ?
            """,
            (appli,)
        ).fetchone()

        if existing:

            flash(
                "❌ Cette application existe déjà",
                "danger"
            )

            conn.close()

            return redirect(
                url_for("applications.applications_main")
            )

        # ----------------------------------------------------
        # INSERT
        # ----------------------------------------------------

        conn.execute(
            """
            INSERT INTO applications (
                appli,
                label,
                endpoint,
                groupe,
                ordre,
                icon,
                ordre_groupe,
                menu_visible
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                appli,
                request.form.get("label", "").strip(),
                request.form.get("endpoint", "").strip(),
                request.form.get("groupe", "").strip(),
                request.form.get("ordre") or 999,
                request.form.get("icon", "").strip(),
                request.form.get("ordre_groupe") or 999,
                1 if request.form.get("menu_visible") else 0
            )
        )

        conn.commit()
        conn.close()

        write_log(
            f"✅ Nouvelle application créée : {appli}"
        )

        flash(
            "✅ Application créée",
            "success"
        )

    except Exception as e:

        write_log(
            f"❌ Erreur création application : {e}"
        )

        flash(
            f"❌ Erreur : {e}",
            "danger"
        )

    return redirect(
        url_for("applications.applications_main")
    )
