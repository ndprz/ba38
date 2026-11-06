"""Module Intervenants temporaires (bénévoles d'entreprises ou d'associations).

Personnes accueillies pour une journée ou quelques jours, qu'on ne veut pas
créer comme bénévoles : table séparée, jamais mélangée aux bénévoles
(plannings, SMS, emailing, cotisations, exports…).
Données : nom, prénom, téléphone/mail/organisme facultatifs, photo, droit à l'image
(accord total avec signature tactile, ou refus). Pensé pour le téléphone.
"""

from flask import Blueprint

intervenants_bp = Blueprint("intervenants", __name__, url_prefix="/intervenants")

from ba38_intervenants import routes  # noqa: E402,F401
