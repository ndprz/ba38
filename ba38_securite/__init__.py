from flask import Blueprint

securite_bp = Blueprint("securite", __name__, url_prefix="/securite")

from ba38_securite import routes
