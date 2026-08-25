# =========================================
# 📧 Envoi via l'API Gmail (compte ba380@banquealimentaire.org)
# =========================================
# Contournement des rebonds temporaires Microsoft/mail.ru causés par
# l'absence d'authentification SPF/DKIM du domaine banquealimentaire.org
# côté Mailjet (nécessite l'accès OVH de la FFBA pour être résolu à la
# source). Envoyer directement depuis la boîte Gmail/Google Workspace
# évite ce problème et dépose au passage une copie dans "Envoyés".
#
# Jeton généré une fois via scripts/init_gmail_token_indicateurs.py
# (flow OAuth interactif, à lancer par une personne connectée au compte
# ba380@banquealimentaire.org).

import os
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

CREDENTIALS_DIR = "/srv/ba38/credentials"
CREDS_FILE = os.path.join(CREDENTIALS_DIR, "credentials_gmail_stocks.json")
TOKEN_FILE = os.path.join(CREDENTIALS_DIR, "token_gmail_indicateurs.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class GmailSendError(Exception):
    pass


def _get_gmail_service():
    if not os.path.exists(TOKEN_FILE):
        raise GmailSendError(
            "Jeton Gmail indicateurs absent — lancer "
            "scripts/init_gmail_token_indicateurs.py (autorisation à faire "
            "une fois, connecté au compte ba380@banquealimentaire.org)."
        )

    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    if not creds.valid:
        raise GmailSendError("Jeton Gmail indicateurs invalide (autorisation à refaire).")

    return build("gmail", "v1", credentials=creds)


def envoyer_mail_gmail(sujet, destinataires, texte, attachment_path=None,
                        sender="ba380@banquealimentaire.org"):
    """
    Envoie un mail via l'API Gmail (compte ba380@banquealimentaire.org),
    avec pièce jointe optionnelle. Lève GmailSendError en cas d'échec.
    """
    service = _get_gmail_service()

    # 🔒 Garde-fou DEV : cette fonction contourne Mailjet (donc le garde-fou
    # déjà en place dans utils.py::envoyer_mail) — sur l'instance DEV, on
    # force donc ici aussi l'envoi vers une adresse de test unique, jamais
    # vers un vrai destinataire.
    if os.getenv("ENVIRONMENT", "").upper() == "DEV":
        sujet = f"🧪 [DEV] {sujet}"
        destinataires = [os.getenv("MAIL_TEST_TO") or "ba380.informatique2@banquealimentaire.org"]

    message = MIMEMultipart()
    message["To"] = ", ".join(destinataires)
    message["From"] = sender
    message["Subject"] = sujet
    message.attach(MIMEText(texte, "plain"))

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header(
            "Content-Disposition", "attachment",
            filename=os.path.basename(attachment_path)
        )
        message.attach(part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    try:
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as e:
        raise GmailSendError(str(e)) from e
