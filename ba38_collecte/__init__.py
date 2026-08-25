from flask import Blueprint

collecte_bp = Blueprint("collecte", __name__)

from ba38_collecte import routes
