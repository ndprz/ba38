# ba38_distribution/routes.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, require_access
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime

from . import distribution_bp   

import sqlite3

@distribution_bp.route("/distribution_main")
@login_required
@require_access("distribution", "lecture")
def distribution_main():

    return render_template("distribution/distribution_main.html")


# ============================================================
#  UTILITAIRES
# ============================================================
@distribution_bp.route("/get_article_libelle/<code>")
@login_required
def get_article_libelle(code):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        row = cur.execute("""
            SELECT libelle
            FROM articles
            WHERE article = ?
        """, (code,)).fetchone()

    if row:
        return jsonify({"libelle": row[0]})
    else:
        return jsonify({"libelle": ""})


