# ============================================================
# 📦 ba38_production_cuisine — Module Cuisine, Étape 1
# ============================================================
# Traçabilité production (réceptions, recettes, étapes de fabrication,
# lots fournisseurs) + registres hygiène site (températures, nettoyage,
# étalonnage thermomètres).
#
# ⚠️ Sans rapport avec `ba38_planning/cuisine.py` (blueprint
# `planning_cuisine_bp`) qui gère le planning des équipes (horaires
# bénévoles). Nommage volontairement différent pour éviter toute confusion :
# blueprints `production_cuisine_bp` / `cuisine_hygiene_bp`, templates sous
# `templates/production_cuisine/` et `templates/cuisine_hygiene/`.
#
# Package "flat" façon ba38_tresorerie / ba38_engagements : chaque fichier
# routes_*.py enregistre ses routes par effet de bord au moment de son
# import ci-dessous, sur les blueprints définis ici.

from flask import Blueprint

production_cuisine_bp = Blueprint("production_cuisine", __name__)
cuisine_hygiene_bp = Blueprint("cuisine_hygiene", __name__)

from ba38_production_cuisine import routes_receptions
from ba38_production_cuisine import routes_recettes
from ba38_production_cuisine import routes_tracabilite
from ba38_production_cuisine import routes_kiosk
from ba38_production_cuisine import routes_hygiene
from ba38_production_cuisine import routes_referentiels
