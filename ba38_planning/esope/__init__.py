"""Planning ESOPE (épicerie sociale) — cahier des charges du 2026-10-14.

Une ligne par affectation (et non une colonne par poste comme les autres
plannings) : fonctions → créneaux types (poste × jour) → créneaux datés →
affectations. Droit d'accès : application 'planning_esope'.

- donnees.py      : lecture / écriture en base, génération du mois
- routes.py       : planning de la semaine, impression, événements, fermetures
- routes_admin.py : paramétrage, planning modèle, bénévoles ESOPE
- liens.py        : phase 1, liens personnels, mails, alertes
- envois.py       : phase 1, envoi du pré-planning, suivi des réponses, verrouillage
- public.py       : phase 1, page « Mes créneaux » du bénévole (sans compte)
"""

from flask import Blueprint

esope_bp = Blueprint("esope", __name__, url_prefix="/planning/esope")

from ba38_planning.esope import routes  # noqa: E402,F401
from ba38_planning.esope import routes_admin  # noqa: E402,F401
from ba38_planning.esope import envois  # noqa: E402,F401
from ba38_planning.esope import public  # noqa: E402,F401
