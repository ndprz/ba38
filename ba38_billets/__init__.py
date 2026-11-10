"""Module Billets spectacle (groupe Événements/Média) : campagnes de places
offertes (ALPEXPO, etc.) → invitation des bénévoles à jour de cotisation →
inscription par lien personnel → tirage au sort après la date limite
(complété par les salariés si besoin) → mails de résultat."""

from flask import Blueprint

billets_bp = Blueprint("billets", __name__, url_prefix="/billets")

from ba38_billets import routes, public  # noqa: E402,F401
