from flask import Blueprint

engagements_bp = Blueprint(
    "engagements",
    __name__
)

from engagements import routes_main
from engagements import routes_workflow
from engagements import routes_parametres
from engagements import routes_abonnements
from engagements import routes_subventions
from engagements import routes_fichiers
from engagements import routes_tresorerie
from engagements import routes_notes_frais
from engagements import routes_reporting
from engagements import routes_ged
from engagements import routes_suivi_budgetaire
