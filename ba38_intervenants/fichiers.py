"""Traitement des images (photo, signature) des intervenants temporaires."""
import base64
import binascii
import io
import os

from PIL import Image, ImageOps

PREFIXE_PNG = "data:image/png;base64,"


def dossier_photos():
    return os.getenv("PHOTOS_INTERVENANTS_DIR", "/srv/ba38/photos_intervenants")


def dossier_signatures():
    # Même dossier que les signatures « droit à l'image » des bénévoles
    return os.getenv("SIGNATURES_DROIT_IMAGE_DIR", "/srv/ba38/signatures_droit_image")


def enregistrer_photo(fichier_stream, intervenant_id):
    """Redresse (EXIF), réduit à 400 px max et enregistre en JPEG. Lève ValueError."""
    try:
        img = Image.open(fichier_stream)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except Exception as e:
        raise ValueError("Image illisible") from e
    img.thumbnail((400, 400))
    os.makedirs(dossier_photos(), exist_ok=True)
    img.info.pop("exif", None)
    img.save(os.path.join(dossier_photos(), f"{intervenant_id}.jpg"), "JPEG", quality=85)


def enregistrer_signature(data_url, nom_fichier):
    """Décode le PNG du canvas, refuse une signature vide, réencode sur fond blanc.

    Lève ValueError avec un message affichable.
    """
    if not data_url or not data_url.startswith(PREFIXE_PNG) or len(data_url) > 2_000_000:
        raise ValueError("Signature manquante ou invalide")
    try:
        brut = base64.b64decode(data_url[len(PREFIXE_PNG):], validate=True)
        img = Image.open(io.BytesIO(brut))
        img.load()
    except (binascii.Error, ValueError, OSError) as e:
        raise ValueError("Signature illisible") from e
    img = img.convert("RGBA")
    fond = Image.new("RGB", img.size, (255, 255, 255))
    fond.paste(img, mask=img.split()[3])
    if fond.convert("L").getextrema()[0] > 250:
        raise ValueError("La personne n'a pas signé")
    fond.thumbnail((800, 400))
    os.makedirs(dossier_signatures(), exist_ok=True)
    fond.save(os.path.join(dossier_signatures(), nom_fichier), "PNG")
