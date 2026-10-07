from flask import render_template
from flask_login import login_required

from ba38_utilitaires.core import require_access

from ba38_cerfa import cerfa_bp


@cerfa_bp.route("/")
@login_required
@require_access("cerfa", "lecture")
def cerfa_menu():
    return render_template("cerfa/menu.html")
