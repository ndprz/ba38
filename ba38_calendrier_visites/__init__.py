"""Calendrier prévisionnel des visites aux associations par les CAR.
Consultable et exportable en PDF par tous ; modifiable par chaque CAR pour ses
associations, par le responsable du pôle Partenariat (et ses suppléants) et les
admins pour toutes, et par les utilisateurs ayant le droit « écriture » pour
leurs propres visites, sur n'importe quelle association."""

from flask import Blueprint

calendrier_visites_bp = Blueprint("calendrier_visites", __name__, url_prefix="/calendrier-visites")

from ba38_calendrier_visites import routes  # noqa: E402,F401
