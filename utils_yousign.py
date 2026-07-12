# utils_yousign.py
"""
Client minimal pour l'API Yousign v3 (signature électronique), utilisé pour
l'envoi en signature de l'Annexe 1 bis (cf. ba38_annexe1bis.py).

Ne connaît rien de la base de données : reçoit les octets du PDF et les
informations du signataire, retourne les identifiants Yousign. L'orchestration
(récupération des données, sauvegarde en base) reste dans ba38_annexe1bis.py.
"""

import os
import requests

from utils import write_log


class YousignError(Exception):
    """Erreur lors d'un appel à l'API Yousign (message déjà lisible par un humain)."""
    pass


def _base_url():
    url = os.getenv("YOUSIGN_API_BASE_URL")
    if not url:
        raise YousignError("YOUSIGN_API_BASE_URL non défini dans le .env")
    return url.rstrip("/")


def _headers():
    api_key = os.getenv("YOUSIGN_API_KEY")
    if not api_key:
        raise YousignError("YOUSIGN_API_KEY non défini dans le .env")
    return {"Authorization": f"Bearer {api_key}"}


def _raise_for_status(resp, contexte):
    if resp.status_code >= 400:
        write_log(f"❌ Yousign [{contexte}] {resp.status_code} : {resp.text[:500]}")
        raise YousignError(f"Erreur Yousign ({contexte}) : {resp.status_code} — {resp.text[:300]}")


def envoyer_signature_request(pdf_bytes, nom_document, signataire_prenom, signataire_nom, signataire_email):
    """
    Crée une demande de signature Yousign complète : création, upload du PDF
    (avec détection automatique de l'ancre `{{s1|signature|W|H}}` intégrée au
    document), ajout du signataire unique, puis activation (déclenche l'envoi
    de l'email Yousign au signataire).

    Retourne {"signature_request_id": ..., "document_id": ...}.
    """
    base = _base_url()
    headers = _headers()

    # 1) Création de la demande de signature
    resp = requests.post(
        f"{base}/signature_requests",
        headers=headers,
        json={"name": nom_document, "delivery_mode": "email"},
        timeout=30,
    )
    _raise_for_status(resp, "création demande")
    signature_request_id = resp.json()["id"]

    # 2) Upload du document, avec détection des ancres
    resp = requests.post(
        f"{base}/signature_requests/{signature_request_id}/documents",
        headers=headers,
        files={"file": (nom_document + ".pdf", pdf_bytes, "application/pdf")},
        data={"nature": "signable_document", "parse_anchors": "true"},
        timeout=30,
    )
    _raise_for_status(resp, "upload document")
    document_id = resp.json()["id"]

    # 3) Ajout du signataire (unique — le président de l'association)
    resp = requests.post(
        f"{base}/signature_requests/{signature_request_id}/signers",
        headers=headers,
        json={
            "info": {
                "first_name": signataire_prenom,
                "last_name": signataire_nom,
                "email": signataire_email,
                "locale": "fr",
            },
            "signature_level": "electronic_signature",
            "signature_authentication_mode": "otp_email",
            "delivery_mode": "email",
        },
        timeout=30,
    )
    _raise_for_status(resp, "ajout signataire")

    # 4) Activation — déclenche l'envoi de l'email au signataire
    resp = requests.post(
        f"{base}/signature_requests/{signature_request_id}/activate",
        headers=headers,
        timeout=30,
    )
    _raise_for_status(resp, "activation")

    write_log(f"✅ Yousign : demande {signature_request_id} activée, signataire {signataire_email}")

    return {"signature_request_id": signature_request_id, "document_id": document_id}


def recuperer_statut(signature_request_id):
    """Retourne le JSON brut de la demande de signature (contient son statut)."""
    resp = requests.get(
        f"{_base_url()}/signature_requests/{signature_request_id}",
        headers=_headers(),
        timeout=30,
    )
    _raise_for_status(resp, "consultation statut")
    return resp.json()


def telecharger_document_signe(signature_request_id):
    """Retourne les octets du PDF signé (une fois la demande terminée)."""
    resp = requests.get(
        f"{_base_url()}/signature_requests/{signature_request_id}/documents/download",
        headers=_headers(),
        timeout=30,
    )
    _raise_for_status(resp, "téléchargement document signé")
    return resp.content


def lister_champs_document(signature_request_id, document_id):
    """Retourne la liste brute des Fields (dont le champ texte 'nom_signataire')
    d'un document, une fois la demande terminée. Le nom exact de la clé
    contenant la valeur saisie par le signataire n'est pas confirmé par la doc
    Yousign — l'appelant doit logger la réponse brute au premier essai réel
    pour la vérifier (cf. ba38_annexe1bis.verifier_statut)."""
    resp = requests.get(
        f"{_base_url()}/signature_requests/{signature_request_id}/documents/{document_id}/fields",
        headers=_headers(),
        timeout=30,
    )
    _raise_for_status(resp, "liste des champs")
    return resp.json()
