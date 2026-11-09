# ba38_distribution/__init__.py

from flask import Blueprint

distribution_bp = Blueprint(
    "distribution",
    __name__,
    template_folder="../templates"
)

# 🔗 Import des sous-modules
from . import mvt_stocks
from . import ramasse_assos
from . import routes
from . import planning_jour
from . import bons_prepa
from . import bons_livraison