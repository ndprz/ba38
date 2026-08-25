from flask import Blueprint

engagements_bp = Blueprint(
    "engagements",
    __name__
)

from ba38_engagements import routes_main
from ba38_engagements import routes_workflow
from ba38_engagements import routes_parametres
from ba38_engagements import routes_abonnements
from ba38_engagements import routes_subventions
from ba38_engagements import routes_fichiers
from ba38_engagements import routes_tresorerie
from ba38_engagements import routes_notes_frais
from ba38_engagements import routes_reporting
from ba38_engagements import routes_ged
from ba38_engagements import routes_suivi_budgetaire
