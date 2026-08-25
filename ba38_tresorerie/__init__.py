from flask import Blueprint

tresorerie_bp = Blueprint("tresorerie", __name__)

# Ré-export pour compatibilité : ba38_participation.py fait
# `from ba38_tresorerie import BAI_NOM, BAI_ADRESSE, ...`
from ba38_tresorerie.constants import (
    BAI_NOM, BAI_ADRESSE, BAI_TEL, BAI_MAIL, BAI_IBAN, BAI_BIC, BAI_SIREN, BAI_NAF,
)

from ba38_tresorerie import menu
from ba38_tresorerie import drive_utils
from ba38_tresorerie import participation_ebp
from ba38_tresorerie import cotisations
from ba38_tresorerie import cotisations_paiements_relance
from ba38_tresorerie import parsol2l
from ba38_tresorerie import cerfa
from ba38_tresorerie import factures_upload
from ba38_tresorerie import factures_decoupage
