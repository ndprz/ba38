"""Module Emailing associations : campagnes de mails Mailjet vers les
adresses email des associations (page Partenaires), avec liste de
destinataires décochable, suivi du statut Mailjet et renvoi Gmail."""

from flask import Blueprint

emailing_bp = Blueprint("emailing", __name__, url_prefix="/emailing")

from ba38_emailing import routes, routes_formulaire  # noqa: E402,F401
