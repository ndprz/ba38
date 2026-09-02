import os
import re

import requests

from ba38_utilitaires.core import write_log

SMSFACTOR_SEND_URL = "https://api.smsfactor.com/send"


def normalize_phone_fr(raw):
    """
    Normalise un numéro français vers le format international sans '+'
    attendu par SmsFactor (ex: '06 12 34 56 78' -> '33612345678').
    Retourne None si le format n'est pas reconnu.
    """
    digits = re.sub(r"\D", "", raw or "")

    if digits.startswith("0") and len(digits) == 10:
        return "33" + digits[1:]

    if digits.startswith("33") and len(digits) == 11:
        return digits

    return None


def envoyer_sms_reel(numero, texte):
    """
    Envoie réellement un SMS via l'API SmsFactor.
    Retourne le dict JSON de réponse (contient 'status', 'ticket', 'cost'...)
    ou lève une exception réseau.
    """
    token = os.environ["SMSFACTOR_API_TOKEN"]

    resp = requests.get(
        SMSFACTOR_SEND_URL,
        params={"text": texte, "to": numero},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    write_log(f"[SMS] Envoi à {numero} -> {data}")
    return data


def is_dev_environment():
    return os.getenv("ENVIRONMENT", "").upper() == "DEV"


def get_dev_test_number():
    return os.getenv("SMS_DEV_TEST_NUMBER")
