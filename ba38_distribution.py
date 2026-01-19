# ba38_distribution.py

from flask import Blueprint, render_template
from flask_login import login_required

distribution_bp = Blueprint("distribution", __name__)

@distribution_bp.route("/distribution_main")
@login_required
def distribution_main():
    return render_template("distribution_main.html")
