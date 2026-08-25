from flask import render_template
from flask_login import login_required

from ba38_utilitaires.core import require_access

from ba38_tresorerie import tresorerie_bp


# ===============================
# 🛠️ Menu utilitaires (accessible à tous les utilisateurs connectés)
# ===============================
@tresorerie_bp.route("/tresorerie")
@login_required
@require_access("tresorerie", "lecture")
def tresorerie():
    return render_template("tresorerie/tresorerie.html")
