# utils_libresign.py
"""
Client minimal pour l'API LibreSign (Nextcloud), utilisé pour l'envoi en
signature de l'Annexe 1 bis — remplace utils_yousign.py (abandonné le
2026-07-16, coût Yousign trop élevé : 1200€ HT/an pour 5 clés API minimum).

LibreSign est auto-hébergé (Nextcloud + app LibreSign), gratuit et open
source (AGPLv3). Contrairement à Yousign, il n'a pas d'ancre texte embarquée
dans le PDF pour positionner le pavé de signature : la position se passe par
coordonnées explicites (page/top/left/width/height, mesurées depuis le HAUT
de la page — mêmes conventions que pdfplumber), transmises à l'API après
création de la demande.

Ne connaît rien de la base de données : reçoit les octets du PDF et les
informations du signataire, retourne les identifiants LibreSign. L'orchestration
(récupération des données, sauvegarde en base) reste dans ba38_annexe1bis.py.
"""

import base64
import os
import requests

from ba38_utilitaires.core import write_log


class LibreSignError(Exception):
    """Erreur lors d'un appel à l'API LibreSign (message déjà lisible par un humain)."""
    pass


# Position du pavé de signature sur la page 7 du PDF annexe1bis, calibrée le
# 2026-07-16 avec pdfplumber contre le label "Signature responsable
# association :" (situé à top≈257, se terminant vers x≈220). À réajuster si
# la mise en page de _build_pdf_bytes change.
PAGE_SIGNATURE = 7
COORDONNEES_PAVE_SIGNATURE = {"page": PAGE_SIGNATURE, "top": 256, "left": 304, "width": 220, "height": 40}


def _base_url():
    url = os.getenv("LIBRESIGN_BASE_URL")
    if not url:
        raise LibreSignError("LIBRESIGN_BASE_URL non défini dans le .env")
    return url.rstrip("/")


def _auth():
    user = os.getenv("LIBRESIGN_USER")
    password = os.getenv("LIBRESIGN_APP_PASSWORD")
    if not user or not password:
        raise LibreSignError("LIBRESIGN_USER / LIBRESIGN_APP_PASSWORD non définis dans le .env")
    return (user, password)


def _headers():
    return {"OCS-APIRequest": "true", "Accept": "application/json"}


def _raise_for_status(resp, contexte):
    if resp.status_code >= 400:
        write_log(f"❌ LibreSign [{contexte}] {resp.status_code} : {resp.text[:500]}")
        raise LibreSignError(f"Erreur LibreSign ({contexte}) : {resp.status_code} — {resp.text[:300]}")


def envoyer_signature_request(pdf_bytes, nom_document, signataire_prenom, signataire_nom, signataire_email,
                               coordonnees=None):
    """
    Crée une demande de signature LibreSign complète : création du fichier +
    signataire (identifié par email, sans compte Nextcloud requis), puis,
    si `coordonnees` est fourni, positionnement du pavé de signature à cet
    emplacement (mêmes clés que COORDONNEES_PAVE_SIGNATURE). Si `coordonnees`
    est None, cette étape est sautée : le signataire place lui-même son pavé
    de signature dans l'interface LibreSign.

    Retourne {"file_id": ..., "uuid": ..., "sign_request_id": ...}.
    """
    base = _base_url()
    auth = _auth()
    nom_complet = f"{signataire_prenom} {signataire_nom}".strip()

    # 1) Création : fichier + signataire (déclenche l'envoi de l'email au signataire)
    resp = requests.post(
        f"{base}/ocs/v2.php/apps/libresign/api/v1/request-signature",
        auth=auth,
        headers=_headers(),
        json={
            "file": {"base64": base64.b64encode(pdf_bytes).decode("ascii")},
            "name": nom_document,
            "signers": [{
                "identifyMethods": [{"method": "email", "value": signataire_email}],
                "displayName": nom_complet,
            }],
        },
        timeout=60,
    )
    _raise_for_status(resp, "création demande")
    data = resp.json()["ocs"]["data"]
    file_id = data["files"][0]["fileId"]
    file_uuid = data["uuid"]
    sign_request_id = data["signers"][0]["signRequestId"]

    # 2) Positionnement du pavé de signature (pas d'ancre texte comme Yousign :
    # coordonnées explicites). Ne pas envoyer `elementId` : sa présence force
    # une recherche d'élément existant et échoue si l'id ne correspond à rien
    # (constaté le 2026-07-15) — l'omettre fait créer un nouvel élément.
    # Sauté si `coordonnees` est None (le signataire place lui-même son pavé).
    if coordonnees is not None:
        resp = requests.patch(
            f"{base}/ocs/v2.php/apps/libresign/api/v1/request-signature",
            auth=auth,
            headers=_headers(),
            json={
                "uuid": file_uuid,
                "visibleElements": [{
                    "signRequestId": sign_request_id,
                    "fileId": file_id,
                    "type": "signature",
                    "coordinates": coordonnees,
                }],
            },
            timeout=30,
        )
        _raise_for_status(resp, "positionnement pavé")

    write_log(f"✅ LibreSign : demande {file_uuid} créée, signataire {signataire_email}")

    return {"file_id": file_id, "uuid": file_uuid, "sign_request_id": sign_request_id}


def recuperer_statut(file_id):
    """Retourne le JSON brut de la validation du fichier (contient son statut).
    Codes de statut confirmés le 2026-07-16 : 0=Brouillon, 1=Prêt à signer,
    3=Signés (à confirmer pour les codes intermédiaires/refus, non testés)."""
    resp = requests.get(
        f"{_base_url()}/ocs/v2.php/apps/libresign/api/v1/file/validate/file_id/{file_id}",
        auth=_auth(),
        headers=_headers(),
        timeout=30,
    )
    _raise_for_status(resp, "consultation statut")
    return resp.json()["ocs"]["data"]


def telecharger_document_signe(file_uuid):
    """Retourne les octets du PDF (signé une fois la demande terminée)."""
    resp = requests.get(
        f"{_base_url()}/apps/libresign/p/pdf/{file_uuid}",
        auth=_auth(),
        timeout=30,
    )
    _raise_for_status(resp, "téléchargement document signé")
    return resp.content
