"""Module CERFA (groupe Finance) : reçus fiscaux.

- abandon de frais bénévoles (frais_*.py) ;
- à venir : CERFA fournisseurs, CERFA donateurs.
"""

from flask import Blueprint

cerfa_bp = Blueprint("cerfa", __name__, url_prefix="/recus-fiscaux")

from ba38_cerfa import routes_menu
from ba38_cerfa import frais_admin
from ba38_cerfa import frais_envois
from ba38_cerfa import frais_public
