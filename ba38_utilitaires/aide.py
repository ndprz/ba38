import os
from flask import Blueprint, current_app, abort, send_file
from flask_login import login_required

aide_bp = Blueprint("aide_bp", __name__)

AIDE_DIR = os.path.join(os.getcwd(), "aide")


@aide_bp.route("/aide/<page>")
@login_required
def aide_page(page):

    filename = f"{page}.md"
    path = os.path.join(AIDE_DIR, filename)

    # Fallback vers aide globale
    if not os.path.exists(path):
        path = os.path.join(AIDE_DIR, "index.md")

        if not os.path.exists(path):
            abort(404)

    return send_file(path, mimetype="text/markdown")
