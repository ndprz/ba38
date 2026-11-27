# =========================================
# 🧺 Module Collecte — tournées de collecte annuelle
# =========================================
# Reprend dans Basilic l'outillage jusqu'ici lancé "à la main" via des
# scripts Python + fichiers .bat (generer_tournees_bai_v2.py,
# Generer_documents_bai38.py, Generer_fiches_2025.py,
# generer_carte_secteurs.py — voir dev/uploads/collecte_fichiers_source).
#
# Étape 1 : page principale du module avec l'upload des fichiers nécessaires
# pour une campagne (année) de collecte donnée :
#   - liste des magasins (export go-on-web de l'année en cours)
#   - PDF des tournées de la collecte précédente (dossier camions du drive
#     collecte), utilisé comme point de départ par l'algorithme d'optimisation
#     — les camions (codes + noms) en sont aussi extraits, pas besoin d'un
#     fichier véhicules séparé pour la simulation
#
# Étape 2 : génération des tournées, en réutilisant telle quelle la logique de
# generer_tournees_bai_v2.py (copiée dans collecte_moteur_tournees.py) plutôt
# que de la réécrire — script déjà validé sur plusieurs campagnes réelles.
# Affichage du résultat dans l'appli + export Excel (fichier natif du script,
# identique à celui produit par l'outil historique) et PDF (rendu du même
# tableau via weasyprint).
#
# Carte des secteurs (generer_carte_secteurs.py, copiée en version allégée
# dans collecte_moteur_carte_secteurs.py) générée automatiquement à chaque
# version de tournées, à partir du même référentiel magasins (donc des mêmes
# secteurs) que la génération en question.
#
# Analyse comparative multi-scénarios (8 configurations camions-supp × max-
# magasins) : lancée en arrière-plan (Thread, cf. ba38_participation.py pour
# le même pattern), stockage temporaire des seuls indicateurs chiffrés en
# JSON dans collecte_analyses — pas de fichier Excel/carte généré pour ces
# 8 scénarios (ce serait 8x plus de travail pour un résultat jetable).
#
# Étape suivante (documents/fiches équipiers) : pas encore ajoutée.

import io
import os
import math
import shutil
import re
import csv
import glob
import time
import json
import copy
import html as html_lib
import sqlite3
import zipfile
import argparse
import subprocess
import threading
from io import StringIO
from datetime import datetime, timedelta
from ba38_utilitaires.taches_fond import lancer_tache_fond

from werkzeug.utils import secure_filename

import anthropic
import markdown
import requests
import pandas as pd
from docx import Document
from openpyxl import load_workbook, Workbook
from openpyxl.drawing.image import Image as ImageOpenpyxl
from openpyxl.styles import Font, Alignment
from openpyxl.formatting.rule import ColorScaleRule
import lxml.html as _lxml_html
from flask import (
    render_template, request, redirect, url_for, flash,
    current_app, send_file, jsonify, Response, abort
)
from flask_login import login_required, current_user
from markupsafe import escape
from weasyprint import HTML
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib.utils import ImageReader

from ba38_utilitaires.core import (
    get_db_connection, get_db_path, require_access, write_log, date_fr,
    envoyer_mail, render_modele_email, is_valid_email,
    generer_token_localisation, verifier_token_localisation,
    LOCALISATION_TOKEN_VALIDITE_JOURS,
    generer_token_saisie_association, verifier_token_saisie_association,
    mailjet_get_message_status,
)
from ba38_utilitaires.organisation import get_organisation
from ba38_collecte import collecte_bp
from ba38_collecte import moteur_tournees as moteur
from ba38_collecte import moteur_carte_secteurs as carte_secteurs

EXTENSIONS_EXCEL = {".xlsx", ".xls"}
EXTENSIONS_PDF = {".pdf"}
# Fichiers campagne-indépendants d'Associations gardant (modèle Excel, texte
# et pièces jointes du mail, lien Drive) — partagés dev+prod à dessein (un
# seul dépôt pour les deux environnements, cf. _chemin_gardee_produits_annee_precedente
# qui suit le même principe). Déplacé le 2026-11-18 hors de /srv/ba38/uploads/
# (dossier générique, nettoyé par erreur ce jour-là, perte du fichier) vers
# /srv/ba38/data/ (déjà utilisé par trésorerie/stocks pour ce type de
# ressource partagée protégée) — le dossier est créé automatiquement s'il
# n'existe pas encore.
MODELES_GARDEE_DIR = "/srv/ba38/data/collecte_gardee"
os.makedirs(MODELES_GARDEE_DIR, exist_ok=True)

# Un type de fichier = une colonne (chemin/date/auteur) dans collecte_campagnes
# + un nom de stockage fixe (les scripts de génération, ajoutés dans une
# prochaine itération, s'appuieront sur ce nommage plutôt que sur le nom
# d'origine du fichier importé).
FICHIERS = {
    "magasins": {
        "champ_chemin": "fichier_magasins",
        "champ_le": "fichier_magasins_le",
        "champ_par": "fichier_magasins_par",
        "nom_stockage": "liste_magasins.xlsx",
        "extensions": EXTENSIONS_EXCEL,
        "label": "Liste des magasins",
        "aide": "Export go-on-web de l'année en cours",
    },
    "pdf_precedent": {
        "champ_chemin": "fichier_pdf_precedent",
        "champ_le": "fichier_pdf_precedent_le",
        "champ_par": "fichier_pdf_precedent_par",
        "nom_stockage": "tournees_precedentes.pdf",
        "extensions": EXTENSIONS_PDF,
        "label": "Tournées de la collecte précédente",
        "aide": "PDF \"fiches jour-véhicules-magasins\", dossier camions de la collecte précédente sur le drive collecte",
    },
    "groupes": {
        "champ_chemin": "fichier_groupes",
        "champ_le": "fichier_groupes_le",
        "champ_par": "fichier_groupes_par",
        "nom_stockage": "liste_groupes.xlsx",
        "extensions": EXTENSIONS_EXCEL,
        "label": "Liste des groupes (associations go-on-web)",
        "aide": "Export go-on-web Association > Groupes — utilisé pour identifier les associations qui gardent leur collecte",
    },
    "participants": {
        "champ_chemin": "fichier_participants",
        "champ_le": "fichier_participants_le",
        "champ_par": "fichier_participants_par",
        "nom_stockage": "liste_participants.xlsx",
        "extensions": EXTENSIONS_EXCEL,
        "label": "Liste des participants (contacts go-on-web)",
        "aide": "Export go-on-web des participants/contacts par groupe — utilisé pour retrouver le référent (nom, email, téléphone) de chaque association qui garde sa collecte",
    },
}


MAIL_TEXTE_DEFAUT = (
    "Bonjour <<nom>>,\n\n"
    "Voici la liste de vos tournées camions et les personnes affectées avec vous :\n\n"
    "<<affectations>>\n\n"
    "Merci."
)

# Ordre chronologique de la semaine de collecte (jeudi → dimanche), pour le
# mail chauffeurs/équipiers — tri alphabétique par défaut ("Dimanche Matin"
# avant "Jeudi Matin") sinon.
ORDRE_DEMI_JOURNEES_MAIL = [
    "Jeudi Matin", "Jeudi Après-midi",
    "Vendredi Matin", "Vendredi Après-midi",
    "Samedi Matin", "Samedi Après-midi",
    "Dimanche Matin", "Dimanche Après-midi",
]


def _dossier_annee(annee):
    return os.path.join(current_app.root_path, "uploads", "collecte", str(annee))


def _dossier_resultats(annee):
    return os.path.join(_dossier_annee(annee), "resultats")


def _dossier_production(annee):
    return os.path.join(_dossier_annee(annee), "production")


def _dossier_sans_vendredi(annee):
    return os.path.join(_dossier_annee(annee), "sans_vendredi")


def _dossier_gardee_pdf(annee):
    return os.path.join(_dossier_annee(annee), "gardee_pdf_recus")


# ============================================================================
# 🚛 PRODUCTION — génération des documents réels (fiches, pointage, équipier,
# index, carte) directement depuis les 3 exports go-on-web du drive collecte,
# en réutilisant tel quel Generer_documents_bai38_depuis_listes.py (copié
# dans ba38_collecte/scripts/generer_documents_production.py). Contrairement
# à la simulation ci-dessus (tournées calculées par optimisation), ici les
# tournées sont déjà décidées dans liste-vehicule.xlsx : ce module se
# contente de mettre en forme les documents à partir de ce qui existe déjà.
#
# Les 3 fichiers sources sont téléchargés à la volée depuis le drive (Google
# Sheets/Excel partagés en "Toute personne disposant du lien"), pas importés
# à la main : la page production doit toujours refléter le dernier état du
# planning go-on-web. Pas d'historique de versions ici (contrairement aux
# générations de simulation) : chaque lancement écrase les documents
# précédents.
#
# Un nouveau dossier Drive (donc 3 nouveaux liens) est créé chaque année par
# le club : les 3 liens sont donc stockés par année dans collecte_campagnes
# (drive_magasins/drive_vehicules/drive_cagettes), pas codés en dur — voir
# la section "🔗 Fichiers Drive" de la page principale du module.
# ============================================================================

DRIVE_CHAMPS = {
    "magasins":  {"champ": "drive_magasins",  "label": "Liste des magasins", "nom_drive": "liste-magasins.xlsx"},
    "vehicules": {"champ": "drive_vehicules", "label": "Liste des véhicules / planning", "nom_drive": "liste-vehicule.xlsx"},
    # "cagettes" (drive_cagettes) volontairement absent depuis que l'historique
    # des cagettes est géré dans l'appli (cagettes_production()) — la colonne
    # drive_cagettes reste en base (relique, jamais relue ni réécrite).
    "colis":     {"champ": "drive_colis",     "label": "Liste des colis", "nom_drive": "liste-colis.xlsx"},
    "groupes":   {"champ": "drive_groupes",   "label": "Liste des groupes", "nom_drive": "liste-groupes.xlsx"},
    "participants": {"champ": "drive_participants", "label": "Liste des participants", "nom_drive": "liste-participants.xlsx"},
    "participants_mailing": {"champ": "drive_participants_mailing", "label": "Liste des participants pour mailing", "nom_drive": "mailing-participants.xlsx"},
}
# "cagettes" volontairement absent ici : l'historique des cagettes n'est
# plus un fichier Drive téléchargé à la volée, mais une grille gérée dans
# l'appli (cagettes_production()) — voir production_generer() qui
# régénère cagettes.xlsx depuis cette table juste avant de lancer le script.
DRIVE_CHAMPS_PRODUCTION = {cle: DRIVE_CHAMPS[cle] for cle in ("magasins", "vehicules", "colis")}


def _ensure_colonne_pdf_equipier(conn):
    """La colonne fichier_pdf_equipier a été ajoutée après la création de la
    table collecte_generations, pour la simulation avec capacité camions
    uniquement (PDF façon 'Fiches équipier', sans bloc équipier) — migration
    paresseuse, même logique que _ensure_colonne_drive_colis."""
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_generations)").fetchall()}
    if "fichier_pdf_equipier" not in colonnes:
        conn.execute("ALTER TABLE collecte_generations ADD COLUMN fichier_pdf_equipier TEXT")


def _ensure_colonne_drive_colis(conn):
    """La colonne drive_colis a été ajoutée après la création de la table
    collecte_campagnes — migration paresseuse pour les bases déjà existantes
    (dev/prod), comme pour les autres tables du module ajoutées en cours de
    route."""
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "drive_colis" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN drive_colis TEXT")


def _id_drive(url):
    """Extrait l'identifiant Drive (segment /d/{ID}/) d'un lien Google Sheets,
    quel que soit le format exact du lien collé (édition, partage...)."""
    if not url:
        return None
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _url_export_drive(url):
    """Construit l'URL d'export xlsx à partir d'un lien Drive collé par
    l'utilisateur. None si le lien est vide ou ne contient pas d'identifiant
    Drive reconnaissable.

    Paramètre anti-cache (horodatage, toujours différent d'un appel à
    l'autre) : Google sert parfois une version mise en cache de l'export
    d'une feuille pourtant modifiée depuis — un incident réel a montré
    "État" figé sur d'anciennes valeurs malgré plusieurs rechargements.
    Un paramètre d'URL unique empêche ce cache de resservir une ancienne
    réponse."""
    fid = _id_drive(url)
    if not fid:
        return None
    return f"https://docs.google.com/spreadsheets/d/{fid}/export?format=xlsx&_cachebust={int(datetime.now().timestamp())}"


def _service_drive_collecte():
    """Client Drive v3 authentifié (compte de service déjà utilisé ailleurs
    dans l'appli, ex. Trésorerie), ou (None, None) si les identifiants sont
    absents — la recherche automatique devient alors indisponible (voir
    _fichier_drive, qui échoue alors clairement, sans repli sur un ancien
    fichier). Timeout de 30s sur toutes les requêtes (liste et
    téléchargement), pour ne jamais bloquer indéfiniment un worker si
    l'API Google reste muette (serveur à mémoire limitée)."""
    chemin_cle = os.getenv("SERVICE_ACCOUNT_FILE")
    id_drive = os.getenv("BA380_COLLECTE_DRIVE_ID")
    if not chemin_cle or not id_drive or not os.path.exists(chemin_cle):
        return None, None
    import httplib2
    import google_auth_httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    credentials = service_account.Credentials.from_service_account_file(
        chemin_cle, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=30))
    return build("drive", "v3", http=http), id_drive


_CACHE_DOSSIER_FICHIERS_DRIVE_COLLECTE = {}


def _dossier_fichiers_drive_collecte(service, drive_id, annee):
    """Résout (et met en cache en mémoire pour la durée de vie du worker —
    ces dossiers ne changent pour ainsi dire jamais une fois la campagne de
    l'année lancée) le sous-dossier 'fichiers go on the web {annee}...' sous
    'Collecte {annee}', pour éviter de refaire ces 2 recherches à chaque
    fichier alors qu'une même page (ex. Préparer l'envoi) en télécharge
    plusieurs d'affilée pour la même année. Pour une année déjà passée,
    plusieurs variantes du dossier peuvent coexister (ex. 'après collecte',
    repris pour les bilans) : s'il y en a plusieurs, on prend celui dont les
    FICHIERS à l'intérieur sont les plus récents (jamais la date du dossier
    lui-même — incident réel 2026-11-09 : Drive ne remet pas forcément à
    jour la date d'un dossier quand un fichier à l'intérieur est remplacé,
    un dossier resté affiché comme modifié des mois plus tôt contenait en
    fait les fichiers les plus frais, pendant qu'un dossier concurrent plus
    récemment modifié ne contenait que d'anciens fichiers — en comparant les
    dossiers entre eux, pas le contenu, le mauvais était choisi en
    silence). Retourne (id, 'Collecte {annee} / <nom du sous-dossier>') ou
    (None, None) si introuvable."""
    if annee in _CACHE_DOSSIER_FICHIERS_DRIVE_COLLECTE:
        return _CACHE_DOSSIER_FICHIERS_DRIVE_COLLECTE[annee]

    def _lister(q):
        resp = service.files().list(
            q=q, corpora="drive", driveId=drive_id,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
            fields="files(id,name,modifiedTime)",
            orderBy="modifiedTime desc", pageSize=10,
        ).execute()
        return resp.get("files", [])

    def _premier(q):
        fichiers = _lister(q)
        return fichiers[0] if fichiers else None

    def _plus_recent_fichier_dans(id_dossier):
        fichiers = service.files().list(
            q=f"'{id_dossier}' in parents and trashed=false "
              f"and mimeType != 'application/vnd.google-apps.folder'",
            corpora="drive", driveId=drive_id,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
            fields="files(modifiedTime)", pageSize=50,
        ).execute().get("files", [])
        return max((f["modifiedTime"] for f in fichiers), default="")

    dossier_annee = _premier(
        f"'{drive_id}' in parents and mimeType='application/vnd.google-apps.folder' "
        f"and name='Collecte {annee}' and trashed=false"
    )
    sous_dossiers = _lister(
        f"'{dossier_annee['id']}' in parents and mimeType='application/vnd.google-apps.folder' "
        f"and name contains 'go on the web {annee}' and trashed=false"
    ) if dossier_annee else []

    if not sous_dossiers:
        # Jamais mis en cache : un dossier pas encore créé (début de
        # campagne) ou un aléa passager de l'API ne doit pas condamner
        # l'année entière jusqu'au prochain redémarrage — on retente à
        # chaque appel tant qu'on n'a pas trouvé.
        return None, None
    if len(sous_dossiers) > 1:
        sous_dossiers.sort(key=lambda d: _plus_recent_fichier_dans(d["id"]), reverse=True)
    dossier_fichiers = sous_dossiers[0]

    resultat = (dossier_fichiers["id"], f"{dossier_annee['name']} / {dossier_fichiers['name']}")
    _CACHE_DOSSIER_FICHIERS_DRIVE_COLLECTE[annee] = resultat
    return resultat


def _chercher_fichier_drive_collecte(service, drive_id, annee, nom_fichier):
    """Retrouve nom_fichier (ex. 'liste-magasins.xlsx') dans le sous-dossier
    'fichiers go on the web {annee}...' du drive partagé BA380 - COLLECTE —
    par nom, jamais par un lien collé à la main qui peut pointer vers une
    copie périmée ou abandonnée (incident réel : le lien enregistré en
    Paramètres pointait vers un fichier différent du vrai export go-on-web,
    resté figé sur un ancien état malgré plusieurs réimports). Retourne
    (id_fichier, type_mime, chemin_affichage, modifie_le) ou None si
    introuvable."""
    id_dossier, chemin_dossier = _dossier_fichiers_drive_collecte(service, drive_id, annee)
    if not id_dossier:
        return None

    resp = service.files().list(
        q=f"'{id_dossier}' in parents and name='{nom_fichier}' and trashed=false",
        corpora="drive", driveId=drive_id,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
        fields="files(id,name,mimeType,modifiedTime)",
        orderBy="modifiedTime desc", pageSize=10,
    ).execute()
    fichiers = resp.get("files", [])
    if not fichiers:
        return None
    fichier = fichiers[0]
    return (fichier["id"], fichier["mimeType"], f"{chemin_dossier} / {fichier['name']}", fichier["modifiedTime"])


def _fichier_drive_disponible(annee, cle):
    """Vérifie, sans le télécharger, qu'un fichier est trouvable par
    recherche automatique dans le drive partagé BA380 - COLLECTE. Sert
    uniquement aux indicateurs de disponibilité des pages (ex. page
    Production) ; la génération elle-même revalide et rapporte toute
    erreur réelle via _fichier_drive / _telecharger_fichier_drive_collecte."""
    conf = DRIVE_CHAMPS[cle]
    nom_drive = conf.get("nom_drive")
    try:
        service, drive_id = _service_drive_collecte()
        return bool(service and nom_drive and _chercher_fichier_drive_collecte(service, drive_id, annee, nom_drive))
    except Exception:
        return False


_CACHE_FICHIERS_DRIVE_UTILISES = {}
_DUREE_CACHE_FICHIERS_DRIVE_UTILISES = 30  # secondes


def _fichiers_drive_collecte_utilises(annee):
    """Pour l'affichage en Paramètres (transparence sur ce que la recherche
    automatique utilise réellement) : pour chaque type de fichier Collecte,
    son chemin complet dans le drive partagé BA380 - COLLECTE et sa date de
    dernière modification, ou chemin/modifie_le=None si introuvable — pas
    d'erreur ici, cette fonction ne fait qu'informer (voir _fichier_drive
    pour le comportement, lui bloquant, au moment d'utiliser réellement un
    fichier).

    Mis en cache 30s (uniquement cette fonction d'affichage, jamais le
    téléchargement réel) : sans ça, chaque chargement de la page d'accueil
    du module déclenche jusqu'à 12 recherches Drive (6 fichiers × 2
    années), à chaque fois qu'elle est ouverte."""
    maintenant = time.time()
    en_cache = _CACHE_FICHIERS_DRIVE_UTILISES.get(annee)
    if en_cache and maintenant - en_cache[0] < _DUREE_CACHE_FICHIERS_DRIVE_UTILISES:
        return en_cache[1]

    resultats = {}
    try:
        service, drive_id = _service_drive_collecte()
    except Exception:
        service, drive_id = None, None
    for cle, conf in DRIVE_CHAMPS.items():
        nom_drive = conf.get("nom_drive")
        info = {"label": conf["label"], "chemin": None, "modifie_le": None}
        if service and nom_drive:
            try:
                trouve = _chercher_fichier_drive_collecte(service, drive_id, annee, nom_drive)
                if trouve:
                    _id, _mime, chemin, modifie_le = trouve
                    from zoneinfo import ZoneInfo
                    info["chemin"] = chemin
                    info["modifie_le"] = (
                        datetime.fromisoformat(modifie_le)
                        .astimezone(ZoneInfo("Europe/Paris"))
                        .strftime("%d/%m/%Y %H:%M")
                    )
            except Exception:
                pass
        resultats[cle] = info

    _CACHE_FICHIERS_DRIVE_UTILISES[annee] = (maintenant, resultats)
    return resultats


def _telecharger_fichier_drive_collecte(annee, cle):
    """Télécharge, via la recherche par nom ci-dessus, le fichier
    correspondant à cle et l'enregistre en local. Retourne le chemin local,
    ou None si la recherche échoue (identifiants absents, dossier/fichier
    introuvable) — voir _fichier_drive, qui prévient alors l'utilisateur
    clairement plutôt que d'utiliser un ancien fichier."""
    conf = DRIVE_CHAMPS[cle]
    nom_drive = conf.get("nom_drive")
    if not nom_drive:
        return None
    service, drive_id = _service_drive_collecte()
    if not service:
        return None
    resultat = _chercher_fichier_drive_collecte(service, drive_id, annee, nom_drive)
    if not resultat:
        return None
    fichier_id, mime_type, _chemin, _modifie_le = resultat

    from googleapiclient.http import MediaIoBaseDownload
    if mime_type == "application/vnd.google-apps.spreadsheet":
        requete = service.files().export_media(
            fileId=fichier_id,
            mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        requete = service.files().get_media(fileId=fichier_id, supportsAllDrives=True)

    tampon = io.BytesIO()
    telechargeur = MediaIoBaseDownload(tampon, requete)
    termine = False
    while not termine:
        _, termine = telechargeur.next_chunk()
    tampon.seek(0)
    contenu = tampon.read()
    if not contenu.startswith(b"PK"):
        return None

    dossier = _dossier_annee(annee)
    os.makedirs(dossier, exist_ok=True)
    noms_stockage = {
        "vehicules": "liste_vehicules.xlsx",
        "colis": "liste-colis.xlsx",
        "participants_mailing": "liste_participants_mailing.xlsx",
    }
    nom_stockage = FICHIERS.get(cle, {}).get("nom_stockage") or noms_stockage[cle]
    chemin = os.path.join(dossier, nom_stockage)
    with open(chemin, "wb") as fichier:
        fichier.write(contenu)
    return chemin


def _fichier_drive(annee, cle):
    """Retourne le chemin local du fichier correspondant à cle, fraîchement
    retéléchargé par recherche automatique dans le drive partagé
    BA380 - COLLECTE (voir _telecharger_fichier_drive_collecte), ou None
    s'il est introuvable.

    Ne retombe JAMAIS sur un ancien fichier (ni l'ancien lien Google Sheets
    collé à la main, ni un fichier précédemment importé localement) : un
    incident réel a montré qu'un fichier périmé pouvait rester utilisé
    silencieusement des semaines sans que personne ne s'en aperçoive
    (« Super U Vinay » resté marqué comme non gardé alors qu'il l'était
    depuis longtemps sur go-on-web). Mieux vaut un échec visible — qui
    pousse à corriger l'accès au drive — qu'une donnée fausse utilisée sans
    le savoir. Prévient l'utilisateur par un message visible en cas
    d'échec."""
    conf = DRIVE_CHAMPS[cle]
    try:
        chemin = _telecharger_fichier_drive_collecte(annee, cle)
        if chemin:
            return chemin
    except Exception as erreur:
        write_log(f"⚠️ Recherche automatique Drive « {cle} » {annee} impossible : {erreur}")
        flash(
            f"⛔ Impossible de récupérer « {conf['label']} » depuis le drive partagé "
            f"BA380 - COLLECTE ({erreur}) — vérifiez l'accès au drive.",
            "danger",
        )
        return None

    flash(
        f"⛔ « {conf['label']} » introuvable dans le drive partagé BA380 - COLLECTE "
        f"pour {annee} — vérifiez que le fichier existe bien dans "
        f"« Collecte {annee} / fichiers go on the web {annee} ».",
        "danger",
    )
    return None


def _est_camion_reel(code):
    """Un camion réel a soit un code 'VXddd' (camion supplémentaire créé par
    le moteur d'optimisation pour la simulation — toujours réel, jamais un
    placeholder), soit un code 'Vddd' inférieur à V090 — à partir de V090 ce
    sont des lignes go-on-web pour le staffing de l'entrepôt BAI (ex. V090 =
    'BAI Entrepot', magasin vide), pas des camions qui collectent réellement
    des magasins."""
    code = str(code).strip().upper()
    if re.match(r"^VX\d+$", code):
        return True
    match = re.match(r"^V(\d+)$", code)
    return bool(match) and int(match.group(1)) < 90


def _heure_decimale(horaire):
    """'11h30' -> 11.5, '08h00' -> 8.0. None si illisible."""
    m = re.match(r"(\d+)h(\d+)?", str(horaire))
    if not m:
        return None
    return int(m.group(1)) + (int(m.group(2)) if m.group(2) else 0) / 60


def _periode_depuis_horaires(debut, fin):
    """Classe un créneau du planning véhicules en 'Matin' ou 'Apres Midi' —
    d'après le MILIEU du créneau (début+fin)/2, pas seulement l'heure de
    début : un créneau '12h00-18h00' commence avant 13h mais est bien un
    créneau d'après-midi (constaté sur V042 Sécurité Civile) — seule
    l'heure de début aurait classé ce créneau à tort en Matin. Repli sur
    l'heure de début seule si l'heure de fin est absente ou illisible."""
    h_debut = _heure_decimale(debut)
    if h_debut is None:
        return "Matin"
    h_fin = _heure_decimale(fin)
    if h_fin is None:
        return "Matin" if h_debut < 13 else "Apres Midi"
    return "Matin" if (h_debut + h_fin) / 2 < 13 else "Apres Midi"


def _charger_affectations_chauffeurs_equipiers(annee):
    """Construit les affectations par personne depuis le planning véhicules."""
    chemin = _fichier_drive(annee, "vehicules")
    if not chemin:
        chemin = os.path.join(_dossier_annee(annee), "liste_vehicules.xlsx")
    if not os.path.exists(chemin):
        raise FileNotFoundError("Le fichier liste_vehicules.xlsx est introuvable")

    df = pd.read_excel(chemin)
    df.columns = [str(col).strip() for col in df.columns]
    personnes_par_affectation = {}
    affectations_par_personne = {}
    personnes_sans_email = set()
    jours = {"jeudi": "Jeudi", "vendredi": "Vendredi", "samedi": "Samedi", "dimanche": "Dimanche"}

    for _, ligne in df.iterrows():
        code = str(ligne.get("Code", "")).strip()
        personne = str(ligne.get("Équipier", ligne.get("equipier", ""))).strip()
        jour = jours.get(str(ligne.get("Tournée", "")).strip().lower())
        debut = str(ligne.get("Début", "")).strip()
        fin = str(ligne.get("Fin", "")).strip()
        if not code or code == "nan" or not personne or personne == "nan" or not jour:
            continue
        if not _est_camion_reel(code):
            continue
        periode = _periode_depuis_horaires(debut, fin)
        if periode == "Apres Midi":
            periode = "Après-midi"
        demi_journee = f"{jour} {periode}"
        email = str(ligne.get("Email", "")).strip().lower()
        if email == "nan":
            email = ""
        role_re = str(ligne.get("R/E", "")).strip().upper()
        if role_re == "R":
            role = "chauffeur"
        elif role_re == "E":
            role = "équipier"
        else:
            # Repli si la colonne R/E est vide pour cette ligne : ancienne
            # heuristique par le nom (utile pour les placeholders type
            # "Chauffeur BD 1" saisis directement dans la colonne personne).
            role = "chauffeur" if personne.lower().startswith("chauffeur") else "équipier"
        cle_personne = email or f"nom:{personne.lower()}"
        affectation = {
            "demi_journee": demi_journee,
            "camion": code,
            "nom_camion": str(ligne.get("Véhicule", "")).strip(),
            "magasin": str(ligne.get("Magasin", "")).strip(),
            "personne": personne,
            "role": role,
            "email": email,
        }
        affectations_par_personne.setdefault(cle_personne, {"nom": personne, "role": role, "email": email, "affectations": []})["affectations"].append(affectation)
        personnes_par_affectation.setdefault((demi_journee, code), set()).add(personne)
        if not email:
            personnes_sans_email.add(personne)

    destinataires = []
    for personne in affectations_par_personne.values():
        affectations_regroupees = {}
        for affectation in personne["affectations"]:
            cle_affectation = (affectation["demi_journee"], affectation["camion"], affectation["nom_camion"])
            groupe = affectations_regroupees.setdefault(cle_affectation, {**affectation, "magasins": []})
            if affectation["magasin"] and affectation["magasin"] != "nan" and affectation["magasin"] not in groupe["magasins"]:
                groupe["magasins"].append(affectation["magasin"])

        lignes_personne = []
        for affectation in affectations_regroupees.values():
            autres = sorted(personnes_par_affectation[(affectation["demi_journee"], affectation["camion"])] - {personne["nom"]})
            lignes_personne.append({**affectation, "autres": autres})
        lignes_personne.sort(key=lambda item: (
            ORDRE_DEMI_JOURNEES_MAIL.index(item["demi_journee"])
            if item["demi_journee"] in ORDRE_DEMI_JOURNEES_MAIL else len(ORDRE_DEMI_JOURNEES_MAIL),
            item["camion"],
        ))
        personne["affectations"] = lignes_personne
        if personne["email"]:
            destinataires.append(personne)

    destinataires.sort(key=lambda personne: personne["nom"].lower())
    return destinataires, sorted(personnes_sans_email, key=str.lower)


def _charger_magasins_par_camion(annee):
    """Regroupe les magasins du planning véhicules réel par demi-journée puis
    camion — pour le filtre de sélection de la page de saisie des cagettes
    (choisir une demi-journée puis un camion pour ne voir que ses magasins).
    Même source que _charger_affectations_chauffeurs_equipiers (planning réel
    du jour, avec Code VIF en colonne — pas la simulation d'optimisation),
    mais restreint aux libellés de demi-journée de moteur.DEMI_JOURNEES pour
    matcher les colonnes de la page de saisie."""
    chemin = _fichier_drive(annee, "vehicules")
    if not chemin:
        chemin = os.path.join(_dossier_annee(annee), "liste_vehicules.xlsx")
    if not os.path.exists(chemin):
        return {}

    df = pd.read_excel(chemin)
    df.columns = [str(col).strip() for col in df.columns]
    jours = {"jeudi": "Jeudi", "vendredi": "Vendredi", "samedi": "Samedi", "dimanche": "Dimanche"}

    par_dj = {}
    for _, ligne in df.iterrows():
        code = str(ligne.get("Code", "")).strip()
        jour = jours.get(str(ligne.get("Tournée", "")).strip().lower())
        debut = str(ligne.get("Début", "")).strip()
        fin = str(ligne.get("Fin", "")).strip()
        if not code or code == "nan" or not jour or not _est_camion_reel(code):
            continue
        code_vif_brut = ligne.get("Code VIF")
        if code_vif_brut is None or (isinstance(code_vif_brut, float) and pd.isna(code_vif_brut)):
            continue
        code_vif = _vif_fmt(code_vif_brut)
        if not code_vif:
            continue
        periode = _periode_depuis_horaires(debut, fin)
        demi_journee = f"{jour} {periode}"
        if demi_journee not in moteur.DEMI_JOURNEES:
            continue
        nom_camion = str(ligne.get("Véhicule", "")).strip()
        camions = par_dj.setdefault(demi_journee, {})
        entree = camions.setdefault(
            code.upper(),
            {"camion": code.upper(), "nom_camion": "" if nom_camion == "nan" else nom_camion, "codes_vif": set()},
        )
        entree["codes_vif"].add(code_vif)

    return {
        dj: sorted(
            [{**c, "codes_vif": sorted(c["codes_vif"])} for c in camions.values()],
            key=lambda c: c["camion"],
        )
        for dj, camions in par_dj.items()
    }


def _ensure_table_cagettes_production(conn):
    """Historique du nb de cagettes par magasin/demi-journée (Production,
    document 1 + onglet 'Cagettes manquantes') — gérée depuis l'appli
    (grille éditable, cagettes_production()) depuis que le fichier Excel
    Cagettes_magasins.xlsx n'est plus déposé sur le drive. Une ligne par
    couple (Code VIF, demi-journée) réellement planifié."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_cagettes_production (
            annee INTEGER NOT NULL,
            code_vif TEXT NOT NULL,
            jour TEXT NOT NULL,
            cag1 TEXT, cag2 TEXT, cag3 TEXT, cag4 TEXT,
            maj_le TEXT,
            auto_defaut INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (annee, code_vif, jour)
        )
    """)
    try:
        conn.execute("ALTER TABLE collecte_cagettes_production ADD COLUMN auto_defaut INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    for col in ("ref1", "ref2", "ref3", "ref4", "ref_le", "ref_total"):
        try:
            conn.execute(f"ALTER TABLE collecte_cagettes_production ADD COLUMN {col} TEXT")
        except Exception:
            pass


def _normaliser_jour_cagettes(s):
    """Copie de normaliser_dj (ba38_collecte/scripts/generer_documents_production.py)
    pour lire le même format de fichier côté import web, sans dépendre du
    script CLI (qui n'est pas pensé pour être importé comme module)."""
    s = str(s).strip()
    if not s or s == "nan":
        return ""
    s_low = s.lower().replace("è", "e").replace("é", "e").replace("à", "a")
    jours = ["jeudi", "vendredi", "samedi", "dimanche"]
    jour = next((j for j in jours if s_low.startswith(j)), "")
    if not jour:
        return ""
    periode = "Apres Midi" if "apres" in s_low or "am" in s_low.split() else "Matin"
    return f"{jour.capitalize()} {periode}"


def _combos_cagettes_production(annee):
    """Couples (Code VIF, demi-journée, nom magasin) réellement planifiés
    cette année dans le planning véhicules réel (même source que
    _charger_magasins_par_camion) — lignes de référence de la grille de
    gestion des cagettes de Production (un magasin peut apparaître
    plusieurs fois s'il est collecté à plusieurs demi-journées)."""
    chemin = _fichier_drive(annee, "vehicules")
    if not chemin:
        chemin = os.path.join(_dossier_annee(annee), "liste_vehicules.xlsx")
    if not os.path.exists(chemin):
        return []

    df = pd.read_excel(chemin)
    df.columns = [str(col).strip() for col in df.columns]
    jours = {"jeudi": "Jeudi", "vendredi": "Vendredi", "samedi": "Samedi", "dimanche": "Dimanche"}

    combos = {}
    for _, ligne in df.iterrows():
        code = str(ligne.get("Code", "")).strip()
        jour = jours.get(str(ligne.get("Tournée", "")).strip().lower())
        debut = str(ligne.get("Début", "")).strip()
        fin = str(ligne.get("Fin", "")).strip()
        if not code or code == "nan" or not jour or not _est_camion_reel(code):
            continue
        code_vif = _vif_fmt(ligne.get("Code VIF"))
        if not code_vif:
            continue
        periode = _periode_depuis_horaires(debut, fin)
        demi_journee = f"{jour} {periode}"
        if demi_journee not in moteur.DEMI_JOURNEES:
            continue
        nom = str(ligne.get("Magasin", "")).strip()
        combos[(code_vif, demi_journee)] = "" if nom == "nan" else nom

    return sorted(
        [{"code_vif": vif, "jour": dj, "nom_magasin": nom} for (vif, dj), nom in combos.items()],
        key=lambda c: (c["jour"], c["nom_magasin"].lower()),
    )


def _contenu_mail_affectations(annee):
    with get_db_connection() as conn:
        ligne = conn.execute(
            "SELECT mail_texte FROM collecte_campagnes WHERE annee = ?",
            (annee,),
        ).fetchone()
    texte = ligne["mail_texte"] if ligne and ligne["mail_texte"] else MAIL_TEXTE_DEFAUT
    return texte


def _chemin_image_mail_chauffeurs_equipiers():
    """Chemin de l'image du mail chauffeurs/équipiers (ex. plan de parking),
    dans l'emplacement PARTAGÉ du module (comme l'affiche des produits de la
    demande d'autorisation) : déposée une fois, elle reste disponible d'une
    campagne et d'une session à l'autre sans redépôt annuel. |None| si
    absente."""
    for extension in EXTENSIONS_IMAGE_MAIL_AUTORISEES:
        chemin = os.path.join(MODELES_GARDEE_DIR, f"mail_chauffeurs_equipiers.{extension}")
        if os.path.exists(chemin):
            return chemin
    return None


PIECE_JOINTE_MAIL_PREFIXE = "mail_chauffeurs_equipiers_pj_"


def _pieces_jointes_mail():
    """Liste des pièces jointes PDF persistantes du mail chauffeurs/équipiers
    — déposées dans l'emplacement partagé du module (comme l'image et
    l'affiche des produits), donc disponibles d'une campagne et d'une
    session à l'autre sans redépôt annuel. Le nom affiché retire le préfixe
    technique utilisé pour les distinguer des autres fichiers partagés."""
    chemins = sorted(glob.glob(os.path.join(MODELES_GARDEE_DIR, f"{PIECE_JOINTE_MAIL_PREFIXE}*.pdf")))
    return [
        {"nom": os.path.basename(chemin)[len(PIECE_JOINTE_MAIL_PREFIXE):], "chemin": chemin}
        for chemin in chemins
    ]


def _corps_mail_affectations(personne, texte=MAIL_TEXTE_DEFAUT):
    """Un seul texte modifiable en ligne, avec le repère <<affectations>> à
    l'endroit où insérer la liste des tournées (demi-journée / véhicule /
    magasin / équipiers) — sur le même principe que <<dates>> pour la
    demande d'autorisation de collecter. Si le repère est absent (ancien
    texte non migré, ou supprimé par erreur), la liste est ajoutée à la
    fin plutôt que perdue."""
    texte = texte.replace("<<nom>>", personne["nom"])

    lignes = []
    demi_journee_precedente = None
    for affectation in personne["affectations"]:
        if demi_journee_precedente is not None and affectation["demi_journee"] != demi_journee_precedente:
            lignes.append("")
        demi_journee_precedente = affectation["demi_journee"]
        camion = affectation["camion"]
        if affectation["nom_camion"] and affectation["nom_camion"] != "nan":
            camion += f" - {affectation['nom_camion']}"
        lignes.append(f"- {affectation['demi_journee']} : {camion}")
        if affectation["magasins"]:
            lignes.append(f"    Magasins : {', '.join(affectation['magasins'])}")
        if affectation["autres"]:
            lignes.append(f"  Avec : {', '.join(affectation['autres'])}")
    bloc_affectations = "\n".join(lignes)

    if "<<affectations>>" in texte:
        texte = texte.replace("<<affectations>>", bloc_affectations)
    else:
        texte = texte.rstrip() + "\n\n" + bloc_affectations
    return texte.strip()


def _corps_mail_affectations_html(personne, texte, image_url):
    """Version HTML du mail (mêmes lignes que _corps_mail_affectations),
    avec le marqueur <<image>> remplacé par une balise <img> pointant vers
    l'image de campagne (parking, consignes...) si une image a été
    déposée, sinon simplement retiré."""
    texte = _corps_mail_affectations(personne, texte)
    html = str(escape(texte)).replace("\n", "<br>\n")
    marqueur = str(escape("<<image>>"))
    if image_url:
        remplacement = f'<img src="{escape(image_url)}" alt="" style="max-width:100%;">'
    else:
        remplacement = ""
    html = html.replace(marqueur, remplacement)
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">{html}</div>'


# ============================================================================
# 👥 Participants — mail aux trieurs (année précédente)
# ============================================================================
# Les "trieurs" sont les personnes affectées au camion V090 ("BAI Entrepôt")
# dans le planning véhicules — un code non réel qui sert à go-on-web pour le
# staffing de l'entrepôt (cf. _est_camion_reel), pas une tournée de collecte.
# Lus sur l'année PRÉCÉDENTE : utile en tout début de campagne, avant que le
# planning de l'année en cours n'existe, pour recontacter les trieurs de l'an
# dernier.
MAIL_TEXTE_TRIEURS_DEFAUT = (
    "Bonjour <<nom>>,\n\n"
    "Vous aviez participé au tri des denrées à l'entrepôt lors de la collecte de l'an dernier, "
    "un grand merci pour votre aide !\n\n"
    "Nous préparons la collecte de cette année et serions ravis de vous compter à nouveau parmi "
    "les trieurs. Si vous êtes disponible, répondez à ce mail ou contactez-nous directement.\n\n"
    "Merci."
)


def _ensure_colonne_mail_trieurs(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "mail_texte_trieurs" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_texte_trieurs TEXT")


def _charger_trieurs_annee_precedente(annee):
    """Personnes affectées au camion V090 dans le planning véhicules de
    l'année précédente — nom + email, dédoublonnés."""
    annee_precedente = annee - 1
    chemin = _fichier_drive(annee_precedente, "vehicules")
    if not chemin:
        chemin = os.path.join(_dossier_annee(annee_precedente), "liste_vehicules.xlsx")
    if not os.path.exists(chemin):
        raise FileNotFoundError(f"Le fichier véhicules {annee_precedente} est introuvable")

    df = pd.read_excel(chemin)
    df.columns = [str(col).strip() for col in df.columns]
    personnes = {}
    sans_email = set()
    for _, ligne in df.iterrows():
        code = str(ligne.get("Code", "")).strip().upper()
        if code != "V090":
            continue
        personne = str(ligne.get("Équipier", ligne.get("equipier", ""))).strip()
        if not personne or personne == "nan":
            continue
        email = str(ligne.get("Email", "")).strip().lower()
        if email == "nan":
            email = ""
        cle = email or f"nom:{personne.lower()}"
        personnes.setdefault(cle, {"nom": personne, "email": email})
        if not email:
            sans_email.add(personne)

    destinataires = sorted(
        (p for p in personnes.values() if p["email"]), key=lambda p: p["nom"].lower()
    )
    return destinataires, sorted(sans_email, key=str.lower)


def _contenu_mail_trieurs(annee):
    with get_db_connection() as conn:
        _ensure_colonne_mail_trieurs(conn)
        ligne = conn.execute(
            "SELECT mail_texte_trieurs FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    return ligne["mail_texte_trieurs"] if ligne and ligne["mail_texte_trieurs"] else MAIL_TEXTE_TRIEURS_DEFAUT


def _corps_mail_trieurs(personne, texte):
    return texte.replace("<<nom>>", personne["nom"])


def _corps_mail_trieurs_html(personne, texte):
    html = str(escape(_corps_mail_trieurs(personne, texte))).replace("\n", "<br>\n")
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">{html}</div>'


# ============================================================================
# 📬 TRAÇABILITÉ DES ENVOIS MAILJET — partagée par toutes les pages d'envoi
# en masse de Collecte (trieurs, disponibilité chauffeurs, prise colis,
# bilan, ressenti x3, affectations tournées, autorisations, non-collecte
# bénévoles, sans-vendredi, associations gardant...). Même principe que
# indicateurs_suivi (ba38_partenaires/indicateurs.py) — capture le MessageID
# Mailjet à l'envoi, vérification du statut final différée sur action
# explicite de l'utilisateur (jamais d'appel Mailjet au chargement de la
# page) — mais généralisé ici dans une seule table partagée (clé
# annee+feature+email) plutôt que redupliqué colonne par colonne dans
# chaque table métier comme c'est le cas ailleurs dans le projet (demande
# utilisateur 2026-11-10).
# ============================================================================

def _ensure_table_collecte_envois_mail(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_envois_mail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annee INTEGER NOT NULL,
            feature TEXT NOT NULL,
            email TEXT NOT NULL,
            nom TEXT,
            envoye_le TEXT,
            erreur TEXT,
            mailjet_status TEXT,
            mailjet_message_id TEXT,
            statut_final TEXT,
            statut_verifie_le TEXT,
            UNIQUE(annee, feature, email)
        )
    """)


def _enregistrer_envoi_mail_collecte(annee, feature, email, nom, resultat=None, erreur=None):
    """À appeler juste après chaque envoyer_mail() d'une boucle d'envoi en
    masse — resultat = objet retourné par envoyer_mail() en cas de succès
    (JSON Mailjet), erreur = message si l'envoi a échoué (exception)."""
    if not email:
        return
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mailjet_status, mailjet_message_id = None, None
    if resultat and resultat.get("Messages"):
        mj_message = resultat["Messages"][0]
        mailjet_status = mj_message.get("Status")
        ids = [str(t["MessageID"]) for t in mj_message.get("To", []) if "MessageID" in t]
        mailjet_message_id = ids[0] if ids else None
    with get_db_connection() as conn:
        _ensure_table_collecte_envois_mail(conn)
        conn.execute("""
            INSERT INTO collecte_envois_mail
                (annee, feature, email, nom, envoye_le, erreur, mailjet_status, mailjet_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(annee, feature, email) DO UPDATE SET
                nom = excluded.nom, envoye_le = excluded.envoye_le, erreur = excluded.erreur,
                mailjet_status = excluded.mailjet_status, mailjet_message_id = excluded.mailjet_message_id,
                statut_final = NULL, statut_verifie_le = NULL
        """, (annee, feature, email, nom, maintenant, erreur, mailjet_status, mailjet_message_id))
        conn.commit()


def _statuts_envois_mail_collecte(annee, feature):
    """{email: ligne} des statuts déjà connus pour cette page, à fusionner
    avec la liste de destinataires affichée (cf. _enregistrer_envoi_mail_collecte)."""
    with get_db_connection() as conn:
        _ensure_table_collecte_envois_mail(conn)
        lignes = conn.execute(
            "SELECT * FROM collecte_envois_mail WHERE annee = ? AND feature = ?", (annee, feature)
        ).fetchall()
    return {r["email"]: dict(r) for r in lignes}


def _destinataires_restants_mail_collecte(annee, feature, destinataires):
    """Retire de la liste les destinataires déjà enregistrés comme envoyés
    avec succès pour cette année/feature — permet de relancer « Envoyer »
    sans dupliquer les mails déjà partis (incident 504 du 2026-11-19 : un
    envoi de masse interrompu en cours de route n'a aucun moyen de reprendre
    sans ce filtre, cf. [[ba38_collecte_mail_masse_arriere_plan]])."""
    statuts = _statuts_envois_mail_collecte(annee, feature)
    return [
        p for p in destinataires
        if not (statuts.get(p["email"]) and not statuts[p["email"]].get("erreur"))
    ]


def _envoyer_mails_masse_arriere_plan(app, annee, feature, destinataires, sujet, corps_fn, sender, current_user_email):
    """Boucle d'envoi un par un, exécutée dans une tâche de fond (cf.
    lancer_tache_fond) — une grande liste de destinataires (ex. 991 pour le
    bilan collecte 2026) dépasse le timeout gunicorn (120 s) si elle est
    envoyée de façon synchrone dans la requête, ce qui tue le worker avant
    la fin de la boucle (WORKER TIMEOUT, 504 côté navigateur, incident du
    2026-11-19). Chaque envoi est enregistré au fur et à mesure (commit
    immédiat), donc un 504/crash en cours de route ne perd aucun statut —
    cf. _destinataires_restants_mail_collecte pour la reprise. current_user_email
    capturé avant le lancement (pas de contexte requête ici) : nécessaire au
    garde-fou mail DEV (cf. ba38_utilitaires.core.envoyer_mail)."""
    with app.app_context():
        for personne in destinataires:
            try:
                resultat = envoyer_mail(
                    sujet, [personne["email"]], corps_fn(personne),
                    sender_override=sender, is_html=True,
                    current_user_email=current_user_email,
                )
                _enregistrer_envoi_mail_collecte(annee, feature, personne["email"], personne.get("nom", ""), resultat=resultat)
            except Exception as e:
                write_log(f"❌ Collecte {annee} : échec envoi mail {feature} à {personne.get('email','')} — {e}")
                _enregistrer_envoi_mail_collecte(annee, feature, personne.get("email", ""), personne.get("nom", ""), erreur=str(e))


@collecte_bp.route("/collecte/envois-mail/verifier-statut", methods=["POST"])
@login_required
@require_access("collecte", "lecture")
def collecte_verifier_statut_mailjet():
    """Vérifie auprès de Mailjet le statut final (sent/opened/bounce/...) de
    chaque message envoyé pour cette page (annee+feature), et le fige en
    base — bouton « 🔄 Vérifier statut Mailjet » commun à toutes les pages
    d'envoi de Collecte."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    feature = request.form.get("feature", "").strip()
    retour = request.form.get("retour") or url_for("collecte.collecte_main", annee=annee)

    with get_db_connection() as conn:
        _ensure_table_collecte_envois_mail(conn)
        lignes = conn.execute(
            "SELECT id, mailjet_message_id FROM collecte_envois_mail "
            "WHERE annee = ? AND feature = ? AND mailjet_message_id IS NOT NULL",
            (annee, feature),
        ).fetchall()

    compte = {}
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        for ligne in lignes:
            statut = mailjet_get_message_status(ligne["mailjet_message_id"])
            if statut:
                conn.execute(
                    "UPDATE collecte_envois_mail SET statut_final = ?, statut_verifie_le = ? WHERE id = ?",
                    (statut, maintenant, ligne["id"]),
                )
                compte[statut] = compte.get(statut, 0) + 1
        conn.commit()

    if compte:
        flash("🔄 Statuts Mailjet vérifiés : " + ", ".join(f"{v} {k}" for k, v in compte.items()), "info")
    else:
        flash("⚠️ Aucun statut récupéré (aucun envoi tracé, ou Mailjet injoignable).", "warning")
    return redirect(retour)


@collecte_bp.route("/collecte/mail_trieurs", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def mail_trieurs():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    try:
        destinataires, personnes_sans_email = _charger_trieurs_annee_precedente(annee)
        mail_texte = _contenu_mail_trieurs(annee)
    except Exception as erreur:
        flash(f"❌ Impossible de charger les trieurs de {annee - 1} : {erreur}", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    if request.method == "POST":
        action = request.form.get("action")
        if not destinataires:
            flash("❌ Aucun trieur avec une adresse email.", "danger")
        elif action == "test":
            email_test = request.form.get("test_destinataire", "")
            exemple = next((p for p in destinataires if p["email"] == email_test), destinataires[0])
            adresse_test = getattr(current_user, "email", "") or ""
            if not adresse_test:
                flash("❌ Votre compte n'a pas d'adresse email pour le test.", "danger")
            else:
                envoyer_mail(
                    f"[TEST] Mail aux trieurs {annee - 1}",
                    [adresse_test],
                    _corps_mail_trieurs_html(exemple, mail_texte),
                    sender_override="ba380.directeur@banquealimentaire.org",
                    is_html=True,
                )
                flash(f"✅ Mail de test envoyé à {adresse_test}.", "success")
        elif action == "envoyer":
            if request.form.get("confirmation_envoi") != "oui":
                flash("❌ Cochez d'abord « Je confirme l'envoi » — rien n'a été envoyé.", "danger")
            else:
                echecs = []
                for personne in destinataires:
                    try:
                        resultat = envoyer_mail(
                            f"Collecte {annee} — recherche de trieurs",
                            [personne["email"]],
                            _corps_mail_trieurs_html(personne, mail_texte),
                            sender_override="ba380.directeur@banquealimentaire.org",
                            is_html=True,
                        )
                        _enregistrer_envoi_mail_collecte(annee, "trieurs", personne["email"], personne.get("nom", ""), resultat=resultat)
                    except Exception as e:
                        echecs.append(personne.get("email", ""))
                        write_log(f"❌ Collecte {annee} : échec envoi mail trieurs à {personne.get('email','')} — {e}")
                        _enregistrer_envoi_mail_collecte(annee, "trieurs", personne.get("email", ""), personne.get("nom", ""), erreur=str(e))
                reussis = len(destinataires) - len(echecs)
                if echecs:
                    flash(f"⚠️ {reussis} mail(s) envoyé(s), {len(echecs)} échec(s) : " + ", ".join(echecs), "warning")
                else:
                    flash(f"✅ {reussis} mail(s) préparé(s).", "success")

    statuts = _statuts_envois_mail_collecte(annee, "trieurs")
    for p in destinataires:
        p["statut_mailjet"] = statuts.get(p["email"])

    return render_template(
        "collecte/mail_trieurs.html",
        annee=annee,
        destinataires=destinataires,
        personnes_sans_email=personnes_sans_email,
        mail_texte=mail_texte,
        feature="trieurs",
    )


@collecte_bp.route("/collecte/<int:annee>/mail_trieurs/apercu")
@login_required
@require_access("collecte", "lecture")
def mail_trieurs_apercu(annee):
    """Rendu HTML exact du mail pour la personne sélectionnée, sans rien
    envoyer (même en test) — même principe que l'aperçu chauffeurs/équipiers."""
    destinataires, _ = _charger_trieurs_annee_precedente(annee)
    if not destinataires:
        abort(404)
    mail_texte = _contenu_mail_trieurs(annee)
    email = request.args.get("email", "")
    personne = next((p for p in destinataires if p["email"] == email), destinataires[0])
    html = _corps_mail_trieurs_html(personne, mail_texte)
    return Response(html, mimetype="text/html")


@collecte_bp.route("/collecte/mail_trieurs/contenu-mail", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_contenu_mail_trieurs():
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("mail_texte", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_mail_trieurs(conn)
        conn.execute("UPDATE collecte_campagnes SET mail_texte_trieurs = ? WHERE annee = ?", (texte, annee))
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.mail_trieurs", annee=annee))


# ============================================================================
# 👥 Chauffeurs/équipiers (année précédente) — demande de disponibilité
# ============================================================================
# Distinct de chauffeurs_equipiers() : ici on ne connaît pas encore le
# planning de l'année en cours, donc pas d'affectation par demi-journée à
# afficher — on recontacte juste les chauffeurs/équipiers de l'an dernier
# pour leur demander s'ils sont disponibles cette année (même principe que
# mail_trieurs, étendu aux deux rôles R/E).
MAIL_TEXTE_DISPONIBILITE_CHAUFFEURS_DEFAUT = (
    "Bonjour <<nom>>,\n\n"
    "Vous aviez participé à la collecte nationale des Banques Alimentaires l'an dernier comme "
    "chauffeur ou équipier, un grand merci pour votre aide !\n\n"
    "Nous préparons la collecte de cette année et serions ravis de vous compter à nouveau parmi "
    "nos bénévoles. Si vous êtes disponible, répondez à ce mail ou contactez-nous directement.\n\n"
    "Merci."
)


def _ensure_colonne_mail_disponibilite_chauffeurs(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "mail_texte_disponibilite_chauffeurs" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_texte_disponibilite_chauffeurs TEXT")


def _charger_chauffeurs_equipiers_annee_precedente(annee):
    """Chauffeurs/équipiers du planning véhicules de l'année précédente —
    nom + rôle + email + téléphone portable, dédoublonnés, SANS affectation
    par demi-journée (contrairement à _charger_affectations_chauffeurs_equipiers) :
    on ne demande ici qu'une disponibilité, pas un planning qui n'existe pas
    encore pour l'année en cours."""
    annee_precedente = annee - 1
    chemin = _fichier_drive(annee_precedente, "vehicules")
    if not chemin:
        chemin = os.path.join(_dossier_annee(annee_precedente), "liste_vehicules.xlsx")
    if not os.path.exists(chemin):
        raise FileNotFoundError(f"Le fichier véhicules {annee_precedente} est introuvable")

    df = pd.read_excel(chemin)
    df.columns = [str(col).strip() for col in df.columns]
    personnes = {}
    sans_email = set()
    for _, ligne in df.iterrows():
        code = str(ligne.get("Code", "")).strip()
        if not code or code == "nan" or not _est_camion_reel(code):
            continue
        personne = str(ligne.get("Équipier", ligne.get("equipier", ""))).strip()
        if not personne or personne == "nan":
            continue
        email = str(ligne.get("Email", "")).strip().lower()
        if email == "nan":
            email = ""
        telephone = str(ligne.get("Portable", "")).strip()
        if telephone == "nan":
            telephone = ""
        role_re = str(ligne.get("R/E", "")).strip().upper()
        if role_re == "R":
            role = "chauffeur"
        elif role_re == "E":
            role = "équipier"
        else:
            role = "chauffeur" if personne.lower().startswith("chauffeur") else "équipier"
        cle = email or f"nom:{personne.lower()}"
        entree = personnes.setdefault(cle, {"nom": personne, "role": role, "email": email, "telephone": telephone})
        if telephone and not entree["telephone"]:
            entree["telephone"] = telephone
        if not email:
            sans_email.add(personne)

    destinataires = sorted(
        (p for p in personnes.values() if p["email"]), key=lambda p: p["nom"].lower()
    )
    return destinataires, sorted(sans_email, key=str.lower)


def _contenu_mail_disponibilite_chauffeurs(annee):
    with get_db_connection() as conn:
        _ensure_colonne_mail_disponibilite_chauffeurs(conn)
        ligne = conn.execute(
            "SELECT mail_texte_disponibilite_chauffeurs FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    return (
        ligne["mail_texte_disponibilite_chauffeurs"] if ligne and ligne["mail_texte_disponibilite_chauffeurs"]
        else MAIL_TEXTE_DISPONIBILITE_CHAUFFEURS_DEFAUT
    )


def _corps_mail_disponibilite_chauffeurs(personne, texte):
    return texte.replace("<<nom>>", personne["nom"]).replace("<<telephone>>", personne["telephone"] or "—")


def _corps_mail_disponibilite_chauffeurs_html(personne, texte):
    html = str(escape(_corps_mail_disponibilite_chauffeurs(personne, texte))).replace("\n", "<br>\n")
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">{html}</div>'


@collecte_bp.route("/collecte/mail_disponibilite_chauffeurs_equipiers", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def mail_disponibilite_chauffeurs_equipiers():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    try:
        destinataires, personnes_sans_email = _charger_chauffeurs_equipiers_annee_precedente(annee)
        mail_texte = _contenu_mail_disponibilite_chauffeurs(annee)
    except Exception as erreur:
        flash(f"❌ Impossible de charger les chauffeurs/équipiers de {annee - 1} : {erreur}", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    if request.method == "POST":
        action = request.form.get("action")
        if not destinataires:
            flash("❌ Aucun chauffeur ou équipier avec une adresse email.", "danger")
        elif action == "test":
            email_test = request.form.get("test_destinataire", "")
            exemple = next((p for p in destinataires if p["email"] == email_test), destinataires[0])
            adresse_test = getattr(current_user, "email", "") or ""
            if not adresse_test:
                flash("❌ Votre compte n'a pas d'adresse email pour le test.", "danger")
            else:
                envoyer_mail(
                    f"[TEST] Disponibilité chauffeurs/équipiers {annee}",
                    [adresse_test],
                    _corps_mail_disponibilite_chauffeurs_html(exemple, mail_texte),
                    sender_override="ba380.directeur@banquealimentaire.org",
                    is_html=True,
                )
                flash(f"✅ Mail de test envoyé à {adresse_test}.", "success")
        elif action == "envoyer":
            if request.form.get("confirmation_envoi") != "oui":
                flash("❌ Cochez d'abord « Je confirme l'envoi » — rien n'a été envoyé.", "danger")
            else:
                echecs = []
                for personne in destinataires:
                    try:
                        resultat = envoyer_mail(
                            f"Collecte {annee} — recherche de chauffeurs et équipiers",
                            [personne["email"]],
                            _corps_mail_disponibilite_chauffeurs_html(personne, mail_texte),
                            sender_override="ba380.directeur@banquealimentaire.org",
                            is_html=True,
                        )
                        _enregistrer_envoi_mail_collecte(annee, "disponibilite_chauffeurs", personne["email"], personne.get("nom", ""), resultat=resultat)
                    except Exception as e:
                        echecs.append(personne.get("email", ""))
                        write_log(f"❌ Collecte {annee} : échec envoi mail disponibilité chauffeurs à {personne.get('email','')} — {e}")
                        _enregistrer_envoi_mail_collecte(annee, "disponibilite_chauffeurs", personne.get("email", ""), personne.get("nom", ""), erreur=str(e))
                reussis = len(destinataires) - len(echecs)
                if echecs:
                    flash(f"⚠️ {reussis} mail(s) envoyé(s), {len(echecs)} échec(s) : " + ", ".join(echecs), "warning")
                else:
                    flash(f"✅ {reussis} mail(s) préparé(s).", "success")

    statuts = _statuts_envois_mail_collecte(annee, "disponibilite_chauffeurs")
    for p in destinataires:
        p["statut_mailjet"] = statuts.get(p["email"])

    return render_template(
        "collecte/mail_disponibilite_chauffeurs_equipiers.html",
        annee=annee,
        destinataires=destinataires,
        personnes_sans_email=personnes_sans_email,
        mail_texte=mail_texte,
        feature="disponibilite_chauffeurs",
    )


@collecte_bp.route("/collecte/<int:annee>/mail_disponibilite_chauffeurs_equipiers/apercu")
@login_required
@require_access("collecte", "lecture")
def mail_disponibilite_chauffeurs_equipiers_apercu(annee):
    """Rendu HTML exact du mail pour la personne sélectionnée, sans rien
    envoyer (même en test) — même principe que mail_trieurs_apercu."""
    destinataires, _ = _charger_chauffeurs_equipiers_annee_precedente(annee)
    if not destinataires:
        abort(404)
    mail_texte = _contenu_mail_disponibilite_chauffeurs(annee)
    email = request.args.get("email", "")
    personne = next((p for p in destinataires if p["email"] == email), destinataires[0])
    html = _corps_mail_disponibilite_chauffeurs_html(personne, mail_texte)
    return Response(html, mimetype="text/html")


@collecte_bp.route("/collecte/mail_disponibilite_chauffeurs_equipiers/contenu-mail", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_contenu_mail_disponibilite_chauffeurs():
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("mail_texte", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_mail_disponibilite_chauffeurs(conn)
        conn.execute(
            "UPDATE collecte_campagnes SET mail_texte_disponibilite_chauffeurs = ? WHERE annee = ?",
            (texte, annee),
        )
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.mail_disponibilite_chauffeurs_equipiers", annee=annee))


MAIL_TEXTE_PRISE_COLIS_DEFAUT = (
    "Bonjour <<nom>>,\n\n"
    "Vous êtes référent(e) pour la collecte de <<magasin>>, merci pour votre implication !\n\n"
    "Le colis de collecte de cette année est prêt — merci de convenir avec nous d'un "
    "rendez-vous pour venir le récupérer dès que possible.\n\n"
    "Merci."
)


def _ensure_colonne_mail_prise_colis(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "mail_texte_prise_colis" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_texte_prise_colis TEXT")


def _charger_primo_responsables_colis(annee):
    """Primo responsables (référents magasin) du référentiel colis de
    l'année, pour les seuls magasins dont le colis est prêt mais pas encore
    pris (colonnes 'Préparé'='OUI' et 'Pris'='NON') — nom + email,
    dédoublonnés (un même référent peut couvrir plusieurs magasins, les
    noms de magasins concernés sont alors regroupés)."""
    chemin = _fichier_drive(annee, "colis")
    if not chemin:
        chemin = os.path.join(_dossier_annee(annee), "liste-colis.xlsx")
    if not os.path.exists(chemin):
        raise FileNotFoundError(f"Le fichier colis {annee} est introuvable")

    df = pd.read_excel(chemin)
    df.columns = [str(col).strip() for col in df.columns]
    personnes = {}
    sans_email = set()
    sans_portable = set()
    for _, ligne in df.iterrows():
        prepare = str(ligne.get("Préparé", "")).strip().upper()
        pris = str(ligne.get("Pris", "")).strip().upper()
        if prepare != "OUI" or pris == "OUI":
            continue
        nom_magasin = str(ligne.get("Nom magasin", "")).strip()
        personne = str(ligne.get("Primo responsable", "")).strip()
        if not personne or personne == "nan":
            continue
        email = str(ligne.get("email", "")).strip().lower()
        if email == "nan":
            email = ""
        portable = str(ligne.get("Portable", "")).strip()
        if portable == "nan":
            portable = ""
        cle = email or f"nom:{personne.lower()}"
        entree = personnes.setdefault(cle, {"nom": personne, "email": email, "portable": portable, "magasins": []})
        if nom_magasin and nom_magasin != "nan" and nom_magasin not in entree["magasins"]:
            entree["magasins"].append(nom_magasin)
        if portable and not entree["portable"]:
            entree["portable"] = portable
        if not email:
            sans_email.add(personne)
        if not entree["portable"]:
            sans_portable.add(personne)
        else:
            sans_portable.discard(personne)

    for entree in personnes.values():
        entree["magasin"] = ", ".join(entree["magasins"])

    destinataires = sorted(
        (p for p in personnes.values() if p["email"]), key=lambda p: p["nom"].lower()
    )
    return destinataires, sorted(sans_email, key=str.lower), sorted(sans_portable, key=str.lower)


def _contenu_mail_prise_colis(annee):
    with get_db_connection() as conn:
        _ensure_colonne_mail_prise_colis(conn)
        ligne = conn.execute(
            "SELECT mail_texte_prise_colis FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    return ligne["mail_texte_prise_colis"] if ligne and ligne["mail_texte_prise_colis"] else MAIL_TEXTE_PRISE_COLIS_DEFAUT


def _corps_mail_prise_colis(personne, texte):
    return texte.replace("<<nom>>", personne["nom"]).replace("<<magasin>>", personne["magasin"])


def _corps_mail_prise_colis_html(personne, texte):
    html = str(escape(_corps_mail_prise_colis(personne, texte))).replace("\n", "<br>\n")
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">{html}</div>'


@collecte_bp.route("/collecte/mail_prise_colis", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def mail_prise_colis():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    try:
        destinataires, personnes_sans_email, personnes_sans_portable = _charger_primo_responsables_colis(annee)
        mail_texte = _contenu_mail_prise_colis(annee)
    except Exception as erreur:
        flash(f"❌ Impossible de charger les primo responsables de {annee} : {erreur}", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    if request.method == "POST":
        action = request.form.get("action")
        if not destinataires:
            flash("❌ Aucun primo responsable avec une adresse email.", "danger")
        elif action == "test":
            email_test = request.form.get("test_destinataire", "")
            exemple = next((p for p in destinataires if p["email"] == email_test), destinataires[0])
            adresse_test = getattr(current_user, "email", "") or ""
            if not adresse_test:
                flash("❌ Votre compte n'a pas d'adresse email pour le test.", "danger")
            else:
                envoyer_mail(
                    f"[TEST] Mail prise colis {annee}",
                    [adresse_test],
                    _corps_mail_prise_colis_html(exemple, mail_texte),
                    sender_override="ba380.directeur@banquealimentaire.org",
                    is_html=True,
                )
                flash(f"✅ Mail de test envoyé à {adresse_test}.", "success")
        elif action == "envoyer":
            if request.form.get("confirmation_envoi") != "oui":
                flash("❌ Cochez d'abord « Je confirme l'envoi » — rien n'a été envoyé.", "danger")
            else:
                echecs = []
                for personne in destinataires:
                    try:
                        resultat = envoyer_mail(
                            f"Collecte {annee} — prise du colis",
                            [personne["email"]],
                            _corps_mail_prise_colis_html(personne, mail_texte),
                            sender_override="ba380.directeur@banquealimentaire.org",
                            is_html=True,
                        )
                        _enregistrer_envoi_mail_collecte(annee, "prise_colis", personne["email"], personne.get("nom", ""), resultat=resultat)
                    except Exception as e:
                        echecs.append(personne.get("email", ""))
                        write_log(f"❌ Collecte {annee} : échec envoi mail prise de colis à {personne.get('email','')} — {e}")
                        _enregistrer_envoi_mail_collecte(annee, "prise_colis", personne.get("email", ""), personne.get("nom", ""), erreur=str(e))
                reussis = len(destinataires) - len(echecs)
                if echecs:
                    flash(f"⚠️ {reussis} mail(s) envoyé(s), {len(echecs)} échec(s) : " + ", ".join(echecs), "warning")
                else:
                    flash(f"✅ {reussis} mail(s) préparé(s).", "success")

    statuts = _statuts_envois_mail_collecte(annee, "prise_colis")
    for p in destinataires:
        p["statut_mailjet"] = statuts.get(p["email"])

    return render_template(
        "collecte/mail_prise_colis.html",
        annee=annee,
        destinataires=destinataires,
        personnes_sans_email=personnes_sans_email,
        personnes_sans_portable=personnes_sans_portable,
        mail_texte=mail_texte,
        feature="prise_colis",
    )


@collecte_bp.route("/collecte/<int:annee>/mail_prise_colis/apercu")
@login_required
@require_access("collecte", "lecture")
def mail_prise_colis_apercu(annee):
    """Rendu HTML exact du mail pour la personne sélectionnée, sans rien
    envoyer (même en test) — même principe que l'aperçu trieurs."""
    destinataires, _, _ = _charger_primo_responsables_colis(annee)
    if not destinataires:
        abort(404)
    mail_texte = _contenu_mail_prise_colis(annee)
    email = request.args.get("email", "")
    personne = next((p for p in destinataires if p["email"] == email), destinataires[0])
    html = _corps_mail_prise_colis_html(personne, mail_texte)
    return Response(html, mimetype="text/html")


@collecte_bp.route("/collecte/mail_prise_colis/contenu-mail", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_contenu_mail_prise_colis():
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("mail_texte", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_mail_prise_colis(conn)
        conn.execute("UPDATE collecte_campagnes SET mail_texte_prise_colis = ? WHERE annee = ?", (texte, annee))
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.mail_prise_colis", annee=annee))


MAIL_TEXTE_BILAN_DEFAUT = (
    "Bonjour <<nom>>,\n\n"
    "Vous avez participé à la collecte de cette année, un grand merci pour votre implication !\n\n"
    "Nous vous invitons à la réunion de bilan de la collecte <<annee>>, l'occasion de partager "
    "les résultats et vos retours d'expérience.\n\n"
    "Merci de nous indiquer votre disponibilité en répondant à ce mail."
)


def _ensure_colonne_mail_bilan(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "mail_texte_bilan" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_texte_bilan TEXT")


def _contenu_mail_bilan(annee):
    with get_db_connection() as conn:
        _ensure_colonne_mail_bilan(conn)
        ligne = conn.execute(
            "SELECT mail_texte_bilan FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    if ligne and ligne["mail_texte_bilan"]:
        return ligne["mail_texte_bilan"]
    return MAIL_TEXTE_BILAN_DEFAUT


def _corps_mail_bilan(personne, texte, annee):
    return texte.replace("<<nom>>", personne["nom"]).replace("<<annee>>", str(annee))


def _corps_mail_bilan_html(personne, texte, annee):
    html = str(escape(_corps_mail_bilan(personne, texte, annee))).replace("\n", "<br>\n")
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">{html}</div>'


@collecte_bp.route("/collecte/mail_bilan", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def mail_bilan():
    """Invitation au bilan de la collecte, envoyée aux participants go-on-web
    de l'année réellement actifs cette année (cf. _participants_actifs_gow —
    affectés à un magasin, chauffeur/équipier, entrepôt, ou association en
    collecte gardée), pas seulement les chauffeurs/équipiers de camion : un
    bilan de collecte concerne tous les participants actifs, magasins
    compris — mais pas une simple inscription sans affectation réelle
    (demande utilisateur 2026-11-10)."""
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    destinataires = _participants_actifs_gow(annee)
    mail_texte = _contenu_mail_bilan(annee)

    if request.method == "POST":
        action = request.form.get("action")
        if not destinataires:
            flash(f"❌ Aucun participant trouvé pour {annee}.", "danger")
        elif action == "test":
            email_test = request.form.get("test_destinataire", "")
            exemple = next((p for p in destinataires if p["email"] == email_test), destinataires[0])
            adresse_test = getattr(current_user, "email", "") or ""
            if not adresse_test:
                flash("❌ Votre compte n'a pas d'adresse email pour le test.", "danger")
            else:
                envoyer_mail(
                    f"[TEST] Collecte {annee} — invitation au bilan",
                    [adresse_test],
                    _corps_mail_bilan_html(exemple, mail_texte, annee),
                    sender_override="ba380.benevolecollecte@banquealimentaire.org",
                    is_html=True,
                )
                flash(f"✅ Mail de test envoyé à {adresse_test}.", "success")
        elif action == "envoyer":
            if request.form.get("confirmation_envoi") != "oui":
                flash("❌ Cochez d'abord « Je confirme l'envoi » — rien n'a été envoyé.", "danger")
            else:
                restants = _destinataires_restants_mail_collecte(annee, "bilan", destinataires)
                if not restants:
                    flash("✅ Tous les mails avaient déjà été envoyés avec succès.", "success")
                else:
                    lancer_tache_fond(
                        target=_envoyer_mails_masse_arriere_plan,
                        args=(
                            current_app._get_current_object(), annee, "bilan", restants,
                            f"Collecte {annee} — invitation au bilan",
                            lambda p: _corps_mail_bilan_html(p, mail_texte, annee),
                            "ba380.benevolecollecte@banquealimentaire.org",
                            current_user.email,
                        ),
                        nom=f"Collecte {annee} : envoi mail bilan",
                        utilisateur=current_user.email,
                    )
                    deja_envoyes = len(destinataires) - len(restants)
                    flash(f"⏳ Envoi de {len(restants)} mail(s) lancé en arrière-plan"
                          + (f" ({deja_envoyes} déjà envoyé(s) précédemment, non renvoyés)" if deja_envoyes else "")
                          + " — rechargez cette page dans quelques instants pour suivre la progression.", "info")

    statuts = _statuts_envois_mail_collecte(annee, "bilan")
    for p in destinataires:
        p["statut_mailjet"] = statuts.get(p["email"])
    nb_deja_envoyes = sum(1 for p in destinataires if statuts.get(p["email"]) and not statuts[p["email"]].get("erreur"))

    return render_template(
        "collecte/mail_bilan.html",
        annee=annee,
        destinataires=destinataires,
        mail_texte=mail_texte,
        feature="bilan",
        nb_restants=len(destinataires) - nb_deja_envoyes,
    )


@collecte_bp.route("/collecte/<int:annee>/mail_bilan/apercu")
@login_required
@require_access("collecte", "lecture")
def mail_bilan_apercu(annee):
    """Rendu HTML exact du mail pour la personne sélectionnée, sans rien
    envoyer (même en test) — même principe que l'aperçu trieurs/prise colis."""
    destinataires = _participants_actifs_gow(annee)
    if not destinataires:
        abort(404)
    mail_texte = _contenu_mail_bilan(annee)
    email = request.args.get("email", "")
    personne = next((p for p in destinataires if p["email"] == email), destinataires[0])
    html = _corps_mail_bilan_html(personne, mail_texte, annee)
    return Response(html, mimetype="text/html")


@collecte_bp.route("/collecte/mail_bilan/contenu-mail", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_contenu_mail_bilan():
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("mail_texte", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_mail_bilan(conn)
        conn.execute("UPDATE collecte_campagnes SET mail_texte_bilan = ? WHERE annee = ?", (texte, annee))
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.mail_bilan", annee=annee))


MAIL_TEXTE_RESSENTI_MAGASINS_DEFAUT = (
    "Bonjour <<nom>>,\n\n"
    "Vous avez été bénévole en magasin lors de la collecte <<annee>>, un grand merci pour votre "
    "engagement !\n\n"
    "Nous aimerions recueillir votre avis : comment s'est passée votre collecte cette année, "
    "qu'avez-vous apprécié, qu'est-ce qui pourrait être amélioré ?\n\n"
    "N'hésitez pas à répondre directement à ce mail pour partager votre ressenti.\n\n"
    "Merci."
)


def _ensure_colonne_mail_ressenti_magasins(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "mail_texte_ressenti_magasins" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_texte_ressenti_magasins TEXT")


def _emails_avec_magasin_gow(annee):
    """Emails (en minuscules) ayant au moins une ligne avec un VRAI magasin
    (comparé au référentiel magasins de l'année, colonne Nom) dans le
    fichier participants go-on-web — une colonne Magasin non vide ne
    suffit PAS : elle sert aussi à noter, pour les chauffeurs/équipiers et
    les bénévoles entrepôt, le nom du camion/véhicule ou un rôle (ex.
    « CAMION DLM 20m3 1 », « CHAUFFEURS », « BAI Entrepot - ... ») plutôt
    qu'un vrai magasin — sans ce contrôle, ces personnes étaient à tort
    comptées comme affectées à un magasin (bug trouvé 2026-11-10 : 990
    « magasin » annoncés contre 832 réels, tout l'écart venant de ces
    libellés de camion/entrepôt). Factorisé entre _charger_benevoles_magasins
    et _participants_actifs_gow."""
    chemin_participants = _fichier_drive(annee, "participants")
    chemin_magasins = _fichier_drive(annee, "magasins")
    if not chemin_participants or not chemin_magasins:
        return set()
    df_mag_ref = pd.read_excel(chemin_magasins)
    df_mag_ref.columns = [str(c).strip() for c in df_mag_ref.columns]
    noms_magasins_reels = set(df_mag_ref["Nom"].astype(str).str.strip())

    df_part = pd.read_excel(chemin_participants)
    df_part.columns = [str(c).strip() for c in df_part.columns]
    df_part = df_part[df_part.get("Année") == annee]
    df_part = df_part[df_part["Magasin"].astype(str).str.strip().isin(noms_magasins_reels)]
    return {str(e).strip().lower() for e in df_part["Email"]}


def _emails_mailing_participants(annee):
    """Emails (en minuscules) présents dans le fichier dédié « participants
    pour mailing » de go-on-web (mailing-participants.xlsx, une ligne par
    inscription magasin/créneau) — utilisé comme filtre SUPPLÉMENTAIRE (ET,
    jamais à la place de) sur les 4 mails post-collecte (bilan + 3 ressentis
    magasins/chauffeurs/entrepôt), demande utilisateur 2026-11-19 : "je veux
    uniquement les personnes ayant participé donc dans mailing participants".
    Contrairement à ce que son nom suggère, ce fichier contient aussi, à lui
    seul, de nombreuses inscriptions sans affectation réelle confirmée (cf.
    _charger_chauffeurs_equipiers_bruts — audit 2026-10-29, jamais utilisé
    seul pour cette raison) : il ne fait donc QUE resserrer la liste déjà
    filtrée par magasin/camion/entrepôt/référent gardée réel, jamais
    l'élargir. Retourne None (plutôt qu'un ensemble vide) si le fichier est
    introuvable, pour que les appelants désactivent ce filtre supplémentaire
    au lieu de vider toute la liste des destinataires par erreur."""
    chemin = _fichier_drive(annee, "participants_mailing")
    if not chemin:
        return None
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    return {
        str(e).strip().lower() for e in df.get("Email", [])
        if str(e).strip() and str(e).strip().lower() != "nan"
    }


def _email_retenu_mailing_participants(email, emails_mailing):
    """True si `email` doit être retenu compte tenu du filtre « mailing
    participants » (cf. _emails_mailing_participants) — toujours True si le
    filtre est indisponible (None, fichier introuvable), sinon seulement si
    l'email y figure."""
    return emails_mailing is None or email in emails_mailing


def _charger_benevoles_magasins(annee):
    """Bénévoles ayant participé EN MAGASIN à la collecte de l'année : parmi
    les participants go-on-web de l'année (cf. _lister_participants_gow),
    uniquement ceux réellement affectés à un magasin (colonne Magasin
    renseignée, cf. _emails_avec_magasin_gow — exclut les simples
    inscriptions sans affectation réelle, demande utilisateur 2026-11-10).
    N'exclut plus les chauffeurs/équipiers de camion ayant aussi un magasin
    (demande utilisateur 2026-11-10 : "ne pas exclure les bénévoles qui
    sont aussi dans une autre fonction") — une personne peut donc recevoir
    à la fois ce mail et le mail ressenti chauffeurs si elle cumule les
    deux rôles."""
    participants = _lister_participants_gow(annee)
    emails_avec_magasin = _emails_avec_magasin_gow(annee)
    emails_mailing = _emails_mailing_participants(annee)
    return [
        p for p in participants
        if p["email"] in emails_avec_magasin and _email_retenu_mailing_participants(p["email"], emails_mailing)
    ]


def _contenu_mail_ressenti_magasins(annee):
    with get_db_connection() as conn:
        _ensure_colonne_mail_ressenti_magasins(conn)
        ligne = conn.execute(
            "SELECT mail_texte_ressenti_magasins FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    if ligne and ligne["mail_texte_ressenti_magasins"]:
        return ligne["mail_texte_ressenti_magasins"]
    return MAIL_TEXTE_RESSENTI_MAGASINS_DEFAUT


def _corps_mail_ressenti_magasins(personne, texte, annee):
    return texte.replace("<<nom>>", personne["nom"]).replace("<<annee>>", str(annee))


def _corps_mail_ressenti_magasins_html(personne, texte, annee):
    html = str(escape(_corps_mail_ressenti_magasins(personne, texte, annee))).replace("\n", "<br>\n")
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">{html}</div>'


@collecte_bp.route("/collecte/mail_ressenti_magasins", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def mail_ressenti_magasins():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    destinataires = _charger_benevoles_magasins(annee)
    mail_texte = _contenu_mail_ressenti_magasins(annee)

    if request.method == "POST":
        action = request.form.get("action")
        if not destinataires:
            flash(f"❌ Aucun bénévole magasin trouvé pour {annee}.", "danger")
        elif action == "test":
            email_test = request.form.get("test_destinataire", "")
            exemple = next((p for p in destinataires if p["email"] == email_test), destinataires[0])
            adresse_test = getattr(current_user, "email", "") or ""
            if not adresse_test:
                flash("❌ Votre compte n'a pas d'adresse email pour le test.", "danger")
            else:
                envoyer_mail(
                    f"[TEST] Collecte {annee} — votre ressenti",
                    [adresse_test],
                    _corps_mail_ressenti_magasins_html(exemple, mail_texte, annee),
                    sender_override="ba380.benevolecollecte@banquealimentaire.org",
                    is_html=True,
                )
                flash(f"✅ Mail de test envoyé à {adresse_test}.", "success")
        elif action == "envoyer":
            if request.form.get("confirmation_envoi") != "oui":
                flash("❌ Cochez d'abord « Je confirme l'envoi » — rien n'a été envoyé.", "danger")
            else:
                restants = _destinataires_restants_mail_collecte(annee, "ressenti_magasins", destinataires)
                if not restants:
                    flash("✅ Tous les mails avaient déjà été envoyés avec succès.", "success")
                else:
                    lancer_tache_fond(
                        target=_envoyer_mails_masse_arriere_plan,
                        args=(
                            current_app._get_current_object(), annee, "ressenti_magasins", restants,
                            f"Collecte {annee} — votre ressenti",
                            lambda p: _corps_mail_ressenti_magasins_html(p, mail_texte, annee),
                            "ba380.benevolecollecte@banquealimentaire.org",
                            current_user.email,
                        ),
                        nom=f"Collecte {annee} : envoi mail ressenti magasins",
                        utilisateur=current_user.email,
                    )
                    deja_envoyes = len(destinataires) - len(restants)
                    flash(f"⏳ Envoi de {len(restants)} mail(s) lancé en arrière-plan"
                          + (f" ({deja_envoyes} déjà envoyé(s) précédemment, non renvoyés)" if deja_envoyes else "")
                          + " — rechargez cette page dans quelques instants pour suivre la progression.", "info")

    statuts = _statuts_envois_mail_collecte(annee, "ressenti_magasins")
    for p in destinataires:
        p["statut_mailjet"] = statuts.get(p["email"])
    nb_deja_envoyes = sum(1 for p in destinataires if statuts.get(p["email"]) and not statuts[p["email"]].get("erreur"))

    return render_template(
        "collecte/mail_ressenti_magasins.html",
        annee=annee,
        destinataires=destinataires,
        mail_texte=mail_texte,
        nb_restants=len(destinataires) - nb_deja_envoyes,
        feature="ressenti_magasins",
    )


@collecte_bp.route("/collecte/<int:annee>/mail_ressenti_magasins/apercu")
@login_required
@require_access("collecte", "lecture")
def mail_ressenti_magasins_apercu(annee):
    """Rendu HTML exact du mail pour la personne sélectionnée, sans rien
    envoyer (même en test) — même principe que l'aperçu bilan collecte."""
    destinataires = _charger_benevoles_magasins(annee)
    if not destinataires:
        abort(404)
    mail_texte = _contenu_mail_ressenti_magasins(annee)
    email = request.args.get("email", "")
    personne = next((p for p in destinataires if p["email"] == email), destinataires[0])
    html = _corps_mail_ressenti_magasins_html(personne, mail_texte, annee)
    return Response(html, mimetype="text/html")


@collecte_bp.route("/collecte/mail_ressenti_magasins/contenu-mail", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_contenu_mail_ressenti_magasins():
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("mail_texte", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_mail_ressenti_magasins(conn)
        conn.execute("UPDATE collecte_campagnes SET mail_texte_ressenti_magasins = ? WHERE annee = ?", (texte, annee))
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.mail_ressenti_magasins", annee=annee))


MAIL_TEXTE_RESSENTI_CHAUFFEURS_DEFAUT = (
    "Bonjour <<nom>>,\n\n"
    "Vous avez été chauffeur ou équipier de camion lors de la collecte <<annee>>, un grand merci "
    "pour votre engagement !\n\n"
    "Nous aimerions recueillir votre avis : comment s'est passée votre collecte cette année, "
    "qu'avez-vous apprécié, qu'est-ce qui pourrait être amélioré ?\n\n"
    "N'hésitez pas à répondre directement à ce mail pour partager votre ressenti.\n\n"
    "Merci."
)


def _ensure_colonne_mail_ressenti_chauffeurs(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "mail_texte_ressenti_chauffeurs" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_texte_ressenti_chauffeurs TEXT")


def _charger_benevoles_chauffeurs_equipiers(annee):
    """Chauffeurs/équipiers de camion réellement nommés dans liste-vehicule.xlsx
    cette année (cf. _charger_chauffeurs_equipiers_bruts), avec email — ceux
    sans email ne peuvent pas recevoir ce mail. Filtre supplémentaire
    "mailing participants" (demande 2026-11-19) : cf. _emails_mailing_participants."""
    personnes = _charger_chauffeurs_equipiers_bruts(annee)
    emails_mailing = _emails_mailing_participants(annee)
    return sorted(
        (
            {"nom": p["nom"], "email": p["email"]} for p in personnes.values()
            if p["email"] and _email_retenu_mailing_participants(p["email"], emails_mailing)
        ),
        key=lambda p: p["nom"].lower(),
    )


def _contenu_mail_ressenti_chauffeurs(annee):
    with get_db_connection() as conn:
        _ensure_colonne_mail_ressenti_chauffeurs(conn)
        ligne = conn.execute(
            "SELECT mail_texte_ressenti_chauffeurs FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    if ligne and ligne["mail_texte_ressenti_chauffeurs"]:
        return ligne["mail_texte_ressenti_chauffeurs"]
    return MAIL_TEXTE_RESSENTI_CHAUFFEURS_DEFAUT


def _corps_mail_ressenti_chauffeurs(personne, texte, annee):
    return texte.replace("<<nom>>", personne["nom"]).replace("<<annee>>", str(annee))


def _corps_mail_ressenti_chauffeurs_html(personne, texte, annee):
    html = str(escape(_corps_mail_ressenti_chauffeurs(personne, texte, annee))).replace("\n", "<br>\n")
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">{html}</div>'


EXPEDITEURS_MAIL_RESSENTI = {
    "ba380.directeur@banquealimentaire.org": "Directeur",
    "ba380.collecte@banquealimentaire.org": "Collecte",
}


def _expediteur_ressenti(form):
    """Expéditeur choisi par l'utilisateur pour les mails de demande de
    bilan/ressenti (chauffeurs/équipiers, entrepôt) — ba380.directeur par
    défaut (comportement historique), repli strict dessus si le POST
    contient autre chose que les 2 adresses validées Mailjet (demande
    utilisateur 2026-11-17)."""
    valeur = form.get("expediteur", "")
    return valeur if valeur in EXPEDITEURS_MAIL_RESSENTI else "ba380.directeur@banquealimentaire.org"


@collecte_bp.route("/collecte/mail_ressenti_chauffeurs", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def mail_ressenti_chauffeurs():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    destinataires = _charger_benevoles_chauffeurs_equipiers(annee)
    mail_texte = _contenu_mail_ressenti_chauffeurs(annee)

    if request.method == "POST":
        action = request.form.get("action")
        expediteur = _expediteur_ressenti(request.form)
        if not destinataires:
            flash(f"❌ Aucun chauffeur/équipier trouvé pour {annee}.", "danger")
        elif action == "test":
            email_test = request.form.get("test_destinataire", "")
            exemple = next((p for p in destinataires if p["email"] == email_test), destinataires[0])
            adresse_test = getattr(current_user, "email", "") or ""
            if not adresse_test:
                flash("❌ Votre compte n'a pas d'adresse email pour le test.", "danger")
            else:
                envoyer_mail(
                    f"[TEST] Collecte {annee} — votre ressenti dans les camions",
                    [adresse_test],
                    _corps_mail_ressenti_chauffeurs_html(exemple, mail_texte, annee),
                    sender_override=expediteur,
                    is_html=True,
                )
                flash(f"✅ Mail de test envoyé à {adresse_test}.", "success")
        elif action == "envoyer":
            if request.form.get("confirmation_envoi") != "oui":
                flash("❌ Cochez d'abord « Je confirme l'envoi » — rien n'a été envoyé.", "danger")
            else:
                restants = _destinataires_restants_mail_collecte(annee, "ressenti_chauffeurs", destinataires)
                if not restants:
                    flash("✅ Tous les mails avaient déjà été envoyés avec succès.", "success")
                else:
                    lancer_tache_fond(
                        target=_envoyer_mails_masse_arriere_plan,
                        args=(
                            current_app._get_current_object(), annee, "ressenti_chauffeurs", restants,
                            f"Collecte {annee} — votre ressenti dans les camions",
                            lambda p: _corps_mail_ressenti_chauffeurs_html(p, mail_texte, annee),
                            expediteur,
                            current_user.email,
                        ),
                        nom=f"Collecte {annee} : envoi mail ressenti chauffeurs",
                        utilisateur=current_user.email,
                    )
                    deja_envoyes = len(destinataires) - len(restants)
                    flash(f"⏳ Envoi de {len(restants)} mail(s) lancé en arrière-plan"
                          + (f" ({deja_envoyes} déjà envoyé(s) précédemment, non renvoyés)" if deja_envoyes else "")
                          + " — rechargez cette page dans quelques instants pour suivre la progression.", "info")

    statuts = _statuts_envois_mail_collecte(annee, "ressenti_chauffeurs")
    for p in destinataires:
        p["statut_mailjet"] = statuts.get(p["email"])
    nb_deja_envoyes = sum(1 for p in destinataires if statuts.get(p["email"]) and not statuts[p["email"]].get("erreur"))

    return render_template(
        "collecte/mail_ressenti_chauffeurs.html",
        annee=annee,
        destinataires=destinataires,
        mail_texte=mail_texte,
        nb_restants=len(destinataires) - nb_deja_envoyes,
        expediteurs=EXPEDITEURS_MAIL_RESSENTI,
        feature="ressenti_chauffeurs",
    )


@collecte_bp.route("/collecte/<int:annee>/mail_ressenti_chauffeurs/apercu")
@login_required
@require_access("collecte", "lecture")
def mail_ressenti_chauffeurs_apercu(annee):
    """Rendu HTML exact du mail pour la personne sélectionnée, sans rien
    envoyer (même en test) — même principe que l'aperçu bilan collecte."""
    destinataires = _charger_benevoles_chauffeurs_equipiers(annee)
    if not destinataires:
        abort(404)
    mail_texte = _contenu_mail_ressenti_chauffeurs(annee)
    email = request.args.get("email", "")
    personne = next((p for p in destinataires if p["email"] == email), destinataires[0])
    html = _corps_mail_ressenti_chauffeurs_html(personne, mail_texte, annee)
    return Response(html, mimetype="text/html")


@collecte_bp.route("/collecte/mail_ressenti_chauffeurs/contenu-mail", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_contenu_mail_ressenti_chauffeurs():
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("mail_texte", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_mail_ressenti_chauffeurs(conn)
        conn.execute("UPDATE collecte_campagnes SET mail_texte_ressenti_chauffeurs = ? WHERE annee = ?", (texte, annee))
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.mail_ressenti_chauffeurs", annee=annee))


CODES_ENTREPOT = ("V090", "V091", "V092")

# Rôles entrepôt enregistrés dans liste_participants.xlsx (colonne Magasin)
# plutôt que dans liste_vehicules.xlsx sous un code V090/V091/V092 — démarche
# différente de celle des chauffeurs/équipiers de camion, mais même réalité
# de terrain (bénévole actif à l'entrepôt). Labels exacts (hors véhicules
# BAI "BAI RENAULT/PEUGEOT/IVECO (...)" plaque, qui restent des camions) :
# comparaison exacte pour 3 d'entre eux, préfixe pour "BAI Entrepot - "
# (adresse en suffixe, susceptible de changer d'une année à l'autre).
LABELS_ENTREPOT_PARTICIPANTS = {"BAI Cuisine", "BAI Divers", "BAI Tournée Metro", "Protection Civile"}


def _est_label_entrepot_participant(label):
    label = str(label or "").strip()
    if label in LABELS_ENTREPOT_PARTICIPANTS:
        return True
    return label.lower().startswith("bai entrepot")

MAIL_TEXTE_RESSENTI_ENTREPOT_DEFAUT = (
    "Bonjour <<nom>>,\n\n"
    "Vous avez été bénévole à l'entrepôt (tri, cuisine...) lors de la collecte <<annee>>, un grand "
    "merci pour votre engagement !\n\n"
    "Nous aimerions recueillir votre avis : comment s'est passée votre collecte cette année, "
    "qu'avez-vous apprécié, qu'est-ce qui pourrait être amélioré ?\n\n"
    "N'hésitez pas à répondre directement à ce mail pour partager votre ressenti.\n\n"
    "Merci."
)


def _ensure_colonne_mail_ressenti_entrepot(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "mail_texte_ressenti_entrepot" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN mail_texte_ressenti_entrepot TEXT")


def _charger_benevoles_entrepot(annee):
    """Personnes affectées à l'entrepôt — soit via un code non réel
    V090/V091/V092 (tri/cuisine/divers, cf. _est_camion_reel) dans le
    planning véhicules de l'année, soit via un rôle entrepôt enregistré
    directement dans liste_participants.xlsx (colonne Magasin, cf.
    LABELS_ENTREPOT_PARTICIPANTS — demande utilisateur 2026-11-19 : ces
    bénévoles-là, ex. "BAI Cuisine", n'apparaissaient dans aucune des
    catégories de _participants_actifs_gow, donc exclus à tort du mail
    bilan/ressenti entrepôt et du comptage §6.11, faute d'être aussi
    déclarés côté liste_vehicules.xlsx) — nom + email, dédoublonnés par
    email entre les deux sources. Même principe que
    _charger_trieurs_annee_precedente, mais sur l'année en cours et les 3
    codes entrepôt (pas seulement V090)."""
    chemin = _fichier_drive(annee, "vehicules")
    if not chemin:
        chemin = os.path.join(_dossier_annee(annee), "liste_vehicules.xlsx")
    if not os.path.exists(chemin):
        raise FileNotFoundError(f"Le fichier véhicules {annee} est introuvable")

    df = pd.read_excel(chemin)
    df.columns = [str(col).strip() for col in df.columns]
    personnes = {}
    for _, ligne in df.iterrows():
        code = str(ligne.get("Code", "")).strip().upper()
        if code not in CODES_ENTREPOT:
            continue
        personne = str(ligne.get("Équipier", ligne.get("equipier", ""))).strip()
        if not personne or personne == "nan":
            continue
        email = str(ligne.get("Email", "")).strip().lower()
        if email == "nan":
            email = ""
        cle = email or f"nom:{personne.lower()}"
        personnes.setdefault(cle, {"nom": personne, "email": email})

    df_part = _lire_participants(annee)
    if not df_part.empty:
        df_part = df_part[df_part.get("Année") == annee]
        for _, ligne in df_part.iterrows():
            if not _est_label_entrepot_participant(ligne.get("Magasin", "")):
                continue
            personne = str(ligne.get("Nom", "")).strip()
            if not personne or personne == "nan":
                continue
            email = str(ligne.get("Email", "")).strip().lower()
            if email == "nan" or not email:
                continue
            personnes.setdefault(email, {"nom": personne, "email": email})

    return sorted(
        (p for p in personnes.values() if p["email"]), key=lambda p: p["nom"].lower()
    )


def _charger_benevoles_entrepot_mailing(annee):
    """_charger_benevoles_entrepot restreint au filtre "mailing participants"
    (demande 2026-11-19, cf. _emails_mailing_participants) — utilisé
    uniquement pour le mail ressenti entrepôt (envoi + aperçu), PAS pour le
    comptage §6.11 (_comptage_participants) ni pour le calcul interne de
    _participants_actifs_gow (qui applique son propre filtre une seule fois,
    sur la liste finale combinée)."""
    emails_mailing = _emails_mailing_participants(annee)
    return [
        p for p in _charger_benevoles_entrepot(annee)
        if _email_retenu_mailing_participants(p["email"], emails_mailing)
    ]


def _contenu_mail_ressenti_entrepot(annee):
    with get_db_connection() as conn:
        _ensure_colonne_mail_ressenti_entrepot(conn)
        ligne = conn.execute(
            "SELECT mail_texte_ressenti_entrepot FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    if ligne and ligne["mail_texte_ressenti_entrepot"]:
        return ligne["mail_texte_ressenti_entrepot"]
    return MAIL_TEXTE_RESSENTI_ENTREPOT_DEFAUT


def _corps_mail_ressenti_entrepot(personne, texte, annee):
    return texte.replace("<<nom>>", personne["nom"]).replace("<<annee>>", str(annee))


def _corps_mail_ressenti_entrepot_html(personne, texte, annee):
    html = str(escape(_corps_mail_ressenti_entrepot(personne, texte, annee))).replace("\n", "<br>\n")
    return f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;">{html}</div>'


@collecte_bp.route("/collecte/mail_ressenti_entrepot", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def mail_ressenti_entrepot():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    try:
        destinataires = _charger_benevoles_entrepot_mailing(annee)
        mail_texte = _contenu_mail_ressenti_entrepot(annee)
    except Exception as erreur:
        flash(f"❌ Impossible de charger les bénévoles entrepôt de {annee} : {erreur}", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    if request.method == "POST":
        action = request.form.get("action")
        expediteur = _expediteur_ressenti(request.form)
        if not destinataires:
            flash(f"❌ Aucun bénévole entrepôt trouvé pour {annee}.", "danger")
        elif action == "test":
            email_test = request.form.get("test_destinataire", "")
            exemple = next((p for p in destinataires if p["email"] == email_test), destinataires[0])
            adresse_test = getattr(current_user, "email", "") or ""
            if not adresse_test:
                flash("❌ Votre compte n'a pas d'adresse email pour le test.", "danger")
            else:
                envoyer_mail(
                    f"[TEST] Collecte {annee} — ressenti à l'entrepôt",
                    [adresse_test],
                    _corps_mail_ressenti_entrepot_html(exemple, mail_texte, annee),
                    sender_override=expediteur,
                    is_html=True,
                )
                flash(f"✅ Mail de test envoyé à {adresse_test}.", "success")
        elif action == "envoyer":
            if request.form.get("confirmation_envoi") != "oui":
                flash("❌ Cochez d'abord « Je confirme l'envoi » — rien n'a été envoyé.", "danger")
            else:
                restants = _destinataires_restants_mail_collecte(annee, "ressenti_entrepot", destinataires)
                if not restants:
                    flash("✅ Tous les mails avaient déjà été envoyés avec succès.", "success")
                else:
                    lancer_tache_fond(
                        target=_envoyer_mails_masse_arriere_plan,
                        args=(
                            current_app._get_current_object(), annee, "ressenti_entrepot", restants,
                            f"Collecte {annee} — ressenti à l'entrepôt",
                            lambda p: _corps_mail_ressenti_entrepot_html(p, mail_texte, annee),
                            expediteur,
                            current_user.email,
                        ),
                        nom=f"Collecte {annee} : envoi mail ressenti entrepôt",
                        utilisateur=current_user.email,
                    )
                    deja_envoyes = len(destinataires) - len(restants)
                    flash(f"⏳ Envoi de {len(restants)} mail(s) lancé en arrière-plan"
                          + (f" ({deja_envoyes} déjà envoyé(s) précédemment, non renvoyés)" if deja_envoyes else "")
                          + " — rechargez cette page dans quelques instants pour suivre la progression.", "info")

    statuts = _statuts_envois_mail_collecte(annee, "ressenti_entrepot")
    for p in destinataires:
        p["statut_mailjet"] = statuts.get(p["email"])
    nb_deja_envoyes = sum(1 for p in destinataires if statuts.get(p["email"]) and not statuts[p["email"]].get("erreur"))

    return render_template(
        "collecte/mail_ressenti_entrepot.html",
        annee=annee,
        destinataires=destinataires,
        feature="ressenti_entrepot",
        nb_restants=len(destinataires) - nb_deja_envoyes,
        mail_texte=mail_texte,
        expediteurs=EXPEDITEURS_MAIL_RESSENTI,
    )


@collecte_bp.route("/collecte/<int:annee>/mail_ressenti_entrepot/apercu")
@login_required
@require_access("collecte", "lecture")
def mail_ressenti_entrepot_apercu(annee):
    """Rendu HTML exact du mail pour la personne sélectionnée, sans rien
    envoyer (même en test) — même principe que l'aperçu bilan collecte."""
    destinataires = _charger_benevoles_entrepot_mailing(annee)
    if not destinataires:
        abort(404)
    mail_texte = _contenu_mail_ressenti_entrepot(annee)
    email = request.args.get("email", "")
    personne = next((p for p in destinataires if p["email"] == email), destinataires[0])
    html = _corps_mail_ressenti_entrepot_html(personne, mail_texte, annee)
    return Response(html, mimetype="text/html")


@collecte_bp.route("/collecte/mail_ressenti_entrepot/contenu-mail", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_contenu_mail_ressenti_entrepot():
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("mail_texte", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_mail_ressenti_entrepot(conn)
        conn.execute("UPDATE collecte_campagnes SET mail_texte_ressenti_entrepot = ? WHERE annee = ?", (texte, annee))
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.mail_ressenti_entrepot", annee=annee))


def _charger_chauffeurs_equipiers_bruts(annee):
    """{cle: {"nom", "email", "camions": {code_camion: nom_camion}}} des
    personnes réellement nommées comme chauffeur ou équipier d'un camion dans
    liste-vehicule.xlsx (colonne Équipier, avec son Email). cle = email en
    minuscules, ou 'sans_email:<nom>' si pas d'email.

    Le fichier « pour mailing » (une ligne par inscription magasin/créneau,
    colonne R/E) n'est volontairement PAS utilisé comme source de rattachement
    camion : il contient de nombreuses personnes seulement inscrites en
    magasin, sans lien réel avec un camion, qui s'avèrent impossibles à
    distinguer de façon fiable des vrais chauffeurs/équipiers par simple
    rapprochement magasin/jour/créneau (essayé puis abandonné — ex. Zhang
    Su-Ping, A Ilham : inscrites R/E sur un magasin desservi par un camion,
    mais absentes de la liste nominative réelle de ce camion dans
    liste-vehicule.xlsx ; audit 2026-10-29, confirmé par l'utilisateur : "il y
    a encore plein de personnes qui sont uniquement en magasin... il ne faut
    pas selectionner ces personnes" / "on ne s'interesse qu'aux personnes
    dans les camions et aux personnes marquées bai"). liste-vehicule.xlsx est
    la seule source car elle nomme directement, pour chaque camion et chaque
    passage, qui est réellement dessus.

    Codes de camion à part dans ce fichier : V090/V091/V092 = staffing BAI
    (entrepôt/cuisine/divers), pas un camion — exclus ici ; V100 = pool
    chauffeurs/équipiers de réserve, pas rattaché à un camion précis mais à
    inclure directement (sans camion). Utilisé par _charger_appel_chauffeurs
    et _comparer_chauffeurs_basilic. Le fichier Drive est retéléchargé à
    chaque appel (cf. _fichier_drive), jamais de version périmée."""
    personnes = {}

    def ajouter(nom, email, camion=None):
        nom = str(nom).strip()
        email = str(email).strip()
        if not nom or nom.lower() == "nan":
            return
        if email.lower() == "nan":
            email = ""
        cle = email.lower() if email else f"sans_email:{nom.lower()}"
        entree = personnes.setdefault(cle, {"nom": nom, "email": email, "camions": {}})
        if camion:
            entree["camions"][camion[0]] = camion[1]

    chemin_vehicules = _fichier_drive(annee, "vehicules")
    if chemin_vehicules:
        df_veh = pd.read_excel(chemin_vehicules)
        df_veh.columns = [str(c).strip() for c in df_veh.columns]
        for _, r in df_veh.iterrows():
            nom_personne = str(r.get("Équipier", "")).strip()
            if not nom_personne or nom_personne.lower() == "nan":
                continue
            code_camion = str(r.get("Code", "")).strip().upper()
            if code_camion in ("V090", "V091", "V092"):
                continue
            if code_camion == "V100":
                ajouter(nom_personne, r.get("Email", ""))
                continue
            if not _est_camion_reel(code_camion):
                continue
            nom_camion = str(r.get("Véhicule", "")).strip()
            ajouter(nom_personne, r.get("Email", ""), camion=(code_camion, nom_camion))
    return personnes


def _charger_appel_chauffeurs(annee):
    """Liste d'appel unique (Nom, Email, BAI, Camion, Nom camion) pour la
    collecte : tous les chauffeurs/équipiers de camion de l'année (cf.
    _charger_chauffeurs_equipiers_bruts) complétée par tous les bénévoles de
    la base Basilic (table `benevoles`, tous types confondus — pas de
    distinction possible entre terrain/entrepôt/cuisine côté Basilic, donc
    pas de filtre ici ; décision utilisateur 2026-10-29). Remplace
    l'ancienne source « colonne BAI du fichier participants go-on-web »,
    jugée moins fiable que la base Basilic elle-même.

    Dédoublonnée par email (une même adresse peut apparaître à la fois comme
    équipier de plusieurs camions/passages ET comme bénévole Basilic — une
    seule ligne par boîte mail réelle, les infos fusionnées) ; les
    chauffeurs/équipiers sans email restent inclus (une ligne par nom dans ce
    cas), les bénévoles Basilic sans email sont ignorés (rapprochement par
    email uniquement). Triée par Nom."""
    personnes = _charger_chauffeurs_equipiers_bruts(annee)
    for p in personnes.values():
        p["bai"] = ""

    for email, nom in _charger_benevoles_basilic_par_email().items():
        entree = personnes.setdefault(email, {"nom": nom, "email": email, "bai": "", "camions": {}})
        entree["bai"] = "BAI"

    resultat = []
    for p in personnes.values():
        codes_camions = sorted(p["camions"].keys())
        resultat.append({
            "nom": p["nom"],
            "email": p["email"],
            "bai": p["bai"],
            "camion": ", ".join(codes_camions),
            "nom_camion": ", ".join(p["camions"][c] for c in codes_camions if p["camions"][c]),
        })
    return sorted(resultat, key=lambda p: p["nom"].lower())


def _ecrire_feuille_appel(ws, personnes):
    """Remplit une feuille Excel au format standard (Nom, Email, BAI, Camion,
    Nom camion) de la liste d'appel chauffeurs/équipiers, réutilisé par les
    comparaisons qui en dérivent pour garder le même format d'export."""
    ws.append(["Nom", "Email", "BAI", "Camion", "Nom camion"])
    for p in personnes:
        ws.append([p["nom"], p["email"], p.get("bai", ""), p.get("camion", ""), p.get("nom_camion", "")])
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 14
    ws.column_dimensions["E"].width = 30
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _enrichir_avec_appel_chauffeurs(personnes_simples, annee):
    """Complète chaque dict {nom, email} d'une liste de comparaison avec les
    colonnes BAI, Camion, Nom camion connues de _charger_appel_chauffeurs
    pour cette adresse email (vide si email inconnue de cette liste) — pour
    exporter au même format que la liste d'appel chauffeurs/équipiers."""
    par_email = {p["email"].lower(): p for p in _charger_appel_chauffeurs(annee) if p["email"]}
    resultat = []
    for p in personnes_simples:
        source = par_email.get(p["email"].lower()) if p["email"] else None
        resultat.append({
            "nom": p["nom"],
            "email": p["email"],
            "bai": source["bai"] if source else "",
            "camion": source["camion"] if source else "",
            "nom_camion": source["nom_camion"] if source else "",
        })
    return resultat


@collecte_bp.route("/collecte/<int:annee>/appel-chauffeurs/export")
@login_required
@require_access("collecte", "lecture")
def appel_chauffeurs_export(annee):
    """Export Excel (Nom, Email, BAI, Camion, Nom camion) de la liste d'appel
    chauffeurs/équipiers + personnes BAI — voir _charger_appel_chauffeurs."""
    personnes = _charger_appel_chauffeurs(annee)
    if not personnes:
        flash("❌ Fichier source manquant (liste-vehicule)", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    wb_export = Workbook()
    ws = wb_export.active
    ws.title = "appel"
    _ecrire_feuille_appel(ws, personnes)

    tampon = io.BytesIO()
    wb_export.save(tampon)
    tampon.seek(0)
    return send_file(
        tampon,
        as_attachment=True,
        download_name=f"appel_chauffeurs_{annee}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _charger_benevoles_basilic_par_email():
    """{email en minuscules: "nom prenom"} pour tous les bénévoles de la base
    Basilic (table `benevoles`, tous types confondus)."""
    with get_db_connection() as conn:
        lignes_benevoles = conn.execute("SELECT nom, prenom, email FROM benevoles").fetchall()
    benevoles_par_email = {}
    for r in lignes_benevoles:
        email = str(r["email"] or "").strip().lower()
        if not email or email == "nan":
            continue
        nom = f"{r['nom'] or ''} {r['prenom'] or ''}".strip()
        benevoles_par_email.setdefault(email, nom)
    return benevoles_par_email


def _comparer_chauffeurs_basilic(annee):
    """Compare dans les 2 sens les chauffeurs/équipiers de camion de l'année
    (liste-vehicule.xlsx, cf. _charger_chauffeurs_equipiers_bruts) et la base
    bénévoles de Basilic (table `benevoles`, tous types confondus).
    Rapprochement par email — les chauffeurs/équipiers sans email ne peuvent
    pas être comparés et sont ignorés ici (unicité sur l'adresse mail,
    demande utilisateur). Retourne (dans_chauffeurs_pas_basilic,
    dans_basilic_pas_chauffeurs), chacune une liste de dicts {nom, email}
    triée par nom."""
    chauffeurs = _charger_chauffeurs_equipiers_bruts(annee)
    chauffeurs_par_email = {
        p["email"].lower(): p["nom"] for p in chauffeurs.values() if p["email"]
    }
    benevoles_par_email = _charger_benevoles_basilic_par_email()

    emails_chauffeurs = set(chauffeurs_par_email)
    emails_benevoles = set(benevoles_par_email)

    dans_chauffeurs_pas_basilic = sorted(
        ({"nom": chauffeurs_par_email[e], "email": e} for e in emails_chauffeurs - emails_benevoles),
        key=lambda p: p["nom"].lower(),
    )
    dans_basilic_pas_chauffeurs = sorted(
        ({"nom": benevoles_par_email[e], "email": e} for e in emails_benevoles - emails_chauffeurs),
        key=lambda p: p["nom"].lower(),
    )
    return dans_chauffeurs_pas_basilic, dans_basilic_pas_chauffeurs


@collecte_bp.route("/collecte/<int:annee>/comparaison-chauffeurs-basilic")
@login_required
@require_access("collecte", "lecture")
def comparaison_chauffeurs_basilic(annee):
    """Compare les chauffeurs/équipiers de camion de l'année et la base
    bénévoles de Basilic, dans les 2 sens — voir _comparer_chauffeurs_basilic."""
    dans_chauffeurs_pas_basilic, dans_basilic_pas_chauffeurs = _comparer_chauffeurs_basilic(annee)
    return render_template(
        "collecte/comparaison_chauffeurs_basilic.html",
        annee=annee,
        dans_chauffeurs_pas_basilic=dans_chauffeurs_pas_basilic,
        dans_basilic_pas_chauffeurs=dans_basilic_pas_chauffeurs,
    )


@collecte_bp.route("/collecte/<int:annee>/comparaison-chauffeurs-basilic/export")
@login_required
@require_access("collecte", "lecture")
def comparaison_chauffeurs_basilic_export(annee):
    """Export Excel (2 onglets, même format que la liste d'appel
    chauffeurs/équipiers) de la comparaison chauffeurs/équipiers / Basilic —
    voir _comparer_chauffeurs_basilic."""
    dans_chauffeurs_pas_basilic, dans_basilic_pas_chauffeurs = _comparer_chauffeurs_basilic(annee)

    wb_export = Workbook()
    ws1 = wb_export.active
    ws1.title = "Chauffeurs sans Basilic"
    _ecrire_feuille_appel(ws1, _enrichir_avec_appel_chauffeurs(dans_chauffeurs_pas_basilic, annee))

    ws2 = wb_export.create_sheet("Basilic sans chauffeurs")
    _ecrire_feuille_appel(ws2, _enrichir_avec_appel_chauffeurs(dans_basilic_pas_chauffeurs, annee))

    tampon = io.BytesIO()
    wb_export.save(tampon)
    tampon.seek(0)
    return send_file(
        tampon,
        as_attachment=True,
        download_name=f"comparaison_chauffeurs_basilic_{annee}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _comparer_bai_basilic(annee):
    """Compare dans les 2 sens la base bénévoles de Basilic (table
    `benevoles`, tous types confondus) et les personnes BAI de l'année dans
    la liste des participants go-on-web (colonne BAI = 'BAI' ou 'A-BAI').
    Rapprochement par email (même principe que _charger_appel_chauffeurs).
    Retourne (dans_basilic_pas_gow, dans_gow_pas_basilic), chacune une liste
    de dicts {nom, email} triée par nom."""
    benevoles_par_email = _charger_benevoles_basilic_par_email()

    bai_gow_par_email = {}
    chemin_participants = _fichier_drive(annee, "participants")
    if chemin_participants:
        df_part = pd.read_excel(chemin_participants)
        df_part.columns = [str(c).strip() for c in df_part.columns]
        personnes_bai = df_part[
            (df_part.get("Année") == annee) & (df_part.get("BAI", "").isin(["BAI", "A-BAI"]))
        ]
        for _, r in personnes_bai.iterrows():
            nom = str(r.get("Nom", "")).strip()
            email = str(r.get("Email", "")).strip().lower()
            if not nom or nom.lower() == "nan" or not email or email == "nan":
                continue
            bai_gow_par_email.setdefault(email, nom)

    emails_benevoles = set(benevoles_par_email)
    emails_gow = set(bai_gow_par_email)

    dans_basilic_pas_gow = sorted(
        ({"nom": benevoles_par_email[e], "email": e} for e in emails_benevoles - emails_gow),
        key=lambda p: p["nom"].lower(),
    )
    dans_gow_pas_basilic = sorted(
        ({"nom": bai_gow_par_email[e], "email": e} for e in emails_gow - emails_benevoles),
        key=lambda p: p["nom"].lower(),
    )
    return dans_basilic_pas_gow, dans_gow_pas_basilic


def _lister_participants_gow(annee):
    """Liste de tous les participants go-on-web de l'année (fichier
    participants, dédoublonnée par email), avec leur statut BAI tel quel
    ('BAI', 'A-BAI', ou '' si aucune de leurs lignes ne porte ce marqueur) —
    pour vérifier visuellement la couverture de ce marquage, en complément de
    _comparer_bai_basilic qui ne montre que les écarts avec Basilic (demande
    utilisateur 2026-10-29 : "mettre tous les participants de gow ... en
    indiquant si ils sont bien noté BAI ou non"). Une même personne peut
    avoir plusieurs lignes (une par magasin) ; si l'une d'elles porte
    BAI/A-BAI, ce statut est retenu pour la personne. Triée par Nom."""
    chemin_participants = _fichier_drive(annee, "participants")
    if not chemin_participants:
        return []
    df_part = pd.read_excel(chemin_participants)
    df_part.columns = [str(c).strip() for c in df_part.columns]
    lignes = df_part[df_part.get("Année") == annee]

    resultat = {}
    for _, r in lignes.iterrows():
        nom = str(r.get("Nom", "")).strip()
        email = str(r.get("Email", "")).strip().lower()
        if not nom or nom.lower() == "nan" or not email or email == "nan":
            continue
        bai_brut = str(r.get("BAI", "")).strip()
        if bai_brut.lower() == "nan":
            bai_brut = ""
        entree = resultat.setdefault(email, {"nom": nom, "email": email, "bai": ""})
        if bai_brut in ("BAI", "A-BAI"):
            entree["bai"] = bai_brut
    return sorted(resultat.values(), key=lambda p: p["nom"].lower())


def _participants_actifs_gow(annee):
    """Participants go-on-web (cf. _lister_participants_gow) réellement actifs
    cette année — affectés à un magasin (collecte BAI directe), nommés
    chauffeur/équipier ou entrepôt (liste-vehicule.xlsx), ou référents d'une
    association en collecte gardée (cf. _referents_association, même
    fonction déjà utilisée par la page Associations gardant — une première
    version de ce filtre avec son propre rapprochement naïf sur la colonne
    Groupe n'en retrouvait que 7 sur 29 réels, faute de gérer le préfixe
    'BAI+' et les doublons multi-lignes par email comme le fait déjà
    _referents_association) — par opposition à une simple inscription/
    référencement sans affectation réelle cette année (demande utilisateur
    2026-11-10, pour mail_bilan : "n'envoyer un mail qu'au bénévole actif
    cette année donc affecté à une action"). Depuis le 2026-11-19, filtre
    supplémentaire "mailing participants" appliqué une seule fois sur le
    résultat final (cf. _emails_mailing_participants) — ne change jamais
    les 4 catégories ci-dessous (donc jamais _comptage_participants, qui ne
    les réutilise pas combinées ainsi), seulement la liste des destinataires
    effectifs du mail bilan."""
    participants = _lister_participants_gow(annee)

    emails_avec_magasin = _emails_avec_magasin_gow(annee)
    emails_camions = {e for e in _charger_chauffeurs_equipiers_bruts(annee).keys() if "@" in e}
    emails_entrepot = {p["email"] for p in _charger_benevoles_entrepot(annee) if p.get("email")}

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    df_participants = _lire_participants(annee)
    emails_gardee = set()
    for asso in associations:
        for referent in _referents_association(asso["nom"], df_participants):
            email_referent = (referent.get("email") or "").strip().lower()
            if email_referent:
                emails_gardee.add(email_referent)

    emails_mailing = _emails_mailing_participants(annee)
    return [
        p for p in participants
        if (
            p["email"] in emails_avec_magasin
            or p["email"] in emails_camions
            or p["email"] in emails_entrepot
            or p["email"] in emails_gardee
        )
        and _email_retenu_mailing_participants(p["email"], emails_mailing)
    ]


def _comptage_participants(annee):
    """Comptage par grande catégorie d'affectation, pour objectiver qui
    compte comme quoi (demande utilisateur 2026-11-10, suite au bug
    _emails_avec_magasin_gow où la colonne Magasin contenait aussi des
    libellés de camion/entrepôt) : vrais magasins (comparés au référentiel,
    cf. _emails_avec_magasin_gow), équipiers/chauffeurs réellement affectés
    à un camion réel V001-V089 (cf. _charger_chauffeurs_equipiers_bruts, qui
    exclut déjà V090/V091/V092 — le pool V100, sans camion précis, est
    exclu ici aussi : demande utilisateur 2026-11-10 "ne pas inclure V100"),
    bénévoles entrepôt V090-V092 (cf. _charger_benevoles_entrepot). Chaque
    bénévole peut apparaître dans plusieurs listes à la fois (ex. magasin
    ET camion). Depuis le 2026-11-19, même filtre supplémentaire "mailing
    participants" que les mails post-collecte (demande utilisateur : "il
    faut aussi appliquer cette règle à Comptage participants") — cf.
    _emails_mailing_participants."""
    emails_mailing = _emails_mailing_participants(annee)

    participants = _lister_participants_gow(annee)
    emails_magasin = _emails_avec_magasin_gow(annee)
    magasins = [
        p for p in participants
        if p["email"] in emails_magasin and _email_retenu_mailing_participants(p["email"], emails_mailing)
    ]

    camions_bruts = _charger_chauffeurs_equipiers_bruts(annee)
    camions = sorted(
        (
            {"nom": v["nom"], "email": v["email"]} for v in camions_bruts.values()
            if v.get("email") and v.get("camions") and _email_retenu_mailing_participants(v["email"], emails_mailing)
        ),
        key=lambda p: p["nom"].lower(),
    )

    entrepot = [
        p for p in _charger_benevoles_entrepot(annee)
        if _email_retenu_mailing_participants(p["email"], emails_mailing)
    ]

    return {"magasins": magasins, "camions": camions, "entrepot": entrepot}


@collecte_bp.route("/collecte/<int:annee>/participants/comptage")
@login_required
@require_access("collecte", "lecture")
def participants_comptage(annee):
    comptage = _comptage_participants(annee)
    return render_template("collecte/participants_comptage.html", annee=annee, **comptage)


@collecte_bp.route("/collecte/<int:annee>/participants/comptage/export")
@login_required
@require_access("collecte", "lecture")
def participants_comptage_export(annee):
    comptage = _comptage_participants(annee)

    wb_export = Workbook()
    ws1 = wb_export.active
    ws1.title = "Magasins"
    ws1.append(["Nom", "Email"])
    for p in comptage["magasins"]:
        ws1.append([p["nom"], p["email"]])

    ws2 = wb_export.create_sheet("Camions")
    ws2.append(["Nom", "Email"])
    for p in comptage["camions"]:
        ws2.append([p["nom"], p["email"]])

    ws3 = wb_export.create_sheet("Entrepot")
    ws3.append(["Nom", "Email"])
    for p in comptage["entrepot"]:
        ws3.append([p["nom"], p["email"]])

    tampon = io.BytesIO()
    wb_export.save(tampon)
    tampon.seek(0)
    return send_file(
        tampon,
        as_attachment=True,
        download_name=f"comptage_participants_{annee}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/<int:annee>/comparaison-bai-basilic")
@login_required
@require_access("collecte", "lecture")
def comparaison_bai_basilic(annee):
    """Compare la base bénévoles de Basilic et les personnes BAI de la liste
    des participants go-on-web, dans les 2 sens — voir _comparer_bai_basilic.
    Affiche aussi la liste complète des participants go-on-web avec leur
    statut BAI — voir _lister_participants_gow."""
    dans_basilic_pas_gow, dans_gow_pas_basilic = _comparer_bai_basilic(annee)
    participants_gow = _lister_participants_gow(annee)
    return render_template(
        "collecte/comparaison_bai_basilic.html",
        annee=annee,
        dans_basilic_pas_gow=dans_basilic_pas_gow,
        dans_gow_pas_basilic=dans_gow_pas_basilic,
        participants_gow=participants_gow,
    )


@collecte_bp.route("/collecte/<int:annee>/comparaison-bai-basilic/export")
@login_required
@require_access("collecte", "lecture")
def comparaison_bai_basilic_export(annee):
    """Export Excel (3 onglets) de la comparaison BAI Basilic / go-on-web —
    voir _comparer_bai_basilic et _lister_participants_gow. Les 2 premiers
    onglets utilisent le même format que la liste d'appel chauffeurs/
    équipiers, le 3e (liste complète des participants go-on-web) une version
    réduite à 3 colonnes (Nom, Email, BAI)."""
    dans_basilic_pas_gow, dans_gow_pas_basilic = _comparer_bai_basilic(annee)
    participants_gow = _lister_participants_gow(annee)

    wb_export = Workbook()
    ws1 = wb_export.active
    ws1.title = "Basilic sans GOW"
    _ecrire_feuille_appel(ws1, _enrichir_avec_appel_chauffeurs(dans_basilic_pas_gow, annee))

    ws2 = wb_export.create_sheet("GOW sans Basilic")
    _ecrire_feuille_appel(ws2, _enrichir_avec_appel_chauffeurs(dans_gow_pas_basilic, annee))

    ws3 = wb_export.create_sheet("Participants GOW")
    ws3.append(["Nom", "Email", "BAI"])
    for p in participants_gow:
        ws3.append([p["nom"], p["email"], p["bai"]])
    ws3.column_dimensions["A"].width = 32
    ws3.column_dimensions["B"].width = 38
    ws3.column_dimensions["C"].width = 10
    ws3.freeze_panes = "A2"
    ws3.auto_filter.ref = ws3.dimensions

    tampon = io.BytesIO()
    wb_export.save(tampon)
    tampon.seek(0)
    return send_file(
        tampon,
        as_attachment=True,
        download_name=f"comparaison_bai_basilic_{annee}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


PRODUCTION_FICHIERS_SORTIE = {
    "excel":     {"nom": "Tournees_BAI38_{annee}_GOTW.xlsx", "label": "Classeur Excel (tournées + contrôles)"},
    "fiches":    {"nom": "fiches_jour_vehicule_magasin_{annee}_GOTW.pdf", "label": "Fiches de collecte"},
    "pointage":  {"nom": "pointage_vehicules_{annee}_GOTW.pdf", "label": "Pointage véhicules"},
    "equipier":  {"nom": "fiches_jour_vehicule_magasin_equipier_{annee}_GOTW.pdf", "label": "Fiches équipier"},
    "index":     {"nom": "fiches_equipier_jour_vehicule_{annee}_GOTW.pdf", "label": "Index alphabétique équipiers"},
    "consignes": {"nom": "vehicule_consignes.xlsx", "label": "Consignes véhicules (1 ligne/camion)"},
    "numeros_camions": {"nom": "numeros_camions_{annee}.pdf", "label": "Numéros de camion (étiquettes)"},
    "carte":     {"nom": "carte_tournees_production.html", "label": "Carte interactive des tournées"},
    "itineraires": {"nom": "itineraires_tournees_{annee}_GOTW.pdf", "label": "📍 Itinéraires des tournées (carte)"},
}


# Défauts repris de lancer_tournees_bai_v2.bat (section PARAMETRES), pas des
# valeurs par défaut de argparse dans le script (différentes, pensées pour un
# usage en ligne de commande sans .bat).
PARAMS_DEFAUT = {
    "camions_supp": 3,
    "poids_nouveaux": 200,
    "max_magasins": 5,
    "optimiser_anciens": True,
    "fusionner_legeres": False,
    "corriger_mal_places": True,
    "plus1_dimanche": True,
}

COLONNES_MAGASINS = [f"Magasin {i}" for i in range(1, 7)]


def _nom_fichier_genere(annee, params, horodatage):
    """Nom de fichier encodant les paramètres utilisés, pour pouvoir garder
    plusieurs versions par année sans les confondre — même logique de
    suffixes que le script d'origine (VX{camions-supp} + _OptAnciens/
    _FusLegeres/_CorMalPlaces), étendue à max-magasins et poids-nouveaux qui
    sont ici aussi modifiables par l'utilisateur."""
    suffixes = ""
    suffixes += "_OptAnciens" if params["optimiser_anciens"] else ""
    suffixes += "_FusLegeres" if params["fusionner_legeres"] else ""
    suffixes += "_CorMalPlaces" if params["corriger_mal_places"] else ""
    return (
        f"Tournees_BAI38_{annee}_{horodatage}"
        f"_VX{params['camions_supp']}_MAX{params['max_magasins']}_PN{params['poids_nouveaux']}"
        f"{suffixes}.xlsx"
    )


def _generer_tournees(campagne, params, mapping_camions=None, avec_type_camion=False, depuis_liste_vehicule=False,
                       utiliser_camions_vides=True, camions_exclus=None):
    """Exécute le pipeline complet (extraction PDF + magasins + optimisation
    + export Excel) via collecte_moteur_tournees, tel qu'orchestré par
    main() dans le script d'origine. Retourne le nom du fichier Excel généré
    et quelques compteurs pour l'affichage/la BDD.

    mapping_camions (optionnel) : dict {code camion tel qu'extrait des
    fiches de départ (PDF de l'année précédente, ou liste-vehicule.xlsx si
    depuis_liste_vehicule) -> code à utiliser à la place} — sert à garder
    certains magasins groupés ensemble sur le même camion (même principe que
    VIFS_ANCRES, mais réglable par campagne plutôt que codé en dur) : le code
    d'un camion pouvant changer d'une année sur l'autre, ce mapping (saisi
    manuellement par l'utilisateur, cf. simulation_capacite_form) permet de
    garder les magasins ancrés sur le bon camion malgré ce changement — déjà
    utilisé par la simulation avec capacité (PDF), et tout autant pertinent
    en partant de liste-vehicule.xlsx. Sans mapping, le comportement est
    strictement identique à avant (codes des fiches de départ utilisés tels
    quels).

    avec_type_camion (optionnel) : ajoute une colonne 'Type camion' (issue de
    liste-vehicule.xlsx) à titre indicatif dans l'export, et transmet aussi
    ce mapping à l'optimiseur (args.types_camions) qui s'en sert pour une
    passe d'échange de tournées entières entre camions réels mal assortis
    en taille (ex. un 12 m3 surchargé pendant qu'un 20 m3 roule à vide) —
    cf. _volume_camion/_tonnage_tournee dans moteur_tournees.py.

    depuis_liste_vehicule (optionnel) : repart de liste-vehicule.xlsx (qui
    contient une première version, partielle, des affectations réelles de
    cette année) au lieu du PDF de l'année précédente, et complète les
    demi-journées encore manquantes pour les magasins déjà partiellement
    affectés (d'après leurs créneaux, cf. _fiches_depuis_liste_vehicule et
    optimiser_tournees/completer_via_creneaux).

    utiliser_camions_vides (optionnel, défaut True) : si False, les camions
    de liste-vehicule.xlsx sans aucun magasin ne sont PAS utilisés comme
    capacité supplémentaire (ex. un camion plus utilisé cette année, sans
    pour autant avoir été retiré du fichier) — seul le mécanisme habituel de
    camions supplémentaires synthétiques (VX3xx, piloté par le champ
    'Camions supplémentaires') reste disponible.

    camions_exclus (optionnel) : liste de codes à retirer complètement de la
    simulation pour cet essai ponctuel ('et si on retirait ce camion ?') —
    tout son contenu est réinjecté ailleurs, sans modifier la flotte réelle."""
    annee = campagne["annee"]
    dossier = _dossier_annee(annee)
    pdf_path = os.path.join(dossier, campagne["fichier_pdf_precedent"])
    magasins_path = _fichier_drive(annee, "magasins")
    if not magasins_path:
        raise RuntimeError("Liste des magasins introuvable dans le drive partagé BA380 - COLLECTE")

    args_ns = argparse.Namespace(
        camions_supp=params["camions_supp"],
        poids_nouveaux=params["poids_nouveaux"],
        max_magasins=params["max_magasins"],
        corriger_mal_places=params["corriger_mal_places"],
        fusionner_legeres=params["fusionner_legeres"],
        optimiser_anciens=params["optimiser_anciens"],
        plus1_dimanche=params.get("plus1_dimanche", True),
        output=None,
        pdf=pdf_path,
        magasins=magasins_path,
        nouveaux=None,
        annee=annee,
        camions_exclus=camions_exclus,
        vifs_sans_vendredi={v.lstrip("0") for v in _vifs_sans_vendredi(annee)},
    )

    if depuis_liste_vehicule:
        fiches = _fiches_depuis_liste_vehicule(annee)
        if not fiches:
            raise RuntimeError("liste-vehicule.xlsx introuvable ou vide — aucune affectation de départ")
        args_ns.completer_via_creneaux = True
        args_ns.creneaux_camions = _creneaux_camions_depuis_liste_vehicule(annee)
    else:
        fiches = moteur.extraire_pdf(pdf_path)
    if mapping_camions:
        for f in fiches:
            f["vehicule"] = mapping_camions.get(f["vehicule"], f["vehicule"])

    camions_actuels_annee = None
    type_par_code = {}
    if avec_type_camion:
        camions_actuels_annee = _camions_liste_vehicule(annee)
        type_par_code = {
            c["code"]: c["type_camion"] for c in camions_actuels_annee if c.get("type_camion")
        }
        args_ns.types_camions = type_par_code

    vifs_pdf_2025 = set()
    for f in fiches:
        for v in f["vif_codes"]:
            vifs_pdf_2025.add(str(v).lstrip("0"))

    df_mag = moteur.lire_magasins(magasins_path, vifs_pdf_2025, params["poids_nouveaux"], annee)

    if avec_type_camion and utiliser_camions_vides:
        # Camions réels de cette année absents des fiches de départ (code non
        # repris, même après mapping) : utilisés en priorité comme capacité
        # supplémentaire à la place des codes synthétiques VX3xx (cf.
        # _pool_camions_supp dans moteur_tournees.py), avec leur vrai nom —
        # pour que le résultat utilise les nouveaux camions réellement
        # ajoutés à la flotte cette année (ex. V042, V043). Exclut aussi les
        # camions VEHICULES_FIGES : un camion figé sans aucun magasin (ex.
        # V023) n'est pas une capacité disponible mais un camion qui n'est
        # plus utilisé cette année (gardé dans les données au cas où il
        # serait réactivé plus tard).
        # "Utilisé" = a au moins un magasin encore ACTIF cette année (pas
        # simplement présent quelque part dans le PDF) : un camion dont tous
        # les magasins sont passés en 'Collecte gardée' sans stockage BAI+
        # (ex. V009 CAMION SOLIFAIM, repris par une association) n'a plus
        # rien à charger depuis le PDF et doit être traité comme une vraie
        # capacité disponible, pas comme "déjà utilisé".
        vifs_actifs_pool = set(df_mag["Code VIF"].astype(str).str.lstrip("0"))
        codes_utilises = {
            f["vehicule"] for f in fiches
            if any(str(v).lstrip("0") in vifs_actifs_pool for v in f["vif_codes"])
        }
        codes_exclus = set(camions_exclus or [])
        args_ns.camions_supp_pool = [
            {"code": c["code"], "nom": c["nom"]}
            for c in camions_actuels_annee
            if c["code"] not in codes_utilises and c["code"] not in moteur.VEHICULES_FIGES
            and c["code"] not in moteur.CAMIONS_EXCLUSIFS and c["code"] not in codes_exclus
        ]

        # Créneaux réels des camions supplémentaires (ex. Mingat 1/2/3,
        # Sécurité Civile, VALDO) : contrairement au mode 'depuis liste
        # vehicule' (qui applique déjà creneaux_camions à TOUS les camions,
        # cf. plus haut), ici on se limite au pool supplémentaire — les
        # camions déjà présents dans les fiches de départ (le PDF de l'an
        # dernier) viennent d'une année où liste-vehicule.xlsx de CETTE année
        # n'est pas forcément encore complètement rempli pour eux, et leur
        # appliquer la même contrainte les bloquerait à tort sur des jours où
        # ils roulent réellement mais pas encore saisis. Les camions neufs du
        # pool, eux, n'ont pas cette ambiguïté : leurs lignes dans
        # liste-vehicule.xlsx sont la seule source de vérité sur leurs jours
        # réels (ex. Sécurité Civile ne roule pas le samedi).
        if not depuis_liste_vehicule:
            codes_pool_supp = {c["code"] for c in args_ns.camions_supp_pool}
            args_ns.creneaux_camions = {
                code: djs for code, djs in _creneaux_camions_depuis_liste_vehicule(annee).items()
                if code in codes_pool_supp
            }

    df_t, hors_secteur = moteur.optimiser_tournees(fiches, df_mag, args_ns)

    colonnes_extra = None
    if avec_type_camion:
        df_t["Type camion"] = df_t["Camion"].map(type_par_code).fillna("")
        colonnes_extra = ["Type camion"]

    dossier_resultats = _dossier_resultats(annee)
    os.makedirs(dossier_resultats, exist_ok=True)
    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    nom_fichier = _nom_fichier_genere(annee, params, horodatage)
    if depuis_liste_vehicule:
        nom_fichier = "Vehicule_" + nom_fichier
    if avec_type_camion:
        nom_fichier = "Capacite_" + nom_fichier
    args_ns.output = os.path.join(dossier_resultats, nom_fichier)

    moteur.generer_excel(
        df_t, df_mag, args_ns, args_ns.output, fiches, hors_secteur, "",
        colonnes_extra=colonnes_extra,
    )

    nom_carte_secteurs = os.path.splitext(nom_fichier)[0] + "_secteurs.html"
    data_secteurs = df_mag[["Nom", "Latitude", "Longitude", "Secteur"]].astype({
        "Nom": str, "Latitude": float, "Longitude": float, "Secteur": str,
    }).to_dict("records")
    polygones = carte_secteurs.calculer_polygones(data_secteurs)
    carte_secteurs.generer_html(data_secteurs, polygones, os.path.join(dossier_resultats, nom_carte_secteurs), annee)

    # Carte des tournées optimisées (itinéraire OSRM réel par demi-journée/camion,
    # Vendredi/Samedi hors véhicules figés — cf. doc §8) : contrairement à la carte
    # des secteurs, celle-ci tient compte du résultat de l'optimisation (df_t).
    chemin_carte_tournees = moteur.generer_carte_tournees(df_t, df_mag, args_ns, dossier_resultats)
    nom_carte_tournees = os.path.basename(chemin_carte_tournees)

    nom_pdf_equipier = None
    if avec_type_camion:
        nom_pdf_equipier = os.path.splitext(nom_fichier)[0] + "_equipier.pdf"
        moteur.generer_pdf_equipier(
            df_t, os.path.join(dossier_resultats, nom_pdf_equipier), annee,
            date_debut=campagne["date_debut"],
            params={**params, "camions_exclus": camions_exclus} if camions_exclus else params,
        )

    return {
        "nom_fichier": nom_fichier,
        "nom_carte_secteurs": nom_carte_secteurs,
        "nom_carte_tournees": nom_carte_tournees,
        "nom_pdf_equipier": nom_pdf_equipier,
        "nb_tournees": len(df_t),
        "nb_magasins": len(df_mag),
        "nb_nouveaux_magasins": int(df_mag["Nouveau"].sum()),
    }


def _normaliser_colonne(s):
    """Copie conforme de la fonction du même nom dans
    ba38_collecte/scripts/generer_documents_production.py (pour une
    détection de colonne identique dans les deux outils)."""
    import unicodedata
    s = unicodedata.normalize('NFD', str(s).strip().lower())
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')


def detecter_colonne(df_cols, contient_tous):
    """Copie conforme de la fonction du même nom dans
    ba38_collecte/scripts/generer_documents_production.py."""
    for col in df_cols:
        cl = _normaliser_colonne(col)
        if all(frag in cl for frag in contient_tous):
            return col
    return None


def _ensure_table_mapping_camions(conn):
    """Correspondance manuelle, saisie une fois par campagne, entre le code
    d'un camion tel qu'extrait du PDF des tournées de l'année précédente et
    le code à utiliser cette année (le code ou le nom d'un camion peut
    changer d'une campagne à l'autre) — utilisée par la simulation avec
    capacité camions pour garder les magasins ancrés (VIFS_ANCRES) sur le
    bon camion malgré ce changement."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_mapping_camions (
            annee INTEGER NOT NULL,
            code_ancien TEXT NOT NULL,
            code_nouveau TEXT NOT NULL,
            maj_le TEXT,
            maj_par TEXT,
            PRIMARY KEY (annee, code_ancien)
        )
    """)


def _mapping_camions(annee):
    """Dict {code_ancien: code_nouveau} enregistré pour cette année, ou {} si
    aucune correspondance n'a encore été saisie."""
    with get_db_connection() as conn:
        _ensure_table_mapping_camions(conn)
        lignes = conn.execute(
            "SELECT code_ancien, code_nouveau FROM collecte_mapping_camions WHERE annee = ?", (annee,)
        ).fetchall()
    return {l["code_ancien"]: l["code_nouveau"] for l in lignes if l["code_nouveau"]}


def _camions_pdf_precedent(annee):
    """Liste des camions (code + nom) distincts extraits du PDF des tournées
    de l'année précédente — sert de base (colonne de gauche) à la
    correspondance manuelle avec les camions de cette année."""
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    if not campagne or not campagne["fichier_pdf_precedent"]:
        return []
    pdf_path = os.path.join(_dossier_annee(annee), campagne["fichier_pdf_precedent"])
    if not os.path.exists(pdf_path):
        return []
    try:
        fiches = moteur.extraire_pdf(pdf_path)
    except Exception as erreur:
        write_log(f"⚠️ Collecte {annee} : lecture PDF année précédente impossible pour mapping camions — {erreur}")
        return []
    vus = {}
    for f in fiches:
        code = f["vehicule"]
        if code not in vus:
            vus[code] = f.get("nom_vehicule", "").strip()
    return [{"code": c, "nom": n} for c, n in sorted(vus.items())]


def _fiches_depuis_liste_vehicule(annee):
    """Construit la structure 'fiches' (même format que moteur.extraire_pdf :
    liste de {demi_journee, vehicule, vif_codes, nom_vehicule}) directement
    depuis les lignes déjà remplies de liste-vehicule.xlsx (Code VIF +
    Magasin renseignés) — sert de point de départ pour la simulation
    'depuis liste-vehicule' : cette liste contient une première version
    (partielle) des affectations réelles de cette année, à compléter et
    optimiser, plutôt que de repartir du PDF de l'année précédente."""
    chemin = _fichier_drive(annee, "vehicules")
    if not chemin or not os.path.exists(chemin):
        return []
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    jours = {"jeudi": "Jeudi", "vendredi": "Vendredi", "samedi": "Samedi", "dimanche": "Dimanche"}

    par_cle = {}  # (demi_journee, code) -> {"vif_codes": [...], "nom_vehicule": str}
    for _, ligne in df.iterrows():
        code = str(ligne.get("Code", "")).strip().upper()
        if not code or code == "NAN" or not _est_camion_reel(code):
            continue
        jour = jours.get(str(ligne.get("Tournée", "")).strip().lower())
        if not jour:
            continue
        debut = str(ligne.get("Début", "")).strip()
        fin = str(ligne.get("Fin", "")).strip()
        periode = _periode_depuis_horaires(debut, fin)
        demi_journee = f"{jour} {periode}"
        if demi_journee not in moteur.DEMI_JOURNEES:
            continue
        code_vif_brut = ligne.get("Code VIF")
        if code_vif_brut is None or (isinstance(code_vif_brut, float) and pd.isna(code_vif_brut)):
            continue
        vif = _vif_fmt(code_vif_brut).lstrip("0")
        if not vif:
            continue
        nom_vehicule = str(ligne.get("Véhicule", "")).strip()

        entree = par_cle.setdefault((demi_journee, code), {"vif_codes": [], "nom_vehicule": ""})
        if vif not in entree["vif_codes"]:
            entree["vif_codes"].append(vif)
        if nom_vehicule and nom_vehicule.lower() != "nan" and not entree["nom_vehicule"]:
            entree["nom_vehicule"] = nom_vehicule

    return [
        {"demi_journee": dj, "vehicule": code, "vif_codes": e["vif_codes"], "nom_vehicule": e["nom_vehicule"]}
        for (dj, code), e in par_cle.items()
    ]


def _creneaux_camions_depuis_liste_vehicule(annee):
    """Pour chaque camion, l'ensemble des demi-journées où liste-vehicule.xlsx
    prévoit une ligne pour lui (Code + Tournée + Début présents), QUE le
    magasin soit déjà renseigné ou non — une ligne vide (camion/jour/heure
    prévus, magasin pas encore saisi) compte comme un créneau réellement
    prévu pour ce camion. Sert à empêcher la simulation 'depuis
    liste-vehicule' d'affecter un magasin à un camion un jour où il ne roule
    pas du tout (ex. V022 ne roule pas le dimanche)."""
    chemin = _fichier_drive(annee, "vehicules")
    if not chemin or not os.path.exists(chemin):
        return {}
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    jours = {"jeudi": "Jeudi", "vendredi": "Vendredi", "samedi": "Samedi", "dimanche": "Dimanche"}

    creneaux = {}
    for _, ligne in df.iterrows():
        code = str(ligne.get("Code", "")).strip().upper()
        if not code or code == "NAN" or not _est_camion_reel(code):
            continue
        jour = jours.get(str(ligne.get("Tournée", "")).strip().lower())
        if not jour:
            continue
        debut = str(ligne.get("Début", "")).strip()
        fin = str(ligne.get("Fin", "")).strip()
        periode = _periode_depuis_horaires(debut, fin)
        demi_journee = f"{jour} {periode}"
        if demi_journee not in moteur.DEMI_JOURNEES:
            continue
        creneaux.setdefault(code, set()).add(demi_journee)
    return creneaux


def _camions_liste_vehicule(annee):
    """Camions (Code + Nom + Type camion) de liste-vehicule.xlsx pour cette
    année — sert de base (colonne de droite) à la correspondance manuelle, et
    source de la colonne 'Type camion' ajoutée à titre indicatif dans la
    simulation avec capacité camions."""
    chemin = _fichier_drive(annee, "vehicules")
    if not chemin or not os.path.exists(chemin):
        return []
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    cols = list(df.columns)
    col_code = detecter_colonne(cols, ['code']) or 'Code'
    col_nom = detecter_colonne(cols, ['vehicule']) or detecter_colonne(cols, ['nom', 'camion'])
    col_type = detecter_colonne(cols, ['type', 'camion'])
    if col_code not in df.columns:
        return []
    def _valeur_propre(col):
        if not col:
            return ''
        val = row.get(col, '')
        return '' if pd.isna(val) else str(val).strip()

    vus = {}
    for _, row in df.iterrows():
        # .upper() : certains codes sont saisis en minuscules dans
        # liste-vehicule.xlsx (ex. 'v039') — normalisé pour matcher la
        # convention des codes extraits du PDF (toujours en majuscules).
        code = _valeur_propre(col_code).upper()
        if not code or code in vus or not _est_camion_reel(code):
            continue
        nom = _valeur_propre(col_nom)
        type_camion = _valeur_propre(col_type)
        vus[code] = {"code": code, "nom": nom, "type_camion": type_camion}
    return [vus[c] for c in sorted(vus)]


def _camions_matin_apresmidi_differents(lignes):
    """Camions dont le contenu diffère entre matin et après-midi d'un même
    jour (Jeudi/Vendredi/Samedi) — à signaler à l'utilisateur plutôt que
    de les laisser invisibles : le moteur essaie d'aligner les deux (cf.
    « Cohérence jour » dans moteur_tournees.py) mais n'y parvient pas
    toujours (jeu de chaises musicales avec d'autres camions). Les camions
    listés dans CAMIONS_UNIFIER_DEMI_JOURNEES ne devraient jamais apparaître
    ici (forcés identiques en tout dernier) — une présence malgré tout
    signalerait un cas à examiner."""
    par_camion_dj = {}
    for l in lignes:
        par_camion_dj[(l["camion"], l["demi_journee"])] = {
            m.strip() for m in l["magasins"].split(",") if m.strip()
        }

    resultat = []
    for jour in ("Jeudi", "Vendredi", "Samedi"):
        dj_matin, dj_am = f"{jour} Matin", f"{jour} Apres Midi"
        camions_jour = sorted({c for (c, d) in par_camion_dj if d in (dj_matin, dj_am)})
        for camion in camions_jour:
            mags_matin = par_camion_dj.get((camion, dj_matin))
            mags_am = par_camion_dj.get((camion, dj_am))
            if mags_matin is None or mags_am is None:
                continue  # absent d'une des deux demi-journées : non comparable
            # Un seul magasin d'écart n'est pas signalé (retour utilisateur
            # direct) : différence jugée mineure, pas la peine d'alerter pour
            # un cas aussi proche d'être identique.
            if len(mags_matin.symmetric_difference(mags_am)) <= 1:
                continue
            if mags_matin != mags_am:
                resultat.append({
                    "camion": camion,
                    "jour": jour,
                    "magasins_matin": sorted(mags_matin),
                    "magasins_apres_midi": sorted(mags_am),
                })
    return resultat


def _camions_non_utilises(annee, lignes):
    """Pour chaque demi-journée présente dans le résultat, les camions
    réels de cette année (liste-vehicule.xlsx) qui ONT un créneau réel
    défini ce jour-là (cf. _creneaux_camions_depuis_liste_vehicule) mais
    sont absents de cette génération — un camion sans créneau défini pour
    une demi-journée (il ne roule simplement pas ce jour-là) n'est jamais
    signalé pour cette demi-journée. Utile pour repérer d'un coup d'œil les
    camions inutilisés avant de valider une version (cf. resultats())."""
    camions = _camions_liste_vehicule(annee)
    if not camions:
        return []
    tous_codes = {c["code"]: c["nom"] for c in camions}
    creneaux_reels = _creneaux_camions_depuis_liste_vehicule(annee)

    utilises_par_dj = {}
    for l in lignes:
        utilises_par_dj.setdefault(l["demi_journee"], set()).add(l["camion"])

    resultat = []
    for dj in moteur.DEMI_JOURNEES:
        if dj not in utilises_par_dj:
            continue
        non_utilises = sorted(
            ({"code": code, "nom": nom, "libelle": code + (f" — {nom}" if nom else "")}
             for code, nom in tous_codes.items()
             if code not in utilises_par_dj[dj] and dj in creneaux_reels.get(code, set())),
            key=lambda c: c["code"],
        )
        if non_utilises:
            resultat.append({"demi_journee": dj, "camions": non_utilises})
    return resultat


def _charger_tournees(generation):
    """Relit le fichier Excel généré (onglet Tournees) pour l'affichage web.
    header=7 : même décalage que celui utilisé par Generer_documents_bai38.py
    et Generer_fiches_2025.py pour lire ce même onglet (5 lignes de titre +
    1 ligne de légende + 1 ligne d'en-têtes vides au-dessus des en-têtes
    réels)."""
    annee = generation["annee"]
    chemin = os.path.join(_dossier_resultats(annee), generation["fichier_excel"])

    df = pd.read_excel(chemin, sheet_name="Tournees", header=7)
    df = df[df["Camion"].notna() & df["Camion"].astype(str).apply(_est_camion_reel)].reset_index(drop=True)

    ordre_dj = {dj: i for i, dj in enumerate(moteur.DEMI_JOURNEES)}
    df["_ordre_dj"] = df["Demi-journee"].map(ordre_dj).fillna(99)
    df = df.sort_values(["_ordre_dj", "Camion"]).reset_index(drop=True)

    lignes = []
    for _, row in df.iterrows():
        magasins = [
            str(row[c]).strip() for c in COLONNES_MAGASINS
            if c in row and str(row[c]).strip() and str(row[c]).strip().lower() != "nan"
        ]
        lignes.append({
            "demi_journee": str(row["Demi-journee"]),
            "ordre_dj": int(row["_ordre_dj"]),
            "camion": str(row["Camion"]),
            "nom_camion": "" if pd.isna(row.get("Nom camion")) else str(row["Nom camion"]),
            "tonnage": 0.0 if pd.isna(row.get("Tonnage")) else float(row["Tonnage"]),
            "km": "" if pd.isna(row.get("Km estimes")) else float(row["Km estimes"]),
            "duree": "" if pd.isna(row.get("Duree estimee")) else str(row["Duree estimee"]),
            "secteur": "" if pd.isna(row.get("Secteur")) else str(row["Secteur"]),
            "magasins": ", ".join(magasins),
            "commentaire": "" if pd.isna(row.get("Commentaire optimisation")) else str(row["Commentaire optimisation"]),
            "type_camion": "" if pd.isna(row.get("Type camion")) else str(row.get("Type camion")),
            "figee": bool(row["Camion"] in moteur.VEHICULES_FIGES),
        })
    return lignes


def _charger_tournees_production(annee):
    """Même principe que _charger_tournees, mais pour le classeur Production
    (plan réel décidé dans go-on-web, pas une simulation) — onglet Tournees
    sans décalage d'en-tête (ligne 1), et sans Durée/Commentaire/Type
    camion/figé (champs propres au moteur de simulation, absents ici)."""
    chemin = os.path.join(_dossier_production(annee), PRODUCTION_FICHIERS_SORTIE["excel"]["nom"].format(annee=annee))
    if not os.path.exists(chemin):
        return []

    df = pd.read_excel(chemin, sheet_name="Tournees")
    df = df[df["Camion"].notna() & df["Camion"].astype(str).apply(_est_camion_reel)].reset_index(drop=True)
    # .upper() : certains codes sont saisis en minuscules dans
    # liste-vehicule.xlsx (ex. 'v039') et peuvent donc atterrir tels quels
    # dans le classeur Production — normalisé ici pour matcher la
    # convention partout ailleurs (toujours en majuscules), sinon 'v039'
    # ressortait signalé à tort comme camion non utilisé (comparaison
    # 'V039' vs 'v039' jamais égale, retour utilisateur direct 2026-10-22).
    df["Camion"] = df["Camion"].astype(str).str.strip().str.upper()

    ordre_dj = {dj: i for i, dj in enumerate(moteur.DEMI_JOURNEES)}
    df["_ordre_dj"] = df["Demi-journee"].map(ordre_dj).fillna(99)
    df = df.sort_values(["_ordre_dj", "Camion"]).reset_index(drop=True)

    lignes = []
    for _, row in df.iterrows():
        magasins = [
            str(row[c]).strip() for c in COLONNES_MAGASINS
            if c in row and str(row[c]).strip() and str(row[c]).strip().lower() != "nan"
        ]
        lignes.append({
            "demi_journee": str(row["Demi-journee"]),
            "ordre_dj": int(row["_ordre_dj"]),
            "camion": str(row["Camion"]),
            "nom_camion": "" if pd.isna(row.get("Nom camion")) else str(row["Nom camion"]),
            "type_camion": "" if pd.isna(row.get("Type camion")) else str(row.get("Type camion")),
            "tonnage": 0.0 if pd.isna(row.get("Tonnage")) else float(row["Tonnage"]),
            "km": "" if pd.isna(row.get("Km estimes")) else float(row["Km estimes"]),
            "secteur": "" if pd.isna(row.get("Secteur")) else str(row["Secteur"]),
            "magasins": ", ".join(magasins),
        })
    return lignes


CONTROLES_PRODUCTION_ONGLETS = [
    ("Magasins-DJ non couverts", "⚠️ Magasins/demi-journée attendus (créneaux) mais sans camion"),
    ("Créneaux incomplets", "⚠️ Créneaux incomplets (jour couvert une demi-journée sur deux)"),
    ("Camion non défini", "⚠️ Magasins sans camion (Code) renseigné"),
    ("Doublons camion-magasin", "⚠️ Magasins affectés à plusieurs camions (même demi-journée)"),
    ("Magasins non planifiés", "⚠️ Magasins jamais planifiés"),
    ("Camions absents", "⚠️ Camions définis sans aucune tournée affectée"),
    ("Camions non définis", "⚠️ Camions présents dans les tournées mais absents du fichier véhicules"),
]


def _controles_production(annee):
    """Relit les onglets de contrôle déjà calculés par
    generer_documents_production.py dans le classeur Production (Magasins-DJ
    non couverts, Créneaux incomplets...) — aucun recalcul ici, uniquement
    une relecture pour affichage sur la page 'Voir/filtrer'."""
    chemin = os.path.join(_dossier_production(annee), PRODUCTION_FICHIERS_SORTIE["excel"]["nom"].format(annee=annee))
    if not os.path.exists(chemin):
        return []
    try:
        noms_onglets = set(pd.ExcelFile(chemin).sheet_names)
    except Exception:
        return []

    controles = []
    for nom_onglet, titre in CONTROLES_PRODUCTION_ONGLETS:
        if nom_onglet not in noms_onglets:
            continue
        df_c = pd.read_excel(chemin, sheet_name=nom_onglet)
        if df_c.empty:
            continue
        controles.append({
            "titre": titre,
            "colonnes": list(df_c.columns),
            "lignes": df_c.fillna("").astype(str).values.tolist(),
        })
    return controles


def _lire_referentiel_magasins_bai(annee, campagne):
    """Relit le fichier magasins de l'année (Drive ou upload) et renvoie la
    liste des magasins du périmètre bai (État='Collecté par la BAI', +
    'Collecte gardée' avec Stockage BAI+, cf. moteur.lire_magasins) avec leurs
    demi-journées d'ouverture (colonne Créneaux, cf. moteur.parse_creneaux).
    Reflète l'état courant du fichier — utilisé uniquement pour (ré)initialiser
    la liste figée de collecte_cagettes_magasins, jamais directement par la
    page de saisie (qui doit rester stable même si le fichier est remplacé en
    cours de campagne)."""
    chemin = _fichier_drive(annee, "magasins")
    if not chemin:
        raise RuntimeError("Liste des magasins introuvable dans le drive partagé BA380 - COLLECTE")

    df = pd.read_excel(chemin)
    df.columns = [c.strip() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]

    rmap = {}
    for col in df.columns:
        cl = col.lower()
        if "vif" in cl or cl == "code":
            rmap[col] = "Code VIF"
        elif ("nom" in cl or "magasin" in cl) and "fiche" not in cl:
            rmap[col] = "Nom"
    df = df.rename(columns=rmap)
    df = df.loc[:, ~df.columns.duplicated()]

    for col in ["Code VIF", "Nom", "État", "Stockage", "Créneaux"]:
        if col not in df.columns:
            df[col] = ""

    etat = df["État"].astype(str).str.strip()
    stockage = df["Stockage"].astype(str).str.strip()
    mask_bai = etat == "Collecté par la BAI"
    mask_gardee_bai_plus = (etat == "Collecte gardée") & stockage.str.contains(r"BAI\s*\+", case=False, regex=True, na=False)
    df = df[mask_bai | mask_gardee_bai_plus].reset_index(drop=True)

    magasins = []
    for _, row in df.iterrows():
        code_vif = _vif_fmt(row["Code VIF"])
        nom_magasin = str(row["Nom"]).strip()
        if not code_vif or code_vif.lower() == "nan":
            continue
        djs = moteur.parse_creneaux(row.get("Créneaux", ""))
        demi_journees = [dj for dj in moteur.DEMI_JOURNEES if djs is None or dj in djs]
        magasins.append({"code_vif": code_vif, "nom_magasin": nom_magasin, "demi_journees": demi_journees})
    return magasins


def _emails_magasin(raw):
    """Découpe la colonne Email du référentiel magasins — fichier externe
    maintenu à la main, où plusieurs adresses sont séparées tantôt par ';'
    tantôt par ',' (contrairement à split_emails(), réservé au champ
    courriel_association de l'appli qui n'utilise que ';')."""
    if not raw:
        return []
    parties = re.split(r"[;,]", str(raw))
    nettoyees = [p.strip().strip("<>") for p in parties]
    return [p for p in nettoyees if is_valid_email(p)]


def _jours_collecte_texte(demi_journees):
    """['Vendredi Matin', 'Vendredi Apres Midi', 'Samedi Matin'] -> 'Vendredi
    et Samedi' — Dimanche n'a qu'un seul créneau matin dans DEMI_JOURNEES,
    d'où le libellé spécial 'Dimanche matin' plutôt que juste 'Dimanche'."""
    jours = [jour for jour in ("Jeudi", "Vendredi", "Samedi")
             if any(dj.startswith(jour) for dj in demi_journees)]
    if "Dimanche Matin" in demi_journees:
        jours.append("Dimanche matin")
    if not jours:
        return ""
    if len(jours) == 1:
        return jours[0]
    return ", ".join(jours[:-1]) + " et " + jours[-1]


MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
           "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def _phrase_jours_collecte(demi_journees, annee):
    """Phrase à insérer dans la lettre de demande d'autorisation — UNIQUEMENT
    les jours (avec leur date calendaire) où CE magasin est collecté, pas la
    plage complète de la campagne (ex. 'le VENDREDI 6 et le SAMEDI 7
    NOVEMBRE 2026'), calculée à partir de date_debut (toujours un jeudi)."""
    offsets = {"Jeudi": 0, "Vendredi": 1, "Samedi": 2, "Dimanche": 3}
    jours_presents = [jour for jour in ("Jeudi", "Vendredi", "Samedi")
                       if any(dj.startswith(jour) for dj in demi_journees)]
    if "Dimanche Matin" in demi_journees:
        jours_presents.append("Dimanche")
    if not jours_presents:
        return "aux dates qui vous seront communiquées"

    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT date_debut FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    if not campagne or not campagne["date_debut"]:
        return "les jours suivants : " + _jours_collecte_texte(demi_journees)

    debut = datetime.strptime(campagne["date_debut"], "%Y-%m-%d")
    parties = []
    for jour in jours_presents:
        date_jour = debut + timedelta(days=offsets[jour])
        libelle = jour.upper() + (" (matin)" if jour == "Dimanche" else "")
        parties.append(f"{libelle} {date_jour.day}")
    mois_annee = f"{MOIS_FR[debut.month - 1].upper()} {debut.year}"

    if len(parties) == 1:
        return f"le {parties[0]} {mois_annee}"
    return "les " + ", ".join(parties[:-1]) + " et " + parties[-1] + " " + mois_annee


def _lire_magasins_autorisation(annee):
    """Magasins de liste_magasins.xlsx avec un email renseigné et sans
    accord encore donné (colonne 'Accord' vide), hors magasins État='Non
    collecté' — cible du publipostage de demande d'autorisation de
    collecter."""
    chemin = _fichier_drive(annee, "magasins")
    if not chemin:
        return []

    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    for col in ["Code VIF", "Nom", "État", "Adresse", "Ville", "C.P.", "Téléphone", "Email", "Créneaux", "Accord"]:
        if col not in df.columns:
            df[col] = ""

    magasins = []
    for _, row in df.iterrows():
        if str(row.get("État", "")).strip() == "Non collecté":
            continue
        email_brut = str(row.get("Email", "")).strip()
        if not email_brut or email_brut.lower() == "nan":
            continue
        accord = row.get("Accord")
        if not (pd.isna(accord) or str(accord).strip() in ("", "nan")):
            continue
        code_vif = _vif_fmt(row.get("Code VIF"))
        if not code_vif or code_vif.lower() == "nan":
            continue
        cp_brut = row.get("C.P.", "")
        try:
            cp = str(int(float(cp_brut))) if str(cp_brut).strip() not in ("", "nan") else ""
        except (TypeError, ValueError):
            cp = str(cp_brut).strip()
        djs = moteur.parse_creneaux(row.get("Créneaux", ""))
        demi_journees = [dj for dj in moteur.DEMI_JOURNEES if djs is None or dj in djs]
        magasins.append({
            "code_vif": code_vif,
            "nom": str(row.get("Nom", "")).strip(),
            "etat": str(row.get("État", "")).strip(),
            "adresse": str(row.get("Adresse", "")).strip(),
            "ville": str(row.get("Ville", "")).strip(),
            "cp": cp,
            "telephone": str(row.get("Téléphone", "")).strip() if str(row.get("Téléphone", "")).strip().lower() != "nan" else "",
            "emails": _emails_magasin(email_brut) or [email_brut],
            "demi_journees": demi_journees,
            "jours_texte": _jours_collecte_texte(demi_journees),
        })
    magasins.sort(key=lambda m: m["nom"].lower())
    return magasins


AUTORISATION_TEXTE_LETTRE_DEFAUT = (
    "Madame la Directrice, Monsieur le Directeur de <<Nom>>,\n\n"
    "La Banque Alimentaire de l'Isère organise chaque année fin novembre une collecte "
    "alimentaire dans les GMS du département.\n\n"
    "En novembre dernier, nous avons collecté 172 tonnes de marchandises dans notre "
    "département.\n\n"
    "Nous nous adressons à vous afin que vous donniez votre accord pour la participation "
    "de votre magasin à la Collecte Nationale des Banques Alimentaires.\n\n"
    "<<dates>>\n\n"
    "Les produits alimentaires que nous souhaitons collecter sont indiqués ci-dessous.\n\n"
    "Nous vous demandons de nous adresser votre accord rapidement à l'aide du coupon "
    "ci-dessous, par courrier, mail (ba380.collecte@banquealimentaire.org). Nous aurons "
    "alors le plaisir de prendre contact avec la personne que vous aurez désignée pour "
    "finaliser la procédure de mise en place dans votre magasin des bénévoles et du "
    "matériel de communication.\n\n"
    "Nous vous prions d'agréer, Madame la Directrice, Monsieur le Directeur, l'assurance "
    "de nos salutations distinguées.\n\n"
    "<<centre>>\n"
    "Pierre Thorel\n"
    "Responsable Collecte à la Banque Alimentaire de l'Isère"
)

AUTORISATION_TEXTE_COUPON_DEFAUT = (
    "NOM DU MAGASIN : <<Nom>>   (Code BA Isère : <<CodeVIF>>)\n"
    "Adresse : <<Adresse>>   <<CP>> <<VILLE>>\n"
    "Responsable à contacter : _____________________   N° Tél : <<Telephone>>\n"
    "Mail : <<Email>>\n"
    "Horaires ouverture/fermeture : ________________          Nb de portes : ____\n"
    "<<autorisation>>\n"
    "Signature du responsable du magasin :"
)

AUTORISATION_TEXTE_MAIL_DEFAUT = (
    "Madame la Directrice, Monsieur le Directeur de <<Nom>>,\n\n"
    "Vous trouverez ci-joint notre demande d'autorisation de collecter à l'occasion de la "
    "collecte nationale des Banques Alimentaires <<Annee>>.\n\n"
    "Merci de nous retourner le coupon-réponse complété, par mail "
    "(ba380.collecte@banquealimentaire.org) ou par courrier.\n\n"
    "Cordialement,\n"
    "La Banque Alimentaire de l'Isère"
)


def _donnees_lettre_autorisation(magasin, annee):
    """Valeurs personnalisées communes aux deux générateurs de lettre
    (.docx éditable pour le modèle, .pdf réellement envoyé — cf.
    _creer_pdf_autorisation). Le texte de la lettre ET celui du
    coupon-réponse sont modifiables en ligne (page Gestion des
    autorisations, même principe que mail_texte pour les
    chauffeurs/équipiers) — plus aucun texte n'est repris du modèle Word,
    seule l'affiche des produits (image) en est encore extraite. Repères
    <<dates>> (lettre) et <<autorisation>> (coupon, ligne OUI/NON avec
    cases à cocher) déclenchent un rendu spécial ; à défaut de
    personnalisation, le texte par défaut est utilisé."""
    aujourdhui = datetime.now()
    jour_mail = f"{aujourdhui.day} {MOIS_FR[aujourdhui.month - 1]} {aujourdhui.year}"

    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT autorisation_texte_lettre, autorisation_texte_coupon "
            "FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    texte_lettre = (campagne["autorisation_texte_lettre"] if campagne else None) or AUTORISATION_TEXTE_LETTRE_DEFAUT
    texte_coupon = (campagne["autorisation_texte_coupon"] if campagne else None) or AUTORISATION_TEXTE_COUPON_DEFAUT

    return {
        "jour_mail": jour_mail,
        "nom": magasin["nom"],
        "code_vif": magasin["code_vif"],
        "adresse": magasin["adresse"],
        "cp": magasin["cp"],
        "ville": magasin["ville"].upper(),
        "telephone": magasin["telephone"],
        "email": ";".join(magasin["emails"]),
        "phrase_jours": _phrase_jours_collecte(magasin["demi_journees"], annee),
        "texte_lettre": texte_lettre,
        "texte_coupon": texte_coupon,
    }


def _chemin_image_produits_autorisation():
    """Chemin de l'affiche déposée pour la demande d'autorisation, dans
    l'emplacement PARTAGÉ (hors des arborescences dev/prod, comme les
    modèles Word du module — cf. MODELES_GARDEE_DIR) : déposée une fois,
    elle reste disponible d'une campagne à l'autre et d'une session à
    l'autre, sans avoir à la redéposer chaque année. |None| si absente."""
    for extension in EXTENSIONS_IMAGE_MAIL_AUTORISEES:
        chemin = os.path.join(MODELES_GARDEE_DIR, f"autorisation_produits.{extension}")
        if os.path.exists(chemin):
            return chemin
    return None


def _image_produits_autorisation():
    """Récupère l'affiche « Nous avons besoin de... » (produits souhaités),
    page 2 du PDF de demande d'autorisation — priorité à l'image déposée
    directement dans l'application (cf. enregistrer_image_autorisation, même
    principe que mail_image pour les chauffeurs/équipiers), sans connaissance
    technique requise ; à défaut, repli sur la plus grande image inline du
    modèle Word partagé (ancien mécanisme, conservé pour compatibilité)."""
    chemin_depose = _chemin_image_produits_autorisation()
    if chemin_depose:
        with open(chemin_depose, "rb") as f:
            return f.read()

    source = _modele_gardee("demande_autorisation_collecte.docx")
    if not os.path.exists(source):
        return None
    document = Document(source)
    plus_grande_taille = 0
    plus_grande_image = None
    for shape in document.inline_shapes:
        taille = shape.width * shape.height
        if taille > plus_grande_taille:
            plus_grande_taille = taille
            plus_grande_image = shape
    if plus_grande_image is None:
        return None
    try:
        rid = plus_grande_image._inline.graphic.graphicData.pic.blipFill.blip.embed
        return document.part.rels[rid].target_part.blob
    except (AttributeError, KeyError):
        return None


def _creer_pdf_autorisation(magasin, annee, dossier):
    """Génère la lettre de demande d'autorisation en PDF — c'est ce fichier
    qui est joint au mail (pas le .docx, dont l'ouverture n'est pas garantie
    chez tous les destinataires externes). Aucun convertisseur Word→PDF
    n'étant disponible sur le serveur, la mise en page est reconstruite à la
    main avec reportlab, sur le même principe que _creer_pdf_association :
    le TEXTE suit les valeurs personnalisées ci-dessus, mais la mise en
    page visuelle est fixée dans ce code — à adapter ici si la lettre change
    de structure d'une année à l'autre (au-delà d'un simple changement de
    texte, qui lui ne nécessite que d'éditer le modèle Word)."""
    donnees = _donnees_lettre_autorisation(magasin, annee)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", magasin["nom"]).strip("_").lower() or "magasin"
    chemin = os.path.join(dossier, f"demande_autorisation_{annee}_{slug}.pdf")

    page_width, page_height = A4
    marge = 20 * mm
    pdf = pdf_canvas.Canvas(chemin, pagesize=A4)
    logo = os.path.join(current_app.root_path, "static", "images", "logo_ba_complet.png")

    y = page_height - 18 * mm
    if os.path.exists(logo):
        pdf.drawImage(ImageReader(logo), marge, y - 12 * mm, width=70 * mm, height=12 * mm,
                       preserveAspectRatio=True, mask="auto")
    y -= 22 * mm

    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawCentredString(page_width / 2, y, "COLLECTE NATIONALE DES BANQUES ALIMENTAIRES")
    y -= 12 * mm

    pdf.setFont("Helvetica", 10)
    pdf.drawString(marge, y, f"Fontaine, le {donnees['jour_mail']}")
    y -= 14 * mm

    def paragraphe(texte, taille=10, gras=False, interligne=5 * mm, avant=2 * mm):
        nonlocal y
        y -= avant
        police = "Helvetica-Bold" if gras else "Helvetica"
        pdf.setFont(police, taille)
        largeur_max = page_width - 2 * marge
        ligne = ""
        for mot in texte.split():
            essai = (ligne + " " + mot).strip()
            if pdf.stringWidth(essai, police, taille) > largeur_max:
                pdf.drawString(marge, y, ligne)
                y -= interligne
                ligne = mot
            else:
                ligne = essai
        if ligne:
            pdf.drawString(marge, y, ligne)
            y -= interligne

    def phrase_dates():
        nonlocal y
        y -= 3 * mm
        pdf.setFont("Helvetica-Bold", 11)
        pdf.setFillColor(colors.red)
        pdf.drawCentredString(page_width / 2, y, f"La collecte se déroulera {donnees['phrase_jours']}.")
        pdf.setFillColor(colors.black)
        y -= 10 * mm

    def bloc_centre(alinea, avant):
        nonlocal y
        y -= avant
        pdf.setFont("Helvetica", 10)
        for ligne in alinea.split("\n")[1:]:
            ligne = ligne.strip()
            if ligne:
                pdf.drawCentredString(page_width / 2, y, ligne)
                y -= 5 * mm

    texte_lettre = donnees["texte_lettre"].replace("<<Nom>>", donnees["nom"])
    alineas = texte_lettre.split("\n\n")
    dates_inserees = False
    for i, alinea in enumerate(alineas):
        alinea = alinea.strip()
        if not alinea:
            continue
        if alinea == "<<dates>>":
            phrase_dates()
            dates_inserees = True
        elif alinea.startswith("<<centre>>"):
            bloc_centre(alinea, avant=0 if i == 0 else 6 * mm)
        else:
            paragraphe(alinea, avant=0 if i == 0 else 2 * mm)
    if not dates_inserees:
        phrase_dates()
    y -= 8 * mm

    lignes_coupon = [
        ligne.strip() for ligne in donnees["texte_coupon"]
        .replace("<<Nom>>", donnees["nom"])
        .replace("<<CodeVIF>>", donnees["code_vif"])
        .replace("<<Adresse>>", donnees["adresse"])
        .replace("<<CP>>", donnees["cp"])
        .replace("<<VILLE>>", donnees["ville"])
        .replace("<<Telephone>>", donnees["telephone"])
        .replace("<<Email>>", donnees["email"])
        .split("\n")
        if ligne.strip()
    ]

    espace_pour_signer = 20 * mm if any(l.startswith("Signature du responsable") for l in lignes_coupon) else 0
    hauteur_coupon = len(lignes_coupon) * 8 * mm + 6 * mm + espace_pour_signer
    pdf.rect(marge, y - hauteur_coupon, page_width - 2 * marge, hauteur_coupon)
    y -= 8 * mm
    for ligne in lignes_coupon:
        if ligne == "<<autorisation>>":
            pdf.setFont("Helvetica-Bold", 10)
            x = marge + 3 * mm
            texte = "AUTORISATION DE COLLECTER : OUI"
            pdf.drawString(x, y, texte)
            x_case_oui = x + pdf.stringWidth(texte, "Helvetica-Bold", 10) + 4 * mm
            cote_case = 4.5 * mm
            pdf.rect(x_case_oui, y - 1 * mm, cote_case, cote_case)
            x_non = x_case_oui + cote_case + 18 * mm
            pdf.drawString(x_non, y, "NON")
            x_case_non = x_non + pdf.stringWidth("NON", "Helvetica-Bold", 10) + 4 * mm
            pdf.rect(x_case_non, y - 1 * mm, cote_case, cote_case)
        else:
            gras = ligne.startswith("NOM DU MAGASIN") or ligne.startswith("Signature du responsable")
            pdf.setFont("Helvetica-Bold" if gras else "Helvetica", 10)
            pdf.drawString(marge + 3 * mm, y, ligne)
        y -= 8 * mm

    pdf.setFont("Helvetica", 8)
    pdf.drawCentredString(
        page_width / 2, 12 * mm,
        "Banque Alimentaire de l'Isère - Tel : 04 76 85 92 50 - Courriel : ba380.collecte@banquealimentaire.org",
    )

    image_produits = _image_produits_autorisation()
    if image_produits:
        pdf.showPage()
        lecteur = ImageReader(io.BytesIO(image_produits))
        largeur_image, hauteur_image = lecteur.getSize()
        marge_page2 = 10 * mm
        largeur_max = page_width - 2 * marge_page2
        hauteur_max = page_height - 2 * marge_page2
        echelle = min(largeur_max / largeur_image, hauteur_max / hauteur_image)
        largeur_finale = largeur_image * echelle
        hauteur_finale = hauteur_image * echelle
        pdf.drawImage(
            lecteur,
            (page_width - largeur_finale) / 2, (page_height - hauteur_finale) / 2,
            width=largeur_finale, height=hauteur_finale, preserveAspectRatio=True, mask="auto",
        )

    pdf.save()
    return chemin


@collecte_bp.route("/collecte/<int:annee>/autorisations", methods=["GET", "POST"])
@login_required
@require_access("collecte", "ecriture")
def autorisations(annee):
    """Publipostage de demande d'autorisation de collecter aux magasins
    ayant un email renseigné et pas encore d'accord (colonne 'Accord' du
    référentiel magasins) — lettre PDF personnalisée jointe au mail (cf.
    _creer_pdf_autorisation)."""
    magasins = _lire_magasins_autorisation(annee)
    dossier = _dossier_annee(annee)

    if request.method == "POST":
        if request.form.get("confirmation") != "oui":
            flash("❌ Confirmez le contrôle avant l'envoi", "danger")
            return redirect(url_for("collecte.autorisations", annee=annee))

        mode_test = request.form.get("mode_test") == "on"
        test_un_magasin = request.form.get("test_un_magasin") == "on"
        code_vif_test = request.form.get("magasin_test_code_vif", "")
        codes_selectionnes = set(request.form.getlist("magasins"))

        if test_un_magasin:
            magasins_a_traiter = [m for m in magasins if m["code_vif"] == code_vif_test]
            if not magasins_a_traiter:
                flash("❌ Sélectionnez un magasin pour le test", "danger")
                return redirect(url_for("collecte.autorisations", annee=annee))
        else:
            magasins_a_traiter = [m for m in magasins if m["code_vif"] in codes_selectionnes]
            if not magasins_a_traiter:
                flash("❌ Aucun magasin sélectionné", "danger")
                return redirect(url_for("collecte.autorisations", annee=annee))

        if mode_test and not getattr(current_user, "email", ""):
            flash("❌ Votre compte n'a pas d'adresse email pour le test", "danger")
            return redirect(url_for("collecte.autorisations", annee=annee))

        with get_db_connection() as conn:
            campagne_mail = conn.execute(
                "SELECT autorisation_texte_mail FROM collecte_campagnes WHERE annee = ?", (annee,)
            ).fetchone()
        texte_mail_modele = (campagne_mail["autorisation_texte_mail"] if campagne_mail else None) or AUTORISATION_TEXTE_MAIL_DEFAUT
        texte_mail_modele = texte_mail_modele.replace("<<Annee>>", str(annee))

        with get_db_connection() as conn:
            deja_envoyes_post = {
                r["code_vif"] for r in conn.execute(
                    "SELECT code_vif FROM collecte_demandes_autorisation WHERE annee = ?", (annee,)
                ).fetchall()
            }

        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        envoyes = 0
        deja_notifies = []
        echecs = []
        # « Test sur un seul magasin » reste un moyen délibéré de forcer un
        # renvoi à un magasin précis (ex. il affirme ne jamais l'avoir reçu) —
        # même logique que sans_vendredi(). Hors de ce cas, jamais de second
        # mail à un magasin déjà notifié (retour utilisateur direct,
        # 2026-10-23 : même bug trouvé et corrigé sur non_collecte_benevoles
        # et sans_vendredi, resté non corrigé ici).
        for magasin in magasins_a_traiter:
            if not mode_test and not test_un_magasin and magasin["code_vif"] in deja_envoyes_post:
                deja_notifies.append(magasin["nom"])
                continue
            fichier_lettre = _creer_pdf_autorisation(magasin, annee, dossier)
            destinataires = [current_user.email] if mode_test else magasin["emails"]
            try:
                resultat = envoyer_mail(
                    sujet=("[TEST] " if mode_test else "") + f"Collecte nationale Banque Alimentaire {annee} — Demande d'autorisation — {magasin['nom']}",
                    destinataires=destinataires,
                    texte=texte_mail_modele.replace("<<Nom>>", magasin["nom"]),
                    sender_override="ba380.collecte@banquealimentaire.org",
                    cc=None if mode_test else ["ba380.collecte@banquealimentaire.org"],
                    attachment_path=fichier_lettre,
                )
                _enregistrer_envoi_mail_collecte(annee, "autorisations", destinataires[0] if destinataires else magasin["code_vif"], magasin["nom"], resultat=resultat)
            except Exception as e:
                echecs.append(magasin["nom"])
                write_log(f"❌ Collecte {annee} : échec envoi demande autorisation à {magasin['nom']} — {e}")
                _enregistrer_envoi_mail_collecte(annee, "autorisations", destinataires[0] if destinataires else magasin["code_vif"], magasin["nom"], erreur=str(e))
                continue
            envoyes += 1
            if not mode_test:
                with get_db_connection() as conn:
                    conn.execute("""
                        INSERT INTO collecte_demandes_autorisation (annee, code_vif, envoye_le, envoye_par)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(annee, code_vif)
                        DO UPDATE SET envoye_le = excluded.envoye_le, envoye_par = excluded.envoye_par
                    """, (annee, magasin["code_vif"], maintenant, current_user.email))
                    conn.commit()

        message = f"✅ {envoyes} demande(s) d'autorisation {'de test ' if mode_test else ''}envoyée(s)"
        if echecs:
            message += f" — ⛔ {len(echecs)} échec(s) : {', '.join(echecs)}"
        if deja_notifies:
            message += f" — déjà notifié(s) précédemment, non renvoyé(s) : {', '.join(deja_notifies)}"
        flash(message, "warning" if (deja_notifies or echecs) else "success")
        write_log(f"📧 Envoi demandes autorisation {annee} : {envoyes} envoyé(s) par {current_user.email}")
        return redirect(url_for("collecte.autorisations", annee=annee))

    with get_db_connection() as conn:
        deja_envoyes = {
            r["code_vif"] for r in conn.execute(
                "SELECT code_vif FROM collecte_demandes_autorisation WHERE annee = ?", (annee,)
            ).fetchall()
        }
        campagne = conn.execute(
            "SELECT autorisation_texte_lettre, autorisation_texte_coupon, autorisation_texte_mail "
            "FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    statuts = _statuts_envois_mail_collecte(annee, "autorisations")
    for magasin in magasins:
        magasin["deja_envoye"] = magasin["code_vif"] in deja_envoyes
        premier_email = magasin["emails"][0] if magasin.get("emails") else None
        magasin["statut_mailjet"] = statuts.get(premier_email) if premier_email else None

    image_produits_url = url_for("collecte.autorisations_image") if _chemin_image_produits_autorisation() else None

    return render_template(
        "collecte/autorisations.html",
        annee=annee,
        magasins=magasins,
        texte_lettre=(campagne["autorisation_texte_lettre"] if campagne else None) or AUTORISATION_TEXTE_LETTRE_DEFAUT,
        texte_coupon=(campagne["autorisation_texte_coupon"] if campagne else None) or AUTORISATION_TEXTE_COUPON_DEFAUT,
        texte_mail=(campagne["autorisation_texte_mail"] if campagne else None) or AUTORISATION_TEXTE_MAIL_DEFAUT,
        image_produits_url=image_produits_url,
        feature="autorisations",
    )


@collecte_bp.route("/collecte/autorisations/texte", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_texte_autorisation():
    """Enregistre le texte de la lettre, du coupon-réponse et/ou du corps du
    mail de la demande d'autorisation, personnalisables en ligne comme
    mail_texte pour les chauffeurs/équipiers (cf.
    enregistrer_contenu_mail_chauffeurs_equipiers)."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte_lettre = request.form.get("texte_lettre", "").strip()
    texte_coupon = request.form.get("texte_coupon", "").strip()
    texte_mail = request.form.get("texte_mail", "").strip()
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE collecte_campagnes SET autorisation_texte_lettre = ?, autorisation_texte_coupon = ?, "
            "autorisation_texte_mail = ? WHERE annee = ?",
            (texte_lettre or None, texte_coupon or None, texte_mail or None, annee),
        )
        conn.commit()
    flash("✅ Texte de la lettre enregistré.", "success")
    return redirect(url_for("collecte.autorisations", annee=annee))


@collecte_bp.route("/collecte/autorisations/image", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_image_autorisation():
    """Dépose l'affiche des produits recherchés (2ᵉ page du PDF de demande
    d'autorisation) directement dans l'application — même principe que
    mail_image pour les chauffeurs/équipiers, mais dans l'emplacement
    PARTAGÉ du module (cf. _chemin_image_produits_autorisation) : déposée
    une fois, elle reste utilisée d'une campagne et d'une session à l'autre,
    sans avoir à la redéposer chaque année ni dépendre du modèle Word."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    fichier = request.files.get("image_produits")

    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier sélectionné.", "warning")
        return redirect(url_for("collecte.autorisations", annee=annee))

    extension = fichier.filename.rsplit(".", 1)[-1].lower() if "." in fichier.filename else ""
    if extension not in EXTENSIONS_IMAGE_MAIL_AUTORISEES:
        flash("⛔ Format non accepté (PNG, JPG ou GIF uniquement).", "danger")
        return redirect(url_for("collecte.autorisations", annee=annee))

    ancien_chemin = _chemin_image_produits_autorisation()
    if ancien_chemin and os.path.exists(ancien_chemin):
        os.remove(ancien_chemin)

    fichier.save(os.path.join(MODELES_GARDEE_DIR, f"autorisation_produits.{extension}"))

    flash("✅ Affiche des produits enregistrée.", "success")
    return redirect(url_for("collecte.autorisations", annee=annee))


@collecte_bp.route("/collecte/autorisations/image")
@login_required
@require_access("collecte", "lecture")
def autorisations_image():
    """Sert l'affiche des produits déposée pour la demande d'autorisation
    (aperçu sur la page de gestion) — authentifiée, contrairement à
    chauffeurs_equipiers_image : cette image n'est jamais chargée par un
    client mail, seulement intégrée côté serveur dans le PDF envoyé."""
    chemin = _chemin_image_produits_autorisation()
    if not chemin:
        abort(404)
    return send_file(chemin)


@collecte_bp.route("/collecte/<int:annee>/autorisations/apercu")
@login_required
@require_access("collecte", "lecture")
def autorisations_apercu(annee):
    """Génère et affiche directement dans le navigateur (pas de
    téléchargement) un exemple de la lettre PDF telle qu'elle serait
    envoyée à un magasin — pour contrôler le rendu sans passer par le mode
    test de l'envoi (donc sans consommer d'envoi Mailjet, même de test)."""
    magasins = _lire_magasins_autorisation(annee)
    if not magasins:
        flash("⛔ Aucun magasin disponible pour l'aperçu", "danger")
        return redirect(url_for("collecte.autorisations", annee=annee))
    code_vif = request.args.get("code_vif", "")
    magasin = next((m for m in magasins if m["code_vif"] == code_vif), magasins[0])

    dossier = _dossier_annee(annee)
    chemin = _creer_pdf_autorisation(magasin, annee, dossier)
    with open(chemin, "rb") as f:
        donnees_pdf = f.read()
    os.remove(chemin)

    return send_file(
        io.BytesIO(donnees_pdf),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"apercu_demande_autorisation_{annee}.pdf",
    )


@collecte_bp.route("/collecte/<int:annee>/autorisations/apercu-mail")
@login_required
@require_access("collecte", "lecture")
def autorisations_apercu_mail(annee):
    """Affiche directement dans le navigateur (pas d'envoi, même de test)
    le texte exact du corps du mail pour le magasin sélectionné — distinct
    de l'aperçu PDF (celui-ci montre la lettre jointe, pas le corps du
    mail qui l'accompagne)."""
    magasins = _lire_magasins_autorisation(annee)
    if not magasins:
        flash("⛔ Aucun magasin disponible pour l'aperçu", "danger")
        return redirect(url_for("collecte.autorisations", annee=annee))
    code_vif = request.args.get("code_vif", "")
    magasin = next((m for m in magasins if m["code_vif"] == code_vif), magasins[0])

    with get_db_connection() as conn:
        campagne_mail = conn.execute(
            "SELECT autorisation_texte_mail FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    texte_mail_modele = (campagne_mail["autorisation_texte_mail"] if campagne_mail else None) or AUTORISATION_TEXTE_MAIL_DEFAUT
    texte = texte_mail_modele.replace("<<Annee>>", str(annee)).replace("<<Nom>>", magasin["nom"])
    return Response(texte, mimetype="text/plain; charset=utf-8")


def _lire_magasins_accord_pour_non_collecte(annee):
    """Magasins de liste_magasins.xlsx déjà passés État='Non collecté' (dans
    le fichier, hand-maintenu hors appli) ET ayant donné leur accord
    auparavant (colonne 'Accord' non vide) — population cible de l'action
    « pas de collecte cette année, faute de bénévoles » (cf.
    non_collecte_benevoles) : l'État est basculé à la main par l'équipe
    AVANT d'utiliser cette page, qui ne sert qu'à prévenir par mail les
    magasins concernés, pas à décider lesquels le sont. Contrairement à
    _lire_magasins_autorisation, les magasins SANS email sont inclus (et
    signalés dans le template) : on doit pouvoir les marquer même si le mail
    ne peut pas leur être envoyé."""
    chemin = _fichier_drive(annee, "magasins")
    if not chemin:
        return []

    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    for col in ["Code VIF", "Nom", "État", "Ville", "Email", "Accord"]:
        if col not in df.columns:
            df[col] = ""

    magasins = []
    for _, row in df.iterrows():
        if str(row.get("État", "")).strip() != "Non collecté":
            continue
        accord = row.get("Accord")
        if pd.isna(accord) or str(accord).strip() in ("", "nan"):
            continue
        code_vif = _vif_fmt(row.get("Code VIF"))
        if not code_vif or code_vif.lower() == "nan":
            continue
        magasins.append({
            "code_vif": code_vif,
            "nom": str(row.get("Nom", "")).strip(),
            "etat": str(row.get("État", "")).strip(),
            "ville": str(row.get("Ville", "")).strip(),
            "emails": _emails_magasin(str(row.get("Email", "")).strip()),
        })
    magasins.sort(key=lambda m: m["nom"].lower())
    return magasins


def _ensure_table_magasins_sans_vendredi(conn):
    """Magasins marqués « pas de collecte le vendredi, collecte à partir du
    samedi » — restriction de planning admin, indépendante de la colonne
    'Créneaux' (déclarative, saisie par le magasin lui-même) — cf.
    sans_vendredi() et moteur.optimiser_tournees (args.vifs_sans_vendredi)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_magasins_sans_vendredi (
            annee INTEGER NOT NULL,
            code_vif TEXT NOT NULL,
            marque_le TEXT,
            marque_par TEXT,
            mail_envoye INTEGER DEFAULT 0,
            mail_envoye_le TEXT,
            PRIMARY KEY (annee, code_vif)
        )
    """)
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_magasins_sans_vendredi)").fetchall()}
    if "mail_envoye" not in colonnes:
        conn.execute("ALTER TABLE collecte_magasins_sans_vendredi ADD COLUMN mail_envoye INTEGER DEFAULT 0")
    if "mail_envoye_le" not in colonnes:
        conn.execute("ALTER TABLE collecte_magasins_sans_vendredi ADD COLUMN mail_envoye_le TEXT")


def _ensure_colonne_sans_vendredi_texte_mail(conn):
    """Texte du mail envoyé aux magasins marqués « pas de collecte le
    vendredi » — personnalisable par campagne, même principe que
    non_collecte_texte_mail/autorisation_texte_mail."""
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "sans_vendredi_texte_mail" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN sans_vendredi_texte_mail TEXT")


SANS_VENDREDI_TEXTE_MAIL_DEFAUT = (
    "Madame la Directrice, Monsieur le Directeur de <<Nom>>,\n\n"
    "Dans le cadre de l'organisation de la collecte nationale des Banques Alimentaires <<Annee>>, nous "
    "vous informons que votre magasin ne sera plus collecté le vendredi.\n\n"
    "La collecte sera désormais réalisée à partir du samedi.\n\n"
    "Nous vous remercions de votre compréhension et de votre engagement à nos côtés.\n\n"
    "Cordialement,\n"
    "La Banque Alimentaire de l'Isère"
)


def _lire_magasins_pour_sans_vendredi(annee):
    """Magasins de liste_magasins.xlsx collectés cette année (État != 'Non
    collecté') ET ayant donné leur accord (colonne 'Accord' non vide) —
    population éligible à la restriction « pas de collecte le vendredi »,
    même filtre que _lire_magasins_autorisation côté État mais restreint
    aux magasins ayant déjà un accord (symétrique de
    _lire_magasins_accord_pour_non_collecte, qui cible lui l'État='Non
    collecté'). Pas de présélection automatique : rien dans go-on-web ni
    dans les fichiers (Créneaux déclarés, PDF année précédente) ne reflète
    de façon fiable qui est déjà réellement non collecté le vendredi cette
    année (retour utilisateur direct, 2026-10-15) — colonne 'Commentaire'
    la seule trace, en texte libre non exploitable automatiquement (ex.
    notes contradictoires). Repérage et sélection entièrement manuels,
    l'utilisateur identifiant les magasins concernés via la visualisation
    des magasins."""
    chemin = _fichier_drive(annee, "magasins")
    if not chemin:
        return []

    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    for col in ["Code VIF", "Nom", "État", "Ville", "Email", "Accord", "Commentaire"]:
        if col not in df.columns:
            df[col] = ""

    magasins = []
    for _, row in df.iterrows():
        if str(row.get("État", "")).strip() == "Non collecté":
            continue
        accord = row.get("Accord")
        if pd.isna(accord) or str(accord).strip() in ("", "nan"):
            continue
        code_vif = _vif_fmt(row.get("Code VIF"))
        if not code_vif or code_vif.lower() == "nan":
            continue
        commentaire = str(row.get("Commentaire", "")).strip()
        try:
            accord_fmt = pd.to_datetime(float(accord), unit="D", origin="1899-12-30").strftime("%d/%m/%Y")
        except (TypeError, ValueError):
            accord_fmt = str(accord).strip()
        magasins.append({
            "code_vif": code_vif,
            "nom": str(row.get("Nom", "")).strip(),
            "etat": str(row.get("État", "")).strip(),
            "ville": str(row.get("Ville", "")).strip(),
            "accord": accord_fmt,
            "emails": _emails_magasin(str(row.get("Email", "")).strip()),
            "commentaire": "" if commentaire.lower() == "nan" else commentaire,
        })
    magasins.sort(key=lambda m: m["nom"].lower())
    return magasins


def _vifs_sans_vendredi(annee):
    """Set des code_vif marqués « pas de collecte le vendredi » pour cette
    année (collecte_magasins_sans_vendredi) — pour pré-cocher la page de
    gestion et pour contraindre l'optimiseur (args.vifs_sans_vendredi,
    cf. _generer_tournees)."""
    with get_db_connection() as conn:
        _ensure_table_magasins_sans_vendredi(conn)
        conn.commit()
        return {
            r["code_vif"] for r in conn.execute(
                "SELECT code_vif FROM collecte_magasins_sans_vendredi WHERE annee = ?", (annee,)
            ).fetchall()
        }


def _ensure_table_detection_sans_vendredi(conn):
    """Détections automatiques (upload d'une capture d'écran du planning
    go-on-web, analysée par l'IA) des enseignes dont les 4 cases du
    vendredi sont grises — vient en complément du repérage manuel
    (_lire_magasins_pour_sans_vendredi), jamais à la place : l'utilisateur
    reste seul à valider via la case à cocher avant tout enregistrement
    (cf. sans_vendredi())."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_detection_sans_vendredi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annee INTEGER NOT NULL,
            statut TEXT NOT NULL DEFAULT 'en_cours',
            enseignes_detectees TEXT,
            erreur TEXT,
            lance_le TEXT,
            lance_par TEXT,
            image_nom TEXT
        )
    """)
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_detection_sans_vendredi)").fetchall()}
    if "image_nom" not in colonnes:
        conn.execute("ALTER TABLE collecte_detection_sans_vendredi ADD COLUMN image_nom TEXT")


def _normaliser_nom_enseigne(nom):
    import unicodedata as _ud_ne, re as _re_ne
    n = _ud_ne.normalize('NFD', str(nom)).encode('ascii', 'ignore').decode('ascii')
    return _re_ne.sub(r"[^a-z0-9]+", ' ', n.lower()).strip()


def _detecter_sans_vendredi_background(app, detection_id, annee, image_bytes, media_type):
    """Envoie l'image à l'API Claude (vision) pour repérer les enseignes
    dont le vendredi est entièrement gris sur la capture d'écran go-on-web
    fournie — tourne en arrière-plan, même principe que
    _generer_redaction_background (appel IA potentiellement long)."""
    import base64, json as _json
    with app.app_context():
        try:
            api_key = os.getenv("CLAUDE_API_KEY")
            if not api_key:
                raise RuntimeError("CLAUDE_API_KEY absente du .env")

            prompt = (
                "Cette image est une capture d'écran d'un planning go-on-web de collecte alimentaire. "
                "Chaque ligne correspond à un magasin (colonne « Enseigne »). La colonne « Ven. » "
                "(vendredi) contient de petites cases représentant les créneaux du vendredi — ATTENTION, "
                "le nombre de cases varie d'un magasin à l'autre (1, 2, 3 ou 4 selon ses créneaux propres), "
                "ce n'est PAS toujours 4. Chaque case est soit verte (pleine, sans chiffre visible), soit "
                "grise (avec un petit chiffre dedans), et parfois une case porte un badge rouge ou est "
                "colorée en rose/magenta.\n\n"
                "Identifie les lignes où TOUTES les cases présentes dans la colonne vendredi sont grises "
                "(aucune verte, aucun badge rouge, aucune case rose/magenta) — qu'il y ait 1 seule case ou "
                "plusieurs, le critère est qu'aucune ne soit verte/rouge/rose, pas un nombre précis de "
                "cases. Ignore uniquement les lignes dont le nom n'est pas totalement lisible ou dont la "
                "ligne est coupée en haut/bas de l'image (jamais à cause du nombre de cases vendredi).\n\n"
                "Réponds UNIQUEMENT avec un objet JSON de cette forme exacte, sans aucun texte autour :\n"
                '{"magasins_vendredi_gris": ["Nom enseigne 1", "Nom enseigne 2"]}'
            )

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=8000,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            texte = "".join(bloc.text for bloc in response.content if bloc.type == "text").strip()
            if not texte:
                # Arrivé en prod le 2026-11-24 (détection PDF association,
                # cf. _detecter_pdf_association_background) : avec
                # max_tokens trop bas, la réflexion interne du modèle
                # consommait tout le budget avant d'écrire la réponse —
                # stop_reason='max_tokens', aucun bloc 'text' en retour,
                # json.loads("") plantait avec une erreur illisible
                # ("Expecting value..."). max_tokens remonté à 8000 (prévu
                # large) ; ce garde-fou donne un message clair si ça se
                # reproduisait quand même.
                raise RuntimeError(
                    f"Réponse vide du modèle (stop_reason={response.stop_reason!r}) — "
                    "document probablement trop complexe pour le budget de réflexion, réessayez."
                )
            if texte.startswith("```"):
                texte = texte.strip("`")
                if texte.lower().startswith("json"):
                    texte = texte[4:]
            data = _json.loads(texte)
            noms = [str(n).strip() for n in data.get("magasins_vendredi_gris", []) if str(n).strip()]

            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE collecte_detection_sans_vendredi SET statut = 'termine', enseignes_detectees = ? WHERE id = ?",
                    (_json.dumps(noms, ensure_ascii=False), detection_id)
                )
                conn.commit()
            write_log(f"🤖 Collecte {annee} : détection vendredi gris #{detection_id} terminée ({len(noms)} enseigne(s))")

        except Exception as e:
            write_log(f"❌ Collecte {annee} : erreur détection vendredi gris #{detection_id} — {e}")
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE collecte_detection_sans_vendredi SET statut = 'erreur', erreur = ? WHERE id = ?",
                    (str(e), detection_id)
                )
                conn.commit()


def _ensure_table_detection_pdf_association(conn):
    """Détections automatiques (upload d'un PDF — fiche envoyée à
    l'association, remplie à la main puis scannée — analysé par l'IA) des
    poids par magasin/produit, en complément de la saisie manuelle
    (gardee_saisie). Jamais enregistré directement dans
    collecte_poids_magasins_gardee / collecte_quantites_produits : les
    valeurs détectées ne font que pré-remplir le formulaire, l'utilisateur
    reste seul à valider en cliquant Enregistrer (ou en corrigeant un champ)
    avant qu'elles soient réellement écrites (demande 2026-11-10)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_detection_pdf_association (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annee INTEGER NOT NULL,
            association TEXT NOT NULL,
            statut TEXT NOT NULL DEFAULT 'en_cours',
            magasins_detectes TEXT,
            produits_detectes TEXT,
            erreur TEXT,
            lance_le TEXT,
            lance_par TEXT,
            pdf_nom TEXT
        )
    """)
    # Avertissement si le nom de l'association n'a pas été retrouvé dans le
    # fichier importé (PDF ou Excel) — voir _nom_association_dans_classeur
    # et le prompt de _detecter_pdf_association_background : signale un
    # fichier probablement déposé par erreur sur la mauvaise association,
    # sans bloquer l'import (les valeurs restent à valider avant
    # enregistrement de toute façon).
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_detection_pdf_association)").fetchall()}
    if "avertissement" not in colonnes:
        conn.execute("ALTER TABLE collecte_detection_pdf_association ADD COLUMN avertissement TEXT")


def _detecter_pdf_association_background(app, detection_id, annee, association, pdf_bytes, magasins_connus, produits_connus):
    """Envoie le PDF reçu à l'API Claude (vision/document) pour lire les
    poids écrits à la main sur la fiche renvoyée par l'association — tourne
    en arrière-plan, même principe que _detecter_sans_vendredi_background.
    Les codes/libellés imprimés servent d'ancrage (fournis dans le prompt)
    pour fiabiliser la lecture de la seule colonne manuscrite."""
    import base64, json as _json
    with app.app_context():
        try:
            api_key = os.getenv("CLAUDE_API_KEY")
            if not api_key:
                raise RuntimeError("CLAUDE_API_KEY absente du .env")

            lignes_magasins = "\n".join(f"{m['code_vif']} — {m['nom']}" for m in magasins_connus)
            lignes_produits = "\n".join(f"{p['code']} — {p['libelle']}" for p in produits_connus)
            prompt = (
                "Ce document PDF est une fiche de collecte alimentaire remplie à la main par une "
                "association puis scannée. Il contient 2 pages : une « FICHE PRODUITS » (tableau "
                "avec colonnes Code, Produit, Poids (kg)) et une « FICHE MAGASINS » (tableau avec "
                "colonnes Code VIF, Magasin, Ville, Poids (kg)). Le texte imprimé (codes et "
                "libellés) est fiable ; seule la dernière colonne « Poids (kg) » est remplie à la "
                "main et doit être lue.\n\n"
                "En haut de chaque page, sous le titre « COLLECTE », une ligne imprimée indique le "
                "nom de l'association suivi de son code (ex. « CCAS DOMENE » puis un code) — lis ce "
                "nom d'association EXACTEMENT comme il est imprimé, sans le corriger ni le "
                "compléter.\n\n"
                "Pour t'aider à associer chaque ligne au bon code même si l'écriture est peu "
                "lisible, voici la liste EXACTE des lignes attendues sur chaque fiche :\n\n"
                f"Magasins (FICHE MAGASINS) :\n{lignes_magasins}\n\n"
                f"Produits (FICHE PRODUITS) :\n{lignes_produits}\n\n"
                "Lis les poids écrits à la main en kg EXACTEMENT comme ils sont écrits, décimales "
                "comprises (jusqu'à 2 chiffres après la virgule ou le point, ex. 55,76 ou 36.665 → "
                "36.67) — ne les arrondis JAMAIS toi-même à l'entier le plus proche si une décimale "
                "est visible, même petite. Parfois le séparateur décimal est une virgule. Ignore les "
                "lignes où rien n'est écrit (case vide ou trait). Si un nombre est vraiment "
                "illisible, ne l'inclus PAS dans la réponse plutôt que de deviner.\n\n"
                "Réponds UNIQUEMENT avec un objet JSON de cette forme exacte, sans aucun texte "
                "autour :\n"
                '{"association_imprimee": "<nom imprimé en haut de page, ou chaîne vide si illisible>", '
                '"magasins": {"<code_vif>": <poids_kg>, ...}, "produits": {"<code_produit>": <poids_kg>, ...}}'
            )

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=8000,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "document", "source": {
                            "type": "base64", "media_type": "application/pdf",
                            "data": base64.b64encode(pdf_bytes).decode("ascii"),
                        }},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            texte = "".join(bloc.text for bloc in response.content if bloc.type == "text").strip()
            if not texte:
                # Incident réel 2026-11-24 (détection PDF ASAT, 24 produits +
                # écriture manuscrite) : avec max_tokens=2000, la réflexion
                # interne du modèle consommait tout le budget avant d'écrire
                # la réponse (stop_reason='max_tokens', aucun bloc 'text') —
                # json.loads("") plantait avec "Expecting value: line 1
                # column 1 (char 0)", illisible pour l'utilisateur. Vérifié
                # empiriquement : ce document nécessitait ~3766 tokens de
                # réflexion avant de produire sa réponse JSON ; max_tokens
                # remonté à 8000 pour laisser une marge large. Ce garde-fou
                # donne un message clair si ça se reproduisait quand même.
                raise RuntimeError(
                    f"Réponse vide du modèle (stop_reason={response.stop_reason!r}) — "
                    "document probablement trop complexe pour le budget de réflexion, réessayez."
                )
            if texte.startswith("```"):
                texte = texte.strip("`")
                if texte.lower().startswith("json"):
                    texte = texte[4:]
            data = _json.loads(texte)

            def _poids_detecte_ou_rien(valeur):
                """Arrondi défensif à 2 décimales (cohérent avec le reste du
                module, cf. _lire_resultats_excel_association) --- Claude
                répond normalement déjà un nombre, mais ceci absorbe un
                éventuel surplus de décimales ou une valeur non numérique
                sans faire planter toute la détection pour une seule ligne."""
                try:
                    return round(float(valeur), 2)
                except (TypeError, ValueError):
                    return None

            codes_vif_connus = {m["code_vif"] for m in magasins_connus}
            codes_produits_connus = {p["code"] for p in produits_connus}
            magasins_detectes = {
                str(code): v for code, valeur in (data.get("magasins") or {}).items()
                if str(code) in codes_vif_connus and (v := _poids_detecte_ou_rien(valeur)) is not None
            }
            produits_detectes = {
                str(code): v for code, valeur in (data.get("produits") or {}).items()
                if str(code) in codes_produits_connus and (v := _poids_detecte_ou_rien(valeur)) is not None
            }

            # Signale un PDF probablement déposé sur la mauvaise association
            # (nom imprimé en haut de page différent de celle choisie) — ne
            # bloque rien, les valeurs restent de toute façon à valider avant
            # enregistrement (cf. _nom_association_dans_classeur côté Excel,
            # même principe). Une détection vide ou illisible (scan
            # recadré...) ne déclenche volontairement aucun avertissement,
            # pour éviter un faux positif sur un document par ailleurs
            # correct.
            association_imprimee = str(data.get("association_imprimee") or "").strip()
            avertissement = None
            if association_imprimee and _normaliser_nom_association(association_imprimee) != _normaliser_nom_association(association):
                avertissement = (
                    f"⚠️ Ce PDF semble concerner « {association_imprimee} », différent de "
                    f"« {association} » — vérifiez qu'il s'agit bien du bon fichier avant d'enregistrer."
                )

            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE collecte_detection_pdf_association "
                    "SET statut = 'termine', magasins_detectes = ?, produits_detectes = ?, avertissement = ? WHERE id = ?",
                    (_json.dumps(magasins_detectes, ensure_ascii=False),
                     _json.dumps(produits_detectes, ensure_ascii=False), avertissement, detection_id)
                )
                conn.commit()
            write_log(
                f"🤖 Collecte gardée {annee} ({association}) : détection PDF #{detection_id} terminée "
                + (f"[association imprimée différente : {association_imprimee!r}] " if avertissement else "")
                + f"({len(magasins_detectes)} magasin(s), {len(produits_detectes)} produit(s))"
            )

        except Exception as e:
            write_log(f"❌ Collecte gardée {annee} ({association}) : erreur détection PDF #{detection_id} — {e}")
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE collecte_detection_pdf_association SET statut = 'erreur', erreur = ? WHERE id = ?",
                    (str(e), detection_id)
                )
                conn.commit()


def _ensure_table_non_collectes_benevoles(conn):
    """Magasins marqués « pas de collecte cette année, faute de bénévoles »
    malgré leur accord donné (colonne 'Accord' du référentiel magasins,
    hand-maintenue hors appli, jamais modifiée par l'application elle-même)
    — cf. non_collecte_benevoles()."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_magasins_non_collectes (
            annee INTEGER NOT NULL,
            code_vif TEXT NOT NULL,
            marque_le TEXT,
            marque_par TEXT,
            mail_envoye INTEGER DEFAULT 0,
            mail_envoye_le TEXT,
            PRIMARY KEY (annee, code_vif)
        )
    """)


def _ensure_colonne_non_collecte_texte_mail(conn):
    """Texte du mail envoyé aux magasins marqués 'pas de collecte, faute de
    bénévoles' — personnalisable par campagne, même principe que les autres
    textes de campagne (ex. autorisation_texte_mail)."""
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "non_collecte_texte_mail" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN non_collecte_texte_mail TEXT")


NON_COLLECTE_TEXTE_MAIL_DEFAUT = (
    "Madame la Directrice, Monsieur le Directeur de <<Nom>>,\n\n"
    "Nous vous remercions d'avoir donné votre accord pour participer à la collecte nationale "
    "des Banques Alimentaires <<Annee>>.\n\n"
    "Faute de bénévoles disponibles pour l'organiser cette année dans votre magasin, nous ne "
    "pourrons malheureusement pas y réaliser la collecte.\n\n"
    "Nous espérons pouvoir de nouveau compter sur votre accueil l'année prochaine, et vous "
    "prions de nous excuser pour la gêne occasionnée.\n\n"
    "Cordialement,\n"
    "La Banque Alimentaire de l'Isère"
)


@collecte_bp.route("/collecte/<int:annee>/non-collecte-benevoles", methods=["GET", "POST"])
@login_required
@require_access("collecte", "ecriture")
def non_collecte_benevoles(annee):
    """Marque les magasins déjà passés État='Non collecté' (et ayant donné
    leur accord auparavant) comme « pas de collecte cette année, faute de
    bénévoles » (collecte_magasins_non_collectes) et leur envoie un mail —
    les magasins sans email sont inclus dans la liste mais signalés, le mail
    ne pouvant pas leur être envoyé (cf. _lire_magasins_accord_pour_non_collecte)."""
    magasins = _lire_magasins_accord_pour_non_collecte(annee)

    if request.method == "POST":
        if request.form.get("confirmation") != "oui":
            flash("❌ Confirmez le contrôle avant l'envoi", "danger")
            return redirect(url_for("collecte.non_collecte_benevoles", annee=annee))

        mode_test = request.form.get("mode_test") == "on"
        test_un_magasin = request.form.get("test_un_magasin") == "on"
        code_vif_test = request.form.get("magasin_test_code_vif", "")
        codes_selectionnes = set(request.form.getlist("magasins"))

        if test_un_magasin:
            magasins_a_traiter = [m for m in magasins if m["code_vif"] == code_vif_test]
            if not magasins_a_traiter:
                flash("❌ Sélectionnez un magasin pour le test", "danger")
                return redirect(url_for("collecte.non_collecte_benevoles", annee=annee))
        else:
            magasins_a_traiter = [m for m in magasins if m["code_vif"] in codes_selectionnes]
            if not magasins_a_traiter:
                flash("❌ Aucun magasin sélectionné", "danger")
                return redirect(url_for("collecte.non_collecte_benevoles", annee=annee))

        if mode_test and not getattr(current_user, "email", ""):
            flash("❌ Votre compte n'a pas d'adresse email pour le test", "danger")
            return redirect(url_for("collecte.non_collecte_benevoles", annee=annee))

        with get_db_connection() as conn:
            _ensure_colonne_non_collecte_texte_mail(conn)
            conn.commit()
            campagne_mail = conn.execute(
                "SELECT non_collecte_texte_mail FROM collecte_campagnes WHERE annee = ?", (annee,)
            ).fetchone()
        texte_mail_modele = (campagne_mail["non_collecte_texte_mail"] if campagne_mail else None) or NON_COLLECTE_TEXTE_MAIL_DEFAUT
        texte_mail_modele = texte_mail_modele.replace("<<Annee>>", str(annee))

        with get_db_connection() as conn:
            _ensure_table_non_collectes_benevoles(conn)
            conn.commit()
            deja_mail_envoye = {
                r["code_vif"] for r in conn.execute(
                    "SELECT code_vif FROM collecte_magasins_non_collectes WHERE annee = ? AND mail_envoye = 1",
                    (annee,)
                ).fetchall()
            }

        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        marques = 0
        envoyes = 0
        sans_email = []
        deja_notifies = []
        for magasin in magasins_a_traiter:
            # « Test sur un seul magasin » reste un moyen délibéré de forcer
            # un renvoi à un magasin précis (ex. il affirme ne jamais l'avoir
            # reçu) — même logique que sans_vendredi(). Hors de ce cas,
            # jamais de second mail à un magasin déjà notifié (sinon un
            # simple réenregistrement/reclic renvoie à tout le monde à
            # chaque fois — retour utilisateur direct, 2026-10-23).
            if not mode_test and not test_un_magasin and magasin["code_vif"] in deja_mail_envoye:
                deja_notifies.append(magasin["nom"])
                continue
            a_un_email = bool(magasin["emails"])
            if a_un_email:
                destinataires = [current_user.email] if mode_test else magasin["emails"]
                try:
                    resultat = envoyer_mail(
                        sujet=("[TEST] " if mode_test else "") + f"Collecte nationale Banque Alimentaire {annee} — Pas de collecte cette année — {magasin['nom']}",
                        destinataires=destinataires,
                        texte=texte_mail_modele.replace("<<Nom>>", magasin["nom"]),
                        sender_override="ba380.collecte@banquealimentaire.org",
                        cc=None if mode_test else ["ba380.collecte@banquealimentaire.org"],
                    )
                    _enregistrer_envoi_mail_collecte(annee, "non_collecte_benevoles", destinataires[0], magasin["nom"], resultat=resultat)
                    envoyes += 1
                except Exception as e:
                    write_log(f"❌ Collecte {annee} : échec envoi non-collecte bénévoles à {magasin['nom']} — {e}")
                    _enregistrer_envoi_mail_collecte(annee, "non_collecte_benevoles", destinataires[0], magasin["nom"], erreur=str(e))
            else:
                sans_email.append(magasin["nom"])

            if not mode_test:
                with get_db_connection() as conn:
                    _ensure_table_non_collectes_benevoles(conn)
                    conn.execute("""
                        INSERT INTO collecte_magasins_non_collectes
                            (annee, code_vif, marque_le, marque_par, mail_envoye, mail_envoye_le)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(annee, code_vif) DO UPDATE SET
                            marque_le = excluded.marque_le, marque_par = excluded.marque_par,
                            mail_envoye = excluded.mail_envoye, mail_envoye_le = excluded.mail_envoye_le
                    """, (annee, magasin["code_vif"], maintenant, current_user.email,
                          1 if a_un_email else 0, maintenant if a_un_email else None))
                    conn.commit()
                marques += 1

        message = (
            f"✅ {marques if not mode_test else len(magasins_a_traiter)} magasin(s) marqué(s) "
            f"« pas de collecte » {'(test) ' if mode_test else ''}— {envoyes} mail(s) envoyé(s)"
        )
        if sans_email:
            message += f" — ⚠️ sans email, à contacter autrement : {', '.join(sans_email)}"
        if deja_notifies:
            message += f" — déjà notifié(s) précédemment, non renvoyé(s) : {', '.join(deja_notifies)}"
        flash(message, "warning" if (sans_email or deja_notifies) else "success")
        write_log(f"📭 Marquage non-collecté (faute de bénévoles) {annee} : {marques} magasin(s), {envoyes} mail(s) par {current_user.email}")
        return redirect(url_for("collecte.non_collecte_benevoles", annee=annee))

    with get_db_connection() as conn:
        _ensure_table_non_collectes_benevoles(conn)
        _ensure_colonne_non_collecte_texte_mail(conn)
        conn.commit()
        deja_marques = {
            r["code_vif"]: r for r in conn.execute(
                "SELECT code_vif, mail_envoye FROM collecte_magasins_non_collectes WHERE annee = ?", (annee,)
            ).fetchall()
        }
        campagne = conn.execute(
            "SELECT non_collecte_texte_mail FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    statuts = _statuts_envois_mail_collecte(annee, "non_collecte_benevoles")
    for magasin in magasins:
        deja = deja_marques.get(magasin["code_vif"])
        magasin["deja_marque"] = deja is not None
        magasin["mail_deja_envoye"] = bool(deja and deja["mail_envoye"])
        premier_email = magasin["emails"][0] if magasin.get("emails") else None
        magasin["statut_mailjet"] = statuts.get(premier_email) if premier_email else None

    return render_template(
        "collecte/non_collecte_benevoles.html",
        annee=annee,
        magasins=magasins,
        texte_mail=(campagne["non_collecte_texte_mail"] if campagne else None) or NON_COLLECTE_TEXTE_MAIL_DEFAUT,
        feature="non_collecte_benevoles",
    )


@collecte_bp.route("/collecte/non-collecte-benevoles/texte", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_texte_non_collecte():
    """Enregistre le texte du mail envoyé aux magasins marqués 'pas de
    collecte, faute de bénévoles' — personnalisable en ligne, même principe
    que enregistrer_texte_autorisation."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte_mail = request.form.get("texte_mail", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_non_collecte_texte_mail(conn)
        conn.execute(
            "UPDATE collecte_campagnes SET non_collecte_texte_mail = ? WHERE annee = ?",
            (texte_mail or None, annee),
        )
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.non_collecte_benevoles", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/non-collecte-benevoles/apercu-mail")
@login_required
@require_access("collecte", "lecture")
def non_collecte_apercu_mail(annee):
    """Affiche directement dans le navigateur (pas d'envoi, même de test) le
    texte exact du mail pour le magasin sélectionné — même principe que
    autorisations_apercu_mail."""
    magasins = _lire_magasins_accord_pour_non_collecte(annee)
    if not magasins:
        flash("⛔ Aucun magasin disponible pour l'aperçu", "danger")
        return redirect(url_for("collecte.non_collecte_benevoles", annee=annee))
    code_vif = request.args.get("code_vif", "")
    magasin = next((m for m in magasins if m["code_vif"] == code_vif), magasins[0])

    with get_db_connection() as conn:
        _ensure_colonne_non_collecte_texte_mail(conn)
        conn.commit()
        campagne_mail = conn.execute(
            "SELECT non_collecte_texte_mail FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    texte_mail_modele = (campagne_mail["non_collecte_texte_mail"] if campagne_mail else None) or NON_COLLECTE_TEXTE_MAIL_DEFAUT
    texte = texte_mail_modele.replace("<<Annee>>", str(annee)).replace("<<Nom>>", magasin["nom"])
    return Response(texte, mimetype="text/plain; charset=utf-8")


@collecte_bp.route("/collecte/<int:annee>/sans-vendredi/detecter-image", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def sans_vendredi_detecter_image(annee):
    """Upload d'une capture d'écran du planning go-on-web, analysée par
    l'IA (arrière-plan) pour repérer les enseignes dont le vendredi est
    entièrement gris — vient uniquement pré-cocher la page sans_vendredi(),
    jamais enregistrer directement (cf. _detecter_sans_vendredi_background)."""
    fichier = request.files.get("image")
    if not fichier or not fichier.filename:
        flash("⛔ Aucune image fournie", "warning")
        return redirect(url_for("collecte.sans_vendredi", annee=annee))

    media_type = fichier.mimetype or ""
    if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        flash("⛔ Format d'image non supporté (PNG/JPEG/WEBP/GIF attendu)", "warning")
        return redirect(url_for("collecte.sans_vendredi", annee=annee))

    image_bytes = fichier.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        flash("⛔ Image trop volumineuse (10 Mo max)", "warning")
        return redirect(url_for("collecte.sans_vendredi", annee=annee))

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_table_detection_sans_vendredi(conn)
        cur = conn.execute("""
            INSERT INTO collecte_detection_sans_vendredi (annee, statut, lance_le, lance_par)
            VALUES (?, 'en_cours', ?, ?)
        """, (annee, maintenant, current_user.email))
        detection_id = cur.lastrowid
        conn.commit()

    # Image conservée sur disque (pas seulement le résultat détecté) pour
    # rester visible d'une session à l'autre — relancé à chaque nouvelle
    # analyse, l'ancienne image d'une détection précédente n'est jamais
    # supprimée (historique).
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[media_type]
    dossier_images = _dossier_sans_vendredi(annee)
    os.makedirs(dossier_images, exist_ok=True)
    image_nom = f"detection_{detection_id}{extension}"
    with open(os.path.join(dossier_images, image_nom), "wb") as f:
        f.write(image_bytes)
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE collecte_detection_sans_vendredi SET image_nom = ? WHERE id = ?",
            (image_nom, detection_id)
        )
        conn.commit()

    app_reel = current_app._get_current_object()
    lancer_tache_fond(
        target=_detecter_sans_vendredi_background,
        args=(app_reel, detection_id, annee, image_bytes, media_type),
        nom="Collecte : detection image vendredi gris",
        utilisateur=current_user.email,
    )
    write_log(f"🤖 Collecte {annee} : détection vendredi gris #{detection_id} lancée par {current_user.email}")
    flash("🤖 Analyse de l'image lancée (compter 15 à 30 secondes) — rechargez la page pour voir le résultat", "info")
    return redirect(url_for("collecte.sans_vendredi", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/sans-vendredi/image/<int:detection_id>")
@login_required
@require_access("collecte", "lecture")
def sans_vendredi_image(annee, detection_id):
    """Ressert l'image uploadée pour une détection donnée — conservée sur
    disque pour rester visible d'une session à l'autre (cf.
    sans_vendredi_detecter_image)."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT image_nom FROM collecte_detection_sans_vendredi WHERE id = ? AND annee = ?",
            (detection_id, annee)
        ).fetchone()
    if not row or not row["image_nom"]:
        abort(404)
    chemin = os.path.join(_dossier_sans_vendredi(annee), row["image_nom"])
    if not os.path.exists(chemin):
        abort(404)
    return send_file(chemin)


@collecte_bp.route("/collecte/<int:annee>/sans-vendredi", methods=["GET", "POST"])
@login_required
@require_access("collecte", "ecriture")
def sans_vendredi(annee):
    """Gestion des magasins (collectés cette année, accord donné) marqués
    « pas de collecte le vendredi, collecte à partir du samedi » — simple
    restriction de planning (collecte_magasins_sans_vendredi), prise en
    compte par l'optimiseur de tournées (args.vifs_sans_vendredi, cf.
    _generer_tournees et moteur.optimiser_tournees) : aucun envoi de mail
    ici, juste un marquage interne."""
    magasins = _lire_magasins_pour_sans_vendredi(annee)

    if request.method == "POST":
        if request.form.get("confirmation") != "oui":
            flash("❌ Confirmez le contrôle avant l'envoi", "danger")
            return redirect(url_for("collecte.sans_vendredi", annee=annee))

        mode_test = request.form.get("mode_test") == "on"
        test_un_magasin = request.form.get("test_un_magasin") == "on"
        code_vif_test = request.form.get("magasin_test_code_vif", "")
        codes_coches = set(request.form.getlist("magasins"))
        # Portée réelle de CE formulaire (magasins_affiches, un champ caché par
        # ligne réellement rendue) — PAS la liste complète recalculée ici :
        # si la page affichée était filtrée (vue par défaut après détection
        # image, cf. magasins_affiches plus bas dans cette fonction), un
        # magasin déjà marqué « sans vendredi » mais absent de cette vue
        # filtrée n'a jamais pu être coché/décoché par l'utilisateur — le
        # considérer comme « éligible » ici l'aurait fait retirer à tort
        # (audit 2026-10-28, perte de données réelle). Le marqueur
        # magasins_affiches_actif (toujours présent dès que cette version du
        # template est servie, MÊME si 0 ligne est affichée) distingue ce cas
        # légitime d'un vieux formulaire sans ce champ du tout, seul cas où
        # on retombe sur la liste complète (comportement historique) —
        # sinon "0 ligne affichée" retombait À TORT sur "tous éligibles" et
        # pouvait désinscrire silencieusement tout le monde (audit 2026-10-29).
        if "magasins_affiches_actif" in request.form:
            codes_eligibles = set(request.form.getlist("magasins_affiches"))
        else:
            codes_eligibles = {m["code_vif"] for m in magasins}

        if test_un_magasin:
            magasins_a_mailer = [m for m in magasins if m["code_vif"] == code_vif_test]
            if not magasins_a_mailer:
                flash("❌ Sélectionnez un magasin pour le test", "danger")
                return redirect(url_for("collecte.sans_vendredi", annee=annee))
        else:
            magasins_a_mailer = [m for m in magasins if m["code_vif"] in codes_coches]

        if mode_test and not getattr(current_user, "email", ""):
            flash("❌ Votre compte n'a pas d'adresse email pour le test", "danger")
            return redirect(url_for("collecte.sans_vendredi", annee=annee))

        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        a_ajouter, a_retirer, deja_mail_envoye = set(), set(), set()
        if not mode_test:
            with get_db_connection() as conn:
                _ensure_table_magasins_sans_vendredi(conn)
                lignes_existantes = conn.execute(
                    "SELECT code_vif, mail_envoye FROM collecte_magasins_sans_vendredi WHERE annee = ?", (annee,)
                ).fetchall()
                deja = {r["code_vif"] for r in lignes_existantes}
                deja_mail_envoye = {r["code_vif"] for r in lignes_existantes if r["mail_envoye"]}
                a_ajouter = (codes_coches & codes_eligibles) - deja
                a_retirer = (deja & codes_eligibles) - codes_coches
                for code in a_retirer:
                    conn.execute(
                        "DELETE FROM collecte_magasins_sans_vendredi WHERE annee = ? AND code_vif = ?",
                        (annee, code),
                    )
                conn.commit()

        with get_db_connection() as conn:
            _ensure_colonne_sans_vendredi_texte_mail(conn)
            conn.commit()
            campagne_mail = conn.execute(
                "SELECT sans_vendredi_texte_mail FROM collecte_campagnes WHERE annee = ?", (annee,)
            ).fetchone()
        texte_mail_modele = (campagne_mail["sans_vendredi_texte_mail"] if campagne_mail else None) or SANS_VENDREDI_TEXTE_MAIL_DEFAUT
        texte_mail_modele = texte_mail_modele.replace("<<Annee>>", str(annee))

        # Marquage AVANT l'envoi (hors test) : la mise à jour mail_envoye
        # ci-dessous porte sur une ligne qui doit déjà exister, sinon elle ne
        # fait rien silencieusement (bug trouvé en testant : mail bien
        # envoyé mais mail_envoye resté à 0 faute de ligne à mettre à jour).
        if not mode_test:
            with get_db_connection() as conn:
                for code in a_ajouter:
                    conn.execute("""
                        INSERT INTO collecte_magasins_sans_vendredi (annee, code_vif, marque_le, marque_par)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(annee, code_vif) DO NOTHING
                    """, (annee, code, maintenant, current_user.email))
                conn.commit()

        envoyes = 0
        sans_email = []
        # Hors test : on envoie le mail à tout magasin coché qui ne l'a pas
        # encore reçu (nouvellement marqué OU déjà marqué mais jamais mailé,
        # ex. les marquages d'avant l'existence de cette fonctionnalité
        # d'envoi) — jamais à un magasin déjà notifié (mail_envoye=1), sinon
        # un simple réenregistrement le renverrait à chaque fois (retour
        # utilisateur direct, 2026-10-21 : marquages du 2026-10-15 jamais
        # mailés faute d'avoir existé à l'époque, blocage le premier jugé
        # trop strict).
        cible_envoi = magasins_a_mailer if (mode_test or test_un_magasin) else [m for m in magasins_a_mailer if m["code_vif"] not in deja_mail_envoye]
        for magasin in cible_envoi:
            if not magasin["emails"]:
                sans_email.append(magasin["nom"])
                continue
            destinataires = [current_user.email] if mode_test else magasin["emails"]
            try:
                resultat = envoyer_mail(
                    sujet=("[TEST] " if mode_test else "") + f"Collecte nationale Banque Alimentaire {annee} — Pas de collecte le vendredi — {magasin['nom']}",
                    destinataires=destinataires,
                    texte=texte_mail_modele.replace("<<Nom>>", magasin["nom"]),
                    sender_override="ba380.collecte@banquealimentaire.org",
                    cc=None if mode_test else ["ba380.collecte@banquealimentaire.org"],
                )
                _enregistrer_envoi_mail_collecte(annee, "sans_vendredi", destinataires[0], magasin["nom"], resultat=resultat)
            except Exception as e:
                write_log(f"❌ Collecte {annee} : échec envoi sans-vendredi à {magasin['nom']} — {e}")
                _enregistrer_envoi_mail_collecte(annee, "sans_vendredi", destinataires[0], magasin["nom"], erreur=str(e))
                continue
            envoyes += 1
            if not mode_test:
                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE collecte_magasins_sans_vendredi SET mail_envoye = 1, mail_envoye_le = ? WHERE annee = ? AND code_vif = ?",
                        (maintenant, annee, magasin["code_vif"]),
                    )
                    conn.commit()

        if not mode_test:
            message = f"✅ {len(a_ajouter)} magasin(s) ajouté(s), {len(a_retirer)} retiré(s) — {envoyes} mail(s) envoyé(s)"
        else:
            message = f"✅ Test : {envoyes} mail(s) envoyé(s) à votre adresse, rien n'a été marqué"
        if sans_email:
            message += f" — ⚠️ sans email, à contacter autrement : {', '.join(sans_email)}"
        flash(message, "warning" if sans_email else "success")
        write_log(f"🚫 Sans vendredi {annee} : +{len(a_ajouter)} / -{len(a_retirer)} / {envoyes} mail(s) par {current_user.email}")
        return redirect(url_for("collecte.sans_vendredi", annee=annee))

    with get_db_connection() as conn:
        _ensure_table_magasins_sans_vendredi(conn)
        conn.commit()
        deja_marques = {
            r["code_vif"]: r for r in conn.execute(
                "SELECT code_vif, mail_envoye FROM collecte_magasins_sans_vendredi WHERE annee = ?", (annee,)
            ).fetchall()
        }
    statuts = _statuts_envois_mail_collecte(annee, "sans_vendredi")
    for magasin in magasins:
        deja = deja_marques.get(magasin["code_vif"])
        magasin["deja_marque"] = deja is not None
        magasin["mail_deja_envoye"] = bool(deja and deja["mail_envoye"])
        premier_email = magasin["emails"][0] if magasin.get("emails") else None
        magasin["statut_mailjet"] = statuts.get(premier_email) if premier_email else None

    derniere_detection = None
    with get_db_connection() as conn:
        _ensure_table_detection_sans_vendredi(conn)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM collecte_detection_sans_vendredi WHERE annee = ? ORDER BY id DESC LIMIT 1",
            (annee,)
        ).fetchone()
    if row:
        derniere_detection = dict(row)
        if row["statut"] == "termine" and row["enseignes_detectees"]:
            noms_detectes = json.loads(row["enseignes_detectees"])
            noms_non_matches = []
            for nom_d in noms_detectes:
                nn = _normaliser_nom_enseigne(nom_d)
                trouve = next((m for m in magasins if _normaliser_nom_enseigne(m["nom"]) == nn), None)
                if trouve is None:
                    trouve = next((m for m in magasins
                                   if nn in _normaliser_nom_enseigne(m["nom"])
                                   or _normaliser_nom_enseigne(m["nom"]) in nn), None)
                if trouve is None:
                    # Dernier recours : ressemblance approximative (ex. OCR
                    # ayant lu « Lidl Tigneu » au lieu de « Lidl Tignieu » —
                    # ni égal ni sous-chaîne, mais clairement le même magasin).
                    # Seuil volontairement élevé (0.93) : deux enseignes
                    # homonymes d'une même ville mais de magasins DIFFÉRENTS
                    # (ex. « Satoriz ... Béal » vs « Satoriz ... G.Péri »,
                    # ratio 0.88) ne doivent jamais être confondues — un faux
                    # positif coche le mauvais magasin, pire qu'un magasin
                    # non reconnu laissé à vérifier manuellement (retour
                    # utilisateur direct, 2026-10-21).
                    import difflib as _difflib_sv
                    proches = _difflib_sv.get_close_matches(
                        nn, [_normaliser_nom_enseigne(m["nom"]) for m in magasins], n=1, cutoff=0.93
                    )
                    if proches:
                        trouve = next((m for m in magasins if _normaliser_nom_enseigne(m["nom"]) == proches[0]), None)
                if trouve:
                    trouve["detecte"] = True
                    trouve["detecte_nouveau"] = not trouve["deja_marque"]
                else:
                    noms_non_matches.append(nom_d)
            derniere_detection["noms_non_matches"] = noms_non_matches

    for magasin in magasins:
        magasin.setdefault("detecte", False)
        magasin.setdefault("detecte_nouveau", False)
        magasin["coche"] = magasin["deja_marque"] or magasin["detecte_nouveau"]

    # Après une détection terminée, n'afficher par défaut que les magasins
    # repérés dans l'image (retour utilisateur direct) — le tableau complet
    # reste accessible via ?tous=1, notamment pour gérer les magasins marqués
    # en dehors de toute image (saisie manuelle historique).
    voir_tous = request.args.get("tous") == "1"
    magasins_affiches = magasins
    if derniere_detection and derniere_detection["statut"] == "termine" and not voir_tous:
        magasins_affiches = [m for m in magasins if m["detecte"]]

    with get_db_connection() as conn:
        _ensure_colonne_sans_vendredi_texte_mail(conn)
        conn.commit()
        campagne = conn.execute(
            "SELECT sans_vendredi_texte_mail FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

    return render_template(
        "collecte/sans_vendredi.html", annee=annee, magasins=magasins_affiches,
        nb_magasins_total=len(magasins), voir_tous=voir_tous,
        derniere_detection=derniere_detection,
        texte_mail=(campagne["sans_vendredi_texte_mail"] if campagne else None) or SANS_VENDREDI_TEXTE_MAIL_DEFAUT,
        feature="sans_vendredi",
    )


@collecte_bp.route("/collecte/sans-vendredi/texte", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_texte_sans_vendredi():
    """Enregistre le texte du mail envoyé aux magasins marqués 'pas de
    collecte le vendredi' — même principe que enregistrer_texte_non_collecte."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte_mail = request.form.get("texte_mail", "").strip()
    with get_db_connection() as conn:
        _ensure_colonne_sans_vendredi_texte_mail(conn)
        conn.execute(
            "UPDATE collecte_campagnes SET sans_vendredi_texte_mail = ? WHERE annee = ?",
            (texte_mail or None, annee),
        )
        conn.commit()
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.sans_vendredi", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/sans-vendredi/apercu-mail")
@login_required
@require_access("collecte", "lecture")
def sans_vendredi_apercu_mail(annee):
    """Affiche directement dans le navigateur (pas d'envoi, même de test) le
    texte exact du mail pour le magasin sélectionné — même principe que
    non_collecte_apercu_mail."""
    magasins = _lire_magasins_pour_sans_vendredi(annee)
    if not magasins:
        flash("⛔ Aucun magasin disponible pour l'aperçu", "danger")
        return redirect(url_for("collecte.sans_vendredi", annee=annee))
    code_vif = request.args.get("code_vif", "")
    magasin = next((m for m in magasins if m["code_vif"] == code_vif), magasins[0])

    with get_db_connection() as conn:
        _ensure_colonne_sans_vendredi_texte_mail(conn)
        conn.commit()
        campagne_mail = conn.execute(
            "SELECT sans_vendredi_texte_mail FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    texte_mail_modele = (campagne_mail["sans_vendredi_texte_mail"] if campagne_mail else None) or SANS_VENDREDI_TEXTE_MAIL_DEFAUT
    texte = texte_mail_modele.replace("<<Annee>>", str(annee)).replace("<<Nom>>", magasin["nom"])
    return Response(texte, mimetype="text/plain; charset=utf-8")


# Boîte englobante large autour de l'Isère (+ départements limitrophes), pour
# repérer une adresse mal géocodée ou une coordonnée manifestement fausse
# dans le référentiel (ex. lat/lon inversées, saisie erronée) — pas une
# limite administrative précise, juste un garde-fou de plausibilité.
ZONE_LAT_MIN, ZONE_LAT_MAX = 44.0, 46.3
ZONE_LON_MIN, ZONE_LON_MAX = 4.2, 6.3


def _coords_plausibles(lat, lon):
    return ZONE_LAT_MIN <= lat <= ZONE_LAT_MAX and ZONE_LON_MIN <= lon <= ZONE_LON_MAX


def _charger_magasins_localisation(annee, campagne):
    """Magasins avec leurs coordonnées GPS, pour la carte « Localisation
    magasins et associations » — deux catégories affichées (contrairement au
    périmètre plus étroit de _lire_referentiel_magasins_bai, réservé aux
    tournées BAI) : magasins collectés par la BAI, ET magasins en collecte
    gardée (assurée par une association partenaire, quel que soit le
    Stockage — ici c'est une vue d'ensemble géographique, pas une contrainte
    de tournée camion). Les magasins 'Non collecté' restent exclus. Reflète
    l'état courant du fichier magasins (pas de liste figée ici, contrairement
    aux cagettes : c'est une vue d'ensemble, pas une saisie à préserver dans
    le temps)."""
    chemin = _fichier_drive(annee, "magasins")
    if not chemin:
        return [], []

    df = pd.read_excel(chemin)
    df.columns = [c.strip() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]

    rmap = {}
    for col in df.columns:
        cl = col.lower()
        if "vif" in cl or cl == "code":
            rmap[col] = "Code VIF"
        elif ("nom" in cl or "magasin" in cl) and "fiche" not in cl:
            rmap[col] = "Nom"
        elif "ville" in cl:
            rmap[col] = "Ville"
        elif "lat" in cl:
            rmap[col] = "Latitude"
        elif "lon" in cl:
            rmap[col] = "Longitude"
    df = df.rename(columns=rmap)
    df = df.loc[:, ~df.columns.duplicated()]

    for col in ["Code VIF", "Nom", "État", "Stockage", "Ville", "Latitude", "Longitude"]:
        if col not in df.columns:
            df[col] = ""

    etat = df["État"].astype(str).str.strip()
    mask_bai = etat == "Collecté par la BAI"
    mask_gardee = etat == "Collecte gardée"
    df = df[mask_bai | mask_gardee].reset_index(drop=True)

    magasins = []
    adresses_invalides = []
    for _, row in df.iterrows():
        code_vif = str(row["Code VIF"]).strip()
        nom_magasin = str(row["Nom"]).strip()
        if not code_vif or code_vif.lower() == "nan":
            continue
        ville = str(row["Ville"]).strip()
        try:
            lat, lon = float(row["Latitude"]), float(row["Longitude"])
            if not _coords_plausibles(lat, lon):
                raise ValueError("hors zone")
        except (ValueError, TypeError):
            adresses_invalides.append({"nom": nom_magasin, "ville": ville})
            continue
        categorie = "bai" if str(row["État"]).strip() == "Collecté par la BAI" else "gardee"
        stockage = str(row["Stockage"]).strip()
        magasins.append({
            "nom": nom_magasin, "ville": ville, "categorie": categorie,
            "stockage": "" if stockage.lower() == "nan" else stockage,
            "lat": lat, "lon": lon,
        })
    return magasins, adresses_invalides


def _charger_lignes_cagettes(annee):
    """Une ligne par magasin (une colonne par demi-journée applicable, + un
    total) depuis la liste figée collecte_cagettes_magasins (cf. bouton
    « Initialiser »), complétée avec les cagettes déjà saisies
    (collecte_cagettes) — jamais depuis le fichier magasins courant. Chaque
    ligne porte aussi un indicateur « _appl_<demi-journée> » : demi-journée
    non applicable à ce magasin (fermé ce créneau, valeur grisée dans la
    grille) vs applicable mais pas encore saisie (None), ainsi qu'un
    indicateur « _n1_<demi-journée> » : le nombre de cagettes saisi l'année
    précédente pour le même magasin/créneau (aide à la saisie), ou None si
    rien n'a été saisi ou importé pour cette année-là."""
    with get_db_connection() as conn:
        magasins = conn.execute(
            "SELECT DISTINCT code_vif, nom_magasin FROM collecte_cagettes_magasins "
            "WHERE annee = ? ORDER BY nom_magasin",
            (annee,)
        ).fetchall()
        applicables = {
            (r["code_vif"], r["demi_journee"])
            for r in conn.execute(
                "SELECT code_vif, demi_journee FROM collecte_cagettes_magasins WHERE annee = ?",
                (annee,)
            ).fetchall()
        }
        saisies = {
            (r["code_vif"], r["demi_journee"]): r["nb_cagettes"]
            for r in conn.execute(
                "SELECT code_vif, demi_journee, nb_cagettes FROM collecte_cagettes WHERE annee = ?",
                (annee,)
            ).fetchall()
        }
        saisies_n1 = {
            (r["code_vif"], r["demi_journee"]): r["nb_cagettes"]
            for r in conn.execute(
                "SELECT code_vif, demi_journee, nb_cagettes FROM collecte_cagettes WHERE annee = ?",
                (annee - 1,)
            ).fetchall()
        }

    lignes = []
    for magasin in magasins:
        code_vif = magasin["code_vif"]
        ligne = {"code_vif": code_vif, "nom_magasin": magasin["nom_magasin"], "total": 0}
        for dj in moteur.DEMI_JOURNEES:
            applicable = (code_vif, dj) in applicables
            valeur = saisies.get((code_vif, dj)) if applicable else None
            ligne[dj] = valeur
            ligne["_appl_" + dj] = applicable
            ligne["_n1_" + dj] = saisies_n1.get((code_vif, dj))
            if valeur:
                ligne["total"] += valeur
        lignes.append(ligne)
    return lignes


COLONNES_DJ_RESULTAT_CAGETTES = [
    'Jeudi matin', 'Jeudi Après-midi',
    'Vendredi matin', 'Vendredi Après midi',
    'Samedi matin', 'Samedi Après midi',
    'Dimanche matin',
]


@collecte_bp.route("/collecte/<int:annee>/cagettes/importer-annee-precedente", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def cagettes_importer_annee_precedente(annee):
    """Permet de (re)charger le fichier « Résultat cagettes magasins AAAA »
    d'une année passée (feuille 'résultats', même format que l'export annuel)
    pour alimenter l'indice de comparaison N-1 affiché dans la grille de
    saisie — remplace entièrement les données déjà présentes pour l'année
    choisie plutôt que de les fusionner, afin qu'un nouveau chargement
    corrige proprement une erreur du précédent."""
    fichier = request.files.get("fichier")
    annee_cible = request.form.get("annee_cible", type=int)
    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier sélectionné", "danger")
        return redirect(url_for("collecte.cagettes", annee=annee))
    if not annee_cible:
        flash("⛔ Année cible invalide", "danger")
        return redirect(url_for("collecte.cagettes", annee=annee))

    try:
        wb = load_workbook(fichier, data_only=True)
        ws = wb["résultats"]
        entetes = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        idx = {nom: i for i, nom in enumerate(entetes)}
        manquantes = [c for c in ("Code VIF", "Nom") + tuple(COLONNES_DJ_RESULTAT_CAGETTES) if c not in idx]
        if manquantes:
            flash(f"⛔ Colonne(s) attendue(s) absente(s) de la feuille « résultats » : {', '.join(manquantes)}", "danger")
            return redirect(url_for("collecte.cagettes", annee=annee))

        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M")
        magasins_rows, cagettes_rows = [], []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[idx["Code VIF"]] in (None, "", "nan"):
                continue
            code_vif = _vif_fmt(row[idx["Code VIF"]])
            nom = str(row[idx["Nom"]] or "").strip()
            if not code_vif or not nom:
                continue
            for col in COLONNES_DJ_RESULTAT_CAGETTES:
                dj = _normaliser_jour_cagettes(col)
                valeur = row[idx[col]]
                if valeur is None or valeur == "":
                    continue
                try:
                    nb = round(float(valeur))
                except (ValueError, TypeError):
                    continue
                magasins_rows.append((annee_cible, code_vif, nom, dj))
                cagettes_rows.append((annee_cible, code_vif, dj, nb, maintenant, current_user.email))
    except KeyError:
        flash("⛔ Feuille « résultats » introuvable dans ce fichier", "danger")
        return redirect(url_for("collecte.cagettes", annee=annee))
    except Exception as erreur:
        write_log(f"❌ Import résultat cagettes {annee_cible} : {erreur}")
        flash(f"⛔ Fichier illisible : {erreur}", "danger")
        return redirect(url_for("collecte.cagettes", annee=annee))

    with get_db_connection() as conn:
        conn.execute("DELETE FROM collecte_cagettes_magasins WHERE annee = ?", (annee_cible,))
        conn.execute("DELETE FROM collecte_cagettes WHERE annee = ?", (annee_cible,))
        conn.executemany(
            "INSERT OR IGNORE INTO collecte_cagettes_magasins (annee, code_vif, nom_magasin, demi_journee) "
            "VALUES (?, ?, ?, ?)",
            magasins_rows,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO collecte_cagettes (annee, code_vif, demi_journee, nb_cagettes, saisi_le, saisi_par) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            cagettes_rows,
        )
        conn.commit()

    flash(f"✅ {len(cagettes_rows)} saisie(s) importée(s) pour {annee_cible} (comparaison N-1 disponible)", "success")
    return redirect(url_for("collecte.cagettes", annee=annee))


# ============================================================================
# 🔬 ANALYSE COMPARATIVE 8 SCÉNARIOS (camions-supp 1-4 × max-magasins 4-5)
# ============================================================================
# Indicateurs repris de l'analyse manuelle de référence
# (uploads/collecte_fichiers_source/analyse_simulations_8configs.docx),
# recalculés directement depuis df_t — seuls les tableaux chiffrés sont
# reproduits ici (§1 "Indicateurs de performance" et §2 "Kilométrage par
# demi-journée" du document) ; l'analyse qualitative du document (rôle de
# chaque camion VX, verdicts, recommandation) reste une lecture humaine du
# tableau, pas quelque chose de recalculable de façon fiable.

SCENARIOS_ANALYSE = [(c, m) for m in (4, 5) for c in (1, 2, 3, 4)]
DJ_VS = ['Vendredi Matin', 'Vendredi Apres Midi', 'Samedi Matin', 'Samedi Apres Midi']
DJ_VS_LABELS = {
    'Vendredi Matin': 'Vendredi Matin', 'Vendredi Apres Midi': 'Vendredi Après-Midi',
    'Samedi Matin': 'Samedi Matin', 'Samedi Apres Midi': 'Samedi Après-Midi',
}


def _duree_token_en_minutes(token):
    """'2h17' -> 137, '45min' -> 45."""
    m = re.match(r"(\d+)h(\d{2})", token)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r"(\d+)min", token)
    if m:
        return int(m.group(1))
    return None


def _parse_duree_minutes(duree_str):
    """'45min – 51min (Hors métropole)' -> 48.0 (moyenne des deux bornes).
    Au-delà d'1h, _fourchette_xl() du moteur passe au format 'XhYY' sans
    suffixe 'min' (ex. '2h17 – 2h38 (Métropole)') — les deux formats
    doivent être reconnus."""
    tokens = re.findall(r"\d+h\d{2}|\d+min", str(duree_str))
    valeurs = [v for v in (_duree_token_en_minutes(t) for t in tokens) if v is not None]
    if len(valeurs) >= 2:
        return (valeurs[0] + valeurs[1]) / 2
    if len(valeurs) == 1:
        return valeurs[0]
    return None


def _compter_secteurs(secteur_str):
    """'Grenoble Centre Est | Grenoble Nord' -> 2."""
    s = str(secteur_str).strip()
    if not s or s.lower() == "nan":
        return 0
    return len([p for p in s.split("|") if p.strip()])


def _calculer_indicateurs_scenario(df_t, camions_supp, max_magasins):
    # Colonnes "Magasin N" dynamiques : le moteur ne crée "Magasin 6" (ou plus)
    # que si une tournée de CE scénario atteint réellement ce nombre de
    # magasins (tolérance de surcharge) — un range(1, 7) fixe plante avec
    # "['Magasin 6'] not in index" dès qu'un scénario n'a aucune surcharge
    # aussi élevée (cas rencontré en PROD, absent des données de test DEV).
    cols_mag = sorted(
        (c for c in df_t.columns if re.fullmatch(r"Magasin \d+", c)),
        key=lambda c: int(c.split(" ")[1])
    )

    df = df_t[df_t["Demi-journee"].isin(DJ_VS)].copy()
    df = df[~df["Camion"].astype(str).isin(moteur.VEHICULES_FIGES)]
    df = df[df["Camion"].astype(str).apply(_est_camion_reel)]

    df["_nb_mag"] = df[cols_mag].apply(
        lambda r: sum(1 for v in r if str(v).strip() and str(v).strip().lower() != "nan"), axis=1
    )
    df["_nb_sec"] = df["Secteur"].apply(_compter_secteurs)
    df["_duree_min"] = df["Duree estimee"].apply(_parse_duree_minutes)

    nb_tournees = len(df)
    est_vx = df["Camion"].astype(str).str.startswith("VX")
    surcharges = df[df["_nb_mag"] > max_magasins]

    km_par_dj = {dj: round(float(df[df["Demi-journee"] == dj]["Km estimes"].sum()), 1) for dj in DJ_VS}

    return {
        "camions_supp": camions_supp,
        "max_magasins": max_magasins,
        "nb_tournees": nb_tournees,
        "tournees_2": int((df["_nb_mag"] == 2).sum()),
        "tournees_3": int((df["_nb_mag"] == 3).sum()),
        "tournees_4": int((df["_nb_mag"] == 4).sum()),
        "tournees_5": int((df["_nb_mag"] == 5).sum()),
        "tournees_6plus": int((df["_nb_mag"] >= 6).sum()),
        "surcharges": int(len(surcharges)),
        "surcharges_detail": [
            {"camion": str(r["Camion"]), "demi_journee": str(r["Demi-journee"]), "nb_magasins": int(r["_nb_mag"])}
            for _, r in surcharges.iterrows()
        ],
        "tournees_3_secteurs": int((df["_nb_sec"] == 3).sum()),
        "tournees_plus3_secteurs": int((df["_nb_sec"] > 3).sum()),
        "secteurs_multiples_detail": [
            {"camion": str(r["Camion"]), "demi_journee": str(r["Demi-journee"]),
             "secteurs": str(r["Secteur"]), "nb_magasins": int(r["_nb_mag"])}
            for _, r in df[df["_nb_sec"] >= 3].iterrows()
        ],
        "km_total": round(float(df["Km estimes"].sum()), 1),
        "moy_magasins_tournee": round(float(df["_nb_mag"].mean()), 2) if nb_tournees else 0,
        "camions_vx_crees": int(df.loc[est_vx, "Camion"].nunique()),
        "magasins_dans_vx": int(df.loc[est_vx, "_nb_mag"].sum()),
        "vx_detail": [
            {"camion": str(r["Camion"]), "demi_journee": str(r["Demi-journee"]),
             "secteur": str(r["Secteur"]), "nb_magasins": int(r["_nb_mag"])}
            for _, r in df[est_vx].iterrows()
        ],
        "duree_moy_min": round(float(df["_duree_min"].mean()), 1) if df["_duree_min"].notna().any() else None,
        "km_par_dj": km_par_dj,
    }


def _lancer_analyse_8configs_background(app, analyse_id, pdf_path, magasins_path, params_communs, annee):
    with app.app_context():
        db_path = get_db_path()
        try:
            fiches = moteur.extraire_pdf(pdf_path)

            vifs_pdf_2025 = set()
            for f in fiches:
                for v in f["vif_codes"]:
                    vifs_pdf_2025.add(str(v).lstrip("0"))

            df_mag = moteur.lire_magasins(magasins_path, vifs_pdf_2025, params_communs["poids_nouveaux"], annee)

            # Garde-fou temps : ce thread d'arrière-plan tourne dans le même
            # process que le serveur web (4 Go de RAM, cf. CLAUDE.md), sans
            # timeout/prlimit possible ici (ce n'est pas un subprocess séparé)
            # — on borne au moins la durée totale, pour ne pas laisser
            # tourner indéfiniment 8 optimisations complètes si l'une d'elles
            # dégénère (audit 2026-10-28). Les scénarios déjà calculés sont
            # conservés plutôt que tout perdre.
            BUDGET_ANALYSE_SECONDES = 1200
            debut = time.monotonic()
            resultats = []
            interrompu = False
            for camions_supp, max_magasins in SCENARIOS_ANALYSE:
                if time.monotonic() - debut > BUDGET_ANALYSE_SECONDES:
                    interrompu = True
                    write_log(f"⏱️ Analyse 8 scénarios #{analyse_id} : budget de {BUDGET_ANALYSE_SECONDES}s "
                              f"dépassé après {len(resultats)} scénario(s), arrêt anticipé")
                    break
                args_ns = argparse.Namespace(
                    camions_supp=camions_supp,
                    poids_nouveaux=params_communs["poids_nouveaux"],
                    max_magasins=max_magasins,
                    corriger_mal_places=params_communs["corriger_mal_places"],
                    fusionner_legeres=params_communs["fusionner_legeres"],
                    optimiser_anciens=params_communs["optimiser_anciens"],
                    output=None, pdf=pdf_path, magasins=magasins_path, nouveaux=None,
                    annee=annee,
                )
                df_t, _ = moteur.optimiser_tournees(copy.deepcopy(fiches), df_mag.copy(), args_ns)
                resultats.append(_calculer_indicateurs_scenario(df_t, camions_supp, max_magasins))
                write_log(f"🔬 Analyse 8 scénarios #{analyse_id} : VX{camions_supp}-MAX{max_magasins} terminé")

            conn = sqlite3.connect(db_path)
            if interrompu:
                conn.execute("""
                    UPDATE collecte_analyses SET statut = 'erreur', resultat = ?, erreur = ?, termine_le = ?
                    WHERE id = ?
                """, (
                    json.dumps(resultats),
                    f"Interrompu après {len(resultats)}/{len(SCENARIOS_ANALYSE)} scénario(s) "
                    f"(budget de {BUDGET_ANALYSE_SECONDES}s dépassé)",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), analyse_id,
                ))
            else:
                conn.execute("""
                    UPDATE collecte_analyses SET statut = 'termine', resultat = ?, termine_le = ?
                    WHERE id = ?
                """, (json.dumps(resultats), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), analyse_id))
            conn.commit()
            conn.close()
            write_log(f"✅ Analyse 8 scénarios #{analyse_id} terminée" if not interrompu else
                      f"⚠️ Analyse 8 scénarios #{analyse_id} interrompue (budget dépassé)")

        except Exception as e:
            write_log(f"❌ Analyse 8 scénarios #{analyse_id} en échec : {e}")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE collecte_analyses SET statut = 'erreur', erreur = ?, termine_le = ? WHERE id = ?",
                (str(e), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), analyse_id)
            )
            conn.commit()
            conn.close()


DOCUMENTS_AIDE_COLLECTE = {
    "aide": {
        "docx": "Documentation_Utilisateur_Module_Collecte_BA38.docx",
        "html": "Documentation_Utilisateur_Module_Collecte_BA38.html",
        "label": "Aide utilisateur — Module Collecte",
    },
    "manuel": {
        "docx": "Manuel_Utilisation_Collecte_BA38.docx",
        "html": "Manuel_Utilisation_Collecte_BA38.html",
        "label": "Manuel d'utilisation détaillé — Module Collecte",
    },
    "regles_optimisation": {
        "docx": "Regles_Fonctionnelles_Optimisation_Simulation_Collecte_BA38.docx",
        "html": None,
        "label": "Règles fonctionnelles d'optimisation — Simulation des tournées",
    },
}


def _decouper_html_par_h1(chemin):
    """Découpe un document HTML (converti depuis Word par
    convert_doc_to_html.py) en un fragment par titre de niveau 1, sous la
    clé "N" pour un titre commençant par "N." (ex. "2. Gestion magasins"
    -> clé "2"), ou sous son texte exact sinon (ex. "Introduction",
    "Table des matières"). Tout ce qui précède le premier titre est sous
    la clé "intro". Ne modifie jamais les images intégrées (déjà réécrites
    en URL statique par le script de conversion)."""
    if not os.path.exists(chemin):
        return {}
    with open(chemin, "r", encoding="utf-8") as f:
        contenu = f.read()

    racine = _lxml_html.fromstring(f"<div>{contenu}</div>")
    sections = {}
    cle_courante = "intro"
    elements_courants = []

    def serialiser(elements):
        return "".join(_lxml_html.tostring(e, encoding="unicode") for e in elements)

    for element in racine.iterchildren():
        if element.tag == "h1":
            sections[cle_courante] = serialiser(elements_courants)
            correspondance = re.match(r"(\d+)\.", element.text_content().strip())
            cle_courante = correspondance.group(1) if correspondance else element.text_content().strip()
            elements_courants = [element]
        else:
            elements_courants.append(element)
    sections[cle_courante] = serialiser(elements_courants)
    return sections


def _decouper_html_par_h2(fragment_html):
    """Comme _decouper_html_par_h1, mais découpe un fragment déjà extrait
    (donc sans <h1> à l'intérieur) sur ses titres de niveau 2, sous la clé
    "N" pour un titre "10.N ..." (dernier chiffre après le point), ou sous
    son texte exact sinon. Utilisé pour répartir la section « 10. Règles
    fonctionnelles détaillées » (numérotée 10.1 à 10.5) vers les 5 mêmes
    clés que le reste du manuel, plutôt que de rester isolée sous la clé
    "10" où aucune modale ne l'afficherait."""
    if not fragment_html:
        return {}
    racine = _lxml_html.fromstring(f"<div>{fragment_html}</div>")
    sections = {}
    cle_courante = "intro"
    elements_courants = []

    def serialiser(elements):
        return "".join(_lxml_html.tostring(e, encoding="unicode") for e in elements)

    for element in racine.iterchildren():
        if element.tag == "h2":
            sections[cle_courante] = serialiser(elements_courants)
            correspondance = re.match(r"10\.(\d+)", element.text_content().strip())
            cle_courante = correspondance.group(1) if correspondance else element.text_content().strip()
            elements_courants = [element]
        else:
            elements_courants.append(element)
    sections[cle_courante] = serialiser(elements_courants)
    return sections


def _sections_aide_collecte():
    """Aide utilisateur du module Collecte, découpée par sujet (clés "1" à
    "6", une par section de la page d'accueil) — la clé "intro" regroupe la
    présentation générale, l'accès, la légende des couleurs et les bonnes
    pratiques (§7), pour n'afficher qu'une présentation générale sur la
    page d'accueil et l'aide propre à chaque sujet dans ce sujet."""
    chemin = os.path.join(
        current_app.root_path, "templates", "docsHtml", DOCUMENTS_AIDE_COLLECTE["aide"]["html"]
    )
    brut = _decouper_html_par_h1(chemin)
    return {
        "intro": brut.get("intro", "") + brut.get("7", ""),
        "1": brut.get("1", ""),
        "2": brut.get("2", ""),
        "3": brut.get("3", ""),
        "4": brut.get("4", ""),
        "5": brut.get("5", ""),
        "6": brut.get("6", ""),
    }


def _sections_manuel_collecte():
    """Manuel d'utilisation détaillé, découpé de la même façon que l'aide
    (cf. _sections_aide_collecte) — la clé "intro" regroupe ici
    l'introduction générale, le déroulé type d'une campagne (§7) et le
    glossaire (§9) ; les bonnes pratiques (§8) ne sont pas reprises ici
    (déjà dans l'aide générale, pour éviter la redite). La section
    « 10. Règles fonctionnelles détaillées » (numérotée 10.1 à 10.5) est
    répartie dans les mêmes clés "1" à "5" que le reste du manuel — sans
    quoi elle resterait isolée sous la clé "10", jamais affichée nulle
    part (cf. _decouper_html_par_h2)."""
    chemin = os.path.join(
        current_app.root_path, "templates", "docsHtml", DOCUMENTS_AIDE_COLLECTE["manuel"]["html"]
    )
    brut = _decouper_html_par_h1(chemin)
    regles = _decouper_html_par_h2(brut.get("10", ""))
    return {
        "intro": brut.get("Introduction", "") + brut.get("7", "") + brut.get("9", ""),
        "1": brut.get("1", "") + regles.get("1", ""),
        "2": brut.get("2", "") + regles.get("2", ""),
        "3": brut.get("3", "") + regles.get("3", ""),
        "4": brut.get("4", "") + regles.get("4", ""),
        "5": brut.get("5", "") + regles.get("5", ""),
        "6": brut.get("6", ""),
    }


@collecte_bp.route("/collecte/documentation/<cle>")
@login_required
@require_access("collecte", "lecture")
def telecharger_documentation_collecte(cle):
    """Télécharge l'un des deux documents Word source de l'aide du module
    (liste blanche fermée — jamais de chemin construit depuis l'entrée
    utilisateur)."""
    info = DOCUMENTS_AIDE_COLLECTE.get(cle)
    if not info:
        abort(404)
    chemin = os.path.join("/srv/ba38/documentation_utilisateur/docs", info["docx"])
    if not os.path.exists(chemin):
        abort(404)
    return send_file(
        chemin,
        as_attachment=True,
        download_name=info["docx"],
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@collecte_bp.route("/collecte")
@login_required
@require_access("collecte", "lecture")
def collecte_main():
    annee = request.args.get("annee", type=int) or datetime.now().year

    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
        campagne_precedente = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee - 1,)
        ).fetchone()

        annees_existantes = [
            row["annee"] for row in conn.execute(
                "SELECT annee FROM collecte_campagnes ORDER BY annee DESC"
            ).fetchall()
        ]

        generations = conn.execute(
            "SELECT * FROM collecte_generations WHERE annee = ? ORDER BY id DESC", (annee,)
        ).fetchall()

        derniere_analyse = conn.execute(
            "SELECT * FROM collecte_analyses WHERE annee = ? ORDER BY id DESC LIMIT 1", (annee,)
        ).fetchone()

    annee_now = datetime.now().year
    if annee_now not in annees_existantes and annee_now not in (annee,):
        annees_existantes = sorted(set(annees_existantes) | {annee_now}, reverse=True)
    if annee not in annees_existantes:
        annees_existantes = sorted(set(annees_existantes) | {annee}, reverse=True)

    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        mercuriale_annees_importees = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT annee FROM collecte_palox_mercuriale WHERE annee IN (?, ?)",
                (annee, annee - 1),
            ).fetchall()
        }
        mercuriale_imports = {
            r["annee"]: dict(r) for r in conn.execute(
                "SELECT * FROM collecte_palox_mercuriale_import WHERE annee IN (?, ?)",
                (annee, annee - 1),
            ).fetchall()
        }

    # La disponibilité des fichiers Drive (magasins, groupes...) n'est plus
    # vérifiée ici : ça demandait jusqu'à 12 recherches Drive à chaque
    # ouverture de cette page, pour un tableau replié par défaut que
    # personne ne regarde à chaque fois. Chaque action (génération,
    # localisation, cagettes...) vérifie déjà elle-même au moment où elle a
    # réellement besoin du fichier (voir _fichier_drive) — la fraîcheur de
    # la donnée utilisée n'y perd rien, seul l'indicateur visuel préventif
    # disparaît. Le tableau de la section Paramètres est maintenant chargé
    # à la demande (voir fichiers_drive_statut ci-dessous).

    return render_template(
        "collecte/index.html",
        annee=annee,
        campagne=campagne,
        campagne_precedente=campagne_precedente,
        annees_existantes=annees_existantes,
        fichiers=FICHIERS,
        derniere_generation=generations[0] if generations else None,
        nb_generations=len(generations),
        derniere_analyse=derniere_analyse,
        aide=_sections_aide_collecte(),
        manuel=_sections_manuel_collecte(),
        mercuriale_importee_annee=annee in mercuriale_annees_importees,
        mercuriale_importee_annee_precedente=(annee - 1) in mercuriale_annees_importees,
        mercuriale_import_annee=mercuriale_imports.get(annee),
        mercuriale_import_annee_precedente=mercuriale_imports.get(annee - 1),
        verrou_bai=_resultat_bai_verrouille(annee),
        verrou_gardee=_resultat_gardee_verrouille(annee),
        info_fichier_n1=_info_fichier_gardee_annee_precedente(annee),
    )


@collecte_bp.route("/collecte/fichiers-drive-statut")
@login_required
@require_access("collecte", "lecture")
def fichiers_drive_statut():
    """Fragment HTML chargé à la demande (JS, quand la section « Paramètres
    de la collecte annuelle » est dépliée) listant le chemin et la date de
    chaque fichier Drive utilisé — jamais calculé au chargement de la page
    principale (voir collecte_main)."""
    annee = request.args.get("annee", type=int) or datetime.now().year
    return render_template(
        "collecte/_fichiers_drive_statut.html",
        annee=annee,
        fichiers_drive_utilises=_fichiers_drive_collecte_utilises(annee),
        fichiers_drive_utilises_precedente=_fichiers_drive_collecte_utilises(annee - 1),
    )


@collecte_bp.route("/collecte/chauffeurs_equipiers", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def chauffeurs_equipiers():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    try:
        destinataires, personnes_sans_email = _charger_affectations_chauffeurs_equipiers(annee)
        mail_texte = _contenu_mail_affectations(annee)
    except Exception as erreur:
        flash(f"❌ Impossible de charger les affectations : {erreur}", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    image_url = (
        url_for("collecte.chauffeurs_equipiers_image", annee=annee, _external=True)
        if _chemin_image_mail_chauffeurs_equipiers() else None
    )
    pieces_jointes = _pieces_jointes_mail()
    chemins_pj = [pj["chemin"] for pj in pieces_jointes]

    if request.method == "POST":
        action = request.form.get("action")

        if not destinataires:
            flash("❌ Aucun chauffeur ou équipier avec une adresse email.", "danger")
        elif action == "test":
            email_test = request.form.get("test_destinataire", "")
            exemple = next((personne for personne in destinataires if personne["email"] == email_test), destinataires[0])
            adresse_test = getattr(current_user, "email", "") or ""
            if not adresse_test:
                flash("❌ Votre compte n’a pas d’adresse email pour le test.", "danger")
            else:
                envoyer_mail(
                    f"[TEST] Affectations tournées {annee}",
                    [adresse_test],
                    _corps_mail_affectations_html(exemple, mail_texte, image_url),
                    sender_override="ba380.directeur@banquealimentaire.org",
                    attachment_paths=chemins_pj or None,
                    is_html=True,
                )
                flash(f"✅ Mail de test envoyé à {adresse_test}.", "success")
        elif action == "envoyer":
            if request.form.get("confirmation_envoi") != "oui":
                flash("❌ Cochez d'abord « Je confirme l'envoi » — rien n'a été envoyé.", "danger")
            else:
                echecs = []
                for personne in destinataires:
                    try:
                        resultat = envoyer_mail(
                            f"Vos affectations tournées {annee}",
                            [personne["email"]],
                            _corps_mail_affectations_html(personne, mail_texte, image_url),
                            sender_override="ba380.directeur@banquealimentaire.org",
                            attachment_paths=chemins_pj or None,
                            is_html=True,
                        )
                        _enregistrer_envoi_mail_collecte(annee, "affectations_tournees", personne["email"], personne.get("nom", ""), resultat=resultat)
                    except Exception as e:
                        echecs.append(personne.get("email", ""))
                        write_log(f"❌ Collecte {annee} : échec envoi mail affectations à {personne.get('email','')} — {e}")
                        _enregistrer_envoi_mail_collecte(annee, "affectations_tournees", personne.get("email", ""), personne.get("nom", ""), erreur=str(e))
                reussis = len(destinataires) - len(echecs)
                if echecs:
                    flash(f"⚠️ {reussis} mail(s) envoyé(s), {len(echecs)} échec(s) : " + ", ".join(echecs), "warning")
                else:
                    flash(f"✅ {reussis} mail(s) préparé(s).", "success")

    statuts = _statuts_envois_mail_collecte(annee, "affectations_tournees")
    for p in destinataires:
        p["statut_mailjet"] = statuts.get(p["email"])

    return render_template(
        "collecte/chauffeurs_equipiers.html",
        annee=annee,
        destinataires=destinataires,
        personnes_sans_email=personnes_sans_email,
        mail_texte=mail_texte,
        image_url=image_url,
        pieces_jointes=pieces_jointes,
        feature="affectations_tournees",
    )


@collecte_bp.route("/collecte/<int:annee>/chauffeurs_equipiers/apercu")
@login_required
@require_access("collecte", "lecture")
def chauffeurs_equipiers_apercu(annee):
    """Affiche directement dans le navigateur (pas d'envoi, même de test) le
    rendu HTML exact du mail pour la personne sélectionnée — même principe
    que l'aperçu PDF de la demande d'autorisation de collecter."""
    destinataires, _ = _charger_affectations_chauffeurs_equipiers(annee)
    if not destinataires:
        abort(404)
    mail_texte = _contenu_mail_affectations(annee)
    image_url = (
        url_for("collecte.chauffeurs_equipiers_image", annee=annee, _external=True)
        if _chemin_image_mail_chauffeurs_equipiers() else None
    )
    email = request.args.get("email", "")
    personne = next((p for p in destinataires if p["email"] == email), destinataires[0])
    html = _corps_mail_affectations_html(personne, mail_texte, image_url)
    return Response(html, mimetype="text/html")


@collecte_bp.route("/collecte/chauffeurs_equipiers/contenu-mail", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_contenu_mail_chauffeurs_equipiers():
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("mail_texte", "").strip()
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE collecte_campagnes SET mail_texte = ? WHERE annee = ?",
            (texte, annee),
        )
    flash("✅ Texte du mail enregistré.", "success")
    return redirect(url_for("collecte.chauffeurs_equipiers", annee=annee))


EXTENSIONS_IMAGE_MAIL_AUTORISEES = {"png", "jpg", "jpeg", "gif"}


@collecte_bp.route("/collecte/chauffeurs_equipiers/image", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_image_mail_chauffeurs_equipiers():
    """Dépose l'image (ex. plan de parking) insérable dans le mail
    chauffeurs/équipiers via le marqueur <<image>> — dans l'emplacement
    PARTAGÉ du module (comme l'affiche des produits de la demande
    d'autorisation) : déposée une fois, elle reste disponible d'une
    campagne et d'une session à l'autre, servie ensuite par une route
    publique (chauffeurs_equipiers_image) car les clients mail chargent les
    images sans être authentifiés."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    fichier = request.files.get("mail_image")

    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier sélectionné.", "warning")
        return redirect(url_for("collecte.chauffeurs_equipiers", annee=annee))

    extension = fichier.filename.rsplit(".", 1)[-1].lower() if "." in fichier.filename else ""
    if extension not in EXTENSIONS_IMAGE_MAIL_AUTORISEES:
        flash("⛔ Format non accepté (PNG, JPG ou GIF uniquement).", "danger")
        return redirect(url_for("collecte.chauffeurs_equipiers", annee=annee))

    ancien_chemin = _chemin_image_mail_chauffeurs_equipiers()
    if ancien_chemin and os.path.exists(ancien_chemin):
        os.remove(ancien_chemin)

    fichier.save(os.path.join(MODELES_GARDEE_DIR, f"mail_chauffeurs_equipiers.{extension}"))

    flash("✅ Image enregistrée — insérez-la dans le texte avec le marqueur <<image>>.", "success")
    return redirect(url_for("collecte.chauffeurs_equipiers", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/chauffeurs_equipiers/image")
def chauffeurs_equipiers_image(annee):
    """Sert l'image du mail chauffeurs/équipiers — volontairement PUBLIQUE
    (pas de @login_required) : un client mail charge les images sans
    authentification. Contenu non sensible (plan de parking, consignes).
    Le paramètre annee est conservé dans l'URL pour ne pas invalider les
    liens déjà présents dans des mails déjà envoyés, mais l'image elle-même
    est désormais partagée entre toutes les campagnes."""
    chemin = _chemin_image_mail_chauffeurs_equipiers()
    if not chemin:
        abort(404)
    return send_file(chemin)


@collecte_bp.route("/collecte/chauffeurs_equipiers/pieces_jointes", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def ajouter_piece_jointe_mail_chauffeurs_equipiers():
    """Ajoute une ou plusieurs pièces jointes PDF persistantes, dans
    l'emplacement PARTAGÉ du module (conservées d'une campagne et d'une
    session à l'autre, jointes automatiquement à chaque test/envoi) — à la
    différence de l'image, plusieurs fichiers peuvent coexister."""
    annee = request.form.get("annee", type=int) or datetime.now().year

    ajoutes = 0
    for fichier in request.files.getlist("pieces_jointes"):
        if not fichier or not fichier.filename:
            continue
        if not fichier.filename.lower().endswith(".pdf"):
            flash(f"⛔ « {fichier.filename} » ignoré : seuls les fichiers PDF sont acceptés.", "warning")
            continue
        nom = secure_filename(fichier.filename)
        fichier.save(os.path.join(MODELES_GARDEE_DIR, f"{PIECE_JOINTE_MAIL_PREFIXE}{nom}"))
        ajoutes += 1

    if ajoutes:
        flash(f"✅ {ajoutes} pièce(s) jointe(s) ajoutée(s).", "success")
    else:
        flash("⛔ Aucun fichier PDF valide sélectionné.", "warning")

    return redirect(url_for("collecte.chauffeurs_equipiers", annee=annee))


@collecte_bp.route("/collecte/chauffeurs_equipiers/pieces_jointes/supprimer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def supprimer_piece_jointe_mail_chauffeurs_equipiers():
    annee = request.form.get("annee", type=int) or datetime.now().year
    nom_a_retirer = request.form.get("fichier", "")

    chemin = os.path.join(MODELES_GARDEE_DIR, f"{PIECE_JOINTE_MAIL_PREFIXE}{nom_a_retirer}")
    if os.path.exists(chemin):
        os.remove(chemin)
        flash(f"🗑️ « {nom_a_retirer} » retiré.", "success")

    return redirect(url_for("collecte.chauffeurs_equipiers", annee=annee))


@collecte_bp.route("/collecte/upload/<type_fichier>", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def upload_fichier(type_fichier):
    annee = request.form.get("annee", type=int)

    if type_fichier not in FICHIERS:
        flash("❌ Type de fichier inconnu", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    if not annee:
        flash("❌ Année manquante", "danger")
        return redirect(url_for("collecte.collecte_main"))

    conf = FICHIERS[type_fichier]
    fichier = request.files.get("fichier")

    if not fichier or not fichier.filename:
        flash("❌ Aucun fichier sélectionné", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    ext = os.path.splitext(fichier.filename)[1].lower()
    if ext not in conf["extensions"]:
        flash(
            f"❌ Extension invalide pour « {conf['label']} » "
            f"(attendu : {', '.join(sorted(conf['extensions']))})",
            "danger"
        )
        return redirect(url_for("collecte.collecte_main", annee=annee))

    dossier = _dossier_annee(annee)
    os.makedirs(dossier, exist_ok=True)
    fichier.save(os.path.join(dossier, conf["nom_stockage"]))

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db_connection() as conn:
        existante = conn.execute(
            "SELECT id FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

        if existante:
            conn.execute(f"""
                UPDATE collecte_campagnes
                SET {conf['champ_chemin']} = ?, {conf['champ_le']} = ?, {conf['champ_par']} = ?
                WHERE annee = ?
            """, (conf["nom_stockage"], maintenant, current_user.email, annee))
        else:
            conn.execute(f"""
                INSERT INTO collecte_campagnes
                    (annee, {conf['champ_chemin']}, {conf['champ_le']}, {conf['champ_par']},
                     date_creation, cree_par)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (annee, conf["nom_stockage"], maintenant, current_user.email,
                  maintenant, current_user.email))

        conn.commit()

    write_log(f"✅ Collecte {annee} : fichier « {conf['label']} » importé par {current_user.email}")
    flash(f"✅ {conf['label']} importé(e) pour la collecte {annee}", "success")

    return redirect(url_for("collecte.collecte_main", annee=annee))


@collecte_bp.route("/collecte/dates", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_dates():
    annee = request.form.get("annee", type=int)
    date_debut = request.form.get("date_debut", "").strip() or None
    date_fin = request.form.get("date_fin", "").strip() or None

    if not annee:
        flash("❌ Année manquante", "danger")
        return redirect(url_for("collecte.collecte_main"))

    with get_db_connection() as conn:
        existante = conn.execute(
            "SELECT id FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

        if existante:
            conn.execute(
                "UPDATE collecte_campagnes SET date_debut = ?, date_fin = ? WHERE annee = ?",
                (date_debut, date_fin, annee)
            )
        else:
            maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT INTO collecte_campagnes (annee, date_debut, date_fin, date_creation, cree_par)
                VALUES (?, ?, ?, ?, ?)
            """, (annee, date_debut, date_fin, maintenant, current_user.email))

        conn.commit()

    write_log(f"📅 Collecte {annee} : dates mises à jour par {current_user.email} ({date_debut} → {date_fin})")
    flash(f"✅ Dates de la collecte {annee} enregistrées", "success")

    return redirect(url_for("collecte.collecte_main", annee=annee))


@collecte_bp.route("/collecte/drive_liens", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_liens_drive():
    annee = request.form.get("annee", type=int)

    if not annee:
        flash("❌ Année manquante", "danger")
        return redirect(url_for("collecte.collecte_main"))

    valeurs = {}
    invalides = []
    for cle, conf in DRIVE_CHAMPS.items():
        brut = request.form.get(cle, "").strip()
        if brut and not _id_drive(brut):
            invalides.append(conf["label"])
        valeurs[conf["champ"]] = brut or None

    if invalides:
        flash(
            f"❌ Lien(s) Drive non reconnu(s), non enregistré(s) : {', '.join(invalides)} "
            f"— coller le lien de partage complet du fichier Google Sheets",
            "danger"
        )
        return redirect(url_for("collecte.collecte_main", annee=annee))

    with get_db_connection() as conn:
        _ensure_colonne_drive_colis(conn)
        existante = conn.execute(
            "SELECT id FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

        if existante:
            conn.execute(
                "UPDATE collecte_campagnes SET drive_magasins = ?, drive_vehicules = ?, "
                 "drive_colis = ?, drive_groupes = ?, drive_participants = ?, "
                 "drive_participants_mailing = ? WHERE annee = ?",
                (valeurs["drive_magasins"], valeurs["drive_vehicules"],
                  valeurs["drive_colis"], valeurs["drive_groupes"], valeurs["drive_participants"],
                  valeurs["drive_participants_mailing"], annee)
            )
        else:
            maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT INTO collecte_campagnes
                    (annee, drive_magasins, drive_vehicules, drive_colis, drive_groupes,
                                         drive_participants, drive_participants_mailing, date_creation, cree_par)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (annee, valeurs["drive_magasins"], valeurs["drive_vehicules"],
                                    valeurs["drive_colis"], valeurs["drive_groupes"], valeurs["drive_participants"],
                                    valeurs["drive_participants_mailing"], maintenant, current_user.email))

        conn.commit()

    write_log(f"🔗 Collecte {annee} : liens Drive mis à jour par {current_user.email}")
    flash(f"✅ Liens Drive de la collecte {annee} enregistrés", "success")

    return redirect(url_for("collecte.collecte_main", annee=annee))


def _get_campagne_ou_redirect(annee):
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

    if not campagne or not _fichier_drive_disponible(annee, "magasins") or not campagne["fichier_pdf_precedent"]:
        flash(
            "⛔ Liste des magasins et PDF des tournées précédentes requis avant de générer "
            f"les tournées {annee}",
            "danger"
        )
        return None
    return campagne


@collecte_bp.route("/collecte/<int:annee>/simulation-capacite")
@login_required
@require_access("collecte", "ecriture")
def simulation_capacite_form(annee):
    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    camions_pdf = _camions_pdf_precedent(annee)
    camions_actuels = _camions_liste_vehicule(annee)
    mapping_existant = _mapping_camions(annee)

    return render_template(
        "collecte/simulation_capacite.html",
        annee=annee,
        params=PARAMS_DEFAUT,
        camions_pdf=camions_pdf,
        camions_actuels=camions_actuels,
        mapping_existant=mapping_existant,
    )


@collecte_bp.route("/collecte/<int:annee>/simulation-capacite/mapping", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def simulation_capacite_mapping(annee):
    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_table_mapping_camions(conn)
        for cle, valeur in request.form.items():
            if not cle.startswith("mapping_"):
                continue
            code_ancien = cle[len("mapping_"):]
            code_nouveau = valeur.strip()
            if not code_nouveau:
                conn.execute(
                    "DELETE FROM collecte_mapping_camions WHERE annee = ? AND code_ancien = ?",
                    (annee, code_ancien)
                )
                continue
            conn.execute("""
                INSERT INTO collecte_mapping_camions (annee, code_ancien, code_nouveau, maj_le, maj_par)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(annee, code_ancien) DO UPDATE SET
                    code_nouveau = excluded.code_nouveau, maj_le = excluded.maj_le, maj_par = excluded.maj_par
            """, (annee, code_ancien, code_nouveau, maintenant, current_user.email))
        conn.commit()

    flash("✅ Correspondance des camions enregistrée", "success")
    return redirect(url_for("collecte.simulation_capacite_form", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/simulation-capacite/generer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def simulation_capacite_generer(annee):
    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    params = {
        "camions_supp": request.form.get("camions_supp", type=int, default=PARAMS_DEFAUT["camions_supp"]),
        "poids_nouveaux": request.form.get("poids_nouveaux", type=int, default=PARAMS_DEFAUT["poids_nouveaux"]),
        "max_magasins": request.form.get("max_magasins", type=int, default=PARAMS_DEFAUT["max_magasins"]),
        "optimiser_anciens": bool(request.form.get("optimiser_anciens")),
        "fusionner_legeres": bool(request.form.get("fusionner_legeres")),
        "corriger_mal_places": bool(request.form.get("corriger_mal_places")),
        "plus1_dimanche": bool(request.form.get("plus1_dimanche")),
    }

    mapping = _mapping_camions(annee)

    try:
        resultat = _generer_tournees(campagne, params, mapping_camions=mapping, avec_type_camion=True)
    except Exception as e:
        write_log(f"❌ Collecte {annee} : échec génération simulation capacité camions — {e}")
        flash(f"❌ Échec de la génération : {e}", "danger")
        return redirect(url_for("collecte.simulation_capacite_form", annee=annee))

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params_enregistres = {**params, "type_simulation": "capacite"}
    with get_db_connection() as conn:
        _ensure_colonne_pdf_equipier(conn)
        cur = conn.execute("""
            INSERT INTO collecte_generations
                (annee, fichier_excel, fichier_carte_secteurs, fichier_carte_tournees, fichier_pdf_equipier,
                 parametres, genere_le, genere_par, nb_tournees, nb_magasins, nb_nouveaux_magasins)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            annee, resultat["nom_fichier"], resultat["nom_carte_secteurs"], resultat["nom_carte_tournees"],
            resultat["nom_pdf_equipier"],
            json.dumps(params_enregistres), maintenant, current_user.email,
            resultat["nb_tournees"], resultat["nb_magasins"], resultat["nb_nouveaux_magasins"],
        ))
        generation_id = cur.lastrowid
        conn.commit()

    write_log(
        f"✅ Collecte {annee} : simulation capacité camions générée par {current_user.email} "
        f"({resultat['nb_tournees']} tournées, {resultat['nb_magasins']} magasins) — {resultat['nom_fichier']}"
    )
    flash(f"✅ Simulation avec capacité camions {annee} générée : {resultat['nb_tournees']} tournées", "success")

    return redirect(url_for("collecte.resultats", generation_id=generation_id))


@collecte_bp.route("/collecte/<int:annee>/simulation-vehicule")
@login_required
@require_access("collecte", "ecriture")
def simulation_vehicule_form(annee):
    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    return render_template(
        "collecte/simulation_vehicule.html",
        annee=annee,
        params=PARAMS_DEFAUT,
    )


@collecte_bp.route("/collecte/<int:annee>/simulation-vehicule/generer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def simulation_vehicule_generer(annee):
    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    params = {
        "camions_supp": request.form.get("camions_supp", type=int, default=PARAMS_DEFAUT["camions_supp"]),
        "poids_nouveaux": request.form.get("poids_nouveaux", type=int, default=PARAMS_DEFAUT["poids_nouveaux"]),
        "max_magasins": request.form.get("max_magasins", type=int, default=PARAMS_DEFAUT["max_magasins"]),
        "optimiser_anciens": bool(request.form.get("optimiser_anciens")),
        "fusionner_legeres": bool(request.form.get("fusionner_legeres")),
        "corriger_mal_places": bool(request.form.get("corriger_mal_places")),
        "plus1_dimanche": bool(request.form.get("plus1_dimanche")),
    }

    mapping = _mapping_camions(annee)
    camions_exclus = [
        c.strip().upper() for c in request.form.get("camions_exclus", "").split(",") if c.strip()
    ]

    try:
        resultat = _generer_tournees(
            campagne, params, mapping_camions=mapping, avec_type_camion=True,
            depuis_liste_vehicule=True, camions_exclus=camions_exclus,
        )
    except Exception as e:
        write_log(f"❌ Collecte {annee} : échec génération simulation depuis liste-vehicule — {e}")
        flash(f"❌ Échec de la génération : {e}", "danger")
        return redirect(url_for("collecte.simulation_vehicule_form", annee=annee))

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params_enregistres = {**params, "type_simulation": "vehicule", "camions_exclus": camions_exclus}
    with get_db_connection() as conn:
        _ensure_colonne_pdf_equipier(conn)
        cur = conn.execute("""
            INSERT INTO collecte_generations
                (annee, fichier_excel, fichier_carte_secteurs, fichier_carte_tournees, fichier_pdf_equipier,
                 parametres, genere_le, genere_par, nb_tournees, nb_magasins, nb_nouveaux_magasins)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            annee, resultat["nom_fichier"], resultat["nom_carte_secteurs"], resultat["nom_carte_tournees"],
            resultat["nom_pdf_equipier"],
            json.dumps(params_enregistres), maintenant, current_user.email,
            resultat["nb_tournees"], resultat["nb_magasins"], resultat["nb_nouveaux_magasins"],
        ))
        generation_id = cur.lastrowid
        conn.commit()

    write_log(
        f"✅ Collecte {annee} : simulation depuis liste-vehicule générée par {current_user.email} "
        f"({resultat['nb_tournees']} tournées, {resultat['nb_magasins']} magasins"
        f"{', camions exclus : ' + ', '.join(camions_exclus) if camions_exclus else ''}) — {resultat['nom_fichier']}"
    )
    suffixe_exclus = f" (sans {', '.join(camions_exclus)})" if camions_exclus else ""
    flash(f"✅ Simulation depuis liste-vehicule {annee} générée{suffixe_exclus} : {resultat['nb_tournees']} tournées", "success")

    return redirect(url_for("collecte.resultats", generation_id=generation_id))


@collecte_bp.route("/collecte/analyse", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def lancer_analyse():
    annee = request.form.get("annee", type=int)

    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    dossier = _dossier_annee(annee)
    pdf_path = os.path.join(dossier, campagne["fichier_pdf_precedent"])
    magasins_path = _fichier_drive(annee, "magasins")
    if not magasins_path:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    params_communs = {
        "poids_nouveaux": PARAMS_DEFAUT["poids_nouveaux"],
        "optimiser_anciens": True,
        "fusionner_legeres": False,
        "corriger_mal_places": True,
    }

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        cur = conn.execute("""
            INSERT INTO collecte_analyses (annee, statut, parametres_communs, lance_le, lance_par)
            VALUES (?, 'en_cours', ?, ?, ?)
        """, (annee, json.dumps(params_communs), maintenant, current_user.email))
        analyse_id = cur.lastrowid
        conn.commit()

    app_reel = current_app._get_current_object()

    lancer_tache_fond(
        target=_lancer_analyse_8configs_background,
        args=(app_reel, analyse_id, pdf_path, magasins_path, params_communs, annee),
        nom="Collecte : analyse 8 scénarios",
        utilisateur=current_user.email,
    )

    write_log(f"🔬 Collecte {annee} : analyse 8 scénarios #{analyse_id} lancée par {current_user.email}")
    flash("🔬 Analyse des 8 scénarios lancée en arrière-plan (compter 1 à 2 minutes)", "info")

    return redirect(url_for("collecte.analyse", analyse_id=analyse_id))


INDICATEURS_PROMPT = [
    ("Nb tournées V/S (hors figés)", "nb_tournees"),
    ("Tournées à 2 magasins", "tournees_2"),
    ("Tournées à 3 magasins", "tournees_3"),
    ("Tournées à 4 magasins", "tournees_4"),
    ("Tournées à 5 magasins", "tournees_5"),
    ("Tournées à 6+ magasins (MAX+2)", "tournees_6plus"),
    ("Tournées > MAX (surcharges)", "surcharges"),
    ("Tournées à 3 secteurs", "tournees_3_secteurs"),
    ("Tournées à > 3 secteurs", "tournees_plus3_secteurs"),
    ("Km total estimé", "km_total"),
    ("Moy. magasins/tournée", "moy_magasins_tournee"),
    ("Camions VX créés", "camions_vx_crees"),
    ("Magasins dans les VX", "magasins_dans_vx"),
    ("Durée moy. estimée (min)", "duree_moy_min"),
]


def _construire_prompt_analyse(annee, scenarios):
    """Prompt prêt à coller dans Claude.ai / ChatGPT pour obtenir la partie
    rédactionnelle (rôle des camions VX, verdicts, recommandation) — sur le
    modèle de l'analyse manuelle de référence
    (uploads/collecte_fichiers_source/analyse_simulations_8configs.docx).
    Pas d'appel API depuis l'appli (choix du 2026-08-17) : uniquement les
    données chiffrées, à coller dans l'outil IA de son choix."""
    entetes = [f"VX{s['camions_supp']}-MAX{s['max_magasins']}" for s in scenarios]

    lignes_tableau = ["| Indicateur | " + " | ".join(entetes) + " |",
                      "|---|" + "|".join(["---"] * len(entetes)) + "|"]
    for label, champ in INDICATEURS_PROMPT:
        lignes_tableau.append("| " + label + " | " + " | ".join(str(s.get(champ, "")) for s in scenarios) + " |")

    parties_surcharges = []
    for s in scenarios:
        detail = s.get("surcharges_detail") or []
        if detail:
            items = "; ".join(f"{d['camion']} ({d['demi_journee']}, {d['nb_magasins']} mag.)" for d in detail)
            parties_surcharges.append(f"- VX{s['camions_supp']}-MAX{s['max_magasins']} ({s['surcharges']}) : {items}")

    parties_vx = []
    for s in scenarios:
        detail = s.get("vx_detail") or []
        if detail:
            items = "; ".join(f"{d['camion']} {d['demi_journee']} → {d['secteur']} ({d['nb_magasins']} mag.)" for d in detail)
            parties_vx.append(f"- VX{s['camions_supp']}-MAX{s['max_magasins']} : {items}")

    parties_secteurs = []
    for s in scenarios:
        detail = s.get("secteurs_multiples_detail") or []
        if detail:
            items = "; ".join(f"{d['camion']} {d['demi_journee']} ({d['secteurs']}, {d['nb_magasins']} mag.)" for d in detail)
            parties_secteurs.append(f"- VX{s['camions_supp']}-MAX{s['max_magasins']} : {items}")

    return f"""Tu es un analyste logistique pour une banque alimentaire (BAI 38 — Isère). Voici les résultats \
de 8 simulations de génération de tournées de collecte alimentaire pour la collecte {annee}, calculés par un \
algorithme d'optimisation réel (pas une estimation manuelle), en faisant varier :
- le nombre de camions supplémentaires (VX) : 1 à 4
- le nombre maximum de magasins par tournée : 4 ou 5

Toutes les données ci-dessous portent uniquement sur le Vendredi et le Samedi (Matin + Après-midi), hors \
véhicules figés (gérés par des associations partenaires, jamais réoptimisés).

## 1. Indicateurs de performance

{chr(10).join(lignes_tableau)}

## 2. Détail des tournées en surcharge (nb magasins > max autorisé)

{chr(10).join(parties_surcharges) if parties_surcharges else "Aucune surcharge sur aucun scénario."}

## 3. Détail des camions supplémentaires (VX) — secteur desservi, nombre de magasins

{chr(10).join(parties_vx) if parties_vx else "Aucun camion supplémentaire créé sur aucun scénario."}

## 4. Détail des tournées à 3 secteurs géographiques ou plus

{chr(10).join(parties_secteurs) if parties_secteurs else "Aucune tournée à 3 secteurs ou plus."}

---

En te basant UNIQUEMENT sur les données ci-dessus (n'invente aucun chiffre), rédige une analyse comparative \
structurée avec :
1. Une analyse des dépassements de capacité : MAX4 est-il structurellement adapté ou non ? à partir de quel \
nombre de camions VX les surcharges disparaissent-elles avec MAX5 ?
2. Le rôle de chaque camion supplémentaire VX dans chaque scénario où il existe (quel secteur il déleste, \
combien de magasins).
3. Une analyse de la cohérence sectorielle (tournées à 3 secteurs ou plus) : quelles tournées reviennent \
dans plusieurs scénarios, signe d'une contrainte géographique structurelle plutôt que d'un mauvais réglage.
4. Une analyse détaillée par configuration (points positifs/négatifs de chaque VX×MAX).
5. Une recommandation finale argumentée avec un tableau de synthèse (surcharges, km total, tournées à 3 \
secteurs) et un verdict par scénario : ★ recommandé, ✓ bonne alternative, ~ acceptable avec nuances, ✗ à éviter.

Style : synthétique, factuel, orienté décision opérationnelle — pas de généralités, uniquement des \
observations appuyées sur les chiffres fournis."""


def _generer_redaction_background(app, analyse_id, prompt):
    """Envoie le prompt à l'API Claude et enregistre le texte rédigé.
    Tourne en arrière-plan (comme l'analyse elle-même) car un appel avec
    réflexion étendue peut dépasser le timeout par défaut de gunicorn (30s)."""
    with app.app_context():
        try:
            api_key = os.getenv("CLAUDE_API_KEY")
            if not api_key:
                raise RuntimeError("CLAUDE_API_KEY absente du .env")

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=16000,
                messages=[{"role": "user", "content": prompt}],
            )
            texte = "".join(bloc.text for bloc in response.content if bloc.type == "text")
            if not texte:
                raise RuntimeError(f"Réponse vide de l'API (stop_reason={response.stop_reason})")

            with get_db_connection() as conn:
                conn.execute("""
                    UPDATE collecte_analyses
                    SET redaction_statut = 'termine', redaction_texte = ?, redaction_genere_le = ?
                    WHERE id = ?
                """, (texte, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), analyse_id))
                conn.commit()

            write_log(f"🤖 Collecte : rédaction IA de l'analyse #{analyse_id} terminée")

        except Exception as e:
            write_log(f"❌ Collecte : erreur rédaction IA analyse #{analyse_id} : {e}")
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE collecte_analyses SET redaction_statut = 'erreur', redaction_erreur = ? WHERE id = ?",
                    (str(e), analyse_id)
                )
                conn.commit()


@collecte_bp.route("/collecte/analyse/<int:analyse_id>/generer_redaction", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def generer_redaction(analyse_id):
    with get_db_connection() as conn:
        analyse_row = conn.execute(
            "SELECT * FROM collecte_analyses WHERE id = ?", (analyse_id,)
        ).fetchone()

    if not analyse_row or not analyse_row["resultat"]:
        flash("⛔ Analyse introuvable ou incomplète", "warning")
        return redirect(url_for("collecte.collecte_main"))

    scenarios = json.loads(analyse_row["resultat"])
    prompt = _construire_prompt_analyse(analyse_row["annee"], scenarios)

    with get_db_connection() as conn:
        conn.execute(
            "UPDATE collecte_analyses SET redaction_statut = 'en_cours', redaction_erreur = NULL WHERE id = ?",
            (analyse_id,)
        )
        conn.commit()

    app_reel = current_app._get_current_object()
    lancer_tache_fond(target=_generer_redaction_background, args=(app_reel, analyse_id, prompt), nom="Collecte : rédaction IA analyse", utilisateur=current_user.email)

    write_log(f"🤖 Collecte : rédaction IA de l'analyse #{analyse_id} lancée par {current_user.email}")
    flash("🤖 Génération de la partie rédactionnelle lancée (compter 30 à 60 secondes)", "info")

    return redirect(url_for("collecte.analyse", analyse_id=analyse_id))


@collecte_bp.route("/collecte/analyse/<int:analyse_id>")
@login_required
@require_access("collecte", "lecture")
def analyse(analyse_id):
    with get_db_connection() as conn:
        analyse_row = conn.execute(
            "SELECT * FROM collecte_analyses WHERE id = ?", (analyse_id,)
        ).fetchone()

    if not analyse_row:
        flash("⛔ Analyse introuvable", "warning")
        return redirect(url_for("collecte.collecte_main"))

    scenarios = json.loads(analyse_row["resultat"]) if analyse_row["resultat"] else []
    prompt_analyse = _construire_prompt_analyse(analyse_row["annee"], scenarios) if scenarios else None
    redaction_html = None
    if analyse_row["redaction_texte"]:
        redaction_html = markdown.markdown(analyse_row["redaction_texte"], extensions=["tables"])
        # Tableaux larges (8 scénarios en colonnes) → scroll horizontal contenu
        # dans la carte plutôt que débordement de toute la page.
        redaction_html = redaction_html.replace("<table>", '<div class="table-responsive"><table>')
        redaction_html = redaction_html.replace("</table>", "</table></div>")

    return render_template(
        "collecte/analyse.html",
        annee=analyse_row["annee"],
        analyse=analyse_row,
        scenarios=scenarios,
        dj_vs=DJ_VS,
        dj_vs_labels=DJ_VS_LABELS,
        prompt_analyse=prompt_analyse,
        redaction_html=redaction_html,
    )


@collecte_bp.route("/collecte/generations/<int:annee>")
@login_required
@require_access("collecte", "lecture")
def generations(annee):
    with get_db_connection() as conn:
        lignes = conn.execute(
            "SELECT * FROM collecte_generations WHERE annee = ? ORDER BY id DESC", (annee,)
        ).fetchall()

    versions = []
    for gen in lignes:
        versions.append({
            "gen": gen,
            "params": json.loads(gen["parametres"]) if gen["parametres"] else {},
        })

    return render_template("collecte/generations.html", annee=annee, versions=versions)


@collecte_bp.route("/collecte/generer")
@login_required
@require_access("collecte", "ecriture")
def generer_form():
    annee = request.args.get("annee", type=int) or datetime.now().year

    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    return render_template(
        "collecte/generer.html",
        annee=annee,
        params=PARAMS_DEFAUT,
    )


@collecte_bp.route("/collecte/generer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def generer():
    annee = request.form.get("annee", type=int)

    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
        return redirect(url_for("collecte.collecte_main", annee=annee))

    params = {
        "camions_supp": request.form.get("camions_supp", type=int, default=PARAMS_DEFAUT["camions_supp"]),
        "poids_nouveaux": request.form.get("poids_nouveaux", type=int, default=PARAMS_DEFAUT["poids_nouveaux"]),
        "max_magasins": request.form.get("max_magasins", type=int, default=PARAMS_DEFAUT["max_magasins"]),
        "optimiser_anciens": bool(request.form.get("optimiser_anciens")),
        "fusionner_legeres": bool(request.form.get("fusionner_legeres")),
        "corriger_mal_places": bool(request.form.get("corriger_mal_places")),
        "plus1_dimanche": bool(request.form.get("plus1_dimanche")),
    }

    try:
        resultat = _generer_tournees(campagne, params)
    except Exception as e:
        write_log(f"❌ Collecte {annee} : échec génération tournées — {e}")
        flash(f"❌ Échec de la génération des tournées : {e}", "danger")
        return redirect(url_for("collecte.generer_form", annee=annee))

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        cur = conn.execute("""
            INSERT INTO collecte_generations
                (annee, fichier_excel, fichier_carte_secteurs, fichier_carte_tournees, parametres,
                 genere_le, genere_par, nb_tournees, nb_magasins, nb_nouveaux_magasins)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            annee, resultat["nom_fichier"], resultat["nom_carte_secteurs"], resultat["nom_carte_tournees"],
            json.dumps(params), maintenant, current_user.email,
            resultat["nb_tournees"], resultat["nb_magasins"], resultat["nb_nouveaux_magasins"],
        ))
        generation_id = cur.lastrowid
        conn.commit()

    write_log(
        f"✅ Collecte {annee} : tournées générées par {current_user.email} "
        f"({resultat['nb_tournees']} tournées, {resultat['nb_magasins']} magasins) — {resultat['nom_fichier']}"
    )
    flash(f"✅ Tournées {annee} générées : {resultat['nb_tournees']} tournées", "success")

    return redirect(url_for("collecte.resultats", generation_id=generation_id))


def _get_generation_ou_redirect(generation_id):
    with get_db_connection() as conn:
        _ensure_colonne_pdf_equipier(conn)
        generation = conn.execute(
            "SELECT * FROM collecte_generations WHERE id = ?", (generation_id,)
        ).fetchone()

    if not generation:
        flash("⛔ Génération introuvable", "warning")
        return None
    return generation


@collecte_bp.route("/collecte/resultats/<int:generation_id>/supprimer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def supprimer_generation(generation_id):
    generation = _get_generation_ou_redirect(generation_id)
    if generation is None:
        return redirect(url_for("collecte.collecte_main"))

    annee = generation["annee"]
    dossier = _dossier_resultats(annee)

    for champ in ("fichier_excel", "fichier_carte_secteurs", "fichier_carte_tournees"):
        nom = generation[champ]
        if not nom:
            continue
        chemin = os.path.join(dossier, nom)
        if os.path.exists(chemin):
            os.remove(chemin)

    with get_db_connection() as conn:
        conn.execute("DELETE FROM collecte_generations WHERE id = ?", (generation_id,))
        conn.commit()

    write_log(
        f"🗑️ Collecte {annee} : génération #{generation_id} ({generation['fichier_excel']}) "
        f"supprimée par {current_user.email}"
    )
    flash(f"🗑️ Génération du {date_fr(generation['genere_le'])} supprimée", "success")

    return redirect(url_for("collecte.generations", annee=annee))


@collecte_bp.route("/collecte/resultats/<int:generation_id>")
@login_required
@require_access("collecte", "lecture")
def resultats(generation_id):
    generation = _get_generation_ou_redirect(generation_id)
    if generation is None:
        return redirect(url_for("collecte.collecte_main"))

    with get_db_connection() as conn:
        autres_generations = conn.execute(
            "SELECT * FROM collecte_generations WHERE annee = ? ORDER BY id DESC",
            (generation["annee"],)
        ).fetchall()

    lignes = _charger_tournees(generation)
    params_utilises = json.loads(generation["parametres"]) if generation["parametres"] else {}

    return render_template(
        "collecte/resultats.html",
        annee=generation["annee"],
        generation=generation,
        autres_generations=autres_generations,
        lignes=lignes,
        params=params_utilises,
        camions_non_utilises=_camions_non_utilises(generation["annee"], lignes),
        matin_apresmidi_differents=_camions_matin_apresmidi_differents(lignes),
    )


@collecte_bp.route("/collecte/<int:annee>/telecharger/<nom_fichier>")
@login_required
@require_access("collecte", "lecture")
def telecharger_fichier_annee(annee, nom_fichier):
    """Téléchargement ad hoc d'un fichier d'analyse déposé directement dans
    le dossier de l'année (comparaisons/exports one-off produits hors appli) —
    restreint aux .xlsx réellement présents dans ce dossier précis, nom de
    fichier validé pour exclure toute traversée de chemin."""
    if not re.fullmatch(r"[\w.\-]+\.xlsx", nom_fichier):
        flash("⛔ Nom de fichier invalide", "warning")
        return redirect(url_for("collecte.collecte_main", annee=annee))
    chemin = os.path.join(_dossier_annee(annee), nom_fichier)
    if not os.path.exists(chemin):
        flash("⛔ Fichier introuvable", "warning")
        return redirect(url_for("collecte.collecte_main", annee=annee))
    return send_file(
        chemin, as_attachment=True, download_name=nom_fichier,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _donnees_localisation(annee):
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

    magasins_bai, magasins_gardee, adresses_invalides = [], [], []
    if campagne and _fichier_drive_disponible(annee, "magasins"):
        magasins, adresses_invalides = _charger_magasins_localisation(annee, campagne)
        magasins_bai = [m for m in magasins if m["categorie"] == "bai"]
        magasins_gardee = [m for m in magasins if m["categorie"] == "gardee"]

    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT nom_association, COMMUNE, latitude, longitude
            FROM associations
            WHERE LOWER(TRIM(COALESCE(validite,''))) = 'oui'
            ORDER BY nom_association
        """).fetchall()
    associations = []
    for r in rows:
        lat, lon = r["latitude"], r["longitude"]
        if lat is None or lon is None or not _coords_plausibles(lat, lon):
            adresses_invalides.append({"nom": r["nom_association"], "ville": r["COMMUNE"] or ""})
            continue
        associations.append({"nom": r["nom_association"], "ville": r["COMMUNE"] or "", "lat": lat, "lon": lon})
    return magasins_bai, magasins_gardee, associations, adresses_invalides


@collecte_bp.route("/collecte/<int:annee>/localisation")
@login_required
@require_access("collecte", "lecture")
def localisation(annee):
    magasins_bai, magasins_gardee, associations, adresses_invalides = _donnees_localisation(annee)
    token = generer_token_localisation(annee)
    lien_partage = url_for("collecte.localisation_lien", annee=annee, token=token, _external=True)

    return render_template(
        "collecte/localisation.html",
        annee=annee,
        magasins_bai=magasins_bai,
        magasins_gardee=magasins_gardee,
        associations=associations,
        adresses_invalides=adresses_invalides,
        lien_partage=lien_partage,
        duree_lien_jours=LOCALISATION_TOKEN_VALIDITE_JOURS,
        est_public=False,
    )


@collecte_bp.route("/collecte/<int:annee>/localisation-lien/<token>")
def localisation_lien(annee, token):
    """Version sans connexion de la carte localisation, pour un lien envoyé
    par mail à des personnes sans compte sur l'appli (cf. generer_token_localisation)."""
    payload = verifier_token_localisation(token)
    if not payload or payload.get("annee") != annee:
        return render_template("collecte/localisation_lien_invalide.html"), 403

    magasins_bai, magasins_gardee, associations, adresses_invalides = _donnees_localisation(annee)

    return render_template(
        "collecte/localisation.html",
        annee=annee,
        magasins_bai=magasins_bai,
        magasins_gardee=magasins_gardee,
        associations=associations,
        adresses_invalides=adresses_invalides,
        lien_partage=None,
        est_public=True,
    )


def _construire_referentiel_collecte(annee):
    """Fusionne le fichier magasins de l'année avec le référentiel
    pluriannuel (collecte_magasins_referentiel, cf. Résultat collecte →
    Évolution magasins) : un magasin qui y figure mais absent du fichier
    magasins de l'année est considéré « Non collecté » cette année-là,
    plutôt que de dépendre uniquement du champ État — parfois incomplet ou
    absent — du fichier de l'année lui-même. Renvoie TOUS les magasins,
    y compris les « Non collecté » — au tri par nom de la route/export de
    filtrer si besoin."""
    magasins_annee = {m["code_vif"]: m for m in _lire_fichier_magasins_brut(annee)}

    with get_db_connection() as conn:
        _ensure_tables_evolution_magasins(conn)
        referentiel = {r["code_vif"]: dict(r) for r in conn.execute(
            "SELECT * FROM collecte_magasins_referentiel"
        ).fetchall()}

    fusion = {}
    for code_vif, m in magasins_annee.items():
        fusion[code_vif] = m
    for code_vif, r in referentiel.items():
        if code_vif in fusion:
            continue
        fusion[code_vif] = {
            "code_vif": code_vif,
            "nom": r["nom"] or "",
            "etat": "Non collecté",
            "adresse": r["adresse"] or "",
            "ville": r["ville"] or "",
            "code_postal": r["code_postal"] or "",
            "telephone": r["telephone"] or "",
            "email": r["email"] or "",
            "stockage": r["stockage"] or "",
            "gardee_par": r["gardee_par"] or "",
        }

    magasins = list(fusion.values())
    magasins.sort(key=lambda m: m["nom"].lower())
    return magasins


@collecte_bp.route("/collecte/<int:annee>/referentiel-collecte")
@login_required
@require_access("collecte", "lecture")
def referentiel_collecte(annee):
    """Référentiel des magasins de l'année (collectés ou non), avec leurs
    coordonnées de contact — l'adresse mail n'est renseignée que pour les
    magasins présents dans l'export go-on-web (colonne Email du fichier
    magasins), les autres l'ont vide."""
    magasins = _construire_referentiel_collecte(annee)
    nb_collectes = sum(1 for m in magasins if m["etat"] != "Non collecté")
    return render_template(
        "collecte/referentiel_collecte.html",
        annee=annee,
        magasins=magasins,
        nb_collectes=nb_collectes,
        nb_sans_email=sum(1 for m in magasins if m["etat"] != "Non collecté" and not m["email"]),
    )


@collecte_bp.route("/collecte/<int:annee>/referentiel-collecte/export")
@login_required
@require_access("collecte", "lecture")
def referentiel_collecte_export(annee):
    """Export Excel du référentiel magasins collecte, mêmes colonnes que
    le tableau à l'écran (y compris les « Non collecté »)."""
    magasins = _construire_referentiel_collecte(annee)

    wb_export = Workbook()
    ws = wb_export.active
    ws.title = "magasins"
    entetes = ["Code VIF", "Nom", "État", "Ville", "Adresse", "Téléphone", "Email", "Stockage", "Gardée par"]
    ws.append(entetes)
    for m in magasins:
        ws.append([
            m["code_vif"], m["nom"], m["etat"], m["ville"], m["adresse"],
            m["telephone"], m["email"], m["stockage"], m["gardee_par"],
        ])

    for colonne in ws.columns:
        lettre = colonne[0].column_letter
        ws.column_dimensions[lettre].width = 14 if lettre not in ("B", "E") else 32

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    tampon = io.BytesIO()
    wb_export.save(tampon)
    tampon.seek(0)
    nom_fichier = f"referentiel_collecte_{annee}.xlsx"
    return send_file(
        tampon,
        as_attachment=True,
        download_name=nom_fichier,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/<int:annee>/cagettes")
@login_required
@require_access("collecte", "lecture")
def cagettes(annee):
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

    if not campagne or not _fichier_drive_disponible(annee, "magasins"):
        flash(f"⛔ Liste des magasins {annee} requise avant de saisir les cagettes", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    initialisee = bool(campagne["cagettes_initialisee_le"])
    lignes = _charger_lignes_cagettes(annee) if initialisee else []

    try:
        magasins_par_camion = _charger_magasins_par_camion(annee) if initialisee else {}
    except Exception as erreur:
        write_log(f"⚠️ Collecte {annee} : filtre camion cagettes indisponible ({erreur})")
        magasins_par_camion = {}

    return render_template(
        "collecte/cagettes.html",
        annee=annee,
        lignes=lignes,
        demi_journees=moteur.DEMI_JOURNEES,
        campagne=campagne,
        initialisee=initialisee,
        magasins_par_camion=magasins_par_camion,
        verrou_bai=_resultat_bai_verrouille(annee),
    )


# Constantes fixes du format d'import VIF « Saisie des cagettes par magasin »
# (réception marchandise) — colonnes et valeurs figées communiquées par
# l'utilisateur, pas de logique métier derrière, juste le gabarit attendu.
CAGETTES_EXPORT_SOCIETE = "01"
CAGETTES_EXPORT_ETAB = "38"
CAGETTES_EXPORT_LIEU = "01"
CAGETTES_EXPORT_DEPOT = "05"
CAGETTES_EXPORT_ARTICLE = "5010000"  # collecte directe (camions BAI)
CAGETTES_EXPORT_ARTICLE_GARDEE = "5010010"  # collecte gardée (associations)
CAGETTES_EXPORT_UNITE = "kg"
CAGETTES_EXPORT_ORIGINE = "co"


@collecte_bp.route("/collecte/<int:annee>/cagettes/export")
@login_required
@require_access("collecte", "lecture")
def exporter_cagettes(annee):
    """Export CSV « Saisie des cagettes par magasin » pour import VIF —
    une ligne par magasin (total toutes demi-journées confondues), quantité
    en poids réel après pesée uniquement (la pesée doit être faite avant
    l'export), datée du jour de génération de l'export."""
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    if not campagne:
        flash(f"⛔ Campagne {annee} introuvable", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    poids_kg = campagne["poids_cagette_pese"] or 0
    date_reception = datetime.now().strftime("%d/%m/%Y")

    lignes = _charger_lignes_cagettes(annee)

    est_dev = os.getenv("ENVIRONMENT", "DEV").upper() != "PROD"
    forcer = est_dev and request.args.get("forcer") == "1"
    exclure_zero = request.args.get("exclure_zero") == "1"

    if not poids_kg and not forcer:
        flash(
            "⛔ Export impossible : la pesée n'a pas été faite (« Poids total pesé » vide) — "
            "le CSV exporterait 0 kg pour tous les magasins.",
            "danger",
        )
        return redirect(url_for("collecte.cagettes", annee=annee))

    magasins_zero = [ligne["nom_magasin"] for ligne in lignes if not (ligne.get("total") or 0)]
    if magasins_zero and not forcer and not exclure_zero:
        flash(
            f"⛔ Export impossible : {len(magasins_zero)} magasin(s) sans aucune cagette saisie — "
            f"{', '.join(magasins_zero)}. Utilisez le bouton « Exporter en excluant les magasins sans "
            f"saisie » pour générer quand même le CSV sans eux.",
            "danger",
        )
        return redirect(url_for("collecte.cagettes", annee=annee))

    if magasins_zero and (forcer or exclure_zero):
        write_log(
            f"📤 Export cagettes {annee} : {len(magasins_zero)} magasin(s) à zéro exclus par "
            f"{current_user.email} — {', '.join(magasins_zero)}"
        )

    tampon = StringIO()
    ecrivain = csv.writer(tampon, delimiter=";")
    # Pas de ligne d'entête : le format d'import VIF n'en attend pas.
    for ligne in lignes:
        total = ligne.get("total") or 0
        if not total:
            continue
        ecrivain.writerow([
            CAGETTES_EXPORT_SOCIETE,
            CAGETTES_EXPORT_ETAB,
            date_reception,
            str(ligne["code_vif"]).zfill(8),
            CAGETTES_EXPORT_LIEU,
            CAGETTES_EXPORT_DEPOT,
            CAGETTES_EXPORT_ARTICLE,
            round(total * poids_kg),
            CAGETTES_EXPORT_UNITE,
            "", "", "", "",
            CAGETTES_EXPORT_ORIGINE,
        ])

    write_log(f"📤 Export CSV cagettes {annee} par {current_user.email}")

    return Response(
        tampon.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=saisie_cagettes_par_magasin_{annee}.csv"},
    )


@collecte_bp.route("/collecte/<int:annee>/cagettes/export-excel")
@login_required
@require_access("collecte", "lecture")
def exporter_cagettes_excel(annee):
    """Export Excel de la grille de saisie des cagettes telle qu'affichée à
    l'écran (une ligne par magasin, une colonne par demi-journée avec « — »
    pour les créneaux non applicables, + les 3 colonnes de total) — pour
    archivage/contrôle, contrairement à l'export CSV qui suit le format
    d'import VIF (exporter_cagettes)."""
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    if not campagne:
        flash(f"⛔ Campagne {annee} introuvable", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    poids_estime = campagne["poids_cagette_estime"] or 0
    poids_pese = campagne["poids_cagette_pese"] or 0
    lignes = _charger_lignes_cagettes(annee)

    colonnes_demi_journees = [dj.replace("Apres Midi", "Après-midi") for dj in moteur.DEMI_JOURNEES]
    entetes = ["Code VIF", "Magasin"] + colonnes_demi_journees + [
        "Total cagettes", "Total poids magasin (kg)", "Total poids magasin après pesée (kg)",
    ]
    rows = []
    sommes_dj = {dj: 0 for dj in moteur.DEMI_JOURNEES}
    total_general = 0
    for ligne in lignes:
        total = ligne.get("total") or 0
        total_general += total
        row = [str(ligne["code_vif"]).zfill(8), ligne["nom_magasin"]]
        for dj in moteur.DEMI_JOURNEES:
            valeur = ligne.get(dj) if ligne.get(f"_appl_{dj}") else None
            row.append("—" if not ligne.get(f"_appl_{dj}") else valeur)
            if valeur:
                sommes_dj[dj] += valeur
        row.append(total)
        row.append(round(total * poids_estime, 2))
        row.append(round(total * poids_pese, 2))
        rows.append(row)

    ligne_total = ["", "Total"] + [sommes_dj[dj] for dj in moteur.DEMI_JOURNEES] + [
        total_general, round(total_general * poids_estime, 2), round(total_general * poids_pese, 2),
    ]
    rows.insert(0, ligne_total)

    df = pd.DataFrame(rows, columns=entetes)
    tampon = io.BytesIO()
    with pd.ExcelWriter(tampon, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Cagettes", index=False)
    tampon.seek(0)

    write_log(f"📤 Export Excel cagettes {annee} par {current_user.email}")

    return send_file(
        tampon,
        as_attachment=True,
        download_name=f"saisie_cagettes_par_magasin_{annee}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


SEUIL_ECART_CAGETTES_N1 = 0.20


@collecte_bp.route("/collecte/<int:annee>/cagettes/export-ecarts")
@login_required
@require_access("collecte", "lecture")
def exporter_ecarts_cagettes(annee):
    """Export Excel des magasins en écart important (seuil en %, ±20% par
    défaut) sur le Total cagettes vs l'année précédente, pour contrôle —
    même logique que la détection côté page (bouton « Écarts importants »),
    recalculés ici côté serveur pour l'export. Les nouveaux magasins / non
    collectés l'an dernier (aucune donnée N-1) sont exclus, comme à l'écran."""
    try:
        seuil = float(request.args.get("seuil", "")) / 100
    except ValueError:
        seuil = SEUIL_ECART_CAGETTES_N1
    if seuil <= 0:
        seuil = SEUIL_ECART_CAGETTES_N1

    lignes = _charger_lignes_cagettes(annee)

    rows = []
    for ligne in lignes:
        total = ligne.get("total") or 0
        total_n1, a_n1 = 0, False
        for dj in moteur.DEMI_JOURNEES:
            n1 = ligne.get("_n1_" + dj)
            if n1 is not None:
                total_n1 += n1
                a_n1 = True
        if not a_n1:
            continue  # nouveau magasin / pas collecté l'an dernier

        if total_n1 == 0:
            important, pct = total > 0, None
        else:
            pct = (total - total_n1) / total_n1
            important = abs(pct) >= seuil
        if not important:
            continue

        rows.append([
            str(ligne["code_vif"]).zfill(8),
            ligne["nom_magasin"],
            total,
            total_n1,
            round(pct * 100, 1) if pct is not None else None,
            "Hausse" if (pct is None or pct >= 0) else "Baisse",
        ])

    entetes = ["Code VIF", "Magasin", f"Total {annee}", f"Total {annee - 1}", "Écart (%)", "Sens"]
    df = pd.DataFrame(rows, columns=entetes)
    tampon = io.BytesIO()
    with pd.ExcelWriter(tampon, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Écarts", index=False)
    tampon.seek(0)

    write_log(f"📤 Export Excel écarts cagettes {annee} par {current_user.email} — {len(rows)} magasin(s)")

    return send_file(
        tampon,
        as_attachment=True,
        download_name=f"ecarts_cagettes_{annee}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ============================================================================
# 📊 STATISTIQUES ENSEIGNES / SOUS-ENSEIGNES — pas de colonne "Enseigne"
# dans go-on-web : déduite du nom du magasin via ENSEIGNES_CAGETTES (table
# de correspondance écrite à la main, structure du commerce alimentaire
# français : Spar/Monoprix/Franprix/Petit Casino sont des sous-marques du
# Groupe Casino, Netto est la marque hard-discount d'Intermarché/Les
# Mousquetaires, Satoriz appartient à Carrefour depuis 2014). Même analyse
# que l'étude ad hoc faite dans la même campagne (répartition du poids par
# enseigne/sous-enseigne, comparée à l'année précédente), mais filtrée sur
# les seules demi-journées vendredi/samedi (demande utilisateur 2026-11-19).
# ============================================================================

ENSEIGNES_CAGETTES = [
    ("Carrefour", [
        ("Carrefour City", ["carrefour city"]),
        ("Carrefour Market", ["carrefour market"]),
        ("Satoriz", ["satoriz"]),
        ("Carrefour", ["carrefour"]),
    ]),
    ("E.Leclerc", [("E.Leclerc", ["leclerc"])]),
    ("Intermarché", [
        ("Intermarché Contact", ["intermarché contact", "intermarche contact"]),
        ("Intermarché Express", ["intermarché express", "intermarche express"]),
        ("Netto", ["netto"]),
        ("Intermarché", ["intermarché", "intermarche"]),
    ]),
    ("Groupe Casino", [
        ("Monoprix", ["monoprix"]),
        ("Franprix", ["franprix"]),
        ("Petit Casino", ["petit casino"]),
        ("Spar", ["spar"]),
        ("Vival", ["vival"]),
        ("Casino", ["casino"]),
    ]),
    ("Système U", [
        ("Super U", ["super u"]),
        ("U Express", ["u express"]),
        ("Utile", ["utile"]),
    ]),
    ("Auchan", [("Auchan", ["auchan"])]),
    ("Lidl", [("Lidl", ["lidl"])]),
    ("Aldi", [("Aldi", ["aldi"])]),
    ("Métro", [("Métro", ["métro", "metro"])]),
    ("Autres indépendants", [
        ("Biocoop", ["biocoop"]),
        ("Manabio", ["manabio"]),
    ]),
]


def _enseigne_sous_enseigne(nom_magasin):
    """Renvoie (enseigne majeure, sous-enseigne) déduites du nom du magasin
    — premier motif trouvé dans ENSEIGNES_CAGETTES (ordre important : les
    sous-marques les plus spécifiques sont listées avant l'enseigne
    générique, ex. « Carrefour City » avant « Carrefour »). Magasin non
    reconnu : ("Autres", nom du magasin) plutôt qu'une disparition
    silencieuse du poids correspondant."""
    nom_bas = nom_magasin.lower()
    for enseigne, sous_enseignes in ENSEIGNES_CAGETTES:
        for sous_enseigne, motifs in sous_enseignes:
            if any(motif in nom_bas for motif in motifs):
                return enseigne, sous_enseigne
    return "Autres", nom_magasin


def _rachats_detectes(codes_disparus, codes_actuels):
    """Détecte un changement d'enseigne (rachat) entre un magasin disparu
    de la liste courante et un magasin actuel partageant la même adresse —
    le Code VIF change avec le nom lors d'un rachat, mais pas l'adresse
    (constaté 2026-11-19 sur plusieurs ex. réels : Casino Fontaine devenu
    Auchan Fontaine, Casino Vif devenu Intermarché VIF...). Purement
    informatif, ne modifie aucun poids — juste pour ne pas lire l'évolution
    d'une enseigne comme une vraie perte/gain de volume alors que ce n'est
    qu'un changement d'enseigne du même magasin physique."""
    if not codes_disparus or not codes_actuels:
        return []
    with get_db_connection() as conn:
        marque = "?," * len(codes_disparus)
        anciens = conn.execute(
            f"SELECT code_vif, nom, adresse, ville FROM collecte_magasins_referentiel "
            f"WHERE code_vif IN ({marque.rstrip(',')}) AND adresse IS NOT NULL AND adresse != ''",
            list(codes_disparus)
        ).fetchall()
        marque = "?," * len(codes_actuels)
        actuels = conn.execute(
            f"SELECT code_vif, nom, adresse, ville FROM collecte_magasins_referentiel "
            f"WHERE code_vif IN ({marque.rstrip(',')}) AND adresse IS NOT NULL AND adresse != ''",
            list(codes_actuels)
        ).fetchall()
    actuels_par_adresse = {}
    for m in actuels:
        actuels_par_adresse.setdefault((m["adresse"], m["ville"]), []).append(m)

    rachats = []
    for ancien in anciens:
        candidats = [
            m for m in actuels_par_adresse.get((ancien["adresse"], ancien["ville"]), [])
            if m["code_vif"] != ancien["code_vif"]
        ]
        if len(candidats) == 1 and candidats[0]["nom"].strip().lower() != ancien["nom"].strip().lower():
            rachats.append({"ancien": ancien["nom"], "nouveau": candidats[0]["nom"]})
    return rachats


FILTRES_JOURS_STATISTIQUES_ENSEIGNES = {
    "vendredi_samedi": ("Vendredi et samedi", ("Vendredi", "Samedi")),
    "jeudi": ("Jeudi", ("Jeudi",)),
    "dimanche": ("Dimanche", ("Dimanche",)),
    "tous": ("Tous les jours", ("Jeudi", "Vendredi", "Samedi", "Dimanche")),
}


def _poids_cagette_annee(annee):
    """Poids moyen (kg) d'une cagette pour une année donnée — priorité au
    poids pesé/estimé de la campagne (collecte_campagnes), renseigné en
    cours de campagne. À défaut (campagnes archivées où ces deux colonnes
    sont restées vides alors que le résultat final existe bel et bien —
    constaté sur 2025 : poids_cagette_pese/estime NULL mais
    collecte_magasins_resultats et collecte_cagettes tous deux renseignés),
    dérivé du résultat officiel final divisé par le nombre total de
    cagettes de l'année (toutes demi-journées confondues) — une
    approximation, mais bien meilleure qu'un poids à 0. Numérateur ET
    dénominateur restreints aux magasins actuellement à l'état « Collecté
    par la BAI » dans le référentiel (collecte_magasins_referentiel) —
    sans ce filtre, les magasins en collecte gardée (triés par
    l'association, pas par la BAI) gonflaient le résultat officiel sans
    gonfler les cagettes dans les mêmes proportions, faussant le ratio
    (incident constaté 2026-11-19 : poids 2025 affiché 161 202 kg contre
    128 537 kg attendus). L'état du référentiel est l'état ACTUEL, pas
    celui de l'année calculée : un magasin qui a changé d'état depuis
    reste une source d'écart résiduel, non corrigeable ici."""
    with get_db_connection() as conn:
        campagne = conn.execute("SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)).fetchone()
        if campagne and (campagne["poids_cagette_pese"] or campagne["poids_cagette_estime"]):
            return campagne["poids_cagette_pese"] or campagne["poids_cagette_estime"]
        total_resultat = conn.execute("""
            SELECT SUM(res.resultat_kg)
            FROM collecte_magasins_resultats res
            JOIN collecte_magasins_referentiel ref ON ref.code_vif = res.code_vif
            WHERE res.annee = ? AND ref.etat = 'Collecté par la BAI'
        """, (annee,)).fetchone()[0] or 0
        total_cagettes = conn.execute("""
            SELECT SUM(cc.nb_cagettes)
            FROM collecte_cagettes cc
            JOIN collecte_magasins_referentiel ref ON ref.code_vif = cc.code_vif
            WHERE cc.annee = ? AND ref.etat = 'Collecté par la BAI'
        """, (annee,)).fetchone()[0] or 0
    return (total_resultat / total_cagettes) if total_cagettes else 0


@collecte_bp.route("/collecte/<int:annee>/cagettes/statistiques-enseignes")
@login_required
@require_access("collecte", "lecture")
def statistiques_enseignes(annee):
    """Poids des cagettes (nombre saisi × poids moyen pesé de la cagette)
    réparti par enseigne et sous-enseigne, sur un jour choisi (vendredi+
    samedi par défaut — créneaux de la collecte nationale proprement dite
    — jeudi ou dimanche via le paramètre ?jours=), comparé à l'année
    précédente sur le(s) même(s) créneau(x). Un magasin collecté l'année
    précédente mais plus cette année (fermé, racheté...) n'a pas de ligne
    dans _charger_lignes_cagettes(annee) : son poids N-1 est repris quand
    même, ajouté séparément ci-dessous (demande utilisateur 2026-11-19),
    pour que le total N-1 ne soit jamais sous-estimé par rapport au
    résultat officiel final."""
    filtre_jours = request.args.get("jours", "vendredi_samedi")
    if filtre_jours not in FILTRES_JOURS_STATISTIQUES_ENSEIGNES:
        filtre_jours = "vendredi_samedi"
    libelle_jours, prefixes_jours = FILTRES_JOURS_STATISTIQUES_ENSEIGNES[filtre_jours]
    demi_journees = [dj for dj in moteur.DEMI_JOURNEES if dj.startswith(prefixes_jours)]

    poids_cagette = _poids_cagette_annee(annee)
    poids_cagette_n1 = _poids_cagette_annee(annee - 1)

    lignes = _charger_lignes_cagettes(annee)

    with get_db_connection() as conn:
        codes_annee = {
            r["code_vif"] for r in conn.execute(
                "SELECT DISTINCT code_vif FROM collecte_cagettes_magasins WHERE annee = ?", (annee,)
            ).fetchall()
        }
        magasins_n1 = conn.execute(
            "SELECT DISTINCT code_vif, nom_magasin FROM collecte_cagettes_magasins WHERE annee = ?",
            (annee - 1,)
        ).fetchall()
        saisies_n1_disparus = {
            (r["code_vif"], r["demi_journee"]): r["nb_cagettes"]
            for r in conn.execute(
                "SELECT code_vif, demi_journee, nb_cagettes FROM collecte_cagettes WHERE annee = ?",
                (annee - 1,)
            ).fetchall()
        }
    codes_disparus = set()
    for magasin_n1 in magasins_n1:
        if magasin_n1["code_vif"] in codes_annee:
            continue
        codes_disparus.add(magasin_n1["code_vif"])
        ligne_n1_seule = {"nom_magasin": magasin_n1["nom_magasin"]}
        for dj in moteur.DEMI_JOURNEES:
            ligne_n1_seule["_n1_" + dj] = saisies_n1_disparus.get((magasin_n1["code_vif"], dj))
        lignes.append(ligne_n1_seule)

    rachats = _rachats_detectes(codes_disparus, codes_annee)

    enseignes = {}
    for ligne in lignes:
        nb_vs = sum(ligne.get(dj) or 0 for dj in demi_journees)
        nb_vs_n1 = sum(ligne.get("_n1_" + dj) or 0 for dj in demi_journees)
        if not nb_vs and not nb_vs_n1:
            continue
        enseigne, sous_enseigne = _enseigne_sous_enseigne(ligne["nom_magasin"])
        bloc_enseigne = enseignes.setdefault(enseigne, {"poids": 0, "poids_n1": 0, "sous_enseignes": {}})
        bloc_sous = bloc_enseigne["sous_enseignes"].setdefault(sous_enseigne, {"poids": 0, "poids_n1": 0})
        poids = round(nb_vs * poids_cagette, 2)
        poids_n1 = round(nb_vs_n1 * poids_cagette_n1, 2)
        bloc_enseigne["poids"] += poids
        bloc_enseigne["poids_n1"] += poids_n1
        bloc_sous["poids"] += poids
        bloc_sous["poids_n1"] += poids_n1

    total_general = sum(b["poids"] for b in enseignes.values())
    total_general_n1 = sum(b["poids_n1"] for b in enseignes.values())

    resultats = []
    for enseigne, bloc in sorted(enseignes.items(), key=lambda kv: kv[1]["poids"], reverse=True):
        sous_tries = sorted(bloc["sous_enseignes"].items(), key=lambda kv: kv[1]["poids"], reverse=True)
        resultats.append({
            "enseigne": enseigne,
            "poids": round(bloc["poids"], 2),
            "poids_n1": round(bloc["poids_n1"], 2),
            "part": (bloc["poids"] / total_general * 100) if total_general else 0,
            "sous_enseignes": [
                {"nom": nom, "poids": round(v["poids"], 2), "poids_n1": round(v["poids_n1"], 2)}
                for nom, v in sous_tries
                if len(bloc["sous_enseignes"]) > 1 or nom != enseigne
            ],
        })

    analyse = None
    if resultats and total_general and total_general_n1:
        leader = resultats[0]
        # Candidats aux "plus fortes variations" : au moins 2% du total N-1,
        # pour ne pas remonter un + ou -100% sur une enseigne anecdotique
        # (quelques kg d'une année sur l'autre).
        seuil = total_general_n1 * 0.02
        candidats = [e for e in resultats if e["poids_n1"] >= seuil]
        for e in candidats:
            e["evolution_pct"] = (e["poids"] - e["poids_n1"]) / e["poids_n1"] * 100
        candidats_tries = sorted(candidats, key=lambda e: e["evolution_pct"], reverse=True)
        plus_forte_hausse = candidats_tries[0] if candidats_tries and candidats_tries[0]["evolution_pct"] > 0 else None
        plus_forte_baisse = candidats_tries[-1] if candidats_tries and candidats_tries[-1]["evolution_pct"] < 0 else None
        analyse = {
            "leader": leader["enseigne"],
            "leader_part": leader["part"],
            "leader_evolution": (leader["poids"] - leader["poids_n1"]) / leader["poids_n1"] * 100 if leader["poids_n1"] else None,
            "evolution_generale": (total_general - total_general_n1) / total_general_n1 * 100,
            "plus_forte_hausse": plus_forte_hausse,
            "plus_forte_baisse": plus_forte_baisse,
        }

    return render_template(
        "collecte/statistiques_enseignes.html",
        annee=annee,
        resultats=resultats,
        total_general=round(total_general, 2),
        total_general_n1=round(total_general_n1, 2),
        poids_cagette=poids_cagette,
        poids_cagette_n1=poids_cagette_n1,
        filtre_jours=filtre_jours,
        libelle_jours=libelle_jours,
        filtres_jours=FILTRES_JOURS_STATISTIQUES_ENSEIGNES,
        analyse=analyse,
        rachats=rachats,
    )


POIDS_CAGETTE_REFERENCE_KG = 11.0  # taille IFCO changée, ex 14 kg
POIDS_CAGETTE_TOLERANCE = 0.20


def _alerte_coherence_poids_cagette(poids_pese):
    """Avertissement (non bloquant) si le poids de la cagette calculé après
    pesée s'écarte de plus de 20% de la référence habituelle (~11 kg) —
    erreur de saisie fréquente (total pesé en kg confondu avec un autre
    poids, cagettes mal comptées…), mais une vraie campagne peut
    légitimement s'en écarter : on prévient, on n'empêche pas d'enregistrer."""
    if poids_pese is None:
        return None
    borne_basse = POIDS_CAGETTE_REFERENCE_KG * (1 - POIDS_CAGETTE_TOLERANCE)
    borne_haute = POIDS_CAGETTE_REFERENCE_KG * (1 + POIDS_CAGETTE_TOLERANCE)
    if borne_basse <= poids_pese <= borne_haute:
        return None
    return (
        f"⚠️ Poids de la cagette calculé inhabituel : {poids_pese:.2f} kg "
        f"(attendu environ {POIDS_CAGETTE_REFERENCE_KG:.0f} kg ± {POIDS_CAGETTE_TOLERANCE * 100:.0f}%, "
        f"soit {borne_basse:.1f} à {borne_haute:.1f} kg) — vérifiez le poids total pesé et le nombre de "
        f"cagettes avant de vous y fier."
    )


@collecte_bp.route("/collecte/<int:annee>/cagettes/poids", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_poids_cagette(annee):
    def _valeur_ou_none(nom_champ):
        brut = request.form.get(nom_champ, "").strip().replace(",", ".")
        if not brut:
            return None
        try:
            return float(brut)
        except ValueError:
            return None

    poids_estime = _valeur_ou_none("poids_cagette_estime")
    poids_total_pese = _valeur_ou_none("poids_total_pese")

    with get_db_connection() as conn:
        total_cagettes = conn.execute(
            "SELECT COALESCE(SUM(nb_cagettes), 0) FROM collecte_cagettes WHERE annee = ?", (annee,)
        ).fetchone()[0]
        poids_pese = (poids_total_pese / total_cagettes) if poids_total_pese and total_cagettes else None
        conn.execute(
            "UPDATE collecte_campagnes SET poids_cagette_estime = ?, poids_total_pese = ?, poids_cagette_pese = ? WHERE annee = ?",
            (poids_estime, poids_total_pese, poids_pese, annee),
        )
        conn.commit()

    write_log(f"⚖️ Collecte {annee} : poids cagette mis à jour par {current_user.email} "
              f"(estimé={poids_estime}, total pesé={poids_total_pese}, cagette après pesée={poids_pese})")
    flash("✅ Poids de la cagette enregistré.", "success")
    alerte = _alerte_coherence_poids_cagette(poids_pese)
    if alerte:
        flash(alerte, "warning")
    return redirect(url_for("collecte.cagettes", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/cagettes/poids/recuperer-palox", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def recuperer_poids_palox_cagette(annee):
    """Remplit « Poids total pesé » avec le total déjà calculé par la Pesée
    palox (même pesée physique à l'entrepôt, juste détaillée par catégorie
    là-bas) — évite de retaper à la main un chiffre déjà en base."""
    poids_total_pese = _synthese_palox(annee)["poids_total"]
    if not poids_total_pese:
        flash(f"⚠️ Aucune pesée palox trouvée pour {annee}, rien à récupérer.", "warning")
        return redirect(url_for("collecte.cagettes", annee=annee))

    with get_db_connection() as conn:
        poids_estime = conn.execute(
            "SELECT poids_cagette_estime FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
        poids_estime = poids_estime["poids_cagette_estime"] if poids_estime else None
        total_cagettes = conn.execute(
            "SELECT COALESCE(SUM(nb_cagettes), 0) FROM collecte_cagettes WHERE annee = ?", (annee,)
        ).fetchone()[0]
        poids_pese = (poids_total_pese / total_cagettes) if total_cagettes else None
        conn.execute(
            "UPDATE collecte_campagnes SET poids_cagette_estime = ?, poids_total_pese = ?, poids_cagette_pese = ? WHERE annee = ?",
            (poids_estime, poids_total_pese, poids_pese, annee),
        )
        conn.commit()

    write_log(
        f"⚖️ Collecte {annee} : poids total pesé repris depuis Pesée palox ({poids_total_pese} kg) "
        f"par {current_user.email}"
    )
    flash(f"✅ Poids total pesé récupéré depuis Pesée palox : {poids_total_pese} kg.", "success")
    alerte = _alerte_coherence_poids_cagette(poids_pese)
    if alerte:
        flash(alerte, "warning")
    return redirect(url_for("collecte.cagettes", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/cagettes/initialiser", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def initialiser_cagettes(annee):
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

    if not campagne or not _fichier_drive_disponible(annee, "magasins"):
        flash(f"⛔ Liste des magasins {annee} requise avant d'initialiser la saisie cagettes", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    try:
        magasins = _lire_referentiel_magasins_bai(annee, campagne)
    except RuntimeError as erreur:
        flash(f"⛔ {erreur}", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M")

    with get_db_connection() as conn:
        conn.execute("DELETE FROM collecte_cagettes_magasins WHERE annee = ?", (annee,))
        for magasin in magasins:
            for dj in magasin["demi_journees"]:
                conn.execute("""
                    INSERT INTO collecte_cagettes_magasins (annee, code_vif, nom_magasin, demi_journee)
                    VALUES (?, ?, ?, ?)
                """, (annee, magasin["code_vif"], magasin["nom_magasin"], dj))
        conn.execute("""
            UPDATE collecte_campagnes SET cagettes_initialisee_le = ?, cagettes_initialisee_par = ?
            WHERE annee = ?
        """, (maintenant, current_user.email, annee))
        conn.commit()

    write_log(
        f"🔒 Collecte {annee} : liste des magasins figée pour la saisie cagettes par "
        f"{current_user.email} — {len(magasins)} magasin(s)"
    )
    flash(f"🔒 Liste des magasins figée ({len(magasins)} magasins)", "success")
    return redirect(url_for("collecte.cagettes", annee=annee))


@collecte_bp.route("/collecte/cagettes/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_cagettes():
    data = request.get_json(force=True) or {}
    annee = data.get("annee")
    lignes = data.get("lignes") or []

    if not isinstance(annee, int):
        return jsonify({"success": False, "error": "Année invalide"}), 400

    verrou = _resultat_bai_verrouille(annee)
    if verrou:
        return jsonify({
            "success": False,
            "error": f"🔒 Résultat collecte BAI {annee} verrouillé depuis le {verrou['le']} — déverrouillez-le d'abord.",
        }), 423

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_db_connection() as conn:
        for ligne in lignes:
            code_vif = str(ligne.get("code_vif", "")).strip()
            demi_journee = str(ligne.get("demi_journee", "")).strip()
            if not code_vif or not demi_journee:
                continue
            nb_cagettes = ligne.get("nb_cagettes")
            if nb_cagettes in ("", None):
                nb_cagettes = None
            else:
                try:
                    nb_cagettes = int(nb_cagettes)
                except (ValueError, TypeError):
                    continue
                if nb_cagettes < 0:
                    continue
            conn.execute("""
                INSERT INTO collecte_cagettes
                    (annee, code_vif, demi_journee, nb_cagettes, saisi_le, saisi_par)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(annee, code_vif, demi_journee)
                DO UPDATE SET nb_cagettes = excluded.nb_cagettes,
                              saisi_le = excluded.saisi_le,
                              saisi_par = excluded.saisi_par
            """, (annee, code_vif, demi_journee, nb_cagettes, maintenant, current_user.email))
        conn.commit()

    write_log(
        f"🧺 Collecte {annee} : cagettes saisies par {current_user.email} — {len(lignes)} ligne(s)"
    )
    return jsonify({"success": True})


@collecte_bp.route("/collecte/resultats/<int:generation_id>/carte_secteurs")
@login_required
@require_access("collecte", "lecture")
def resultats_carte_secteurs(generation_id):
    generation = _get_generation_ou_redirect(generation_id)
    if generation is None:
        return redirect(url_for("collecte.collecte_main"))

    if not generation["fichier_carte_secteurs"]:
        flash("⛔ Pas de carte des secteurs pour cette génération", "warning")
        return redirect(url_for("collecte.resultats", generation_id=generation_id))

    chemin = os.path.join(_dossier_resultats(generation["annee"]), generation["fichier_carte_secteurs"])
    return send_file(chemin, mimetype="text/html")


@collecte_bp.route("/collecte/resultats/<int:generation_id>/carte_tournees")
@login_required
@require_access("collecte", "lecture")
def resultats_carte_tournees(generation_id):
    generation = _get_generation_ou_redirect(generation_id)
    if generation is None:
        return redirect(url_for("collecte.collecte_main"))

    if not generation["fichier_carte_tournees"]:
        flash("⛔ Pas de carte des tournées pour cette génération", "warning")
        return redirect(url_for("collecte.resultats", generation_id=generation_id))

    chemin = os.path.join(_dossier_resultats(generation["annee"]), generation["fichier_carte_tournees"])
    return send_file(chemin, mimetype="text/html")


@collecte_bp.route("/collecte/resultats/<int:generation_id>/excel")
@login_required
@require_access("collecte", "lecture")
def resultats_excel(generation_id):
    generation = _get_generation_ou_redirect(generation_id)
    if generation is None:
        return redirect(url_for("collecte.collecte_main"))

    chemin = os.path.join(_dossier_resultats(generation["annee"]), generation["fichier_excel"])
    return send_file(
        chemin,
        as_attachment=True,
        download_name=generation["fichier_excel"],
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/resultats/<int:generation_id>/pdf-equipier")
@login_required
@require_access("collecte", "lecture")
def resultats_pdf_equipier(generation_id):
    generation = _get_generation_ou_redirect(generation_id)
    if generation is None:
        return redirect(url_for("collecte.collecte_main"))
    if not generation["fichier_pdf_equipier"]:
        flash("⛔ Pas de PDF équipier pour cette génération (disponible uniquement pour la simulation avec capacité camions)", "warning")
        return redirect(url_for("collecte.resultats", generation_id=generation_id))

    chemin = os.path.join(_dossier_resultats(generation["annee"]), generation["fichier_pdf_equipier"])
    return send_file(
        chemin,
        as_attachment=True,
        download_name=generation["fichier_pdf_equipier"],
        mimetype="application/pdf",
    )


@collecte_bp.route("/collecte/resultats/<int:generation_id>/pdf", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def resultats_pdf(generation_id):
    """Le PDF complet reste accessible en GET (lien direct, historique) ; un
    POST avec 'lignes_filtrees' (JSON, liste ORDONNÉE de [camion,
    demi_journee]) restreint ET réordonne le PDF pour correspondre
    exactement aux lignes et à l'ordre actuellement affichés dans le
    tableau Tabulator (groupes inclus — cf. telechargerPdfFiltre() dans
    resultats.html) : on ne se contente pas de filtrer, l'ordre d'arrivée
    de cette liste prime sur l'ordre naturel de _charger_tournees()."""
    generation = _get_generation_ou_redirect(generation_id)
    if generation is None:
        return redirect(url_for("collecte.collecte_main"))

    lignes = _charger_tournees(generation)

    cles_brutes = request.form.get("lignes_filtrees", "").strip()
    if cles_brutes:
        try:
            cles = [tuple(c) for c in json.loads(cles_brutes)]
            lignes_par_cle = {(l["camion"], l["demi_journee"]): l for l in lignes}
            lignes = [lignes_par_cle[c] for c in cles if c in lignes_par_cle]
        except (ValueError, TypeError, IndexError):
            pass

    html = render_template(
        "collecte/resultats_pdf.html", annee=generation["annee"], lignes=lignes,
        generation=generation,
        params=json.loads(generation["parametres"]) if generation["parametres"] else {},
        org=get_organisation(),
    )

    pdf_buffer = io.BytesIO()
    HTML(string=html).write_pdf(pdf_buffer)
    pdf_buffer.seek(0)

    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=os.path.splitext(generation["fichier_excel"])[0] + ".pdf",
        mimetype="application/pdf",
    )


def _date_fr_courte(iso):
    """Convertit une date ISO (AAAA-MM-JJ, celle du <input type=date>) en
    JJ/MM/AAAA (format attendu par --date-jeudi du script de production).
    Retourne None si iso est vide ou invalide."""
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return None


def _dossier_fiches_camion_test(annee):
    return os.path.join(_dossier_annee(annee), "fiches_camion_test_vision")


def _ensure_table_cagettes_test_vision(conn):
    """Essai (demande utilisateur 2026-11-20) : saisie automatique du nombre
    de cagettes ramenées par détection vision Claude sur la fiche camion
    scannée, au lieu de la saisie manuelle actuelle — table purement
    ADDITIVE et séparée de la vraie collecte_cagettes (même clé
    (année, code_vif, demi_journee)), aucune écriture jamais dans la vraie
    table : objectif de pouvoir comparer réel vs détecté avant de décider
    si ça remplace un jour la saisie manuelle (cf.
    _detecter_fiche_camion_background, essai_vision_fiche_camion)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_cagettes_test_vision (
            annee INTEGER NOT NULL,
            code_vif TEXT NOT NULL,
            demi_journee TEXT NOT NULL,
            nb_cagettes INTEGER,
            vehicule TEXT,
            quai TEXT,
            detecte_le TEXT,
            detecte_par TEXT,
            fiche_nom TEXT,
            PRIMARY KEY (annee, code_vif, demi_journee)
        )
    """)
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_cagettes_test_vision)").fetchall()}
    if "detection_id" not in colonnes:
        conn.execute("ALTER TABLE collecte_cagettes_test_vision ADD COLUMN detection_id INTEGER")
    if "page_num" not in colonnes:
        conn.execute("ALTER TABLE collecte_cagettes_test_vision ADD COLUMN page_num INTEGER")


def _ensure_table_detection_fiche_camion(conn):
    """Détections automatiques (upload d'une fiche camion scannée, remplie
    à la main) du nombre de cagettes ramenées par magasin/tournée — cf.
    _ensure_table_cagettes_test_vision (table résultat, jamais la vraie
    collecte_cagettes)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_detection_fiche_camion (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annee INTEGER NOT NULL,
            statut TEXT NOT NULL DEFAULT 'en_cours',
            resultat_detecte TEXT,
            erreur TEXT,
            lance_le TEXT,
            lance_par TEXT,
            pdf_nom TEXT,
            pages_traitees INTEGER DEFAULT 0,
            pages_total INTEGER
        )
    """)
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_detection_fiche_camion)").fetchall()}
    if "pages_traitees" not in colonnes:
        conn.execute("ALTER TABLE collecte_detection_fiche_camion ADD COLUMN pages_traitees INTEGER DEFAULT 0")
    if "pages_total" not in colonnes:
        conn.execute("ALTER TABLE collecte_detection_fiche_camion ADD COLUMN pages_total INTEGER")


def _prompt_detection_fiche_camion():
    demi_journees_connues = "\n".join(f"- {dj}" for dj in moteur.DEMI_JOURNEES)
    return (
        "Cette page PDF est UNE PAGE d'une « fiche camion » de collecte alimentaire, générée par "
        "informatique puis remplie à la main par le chauffeur après sa tournée, avant d'être "
        "scannée (plusieurs fiches, une par véhicule, peuvent être scannées ensemble en un seul "
        "PDF multi-pages, mais cette page ne porte que sur UN SEUL véhicule). En haut de page : une "
        "année, une demi-journée (ex. « Vendredi Matin »), un véhicule (code entre parenthèses ou "
        "en gras, ex. « V001 »), un quai, et une case « Enregistré VIF ».\n\n"
        "Le corps de la fiche contient un bloc par magasin visité : nom du magasin, adresse, "
        "horaires, et un CODE VIF imprimé (ex. 02380004) — ce code imprimé est fiable, utilise-le "
        "pour identifier le magasin, jamais le nom seul. Pour chaque magasin, 4 colonnes "
        "« Tournée 1 » à « Tournée 4 » (le même camion peut repasser plusieurs fois au même "
        "magasin dans la demi-journée) : chacune a une ligne « prendre N » imprimée (nombre de "
        "caisses vides prévues, à IGNORER COMPLÈTEMENT) puis une ligne « cagettes ramenées » où le "
        "chauffeur écrit à la main le nombre de cagettes rapportées après cette tournée. Une "
        "colonne séparée « Collecté dimanche » peut aussi porter un nombre manuscrit pour ce même "
        "magasin (tournée du dimanche, reportée sur cette même fiche).\n\n"
        "RÈGLE ABSOLUE sur la ligne « prendre » : ne lis JAMAIS un chiffre qui s'y trouve comme "
        "s'il s'agissait du nombre de cagettes ramenées, quelle que soit sa forme — même s'il est "
        "manuscrit, même s'il remplace (en le rayant) le nombre imprimé d'origine, même s'il "
        "ressemble à un 0 ou tout autre chiffre qui te semblerait pertinent. Un chauffeur corrige "
        "parfois le nombre de caisses vides prévues directement sur cette ligne (ex. un « 0 » "
        "manuscrit qui raye un « 22 » imprimé, pour dire qu'il n'a finalement pas pris de caisses "
        "vides pour cette tournée) : ce chiffre ne concerne QUE les caisses vides à prendre, jamais "
        "les cagettes ramenées — la seule valeur à rendre pour chaque tournée est celle écrite sur "
        "la ligne « cagettes ramenées » elle-même (ou dans l'espace juste en dessous si elle sert "
        "de correction, cf. plus bas), jamais celle de la ligne « prendre » au-dessus.\n\n"
        "Pour chaque case « cagettes ramenées » (ou « Collecté dimanche ») : lis UNIQUEMENT un "
        "nombre entier s'il y en a un clairement écrit (0 compris — un 0 écrit est une vraie "
        "valeur, pas une case vide). Si la case est vide, ou si ce qui y est écrit n'est "
        "manifestement pas un nombre (un mot, un paraphe, une remarque manuscrite), ne mets RIEN "
        "pour cette tournée (null) plutôt que de deviner — une tournée visiblement non réalisée "
        "(case « prendre » elle-même vide/non imprimée) n'a de toute façon aucune valeur à lire. "
        "N'additionne rien toi-même, rends une valeur par tournée séparément.\n\n"
        "ATTENTION aux corrections manuscrites, TRÈS fréquentes sur ces fiches : il arrive qu'un "
        "chauffeur se trompe en écrivant sur la ligne imprimée « cagettes ramenées », rature ou "
        "griffonne illisiblement par-dessus son premier chiffre, PUIS réécrive le bon nombre en "
        "GROS, plus bas, dans l'espace blanc encore libre sous cette ligne (cet espace existe "
        "justement pour ça). Quand tu vois à la fois (a) un gribouillis raturé/illisible sur la "
        "ligne « cagettes ramenées » elle-même et (b) un chiffre plus grand et plus net écrit "
        "juste en dessous dans l'espace blanc, c'est TOUJOURS ce second chiffre, le plus grand, le "
        "plus bas et le plus net, qui est la valeur corrigée à retenir — ignore complètement le "
        "gribouillis raturé du dessus, ne tente même pas de le déchiffrer. Un simple trait "
        "horizontal après ou autour de ce chiffre du dessous (soulignement, fin de geste du "
        "stylo) n'en fait PAS une rature : ne le compte pas comme barré pour autant.\n\n"
        "Si cette page n'est PAS une fiche camion (page de garde, page blanche, autre document), "
        "réponds avec un objet magasins vide et une demi_journee/vehicule vides.\n\n"
        "La demi-journée en haut de page doit être rendue EXACTEMENT comme l'une de ces valeurs "
        f"(recopie-la à l'identique, jamais une autre orthographe) :\n{demi_journees_connues}\n\n"
        "Réponds UNIQUEMENT avec un objet JSON de cette forme exacte, sans aucun texte autour "
        "(exemple avec 2 magasins) :\n"
        '{"demi_journee": "Vendredi Matin", "vehicule": "V001", "quai": "P6", '
        '"magasins": {"02380004": {"tournees": [16, null, null, null], "dimanche": null}, '
        '"02380273": {"tournees": [0, null, null, null], "dimanche": null}}}'
    )


def _ensure_table_cache_page_fiche_camion(conn):
    """Cache par page (demande utilisateur 2026-11-25) : évite de rappeler
    l'API Claude pour une page déjà analysée avec succès, si la MÊME page
    (octets identiques) est soumise à nouveau — cas typique d'un nouvel
    upload qui reprend après une page en erreur, ou d'un re-test. Clé =
    sha256 des octets de la page PDF extraite (indépendant de
    codes_connus : le filtrage par magasins connus reste appliqué après
    lecture du cache, cf. _detecter_fiche_camion_une_page)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_detection_fiche_camion_cache_page (
            page_hash TEXT PRIMARY KEY,
            resultat_json TEXT NOT NULL,
            mis_en_cache_le TEXT
        )
    """)


def _detecter_fiche_camion_une_page_brute(pdf_bytes_page):
    """Un seul appel API Claude (vision/document) sur UNE page de fiche
    camion — cf. _detecter_fiche_camion_background pour le découpage
    page par page d'un scan multi-pages (un scan complet de demi-journée
    peut porter une vingtaine de véhicules, bien au-delà du budget de
    réflexion du modèle si on lui envoie tout en un seul appel, cf.
    incident 2026-11-24 « max_tokens », déjà rencontré sur la fiche
    association). Lève une exception si l'appel échoue ; retourne
    toujours un dict (vide si la page n'est pas une fiche camion), SANS
    filtrage par magasins connus (cf. _detecter_fiche_camion_une_page)."""
    import base64, json as _json
    api_key = os.getenv("CLAUDE_API_KEY")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY absente du .env")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.b64encode(pdf_bytes_page).decode("ascii"),
                }},
                {"type": "text", "text": _prompt_detection_fiche_camion()},
            ],
        }],
    )
    texte = "".join(bloc.text for bloc in response.content if bloc.type == "text").strip()
    if not texte:
        raise RuntimeError(
            f"Réponse vide du modèle (stop_reason={response.stop_reason!r}) — "
            "page probablement trop complexe pour le budget de réflexion."
        )
    if texte.startswith("```"):
        texte = texte.strip("`")
        if texte.lower().startswith("json"):
            texte = texte[4:]
    data = _json.loads(texte)

    def _entier_ou_rien(v):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None

    demi_journee = str(data.get("demi_journee") or "").strip()
    vehicule = str(data.get("vehicule") or "").strip()
    quai = str(data.get("quai") or "").strip()
    magasins = {}
    for code_vif, valeurs in (data.get("magasins") or {}).items():
        code_vif = str(code_vif).strip()
        tournees = [_entier_ou_rien(v) for v in (valeurs.get("tournees") or [])]
        tournees_valides = [v for v in tournees if v is not None]
        dimanche = _entier_ou_rien(valeurs.get("dimanche"))
        magasins[code_vif] = {
            "tournees": tournees,
            "total_tournees": sum(tournees_valides) if tournees_valides else None,
            "dimanche": dimanche,
        }
    return {"demi_journee": demi_journee, "vehicule": vehicule, "quai": quai, "magasins": magasins}


def _detecter_fiche_camion_une_page(pdf_bytes_page, codes_connus):
    """Comme _detecter_fiche_camion_une_page_brute, mais en passant par le
    cache par page (évite l'appel API si cette page exacte a déjà été
    analysée avec succès) puis en filtrant le résultat aux seuls magasins
    de codes_connus (fait ici et non dans le cache, pour rester valable
    même si codes_connus change entre deux analyses). La clé de cache
    inclut le texte du prompt : toute modification du prompt (consigne
    affinée suite à une erreur constatée) invalide donc automatiquement
    les anciennes pages en cache, sans purge manuelle à prévoir."""
    import hashlib, json as _json

    cle_brute = _prompt_detection_fiche_camion().encode("utf-8") + b"|" + pdf_bytes_page
    page_hash = hashlib.sha256(cle_brute).hexdigest()
    with get_db_connection() as conn:
        _ensure_table_cache_page_fiche_camion(conn)
        ligne = conn.execute(
            "SELECT resultat_json FROM collecte_detection_fiche_camion_cache_page WHERE page_hash = ?",
            (page_hash,)
        ).fetchone()

    if ligne:
        data = _json.loads(ligne["resultat_json"])
    else:
        data = _detecter_fiche_camion_une_page_brute(pdf_bytes_page)
        with get_db_connection() as conn:
            _ensure_table_cache_page_fiche_camion(conn)
            conn.execute("""
                INSERT INTO collecte_detection_fiche_camion_cache_page (page_hash, resultat_json, mis_en_cache_le)
                VALUES (?, ?, ?)
                ON CONFLICT(page_hash) DO NOTHING
            """, (page_hash, _json.dumps(data, ensure_ascii=False), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()

    magasins = {
        code_vif: valeurs for code_vif, valeurs in data["magasins"].items()
        if code_vif in codes_connus
    }
    return {
        "demi_journee": data["demi_journee"], "vehicule": data["vehicule"],
        "quai": data["quai"], "magasins": magasins, "depuis_cache": bool(ligne),
    }


def _detecter_fiche_camion_background(app, detection_id, annee, pdf_bytes, codes_connus):
    """Découpe le PDF page par page (pypdf) et envoie CHAQUE page séparément
    à l'API Claude (cf. _detecter_fiche_camion_une_page) — un scan complet
    de demi-journée contient une page par véhicule (observé : jusqu'à
    29 pages), largement au-delà de ce qu'un seul appel peut lire sans
    épuiser son budget de réflexion (cf. _detecter_pdf_association_background,
    même symptôme stop_reason='max_tokens'). La progression
    (pages_traitees/pages_total) est mise à jour après chaque page pour que
    l'écran puisse l'afficher. Une page en échec n'interrompt pas les
    suivantes : son erreur est juste rapportée en avertissement à la fin.
    Résultats cumulés (sommés si un même (code_vif, demi_journee) revient
    sur plusieurs pages — plusieurs véhicules ayant visité le même magasin
    la même demi-journée) puis écrits directement dans la table d'essai
    collecte_cagettes_test_vision (jamais la vraie collecte_cagettes) : pas
    d'étape de validation manuelle supplémentaire, cette table étant déjà,
    par construction, un essai de comparaison sans aucun risque sur la
    saisie réelle."""
    import json as _json
    from pypdf import PdfReader, PdfWriter
    import io as _io

    with app.app_context():
        pages_en_erreur = []
        try:
            reader = PdfReader(_io.BytesIO(pdf_bytes))
            nb_pages = len(reader.pages)
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE collecte_detection_fiche_camion SET pages_total = ? WHERE id = ?",
                    (nb_pages, detection_id)
                )
                conn.commit()

            # (code_vif, demi_journee) -> accumulateur
            accumulateur = {}
            vehicules_par_cle = {}
            pages_par_cle = {}
            pages_depuis_cache = 0

            for i in range(nb_pages):
                try:
                    writer = PdfWriter()
                    writer.add_page(reader.pages[i])
                    tampon = _io.BytesIO()
                    writer.write(tampon)
                    resultat_page = _detecter_fiche_camion_une_page(tampon.getvalue(), codes_connus)
                    if resultat_page.get("depuis_cache"):
                        pages_depuis_cache += 1
                except Exception as e:
                    pages_en_erreur.append(f"page {i + 1} : {e}")
                    resultat_page = None

                if resultat_page:
                    demi_journee = resultat_page["demi_journee"]
                    vehicule = resultat_page["vehicule"]
                    quai = resultat_page["quai"]
                    if demi_journee and demi_journee not in moteur.DEMI_JOURNEES:
                        pages_en_erreur.append(f"page {i + 1} : demi-journée non reconnue {demi_journee!r}")
                    else:
                        for code_vif, valeurs in resultat_page["magasins"].items():
                            if valeurs["total_tournees"] is not None and demi_journee:
                                cle = (code_vif, demi_journee)
                                accumulateur[cle] = (accumulateur.get(cle) or 0) + valeurs["total_tournees"]
                                vehicules_par_cle[cle] = vehicule
                                pages_par_cle[cle] = i + 1
                            if valeurs["dimanche"] is not None and "Dimanche Matin" in moteur.DEMI_JOURNEES:
                                cle = (code_vif, "Dimanche Matin")
                                accumulateur[cle] = (accumulateur.get(cle) or 0) + valeurs["dimanche"]
                                vehicules_par_cle[cle] = vehicule
                                pages_par_cle[cle] = i + 1

                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE collecte_detection_fiche_camion SET pages_traitees = ? WHERE id = ?",
                        (i + 1, detection_id)
                    )
                    conn.commit()

            maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with get_db_connection() as conn:
                _ensure_table_cagettes_test_vision(conn)
                for (code_vif, demi_journee), total in accumulateur.items():
                    conn.execute("""
                        INSERT INTO collecte_cagettes_test_vision
                            (annee, code_vif, demi_journee, nb_cagettes, vehicule, quai, detecte_le, detecte_par, fiche_nom, detection_id, page_num)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(annee, code_vif, demi_journee) DO UPDATE SET
                            nb_cagettes = excluded.nb_cagettes, vehicule = excluded.vehicule,
                            quai = excluded.quai, detecte_le = excluded.detecte_le,
                            detecte_par = excluded.detecte_par, fiche_nom = excluded.fiche_nom,
                            detection_id = excluded.detection_id, page_num = excluded.page_num
                    """, (annee, code_vif, demi_journee, total, vehicules_par_cle.get((code_vif, demi_journee)),
                          None, maintenant, "detection_vision", f"detection_{detection_id}.pdf",
                          detection_id, pages_par_cle.get((code_vif, demi_journee))))
                conn.execute(
                    "UPDATE collecte_detection_fiche_camion SET statut = 'termine', resultat_detecte = ?, erreur = ? WHERE id = ?",
                    (
                        _json.dumps({f"{c}|{dj}": t for (c, dj), t in accumulateur.items()}, ensure_ascii=False),
                        ("; ".join(pages_en_erreur) if pages_en_erreur else None),
                        detection_id,
                    )
                )
                conn.commit()
            write_log(
                f"🤖 Collecte {annee} : détection fiche camion #{detection_id} terminée "
                f"({nb_pages} page(s), {len(accumulateur)} ligne(s) magasin/demi-journée"
                + (f", {pages_depuis_cache} page(s) réutilisée(s) du cache" if pages_depuis_cache else "")
                + (f", {len(pages_en_erreur)} page(s) en erreur" if pages_en_erreur else "") + ")"
            )

        except Exception as e:
            write_log(f"❌ Collecte {annee} : erreur détection fiche camion #{detection_id} — {e}")
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE collecte_detection_fiche_camion SET statut = 'erreur', erreur = ? WHERE id = ?",
                    (str(e), detection_id)
                )
                conn.commit()


@collecte_bp.route("/collecte/<int:annee>/essai-vision-camion", methods=["GET"])
@login_required
@require_access("collecte", "lecture")
def essai_vision_fiche_camion(annee):
    """Page d'essai (demande utilisateur 2026-11-20) : upload d'une fiche
    camion scannée, détection vision Claude du nombre de cagettes ramenées,
    et tableau de comparaison réel (collecte_cagettes) vs détecté
    (collecte_cagettes_test_vision) magasin par magasin, pour décider si ça
    remplace un jour la saisie manuelle. Aucune écriture dans la vraie
    collecte_cagettes."""
    with get_db_connection() as conn:
        _ensure_table_cagettes_test_vision(conn)
        _ensure_table_detection_fiche_camion(conn)
        reels = {
            (r["code_vif"], r["demi_journee"]): r["nb_cagettes"]
            for r in conn.execute(
                "SELECT code_vif, demi_journee, nb_cagettes FROM collecte_cagettes WHERE annee = ?", (annee,)
            ).fetchall()
        }
        detectes = {
            (r["code_vif"], r["demi_journee"]): dict(r)
            for r in conn.execute(
                "SELECT * FROM collecte_cagettes_test_vision WHERE annee = ?", (annee,)
            ).fetchall()
        }
        noms_magasins = {
            r["code_vif"]: r["nom_magasin"] for r in conn.execute(
                "SELECT DISTINCT code_vif, nom_magasin FROM collecte_cagettes_magasins WHERE annee = ?", (annee,)
            ).fetchall()
        }
        detections = [dict(r) for r in conn.execute(
            "SELECT * FROM collecte_detection_fiche_camion WHERE annee = ? ORDER BY id DESC LIMIT 10", (annee,)
        ).fetchall()]

    cles = sorted(set(reels) | set(detectes), key=lambda c: (noms_magasins.get(c[0], c[0]), c[1]))
    comparaison = []
    for code_vif, demi_journee in cles:
        reel = reels.get((code_vif, demi_journee))
        detecte = detectes.get((code_vif, demi_journee))
        nb_detecte = detecte["nb_cagettes"] if detecte else None
        comparaison.append({
            "code_vif": code_vif,
            "nom": noms_magasins.get(code_vif, code_vif),
            "demi_journee": demi_journee,
            "reel": reel,
            "detecte": nb_detecte,
            "ecart": (nb_detecte - reel) if (reel is not None and nb_detecte is not None) else None,
            "vehicule": detecte["vehicule"] if detecte else None,
            "detection_id": detecte["detection_id"] if detecte else None,
            "page_num": detecte["page_num"] if detecte else None,
        })

    # Mêmes filtres demi-journée/camion que la saisie manuelle (cagettes()),
    # pour retrouver facilement la ligne d'un magasin précis dans une liste
    # potentiellement longue (demande utilisateur 2026-11-24).
    try:
        magasins_par_camion = _charger_magasins_par_camion(annee)
    except Exception as erreur:
        write_log(f"⚠️ Collecte {annee} : filtre camion essai vision camion indisponible ({erreur})")
        magasins_par_camion = {}

    return render_template(
        "collecte/essai_vision_fiche_camion.html",
        annee=annee,
        comparaison=comparaison,
        detections=detections,
        demi_journees=moteur.DEMI_JOURNEES,
        magasins_par_camion=magasins_par_camion,
    )


@collecte_bp.route("/collecte/<int:annee>/essai-vision-camion/detecter", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def essai_vision_fiche_camion_detecter(annee):
    """Upload de la fiche camion scannée (PDF), lance la détection en
    arrière-plan — cf. _detecter_fiche_camion_background."""
    fichier = request.files.get("pdf")
    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier fourni", "warning")
        return redirect(url_for("collecte.essai_vision_fiche_camion", annee=annee))
    if (fichier.mimetype or "") != "application/pdf":
        flash("⛔ Format non supporté (PDF attendu)", "warning")
        return redirect(url_for("collecte.essai_vision_fiche_camion", annee=annee))

    pdf_bytes = fichier.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        flash("⛔ Fichier trop volumineux (10 Mo max)", "warning")
        return redirect(url_for("collecte.essai_vision_fiche_camion", annee=annee))

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_table_detection_fiche_camion(conn)
        cur = conn.execute("""
            INSERT INTO collecte_detection_fiche_camion (annee, statut, lance_le, lance_par, pdf_nom)
            VALUES (?, 'en_cours', ?, ?, ?)
        """, (annee, maintenant, current_user.email, fichier.filename))
        detection_id = cur.lastrowid
        conn.commit()

    dossier = _dossier_fiches_camion_test(annee)
    os.makedirs(dossier, exist_ok=True)
    with open(os.path.join(dossier, f"detection_{detection_id}.pdf"), "wb") as f:
        f.write(pdf_bytes)

    with get_db_connection() as conn:
        codes_connus = {
            r["code_vif"] for r in conn.execute(
                "SELECT DISTINCT code_vif FROM collecte_cagettes_magasins WHERE annee = ?", (annee,)
            ).fetchall()
        }

    app_reel = current_app._get_current_object()
    lancer_tache_fond(
        target=_detecter_fiche_camion_background,
        args=(app_reel, detection_id, annee, pdf_bytes, codes_connus),
        nom="Collecte : detection fiche camion (essai vision)",
        utilisateur=current_user.email,
    )
    write_log(f"🤖 Collecte {annee} : détection fiche camion #{detection_id} lancée par {current_user.email}")
    flash("🤖 Analyse de la fiche lancée (compter 15 à 30 secondes) — rechargez la page pour voir le résultat", "info")
    return redirect(url_for("collecte.essai_vision_fiche_camion", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/essai-vision-camion/feuille/<int:detection_id>/<int:page_num>")
@login_required
@require_access("collecte", "lecture")
def essai_vision_fiche_camion_feuille(annee, detection_id, page_num):
    """Ressert UNE SEULE page (celle du véhicule/demi-journée concerné) du
    PDF uploadé lors de la détection #detection_id, pour comparer
    visuellement avec le détecté en cas d'écart (demande utilisateur
    2026-11-24). Le PDF complet reste sur disque sous
    detection_{detection_id}.pdf (cf. essai_vision_fiche_camion_detecter)."""
    from pypdf import PdfReader, PdfWriter

    chemin = os.path.join(_dossier_fiches_camion_test(annee), f"detection_{detection_id}.pdf")
    if not os.path.isfile(chemin):
        abort(404)
    reader = PdfReader(chemin)
    if not (1 <= page_num <= len(reader.pages)):
        abort(404)
    writer = PdfWriter()
    writer.add_page(reader.pages[page_num - 1])
    tampon = io.BytesIO()
    writer.write(tampon)
    tampon.seek(0)
    return send_file(
        tampon, mimetype="application/pdf", as_attachment=False,
        download_name=f"fiche_camion_{annee}_p{page_num}.pdf",
    )


@collecte_bp.route("/collecte/<int:annee>/cagettes_production")
@login_required
@require_access("collecte", "lecture")
def cagettes_production(annee):
    """Grille éditable du nb de cagettes par magasin/demi-journée (Production),
    une ligne par couple réellement planifié dans le planning véhicules —
    remplace complètement l'ancien fichier Excel Cagettes_magasins.xlsx
    déposé sur le drive : la génération des documents réels (production_generer)
    régénère ce fichier à la volée depuis cette table avant de lancer le
    script, sans jamais relire le drive pour les cagettes.

    Un magasin ENTIÈREMENT sans donnée (aucune valeur en Cagette 1 ou 2 sur
    AUCUNE de ses demi-journées — typiquement un nouveau magasin, ou tout
    simplement le tout début d'une nouvelle campagne) reçoit automatiquement
    une valeur par défaut sur toutes ses lignes (auto_defaut=1) : ses propres
    valeurs Cagette 1-4 de l'année précédente (même magasin, même
    demi-journée) si elles existent, sinon 10/10 — directement depuis la
    base, sans export/import Excel. Un magasin déjà documenté sur au moins
    une demi-journée n'est jamais concerné, même si une autre de ses
    demi-journées est encore vide — un trou isolé sur un magasin déjà connu
    reste un vrai manque à saisir, pas un nouveau magasin
    (cf. la discussion : 87 lignes vides au total, mais seulement ~20
    magasins réellement nouveaux). La ligne reste en évidence (jaune) tant
    que ces valeurs n'ont pas été modifiées à la main (cf.
    cagettes_production_enregistrer)."""
    combos = _combos_cagettes_production(annee)
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_table_cagettes_production(conn)
        lignes_db = conn.execute(
            "SELECT * FROM collecte_cagettes_production WHERE annee = ?", (annee,)
        ).fetchall()
        valeurs = {(l["code_vif"], l["jour"]): l for l in lignes_db}

        # Un magasin est considéré "déjà documenté" dès qu'une de ses lignes
        # a une vraie valeur (saisie à la main ou importée, donc auto_defaut=0)
        # — les lignes auto-défautées d'un passage précédent de cette route
        # ne comptent volontairement pas comme une "vraie" donnée.
        vifs_documentes = {
            l["code_vif"] for l in lignes_db
            if (l["cag1"] or l["cag2"]) and not l["auto_defaut"]
        }
        vifs_combos = {c["code_vif"] for c in combos}
        vifs_nouveaux = vifs_combos - vifs_documentes

        # Valeurs de l'année précédente (même magasin/demi-journée), pour
        # proposer comme point de départ au lieu d'un 10/10 arbitraire —
        # directement en base, sans export/import Excel.
        lignes_precedentes = conn.execute(
            "SELECT code_vif, jour, cag1, cag2, cag3, cag4 FROM collecte_cagettes_production WHERE annee = ?",
            (annee - 1,)
        ).fetchall()
        valeurs_precedentes = {(l["code_vif"], l["jour"]): l for l in lignes_precedentes}

        for combo in combos:
            vif = combo["code_vif"]
            if vif in vifs_documentes:
                # Magasin déjà connu : ne jamais forcer 10/10, même sur une
                # demi-journée encore vide (vrai manque à saisir à la main) —
                # et on annule un éventuel 10/10 posé à tort par une version
                # précédente de cette logique, avant qu'elle ne distingue par
                # magasin plutôt que par ligne.
                v = valeurs.get((vif, combo["jour"]))
                if v and v["auto_defaut"]:
                    conn.execute("""
                        UPDATE collecte_cagettes_production
                        SET cag1 = '', cag2 = '', auto_defaut = 0, maj_le = ?
                        WHERE annee = ? AND code_vif = ? AND jour = ?
                    """, (maintenant, annee, vif, combo["jour"]))
                continue
            precedente = valeurs_precedentes.get((vif, combo["jour"]))
            if precedente and (precedente["cag1"] or precedente["cag2"]):
                cag1, cag2 = precedente["cag1"] or "", precedente["cag2"] or ""
                cag3, cag4 = precedente["cag3"] or "", precedente["cag4"] or ""
            else:
                cag1, cag2, cag3, cag4 = "10", "10", "", ""
            conn.execute("""
                INSERT INTO collecte_cagettes_production (annee, code_vif, jour, cag1, cag2, cag3, cag4, maj_le, auto_defaut)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(annee, code_vif, jour) DO UPDATE SET
                    cag1 = excluded.cag1, cag2 = excluded.cag2, cag3 = excluded.cag3, cag4 = excluded.cag4,
                    maj_le = excluded.maj_le, auto_defaut = 1
            """, (annee, vif, combo["jour"], cag1, cag2, cag3, cag4, maintenant))
        conn.commit()
        lignes_db = conn.execute(
            "SELECT * FROM collecte_cagettes_production WHERE annee = ?", (annee,)
        ).fetchall()
    valeurs = {(l["code_vif"], l["jour"]): l for l in lignes_db}
    lignes = []
    for combo in combos:
        v = valeurs.get((combo["code_vif"], combo["jour"]))
        lignes.append({
            **combo,
            "cag1": v["cag1"] if v else "",
            "cag2": v["cag2"] if v else "",
            "cag3": v["cag3"] if v else "",
            "cag4": v["cag4"] if v else "",
            "auto_defaut": bool(v["auto_defaut"]) if v else False,
            "ref1": v["ref1"] if v else "",
            "ref2": v["ref2"] if v else "",
            "ref3": v["ref3"] if v else "",
            "ref4": v["ref4"] if v else "",
            "ref_total": v["ref_total"] if v else "",
        })
    return render_template("collecte/cagettes_production.html", annee=annee, lignes=lignes)


@collecte_bp.route("/collecte/<int:annee>/cagettes_production/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def cagettes_production_enregistrer(annee):
    data = request.get_json(force=True) or {}
    code_vif = _vif_fmt(data.get("code_vif", ""))
    jour = data.get("jour", "")
    champ = data.get("champ", "")
    valeur = str(data.get("valeur", "")).strip()
    if champ not in ("cag1", "cag2", "cag3", "cag4") or not code_vif or jour not in moteur.DEMI_JOURNEES:
        return jsonify(success=False, erreur="Paramètres invalides"), 400

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_table_cagettes_production(conn)
        # Toute saisie manuelle vaut confirmation — sort la ligne du mode
        # "10/10 par défaut" (auto_defaut), même si la valeur revient à 10.
        conn.execute(f"""
            INSERT INTO collecte_cagettes_production (annee, code_vif, jour, {champ}, maj_le, auto_defaut)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(annee, code_vif, jour) DO UPDATE SET
                {champ} = excluded.{champ}, maj_le = excluded.maj_le, auto_defaut = 0
        """, (annee, code_vif, jour, valeur, maintenant))
        conn.commit()
    return jsonify(success=True)


@collecte_bp.route("/collecte/<int:annee>/cagettes_production/importer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def cagettes_production_importer(annee):
    """Charge un fichier historique comme RÉFÉRENCE de comparaison (ref1..ref4)
    à côté des valeurs déjà saisies — n'écrase jamais cag1..cag4 : l'utilisateur
    reporte lui-même la bonne valeur cellule par cellule après avoir comparé
    (feedback utilisateur : un import qui écrase silencieusement une saisie
    manuelle n'est pas acceptable). Deux formats de fichier acceptés :
      - détaillé (ancien Cagettes_magasins.xlsx) : colonnes Code VIF, jour,
        cag1..cag4 → reporté tel quel sur ref1..ref4, plus leur somme sur
        ref_total ;
      - synthétique (ex. « cagettes bai.xlsx ») : colonnes Code VIF + une
        colonne par demi-journée (Jeudi matin, Vendredi Après midi, ...)
        contenant un total unique → reporté sur ref_total uniquement
        (ref1..4 vidées, faute de détail par passage) — comparé à la colonne
        Total (somme cag1..4) affichée dans la grille."""
    fichier = request.files.get("fichier_cagettes")
    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier sélectionné.", "warning")
        return redirect(url_for("collecte.cagettes_production", annee=annee))
    try:
        df = pd.read_excel(fichier, sheet_name=0)
    except Exception as e:
        flash(f"⛔ Fichier illisible : {e}", "danger")
        return redirect(url_for("collecte.cagettes_production", annee=annee))

    def _normaliser_entete(s):
        return str(s).strip().lower().replace("è", "e").replace("é", "e").replace("à", "a")

    colonnes = {_normaliser_entete(c): c for c in df.columns}
    col_vif = colonnes.get("code vif")
    if not col_vif:
        flash(f"⛔ Colonne « Code VIF » introuvable dans ce fichier "
              f"(colonnes trouvées : {', '.join(str(c) for c in df.columns)})", "danger")
        return redirect(url_for("collecte.cagettes_production", annee=annee))
    col_jour = colonnes.get("jour")

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with get_db_connection() as conn:
        _ensure_table_cagettes_production(conn)

        if col_jour:
            # Format détaillé : une ligne par (Code VIF, jour), 4 passages.
            cols_cag = {k: colonnes.get(k) for k in ("cag1", "cag2", "cag3", "cag4")}
            total = len(df)
            for _, r in df.iterrows():
                code_vif = _vif_fmt(r.get(col_vif, ""))
                jour = _normaliser_jour_cagettes(r.get(col_jour, ""))
                if not code_vif or not jour:
                    continue
                vals = []
                for k in ("cag1", "cag2", "cag3", "cag4"):
                    col = cols_cag.get(k)
                    v = r.get(col, "") if col else ""
                    v = str(v).strip().split(".")[0] if str(v).strip() not in ("", "nan") else ""
                    vals.append(v)
                ref_total = str(sum(int(v) for v in vals if v.lstrip("-").isdigit()))
                conn.execute("""
                    INSERT INTO collecte_cagettes_production (annee, code_vif, jour, ref1, ref2, ref3, ref4, ref_total, ref_le)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(annee, code_vif, jour) DO UPDATE SET
                        ref1 = excluded.ref1, ref2 = excluded.ref2, ref3 = excluded.ref3, ref4 = excluded.ref4,
                        ref_total = excluded.ref_total, ref_le = excluded.ref_le
                """, (annee, code_vif, jour, *vals, ref_total, maintenant))
                n += 1
        else:
            # Format synthétique : une colonne par demi-journée, un seul total.
            colonnes_dj = {}
            for header_norm, header_orig in colonnes.items():
                dj = _normaliser_jour_cagettes(header_orig)
                if dj:
                    colonnes_dj[dj] = header_orig
            if not colonnes_dj:
                flash(f"⛔ Aucune colonne de demi-journée reconnue dans ce fichier "
                      f"(colonnes trouvées : {', '.join(str(c) for c in df.columns)})", "danger")
                return redirect(url_for("collecte.cagettes_production", annee=annee))
            total = len(df) * len(colonnes_dj)
            for _, r in df.iterrows():
                code_vif = _vif_fmt(r.get(col_vif, ""))
                if not code_vif:
                    continue
                for jour, header in colonnes_dj.items():
                    v = r.get(header, "")
                    if v is None or str(v).strip() in ("", "nan"):
                        continue
                    try:
                        nb = str(round(float(v)))
                    except (ValueError, TypeError):
                        continue
                    conn.execute("""
                        INSERT INTO collecte_cagettes_production (annee, code_vif, jour, ref1, ref2, ref3, ref4, ref_total, ref_le)
                        VALUES (?, ?, ?, '', '', '', '', ?, ?)
                        ON CONFLICT(annee, code_vif, jour) DO UPDATE SET
                            ref1 = '', ref2 = '', ref3 = '', ref4 = '',
                            ref_total = excluded.ref_total, ref_le = excluded.ref_le
                    """, (annee, code_vif, jour, nb, maintenant))
                    n += 1
        conn.commit()
    ignorees = total - n
    message = f"✅ {n} valeur(s) de référence chargée(s) sur {total} possible(s) dans le fichier"
    if ignorees:
        message += f" ({ignorees} ignorée(s) — Code VIF ou demi-journée non reconnu, ou case vide)"
    message += " — les valeurs déjà saisies (Cagette 1-4) n'ont pas été modifiées."
    flash(message, "success" if n else "warning")
    write_log(f"📥 Import référence cagettes production {annee} ({n}/{total}) par {current_user.email}")
    return redirect(url_for("collecte.cagettes_production", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/cagettes_production/exporter")
@login_required
@require_access("collecte", "lecture")
def cagettes_production_exporter(annee):
    """Export Excel de la grille de gestion des cagettes — même format que
    l'ancien fichier Cagettes_magasins.xlsx (colonnes Code VIF, jour,
    cag1..cag4, feuille 'Feuil1'), pour archivage/consultation hors ligne,
    ou pour réimporter ailleurs (cf. cagettes_production_importer)."""
    with get_db_connection() as conn:
        _ensure_table_cagettes_production(conn)
        lignes = conn.execute(
            "SELECT code_vif, jour, cag1, cag2, cag3, cag4 FROM collecte_cagettes_production WHERE annee = ? ORDER BY jour, code_vif",
            (annee,),
        ).fetchall()

    df = pd.DataFrame(
        [{"Code VIF": l["code_vif"], "jour": l["jour"], "cag1": l["cag1"], "cag2": l["cag2"],
          "cag3": l["cag3"], "cag4": l["cag4"]} for l in lignes],
        columns=["Code VIF", "jour", "cag1", "cag2", "cag3", "cag4"],
    )
    tampon = io.BytesIO()
    with pd.ExcelWriter(tampon, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Feuil1", index=False)
    tampon.seek(0)

    write_log(f"📤 Export cagettes production {annee} par {current_user.email}")

    return send_file(
        tampon,
        as_attachment=True,
        download_name=f"cagettes_production_{annee}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/production")
@login_required
@require_access("collecte", "lecture")
def production():
    annee = request.args.get("annee", type=int) or datetime.now().year
    dossier = _dossier_production(annee)

    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

    liens_manquants = [
        conf["label"] for cle, conf in DRIVE_CHAMPS_PRODUCTION.items()
        if not _fichier_drive_disponible(annee, cle)
    ]

    fichiers = {}
    for cle, conf in PRODUCTION_FICHIERS_SORTIE.items():
        nom = conf["nom"].format(annee=annee)
        chemin = os.path.join(dossier, nom)
        existe = os.path.exists(chemin)
        fichiers[cle] = {
            "label": conf["label"],
            "nom": nom,
            "existe": existe,
            "genere_le": datetime.fromtimestamp(os.path.getmtime(chemin)) if existe else None,
        }

    derniere_generation = max(
        (f["genere_le"] for f in fichiers.values() if f["genere_le"]), default=None
    )

    journal = None
    chemin_journal = os.path.join(dossier, "dernier_journal.log")
    if os.path.exists(chemin_journal):
        with open(chemin_journal, "r", encoding="utf-8") as f:
            journal = f.read()

    return render_template(
        "collecte/collecte_production.html",
        annee=annee,
        fichiers=fichiers,
        derniere_generation=derniere_generation,
        journal=journal,
        date_debut=_date_fr_courte(campagne["date_debut"]) if campagne else None,
        date_fin=_date_fr_courte(campagne["date_fin"]) if campagne else None,
        liens_manquants=liens_manquants,
    )


def _generer_production_arriere_plan(app, cmd, dossier, annee, email):
    """Exécute la génération complète en dehors de la requête HTTP : la durée
    réelle (itinéraires PDF compris, appels OSRM/tuiles carte par tournée)
    dépasse désormais le délai de réponse gunicorn/nginx (504 Gateway Timeout
    observé 2026-11-06) alors que le script lui-même reste borné à 5 minutes
    (timeout=300, prlimit mémoire — consigne CLAUDE.md incident pandoc). La
    page Production relit dernier_journal.log à chaque affichage, donc un
    simple rechargement suffit à voir le résultat une fois terminé."""
    with app.app_context():
        try:
            resultat = subprocess.run(
                ["prlimit", "--as=1572864000", "--"] + cmd,
                capture_output=True, text=True, timeout=300,
            )
            sortie = (resultat.stdout or "") + "\n" + (resultat.stderr or "")
            echec = resultat.returncode != 0
        except subprocess.TimeoutExpired as exc:
            sortie = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")) \
                + "\n⏱️ Délai de 5 minutes dépassé, génération interrompue."
            echec = True

        with open(os.path.join(dossier, "dernier_journal.log"), "w", encoding="utf-8") as f:
            f.write(sortie)

        # Nom fixe pour la carte (générée sous un nom horodaté par le script)
        # afin d'offrir un lien de téléchargement stable.
        cartes = sorted(glob.glob(os.path.join(dossier, "carte_tournees_bai38_*.html")))
        if cartes:
            os.replace(cartes[-1], os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["carte"]["nom"]))

        if echec:
            write_log(f"❌ Génération documents production {annee} en échec par {email}\n{sortie[-4000:]}")
        else:
            write_log(f"📄 Génération documents production {annee} par {email}")


@collecte_bp.route("/collecte/production/generer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def production_generer():
    annee = request.form.get("annee", type=int) or datetime.now().year
    camion = request.form.get("camion", "").strip().upper()
    # Sécurité + nommage de fichier : un code camion go-on-web est toujours
    # alphanumérique (ex. V003) — on rejette tout le reste plutôt que de
    # laisser passer un caractère de type '/', '..' etc. dans un nom de fichier.
    camion = re.sub(r"[^A-Z0-9]", "", camion)

    # Date du jeudi : renseignée une fois pour toutes sur la page principale
    # du module.
    with get_db_connection() as conn:
        _ensure_colonne_drive_colis(conn)
        conn.commit()
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    date_jeudi = _date_fr_courte(campagne["date_debut"]) if campagne else None

    dossier = _dossier_production(annee)
    os.makedirs(dossier, exist_ok=True)

    # Les 3 fichiers sont retrouvés automatiquement par nom dans le drive
    # partagé BA380 - COLLECTE (voir _fichier_drive) — jamais de repli sur
    # un ancien fichier, la page production doit toujours refléter le
    # dernier état du planning go-on-web, pas un import ponctuel.
    manquants = []
    for cle in DRIVE_CHAMPS_PRODUCTION:
        chemin_source = _fichier_drive(annee, cle)
        if not chemin_source or not os.path.exists(chemin_source):
            manquants.append(DRIVE_CHAMPS_PRODUCTION[cle]["label"])
            continue
        shutil.copy(chemin_source, os.path.join(dossier, f"{cle}.xlsx"))
    if manquants:
        flash(
            f"❌ Fichier(s) introuvable(s) pour {annee} : {', '.join(manquants)} — "
            f"vérifier le dossier « Collecte {annee} / fichiers go on the web {annee} » "
            f"dans le drive partagé BA380 - COLLECTE.",
            "danger"
        )
        write_log(f"❌ Génération documents production {annee} : fichier(s) introuvable(s) ({', '.join(manquants)})")
        return redirect(url_for("collecte.production", annee=annee))

    # Cagettes : régénéré depuis la grille de gestion (collecte_cagettes_production),
    # jamais depuis le drive — voir cagettes_production() et DRIVE_CHAMPS_PRODUCTION.
    with get_db_connection() as conn:
        _ensure_table_cagettes_production(conn)
        lignes_cag = conn.execute(
            "SELECT code_vif, jour, cag1, cag2, cag3, cag4 FROM collecte_cagettes_production WHERE annee = ?",
            (annee,),
        ).fetchall()
    df_cag_export = pd.DataFrame(
        [{"Code VIF": l["code_vif"], "jour": l["jour"], "cag1": l["cag1"], "cag2": l["cag2"],
          "cag3": l["cag3"], "cag4": l["cag4"]} for l in lignes_cag],
        columns=["Code VIF", "jour", "cag1", "cag2", "cag3", "cag4"],
    )
    df_cag_export.to_excel(os.path.join(dossier, "cagettes.xlsx"), sheet_name="Feuil1", index=False)

    # Associations gardant leur collecte : régénéré depuis la base (même
    # source que la page Localisation) — sert uniquement au contrôle
    # "Adresses invalides", en complément des magasins.
    with get_db_connection() as conn:
        lignes_assoc = conn.execute("""
            SELECT nom_association, COMMUNE, latitude, longitude FROM associations
            WHERE LOWER(TRIM(COALESCE(validite,''))) = 'oui'
        """).fetchall()
    df_assoc_export = pd.DataFrame(
        [{"Nom": l["nom_association"], "Ville": l["COMMUNE"], "Latitude": l["latitude"], "Longitude": l["longitude"]}
         for l in lignes_assoc],
        columns=["Nom", "Ville", "Latitude", "Longitude"],
    )
    df_assoc_export.to_excel(os.path.join(dossier, "associations.xlsx"), index=False)

    script_path = os.path.join(
        current_app.root_path, "ba38_collecte", "scripts", "generer_documents_production.py"
    )
    venv_python = os.path.join(current_app.root_path, "venv", "bin", "python")

    # Un camion précis : aperçu rapide de sa seule fiche de collecte,
    # ouverte directement dans le navigateur, sans toucher aux documents
    # officiels (classeur Excel, pointage, équipier, index, consignes,
    # carte) — --fiche-seule arrête le script juste après ce document.
    if camion:
        chemin_fiche = os.path.join(dossier, f"fiche_collecte_{annee}_{camion}.pdf")
        cmd = [
            venv_python, script_path,
            "--magasins", os.path.join(dossier, "magasins.xlsx"),
            "--vehicules", os.path.join(dossier, "vehicules.xlsx"),
            "--cagettes", os.path.join(dossier, "cagettes.xlsx"),
            "--colis", os.path.join(dossier, "colis.xlsx"),
            "--associations", os.path.join(dossier, "associations.xlsx"),
            "--annee", str(annee),
            "--camion", camion,
            "--fiche-seule",
            "--output-fiches", chemin_fiche,
            # Jamais réellement écrit en --fiche-seule (le script s'arrête
            # avant), mais nécessaire pour que le script déduise son dossier
            # de sortie par défaut (out_dir) du bon endroit plutôt que du
            # chemin Windows codé en dur.
            "--output-excel", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["excel"]["nom"].format(annee=annee)),
        ]
        if date_jeudi:
            cmd += ["--date-jeudi", date_jeudi]

        try:
            # timeout + prlimit : consigne du projet suite à l'incident pandoc
            # OOM du 2026-10-07 (CLAUDE.md), pour toute commande potentiellement
            # lourde — ce script traite des fichiers Excel/PDF/carte générés à
            # partir de données saisies, jamais déposés tels quels par un tiers.
            resultat = subprocess.run(
                ["prlimit", "--as=1572864000", "--"] + cmd,
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            flash(f"❌ Génération de la fiche du camion {camion} interrompue (délai de 5 minutes dépassé)", "danger")
            write_log(f"⏱️ Fiche camion {camion} ({annee}) : timeout dépassé, interrompue par {current_user.email}")
            return redirect(url_for("collecte.production", annee=annee))
        sortie = (resultat.stdout or "") + "\n" + (resultat.stderr or "")

        if resultat.returncode != 0 or not os.path.exists(chemin_fiche):
            flash(f"❌ Échec de la génération de la fiche du camion {camion}", "danger")
            write_log(
                f"❌ Fiche camion {camion} ({annee}) en échec par {current_user.email}\n{sortie[-4000:]}"
            )
            return redirect(url_for("collecte.production", annee=annee))

        write_log(f"📄 Fiche camion {camion} ({annee}) générée par {current_user.email}")
        return send_file(chemin_fiche, mimetype="application/pdf", as_attachment=False)

    # Génération en arrière-plan (cf. _generer_production_arriere_plan) : un
    # second clic pendant qu'une génération tourne déjà lancerait un 2e
    # subprocess concurrent (chacun plafonné à 1,5 Go via prlimit — risque de
    # cumul mémoire sur ce serveur à 8 Go, cf. CLAUDE.md) — on le bloque en
    # relisant l'état du journal plutôt qu'un verrou en mémoire (3 workers
    # gunicorn = processus séparés, un verrou en mémoire ne serait pas partagé).
    chemin_journal = os.path.join(dossier, "dernier_journal.log")
    if os.path.exists(chemin_journal):
        age = time.time() - os.path.getmtime(chemin_journal)
        with open(chemin_journal, "r", encoding="utf-8") as f:
            debut_journal = f.read(50)
        if debut_journal.startswith("⏳ Génération lancée") and age < 300:
            flash("⏳ Une génération est déjà en cours pour cette année — attendez qu'elle se termine "
                  "(jusqu'à 5 minutes) avant d'en relancer une autre.", "warning")
            return redirect(url_for("collecte.production", annee=annee))

    # Génération complète (tous camions) : régénère les 6 documents officiels.
    # La carte HTML est régénérée à chaque lancement sous un nom horodaté :
    # on supprime les anciennes avant de relancer (pas d'historique ici).
    for ancienne_carte in glob.glob(os.path.join(dossier, "carte_tournees_bai38_*.html")):
        os.remove(ancienne_carte)

    cmd = [
        venv_python, script_path,
        "--magasins", os.path.join(dossier, "magasins.xlsx"),
        "--vehicules", os.path.join(dossier, "vehicules.xlsx"),
        "--cagettes", os.path.join(dossier, "cagettes.xlsx"),
        "--colis", os.path.join(dossier, "colis.xlsx"),
        "--associations", os.path.join(dossier, "associations.xlsx"),
        "--annee", str(annee),
        "--output-excel", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["excel"]["nom"].format(annee=annee)),
        "--output-fiches", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["fiches"]["nom"].format(annee=annee)),
        "--output-pointage", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["pointage"]["nom"].format(annee=annee)),
        "--output-equipier", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["equipier"]["nom"].format(annee=annee)),
        "--output-index", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["index"]["nom"].format(annee=annee)),
        "--output-vehicule-consignes", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["consignes"]["nom"]),
        "--output-numeros-camions", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["numeros_camions"]["nom"].format(annee=annee)),
        "--output-itineraires", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["itineraires"]["nom"].format(annee=annee)),
    ]
    if date_jeudi:
        cmd += ["--date-jeudi", date_jeudi]

    # Itinéraires des tournées (carte) : le document le plus long à produire,
    # pas nécessaire à chaque génération (ex. simple correctif sur un autre
    # document) — case à cocher, décochée par défaut.
    if request.form.get("avec_itineraires") != "1":
        cmd += ["--skip-itineraires"]

    # Camions déjà imprimés : exclus seulement du document 1 (fiches de
    # collecte), pour une régénération partielle sans réimprimer les fiches
    # déjà distribuées — mêmes caractères autorisés que --camion (sécurité
    # nom de fichier / injection de commande).
    camions_exclus_bruts = request.form.get("camions_exclus", "")
    codes_exclus = [
        re.sub(r"[^A-Z0-9]", "", c.strip().upper())
        for c in camions_exclus_bruts.split(",")
    ]
    codes_exclus = [c for c in codes_exclus if c]
    if codes_exclus:
        cmd += ["--camions-exclus-fiches", ",".join(codes_exclus)]

    with open(os.path.join(dossier, "dernier_journal.log"), "w", encoding="utf-8") as f:
        f.write(f"⏳ Génération lancée à {datetime.now().strftime('%H:%M:%S')} par {current_user.email} "
                f"— rechargez cette page dans quelques minutes pour voir le résultat.")

    threading.Thread(
        target=_generer_production_arriere_plan,
        args=(current_app._get_current_object(), cmd, dossier, annee, current_user.email),
        daemon=True,
    ).start()
    flash("⏳ Génération lancée en arrière-plan (jusqu'à 5 minutes) — rechargez cette page dans quelques "
          "instants pour récupérer les documents et le journal.", "info")
    write_log(f"📄 Génération documents production {annee} lancée par {current_user.email}")
    return redirect(url_for("collecte.production", annee=annee))


@collecte_bp.route("/collecte/production/telecharger/<cle>")
@login_required
@require_access("collecte", "lecture")
def production_telecharger(cle):
    annee = request.args.get("annee", type=int) or datetime.now().year

    if cle not in PRODUCTION_FICHIERS_SORTIE:
        flash("❌ Fichier inconnu", "danger")
        return redirect(url_for("collecte.production", annee=annee))

    nom = PRODUCTION_FICHIERS_SORTIE[cle]["nom"].format(annee=annee)
    chemin = os.path.join(_dossier_production(annee), nom)
    if not os.path.exists(chemin):
        flash("❌ Fichier introuvable — lancez une génération", "danger")
        return redirect(url_for("collecte.production", annee=annee))

    mimetype = "text/html" if cle == "carte" else None
    return send_file(chemin, as_attachment=(cle != "carte"), download_name=nom, mimetype=mimetype)


@collecte_bp.route("/collecte/production/resultats")
@login_required
@require_access("collecte", "lecture")
def production_resultats():
    """Affichage filtrable/triable (Tabulator) des tournées Production —
    même présentation que les résultats de simulation (collecte.resultats),
    mais à partir du classeur Production (plan réel go-on-web), sans notion
    de version ni de véhicule figé (propres au moteur de simulation)."""
    annee = request.args.get("annee", type=int) or datetime.now().year
    lignes = _charger_tournees_production(annee)
    if not lignes:
        flash("❌ Classeur Excel introuvable — lancez une génération", "danger")
        return redirect(url_for("collecte.production", annee=annee))

    return render_template(
        "collecte/production_resultats.html",
        annee=annee,
        lignes=lignes,
        camions_non_utilises=_camions_non_utilises(annee, lignes),
        matin_apresmidi_differents=_camions_matin_apresmidi_differents(lignes),
        controles=_controles_production(annee),
    )


@collecte_bp.route("/collecte/production/resultats_pdf", methods=["GET", "POST"])
@login_required
@require_access("collecte", "lecture")
def production_resultats_pdf():
    """PDF des tournées Production, respectant filtres/tri Tabulator — même
    principe que collecte.resultats_pdf (cf. telechargerPdfFiltre())."""
    annee = request.values.get("annee", type=int) or datetime.now().year
    lignes = _charger_tournees_production(annee)
    if not lignes:
        flash("❌ Classeur Excel introuvable — lancez une génération", "danger")
        return redirect(url_for("collecte.production", annee=annee))

    cles_brutes = request.form.get("lignes_filtrees", "").strip()
    if cles_brutes:
        try:
            cles = [tuple(c) for c in json.loads(cles_brutes)]
            lignes_par_cle = {(l["camion"], l["demi_journee"]): l for l in lignes}
            lignes = [lignes_par_cle[c] for c in cles if c in lignes_par_cle]
        except (ValueError, TypeError, IndexError):
            pass

    html = render_template(
        "collecte/production_resultats_pdf.html", annee=annee, lignes=lignes,
        org=get_organisation(),
    )

    pdf_buffer = io.BytesIO()
    HTML(string=html).write_pdf(pdf_buffer)
    pdf_buffer.seek(0)

    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"Tournees_production_{annee}.pdf",
        mimetype="application/pdf",
    )


# ============================================================================
# 🏠 ASSOCIATIONS GARDANT — associations qui gardent leur collecte au lieu de
# la remettre à la BAI (magasins État='Collecte gardée' du référentiel).
# Croise deux exports go-on-web déjà utilisés ailleurs dans le module :
#   - liste_magasins.xlsx (FICHIERS['magasins']) : un magasin par ligne,
#     colonne 'Gardée par' = nom de l'association qui en assure la collecte
#     (source de vérité pour "qui garde quoi" — voir _construire_associations)
#   - liste_groupes.xlsx  (FICHIERS['groupes'])  : un groupe/association par
#     ligne, sert uniquement à enrichir chaque association trouvée dans
#     'Gardée par' (type, contacts, fiche groupe) — sa colonne 'Nombre
#     magasins' n'est PAS utilisée pour décider qui apparaît dans la liste,
#     car elle accuse parfois un retard sur 'Gardée par' (association trouvée
#     quand même, juste avec des métadonnées vides — voir 'trouvee').
# Étape 1 du sous-projet : constituer/télécharger la liste croisée. L'envoi
# d'une demande de résultats aux associations et la saisie du tonnage reçu
# sont des étapes suivantes, pas encore implémentées.
# ============================================================================

def _valeur_propre(v):
    return "" if pd.isna(v) else v


def _vif_fmt(v):
    """Formate un Code VIF en texte à 8 chiffres, zéros non significatifs
    conservés (ex. 9990005 → '09990005') — Excel/pandas lisent ce genre de
    code comme un nombre et perdent sinon le zéro de tête."""
    if pd.isna(v):
        return ""
    s = str(v).strip().split(".")[0]
    return s.zfill(8) if s.isdigit() else s


def _normaliser_gardee_par(s):
    """Nettoie la colonne 'Gardée par' pour la faire correspondre au nom du
    groupe : certains magasins à stockage partagé notent l'association sous
    la forme 'BAI+Nom' (ex. 'BAI+Equilibre') plutôt que juste 'Nom' — préfixe
    à retirer avant comparaison."""
    s = str(s).strip()
    return re.sub(r'^BAI\s*\+\s*', '', s, flags=re.IGNORECASE).strip()


def _lire_magasins_gardes(annee):
    """Magasins État='Collecte gardée' du référentiel de l'année, plus les
    magasins État='Collecté par la BAI' dont la colonne 'Stockage' révèle
    quand même une association de stockage (ex. 'BAI+CCAS DOMENE' pour
    Intermarché Domène) — la BAI collecte, mais cette association doit
    recevoir la même réponse que les associations gardant officiellement un
    magasin (demande utilisateur 2026-11-02). Le rattachement à l'association
    se fait ensuite via _associations_secondaires_stockage, comme pour les
    magasins 'Collecte gardée' à stockage partagé. DataFrame vide si
    introuvable dans le drive."""
    chemin = _fichier_drive(annee, "magasins")
    if not chemin:
        return pd.DataFrame()
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    if "État" not in df.columns:
        return pd.DataFrame()
    etat = df["État"].astype(str).str.strip()
    stockage = df.get("Stockage", pd.Series(dtype=str)).astype(str)
    mask_gardee = etat == "Collecte gardée"
    mask_bai_avec_association = (etat == "Collecté par la BAI") & stockage.map(
        lambda s: bool(_associations_secondaires_stockage(s, ""))
    )
    return df[mask_gardee | mask_bai_avec_association].reset_index(drop=True)


def _lire_groupes(annee):
    """Tous les groupes (associations) go-on-web de l'année, colonnes
    normalisées (DataFrame vide si liste_groupes.xlsx est absent). Pas
    filtré sur 'Nombre magasins' : cette colonne, calculée côté go-on-web,
    accuse parfois un retard par rapport à la colonne 'Gardée par' du
    référentiel magasins (ex. une association vient de reprendre un magasin
    mais son compteur n'est pas encore remonté) — s'y fier pour décider
    qu'une association « n'existe pas » ferait disparaître des associations
    bien réelles de la liste."""
    chemin = _fichier_drive(annee, "groupes")
    if not chemin:
        return pd.DataFrame()
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _lire_participants(annee):
    """Tous les participants/contacts go-on-web de l'année (toutes années de
    collecte confondues dans l'export), colonnes normalisées (DataFrame vide
    si introuvable dans le drive)."""
    chemin = _fichier_drive(annee, "participants")
    if not chemin:
        return pd.DataFrame()
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _normaliser_nom_association(s):
    """Normalise un nom d'association pour comparaison robuste : casse,
    accents (ex. 'Côte' == 'Cote'), ponctuation, mots de liaison 'du'/'de'
    (ex. 'CCAS du Val de Virieu' == 'CCAS VAL DE VIRIEU'), et abréviations
    connues utilisées tantôt côté go-on-web tantôt côté base associations
    ('CR' / 'Croix Rouge', 'CS' / 'Centre Social', 'Entraide' / 'entr.')."""
    import unicodedata
    s = str(s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = s.replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^cr\b", "croix rouge", s)
    s = re.sub(r"^cs\b", "centre social", s)
    s = re.sub(r"\bentraide\b", "entr", s)
    s = re.sub(r"\b(du|de)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


# Libellés go-on-web trop différents de la base associations pour être
# rapprochés par la seule normalisation générique ci-dessus : on indique
# explicitement quel nom chercher à la place (clés/valeurs en clair,
# normalisées comme le reste au chargement — voir _code_vif_association).
#   - ASAT : le libellé go-on-web ajoute un descriptif complet après le
#     sigle, alors que la base ne connaît que le sigle seul ('ASAT
#     Distribution').
#   - CR Villefontaine (Bourgoin) : même association que 'CR Bourgoin',
#     juste une autre façon de la nommer côté go-on-web.
ALIAS_NOM_ASSOCIATION_BRUT = {
    "ASAT Association Solidaire Active De Tignieu": "ASAT",
    "CR Villefontaine (Bourgoin)": "CR Bourgoin",
}
ALIAS_NOM_ASSOCIATION = {
    _normaliser_nom_association(k): _normaliser_nom_association(v)
    for k, v in ALIAS_NOM_ASSOCIATION_BRUT.items()
}


def _cle_recherche_association(nom_asso):
    """Clé de recherche normalisée pour retrouver une association — alias
    explicites compris (voir ALIAS_NOM_ASSOCIATION). Utilisée à la fois pour
    le rapprochement avec liste_groupes.xlsx et pour le Code VIF (base
    associations), afin que les deux profitent des mêmes alias."""
    cle = _normaliser_nom_association(nom_asso)
    return ALIAS_NOM_ASSOCIATION.get(cle, cle)


def _ensure_table_code_vif_overrides(conn):
    """Table des corrections manuelles de Code VIF propres à la liste
    'Associations gardant' — n'affecte jamais code_VIF dans la table
    associations (partagée avec indicateurs, cotisations, etc.), seulement
    ce que cette liste affiche/exporte."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_gardee_code_vif_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom_normalise TEXT NOT NULL UNIQUE,
            nom_association TEXT NOT NULL,
            code_vif TEXT NOT NULL,
            defini_par TEXT,
            defini_le TEXT
        )
    """)


def _ensure_table_email_overrides(conn):
    """Table des corrections manuelles d'email d'envoi propres à la liste
    'Associations gardant' — n'affecte jamais la table associations
    (Partenaires) ni liste_participants.xlsx (go-on-web), seulement ce que
    cette liste affiche/utilise pour l'envoi. Même principe que
    _ensure_table_code_vif_overrides (clé = nom normalisé, pas d'année :
    le bon contact d'une association reste valable d'une campagne à
    l'autre)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_gardee_email_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom_normalise TEXT NOT NULL UNIQUE,
            nom_association TEXT NOT NULL,
            email TEXT NOT NULL,
            defini_par TEXT,
            defini_le TEXT
        )
    """)


def _email_overrides():
    """Dict {nom normalisé: email(s) forcé(s)} — cf.
    _ensure_table_email_overrides."""
    with get_db_connection() as conn:
        _ensure_table_email_overrides(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT nom_normalise, email FROM collecte_gardee_email_overrides"
        ).fetchall()
    return {r["nom_normalise"]: r["email"] for r in rows}


def _ensure_table_gratuit_gardee(conn):
    """Statut Gratuit (O/N) par association gardant — sert à la colonne
    « Gratuit » du BL VIF. Pas de ligne = Gratuit (valeur par défaut).
    Clé = nom normalisé, sans année (même principe que les overrides)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_gardee_gratuit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom_normalise TEXT NOT NULL UNIQUE,
            nom_association TEXT NOT NULL,
            gratuit INTEGER NOT NULL DEFAULT 1,
            defini_par TEXT,
            defini_le TEXT
        )
    """)


def _gratuit_associations():
    """Dict {nom normalisé: True/False} ; absent = Gratuit (défaut)."""
    with get_db_connection() as conn:
        _ensure_table_gratuit_gardee(conn)
        conn.commit()
        rows = conn.execute("SELECT nom_normalise, gratuit FROM collecte_gardee_gratuit").fetchall()
    return {r["nom_normalise"]: bool(r["gratuit"]) for r in rows}


def _ensure_table_suivi_gardee(conn):
    """Table de suivi manuel des réponses des associations gardant leur
    collecte — deux cases indépendantes (réponse au 1er message, résultat
    envoyé) cochées à la main par l'équipe collecte, la vérification
    elle-même se faisant hors de l'application (mail reçu, fichier reçu,
    ou saisie en ligne consultée) — cf. _saisie_association_avancement
    pour un indice automatique affiché à titre d'aide, mais qui ne coche
    jamais rien tout seul."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_gardee_suivi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annee INTEGER NOT NULL,
            nom_association TEXT NOT NULL,
            repondu INTEGER NOT NULL DEFAULT 0,
            repondu_le TEXT,
            repondu_par TEXT,
            resultat_envoye INTEGER NOT NULL DEFAULT 0,
            resultat_envoye_le TEXT,
            resultat_envoye_par TEXT,
            UNIQUE(annee, nom_association)
        )
    """)
    # Migration paresseuse : colonne ajoutée après coup pour la notification
    # mail « saisie en ligne » (cf. _notifier_saisie_association).
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_gardee_suivi)").fetchall()}
    if "dernier_mail_saisie_le" not in colonnes:
        conn.execute("ALTER TABLE collecte_gardee_suivi ADD COLUMN dernier_mail_saisie_le TEXT")
    # 3e case de suivi, indépendante des 2 premières (qui suivent l'échange
    # de mails) : une association peut saisir ses résultats en ligne sans
    # jamais passer par ce circuit. "Traitée" cochait qu'on a fini, POUR
    # CETTE ASSOCIATION, la réception VIF (export CSV poids par magasin),
    # le transfert produits et le BL — sans attendre que TOUTES les
    # associations aient répondu, certaines traînant beaucoup plus que
    # d'autres (demande utilisateur 2026-11-16). Retirée du tableau et de
    # la logique d'exclusion des exports le 2026-11-23, remplacée par les
    # 3 cases automatiques ci-dessous — colonne conservée en base (non
    # supprimée) pour garder l'historique, mais plus lue ni affichée.
    if "traite" not in colonnes:
        conn.execute("ALTER TABLE collecte_gardee_suivi ADD COLUMN traite INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE collecte_gardee_suivi ADD COLUMN traite_le TEXT")
        conn.execute("ALTER TABLE collecte_gardee_suivi ADD COLUMN traite_par TEXT")
    # 3 cases automatiques indépendantes, une par export VIF (poids par
    # magasin / transferts / BL) — chaque export coche la sienne juste
    # après sa propre génération, pour être rejouable en plusieurs fois
    # (associations qui répondent au fil de l'eau) sans jamais redoubler
    # une association déjà traitée par CET export précis (demande
    # utilisateur 2026-11-23). Décochables à la main (correction/re-export
    # forcé).
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_gardee_suivi)").fetchall()}
    for champ in ("poids_exporte", "transferts_exportes", "bl_genere"):
        if champ not in colonnes:
            conn.execute(f"ALTER TABLE collecte_gardee_suivi ADD COLUMN {champ} INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"ALTER TABLE collecte_gardee_suivi ADD COLUMN {champ}_le TEXT")
            conn.execute(f"ALTER TABLE collecte_gardee_suivi ADD COLUMN {champ}_par TEXT")
    # L'association elle-même confirme avoir terminé sa saisie en ligne
    # (bouton dédié sur saisie_association.html) — à ce moment, tout produit
    # du modèle resté vide est enregistré à 0 (visible dans la grille
    # Quantités par produit), pour que le calcul de complétude
    # (_saisie_association_avancement, qui exige une valeur sur CHAQUE
    # produit du catalogue) puisse atteindre 100% sans exiger que
    # l'association tape elle-même un 0 sur chaque produit qu'elle n'a pas
    # (demande utilisateur 2026-11-23, suite au constat que le BL VIF ne se
    # générait quasiment jamais avec la règle des 100% seule).
    if "saisie_en_ligne_terminee" not in colonnes:
        conn.execute("ALTER TABLE collecte_gardee_suivi ADD COLUMN saisie_en_ligne_terminee INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE collecte_gardee_suivi ADD COLUMN saisie_en_ligne_terminee_le TEXT")
        conn.execute("ALTER TABLE collecte_gardee_suivi ADD COLUMN saisie_en_ligne_terminee_par TEXT")


def _lire_suivi_gardee(annee):
    """{nom_association: {'repondu': bool, 'resultat_envoye': bool, 'traite': bool,
    'poids_exporte': bool, 'transferts_exportes': bool, 'bl_genere': bool,
    'saisie_en_ligne_terminee': bool}}"""
    with get_db_connection() as conn:
        _ensure_table_suivi_gardee(conn)
        rows = conn.execute(
            "SELECT nom_association, repondu, resultat_envoye, traite, "
            "poids_exporte, transferts_exportes, bl_genere, saisie_en_ligne_terminee "
            "FROM collecte_gardee_suivi WHERE annee = ?",
            (annee,),
        ).fetchall()
    return {
        r["nom_association"]: {
            "repondu": bool(r["repondu"]), "resultat_envoye": bool(r["resultat_envoye"]), "traite": bool(r["traite"]),
            "poids_exporte": bool(r["poids_exporte"]), "transferts_exportes": bool(r["transferts_exportes"]),
            "bl_genere": bool(r["bl_genere"]),
            "saisie_en_ligne_terminee": bool(r["saisie_en_ligne_terminee"]),
        }
        for r in rows
    }


def _noms_associations_gardee_avec_champ(annee, champ_specifique):
    """Noms des associations déjà cochées pour UN champ de suivi précis
    (poids_exporte / transferts_exportes / bl_genere, coché automatiquement
    par l'export correspondant). Double usage : exclusion d'un export déjà
    rejoué plusieurs fois (ne pas redoubler une association déjà traitée
    PAR CET export précis, demande utilisateur 2026-11-23), ET condition
    d'éligibilité de l'export SUIVANT dans la chaîne poids → transferts →
    BL — les 3 CSV doivent être générés dans cet ordre, une association ne
    passe à la phase suivante que si la précédente est faite pour elle
    (demande utilisateur 2026-11-23). La case « Traitée » (ancien
    coupe-circuit commun aux 3 exports, créé le 2026-11-16 avant que ces 3
    cases dédiées existent) n'a plus d'effet ici depuis le 2026-11-23 —
    devenue redondante et source de confusion (une association cochée
    Traitée avant cette date restait exclue de tout, sans que ce soit
    visible dans le détail)."""
    suivi = _lire_suivi_gardee(annee)
    return {nom for nom, s in suivi.items() if s.get(champ_specifique)}


def _ensure_table_poids_magasins_gardee(conn):
    """Ajoute la dimension association à la clé de collecte_poids_magasins_gardee
    (auparavant (annee, code_vif) seul, une unique pesée par magasin quelle
    que soit l'association) : un magasin collecté par PLUSIEURS associations
    (ex. Intermarché La Mure, partagé entre Beurrepinard et Coll Entraide du
    Trièves) correspond en réalité à plusieurs réceptions VIF distinctes,
    chacune avec son propre poids — avant ce correctif, la 2e association à
    saisir écrasait silencieusement la pesée de la 1ère, sans que rien ne le
    signale (demande utilisateur 2026-11-23). Migration paresseuse (SQLite
    ne sait pas modifier une clé primaire en place) : recrée la table avec
    nom_association dans la clé, en reportant chaque ancienne ligne vers
    CHAQUE association à laquelle le magasin est rattaché cette année-là
    (donc dupliquée à l'identique pour un magasin partagé — à corriger
    ensuite manuellement association par association, un log le signale)."""
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_poids_magasins_gardee)").fetchall()}
    if "nom_association" in colonnes:
        return
    anciennes = conn.execute(
        "SELECT annee, code_vif, poids_kg, saisi_le, saisi_par FROM collecte_poids_magasins_gardee"
    ).fetchall()
    conn.execute("ALTER TABLE collecte_poids_magasins_gardee RENAME TO collecte_poids_magasins_gardee_ancien")
    conn.execute("""
        CREATE TABLE collecte_poids_magasins_gardee (
            annee INTEGER NOT NULL,
            code_vif TEXT NOT NULL,
            nom_association TEXT NOT NULL,
            poids_kg REAL,
            saisi_le TEXT,
            saisi_par TEXT,
            PRIMARY KEY (annee, code_vif, nom_association)
        )
    """)
    par_annee = {}
    for ligne in anciennes:
        par_annee.setdefault(ligne["annee"], []).append(ligne)
    for annee_migree, lignes in par_annee.items():
        df_mag = _lire_magasins_gardes(annee_migree)
        noms_par_code_vif = {}
        if not df_mag.empty:
            if "Code VIF" in df_mag.columns:
                df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
            df_groupes = _lire_groupes(annee_migree)
            associations, _ = _construire_associations(df_mag, df_groupes)
            for asso in associations:
                for m in asso["magasins"]:
                    noms_par_code_vif.setdefault(_vif_fmt(m.get("Code VIF")), []).append(asso["nom"])
        for ligne in lignes:
            noms = noms_par_code_vif.get(ligne["code_vif"]) or [""]
            if len(noms) > 1:
                write_log(
                    f"⚠️ Migration poids_magasins_gardee {annee_migree} : magasin {ligne['code_vif']} "
                    f"partagé entre {len(noms)} associations ({', '.join(noms)}) — valeur "
                    f"{ligne['poids_kg']} dupliquée à l'identique dans chacune, à vérifier/corriger "
                    "manuellement (une seule des deux a peut-être réellement saisi cette pesée)."
                )
            for nom in noms:
                conn.execute("""
                    INSERT INTO collecte_poids_magasins_gardee
                        (annee, code_vif, nom_association, poids_kg, saisi_le, saisi_par)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (annee_migree, ligne["code_vif"], nom, ligne["poids_kg"], ligne["saisi_le"], ligne["saisi_par"]))
    conn.execute("DROP TABLE collecte_poids_magasins_gardee_ancien")
    write_log("✅ Migration collecte_poids_magasins_gardee : nom_association ajouté à la clé")


def _noms_associations_completes(annee):
    """Noms des associations gardant dont la saisie est complète — même
    calcul que le badge vert « X/X complet » affiché dans Saisie collecte
    gardée (cf. _saisie_association_avancement), pour ne générer le BL VIF
    que pour les associations entièrement remplies, jamais une association
    partiellement saisie (demande utilisateur 2026-11-17)."""
    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if df_mag.empty:
        return set()
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    produits = _lire_produits_modele()
    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        poids_magasins = {}
        for r in conn.execute(
            "SELECT nom_association, code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ?", (annee,)
        ).fetchall():
            poids_magasins.setdefault(r["nom_association"], {})[r["code_vif"]] = r["poids_kg"]
        poids_produits = {}
        for r in conn.execute(
            "SELECT association, code_produit, poids_kg FROM collecte_quantites_produits WHERE annee = ?", (annee,)
        ).fetchall():
            poids_produits.setdefault(r["association"], {})[r["code_produit"]] = r["poids_kg"]
    complets = set()
    for asso in associations:
        remplis, total = _saisie_association_avancement(
            annee, asso["nom"], asso["magasins"], produits, poids_magasins, poids_produits
        )
        if total and remplis == total:
            complets.add(asso["nom"])
    return complets


def _marquer_suivi_gardee(annee, nom_association, champ, valeur, utilisateur):
    """champ : 'repondu' ou 'resultat_envoye'."""
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if valeur else None
    with get_db_connection() as conn:
        _ensure_table_suivi_gardee(conn)
        conn.execute(f"""
            INSERT INTO collecte_gardee_suivi (annee, nom_association, {champ}, {champ}_le, {champ}_par)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(annee, nom_association)
            DO UPDATE SET {champ} = excluded.{champ}, {champ}_le = excluded.{champ}_le, {champ}_par = excluded.{champ}_par
        """, (annee, nom_association, 1 if valeur else 0, maintenant, utilisateur if valeur else None))
        conn.commit()


ETAPES_VIF_GARDEE = ("poids_exporte", "transferts_exportes", "bl_genere")


def _ensure_table_gardee_vif_a_confirmer(conn):
    """CSV générés pour VIF (poids par magasin / transferts TRANCOG / BL)
    mais pas encore confirmés comme réellement importés dans VIF — avant
    ce correctif, la case de suivi (poids_exporte / transferts_exportes /
    bl_genere) se cochait automatiquement dès le CSV généré, ce qui a
    permis à un BL de se générer pour des associations dont le CSV de
    réception/transfert avait en réalité été généré en test puis jamais
    importé dans VIF (incident réel 2026-11-24, stocks VIF négatifs).
    Désormais le CSV passe par cette file d'attente ; la case ne se coche
    (et l'étape suivante ne se débloque) qu'après confirmation explicite
    (cf. confirmer_vif_gardee)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_gardee_vif_a_confirmer (
            annee INTEGER NOT NULL,
            etape TEXT NOT NULL,
            nom_association TEXT NOT NULL,
            genere_le TEXT,
            genere_par TEXT,
            PRIMARY KEY (annee, etape, nom_association)
        )
    """)


def _mettre_en_attente_confirmation_vif(annee, etape, nom_association, utilisateur):
    with get_db_connection() as conn:
        _ensure_table_gardee_vif_a_confirmer(conn)
        conn.execute("""
            INSERT INTO collecte_gardee_vif_a_confirmer (annee, etape, nom_association, genere_le, genere_par)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(annee, etape, nom_association)
            DO UPDATE SET genere_le = excluded.genere_le, genere_par = excluded.genere_par
        """, (annee, etape, nom_association, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), utilisateur))
        conn.commit()


def _noms_associations_gardee_en_attente_confirmation(annee, etape):
    """Noms déjà inclus dans un CSV généré pour CETTE étape mais pas
    encore confirmés importés dans VIF — à exclure d'une régénération du
    même CSV (sinon la même association se retrouverait dans 2 CSV
    distincts en attente), mais SANS jamais compter comme condition
    d'éligibilité de l'étape suivante (cf. _noms_associations_gardee_avec_champ,
    qui reste basée uniquement sur la case confirmée)."""
    with get_db_connection() as conn:
        _ensure_table_gardee_vif_a_confirmer(conn)
        return {r["nom_association"] for r in conn.execute(
            "SELECT nom_association FROM collecte_gardee_vif_a_confirmer WHERE annee = ? AND etape = ?",
            (annee, etape),
        ).fetchall()}


def _confirmer_vif_gardee(annee, etape, utilisateur):
    """Confirme l'import VIF réel des associations en attente pour CETTE
    étape : coche la case de suivi correspondante (ce qui débloque l'étape
    suivante) et vide la file d'attente. Retourne la liste des noms
    confirmés."""
    noms = sorted(_noms_associations_gardee_en_attente_confirmation(annee, etape))
    for nom in noms:
        _marquer_suivi_gardee(annee, nom, etape, True, utilisateur)
    with get_db_connection() as conn:
        _ensure_table_gardee_vif_a_confirmer(conn)
        conn.execute(
            "DELETE FROM collecte_gardee_vif_a_confirmer WHERE annee = ? AND etape = ?",
            (annee, etape),
        )
        conn.commit()
    return noms


def _notifier_saisie_association(annee, nom_association):
    """Prévient l'équipe collecte (ba380.collecte) qu'une association a
    saisi ou modifié ses résultats via le formulaire public saisie_association
    — au plus UN mail par association et par jour d'activité (pas un mail par
    case enregistrée, l'autosave écrivant une case à la fois), grâce à
    collecte_gardee_suivi.dernier_mail_saisie_le (demande utilisateur
    2026-11-03). N'affecte jamais les cases 'repondu'/'resultat_envoye',
    cochées à la main uniquement (cf. _ensure_table_suivi_gardee)."""
    aujourdhui = datetime.now().strftime("%Y-%m-%d")
    with get_db_connection() as conn:
        _ensure_table_suivi_gardee(conn)
        ligne = conn.execute(
            "SELECT dernier_mail_saisie_le FROM collecte_gardee_suivi WHERE annee = ? AND nom_association = ?",
            (annee, nom_association),
        ).fetchone()
        if ligne and ligne["dernier_mail_saisie_le"] == aujourdhui:
            return  # déjà notifié aujourd'hui pour cette association
        conn.execute("""
            INSERT INTO collecte_gardee_suivi (annee, nom_association, dernier_mail_saisie_le)
            VALUES (?, ?, ?)
            ON CONFLICT(annee, nom_association) DO UPDATE SET dernier_mail_saisie_le = excluded.dernier_mail_saisie_le
        """, (annee, nom_association, aujourdhui))
        conn.commit()

    try:
        envoyer_mail(
            sujet=f"Collecte {annee} — saisie de « {nom_association} »",
            destinataires=["ba380.collecte@banquealimentaire.org"],
            texte=(
                f"L'association « {nom_association} » vient de saisir ou modifier ses résultats "
                f"de collecte gardée {annee} (poids par magasin et/ou quantités par produit).<br><br>"
                f"<a href=\"{url_for('collecte.gardee_saisie', annee=annee, association=nom_association, _external=True)}\">"
                f"Voir les résultats saisis par « {nom_association} »</a>"
            ),
            sender_override="ba380.collecte@banquealimentaire.org",
            is_html=True,
        )
    except Exception as e:
        write_log(f"⚠️ Échec notification saisie association « {nom_association} » {annee} : {e}")


def _saisie_association_avancement(annee, nom_association, magasins, produits, poids_magasins, poids_produits):
    """Indice automatique (nb rempli / nb attendu) de la saisie en ligne
    pour une association — affiché en aide à la décision à côté des cases
    à cocher manuelles, jamais utilisé pour les cocher automatiquement
    (cf. _ensure_table_suivi_gardee)."""
    codes_magasins = [_vif_fmt(m.get("Code VIF")) for m in magasins]
    remplis_magasins = sum(
        1 for c in codes_magasins if poids_magasins.get(nom_association, {}).get(c) is not None
    )
    remplis_produits = sum(
        1 for p in produits if poids_produits.get(nom_association, {}).get(p["code"]) is not None
    )
    total = len(codes_magasins) + len(produits)
    remplis = remplis_magasins + remplis_produits
    return remplis, total


def _table_code_vif_associations():
    """Charge depuis la table associations (+ les corrections manuelles
    propres à cette liste) de quoi retrouver le Code VIF d'une association
    gardant sa collecte à partir de son libellé (voir _code_vif_association) :
    - 'overrides' : {nom normalisé: code_VIF forcé manuellement} ;
    - 'exact' : {nom normalisé: [(id, code_VIF), ...]} ;
    - 'liste' : [(nom normalisé, id, code_VIF), ...] triée par nom, pour le
      repli par préfixe (ex. 'Trois Robert' → une seule antenne par jour de
      passage, ex. '3 ABI Lundi' / 'Mardi' / 'Mercredi' dans la base — on
      prend alors la première par ordre alphabétique)."""
    with get_db_connection() as conn:
        _ensure_table_code_vif_overrides(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT id, nom_association, code_VIF FROM associations"
        ).fetchall()
        overrides_rows = conn.execute(
            "SELECT nom_normalise, code_vif FROM collecte_gardee_code_vif_overrides"
        ).fetchall()
    exact = {}
    liste = []
    for r in rows:
        cle = _normaliser_nom_association(r["nom_association"])
        if not cle:
            continue
        exact.setdefault(cle, []).append((r["id"], r["code_VIF"]))
        liste.append((cle, r["id"], r["code_VIF"]))
    liste.sort(key=lambda t: t[0])
    overrides = {r["nom_normalise"]: r["code_vif"] for r in overrides_rows}
    return {"exact": exact, "liste": liste, "overrides": overrides}


def _code_vif_association(nom_asso, index):
    """Retrouve le Code VIF d'une association « gardant » sa collecte en
    cherchant son libellé (colonne 'Gardée par' / liste_groupes.xlsx) dans la
    base associations (nom_association). Renvoie (code_vif, écart) — écart
    non vide si le nom est introuvable ou sans Code VIF renseigné, pour
    signaler les cas à corriger manuellement.

    Si le nom exact n'existe pas mais que la base contient plusieurs
    antennes déclinées par jour de passage (ex. '3 ABI Lundi', '3 ABI
    Mardi', '3 ABI Mercredi' pour '3 ABI'), on prend la première par ordre
    alphabétique plutôt que de signaler un écart.

    Une correction manuelle (voir _ensure_table_code_vif_overrides), propre
    à cette liste et sans effet sur la base associations, est toujours
    prioritaire — renvoie alors (code_vif, "", True) au lieu de (code_vif,
    écart)."""
    cle = _cle_recherche_association(nom_asso)

    if cle in index["overrides"]:
        return index["overrides"][cle], "", True

    correspondances = index["exact"].get(cle, [])

    if not correspondances:
        prefixe = cle + " "
        correspondances = [
            (id_, code) for c, id_, code in index["liste"] if c.startswith(prefixe)
        ][:1]

    if not correspondances:
        return "", "⚠️ Association introuvable dans la base (nom à vérifier)", False
    if len(correspondances) > 1:
        return "", "⚠️ Plusieurs associations portent ce nom dans la base — à vérifier manuellement", False
    code_vif = correspondances[0][1]
    if not code_vif:
        return "", "⚠️ Code VIF non renseigné pour cette association", False
    return code_vif, "", False


def _referents_association(nom_asso, df_participants):
    """Contacts connus pour un groupe (association) donné, à partir de
    liste_participants.xlsx (colonne 'Groupe' — notée 'BAI+Nom' pour les
    magasins à stockage partagé, comme 'Gardée par' dans liste_magasins.xlsx,
    d'où la même normalisation). Dédoublonnés par (Nom, Email) ; ceux dont le
    nom porte la mention '(Ref)' (référent désigné côté go-on-web) sont mis
    en tête.

    Si au moins une personne est taguée 'GR' (Groupe Responsable, colonne
    R/E) pour ce groupe, SEULES ces personnes sont renvoyées — c'est le
    vrai responsable de l'association, pas besoin de notifier tout le
    groupe. Sinon (information pas encore renseignée côté go-on-web pour
    cette association — environ 165 associations sur 272 à ce jour),
    repli sur la liste complète des participants du groupe, comme avant."""
    if df_participants.empty or "Groupe" not in df_participants.columns:
        return []
    groupe = df_participants["Groupe"].map(_normaliser_gardee_par)
    sub = df_participants[groupe == nom_asso].fillna("")
    if sub.empty:
        return []

    vus = set()
    contacts = []
    for _, r in sub.iterrows():
        nom = str(r.get("Nom", "")).strip()
        if not nom:
            continue
        email = str(r.get("Email", "")).strip()
        cle = (nom, email)
        if cle in vus:
            continue
        vus.add(cle)
        telephone = str(r.get("Portable", "")).strip() or str(r.get("Téléphone", "")).strip()
        contacts.append({
            "nom": nom,
            "email": email,
            "telephone": telephone,
            "referent": "ref" in nom.lower().replace("é", "e"),
            "groupe_responsable": str(r.get("R/E", "")).strip().upper() == "GR",
        })

    responsables = [c for c in contacts if c["groupe_responsable"]]
    if responsables:
        contacts = responsables
    contacts.sort(key=lambda c: (not c["referent"], c["nom"]))
    return contacts


def _associations_secondaires_stockage(stockage, primaire):
    """Certains magasins sont gardés à tour de rôle par DEUX associations
    (ex. une journée chacune) — la colonne 'Gardée par' n'en retient qu'une,
    la seconde n'apparaît que dans le texte libre 'Stockage' (ex.
    'Beurrepinard+la Roseraie'). Renvoie les noms distincts de la principale
    et de 'BAI' (qui désigne un stockage partagé avec la banque alimentaire,
    pas une association)."""
    stockage = str(stockage)
    if "+" not in stockage:
        return []

    def cle(s):
        # espaces ignorés : 'BAI+3ABI' et 'BAI+3 ABI' doivent être reconnus
        # comme la même association malgré l'incohérence de saisie.
        return re.sub(r"\s+", "", s).lower()

    primaire_cle = cle(primaire)
    tokens = [t.strip() for t in stockage.split("+") if t.strip()]
    return [t for t in tokens if cle(t) not in ("bai", primaire_cle)]


def _association_effective(row):
    """Association à laquelle rattacher ce magasin : 'Gardée par' en
    priorité, sinon l'association révélée par 'Stockage' (cf.
    _associations_secondaires_stockage) — cas des magasins 'Collecté par la
    BAI' dont le stockage est partagé avec une association (ex. Intermarché
    Domène, Stockage='BAI+CCAS DOMENE', 'Gardée par' vide)."""
    gardee_par = row.get("Gardée par", "")
    primaire = _normaliser_gardee_par(gardee_par) if pd.notna(gardee_par) else ""
    if primaire:
        return primaire
    stockage = row.get("Stockage", "")
    stockage = stockage if pd.notna(stockage) else ""
    secondaires = _associations_secondaires_stockage(stockage, "")
    return " + ".join(secondaires) if secondaires else ""


def _modele_gardee(nom):
    chemin = os.path.join(MODELES_GARDEE_DIR, nom)
    if not os.path.exists(chemin):
        chemin = os.path.join(current_app.root_path, "uploads", "collecte_fichiers_source", nom)
    return chemin


LIGNE_PREMIER_PRODUIT_MODELE = 16


def _derniere_ligne_utilisee(ws, plafond=200, colonnes=10):
    """Dernière ligne réellement remplie d'une feuille (sur ses premières
    `colonnes` colonnes), en ignorant le padding vide qu'un export Google
    Sheets ajoute systématiquement (grille par défaut de 1000x26) —
    ws.max_row seul n'est pas fiable dans ce cas, il refléterait la taille
    de la grille et non le contenu. N'utilise jamais ws[r] / ws.iter_rows :
    ces accès recalculent la largeur de la feuille (self.max_column) à
    chaque appel, ce qui, répété jusqu'à `plafond` fois sur une grille
    Google de plusieurs milliers de cellules, provoque un timeout
    (déjà rencontré en production sur ce fichier)."""
    for r in range(min(ws.max_row, plafond), 0, -1):
        if any(ws.cell(r, c).value not in (None, "") for c in range(1, colonnes + 1)):
            return r
    return 1


def _tronquer_grille_google_sheets(ws, marge_lignes=15, derniere_colonne=15):
    """Supprime les lignes/colonnes vides que Google Sheets ajoute par
    défaut (grille 1000x26) au-delà du contenu réel — sans ça, chaque
    load_workbook()+save() ultérieur (une fois par association générée)
    traite des dizaines de milliers de cellules vides et ralentit
    suffisamment pour provoquer un timeout serveur (déjà rencontré en
    production). Appelé une seule fois à la synchronisation, jamais à la
    génération des fichiers association elle-même."""
    derniere_ligne = _derniere_ligne_utilisee(ws, colonnes=derniere_colonne)
    limite_ligne = derniere_ligne + marge_lignes
    if ws.max_row > limite_ligne:
        ws.delete_rows(limite_ligne + 1, ws.max_row - limite_ligne)
    if ws.max_column > derniere_colonne:
        ws.delete_cols(derniere_colonne + 1, ws.max_column - derniere_colonne)


def _derniere_ligne_produits(ws):
    """Première ligne à partir de LIGNE_PREMIER_PRODUIT_MODELE dont la
    colonne A (code) est vide moins un — s'arrête naturellement avant la
    ligne « Autres » (code vide) sans jamais atteindre les lignes de
    légende plus bas (« Total », « Type calcul= », qui ont, elles, du
    contenu en colonne A ou B mais sont hors de la zone produits)."""
    r = LIGNE_PREMIER_PRODUIT_MODELE
    while ws.cell(r, 1).value not in (None, ""):
        r += 1
    return r - 1


def _lire_produits_modele():
    """Liste des produits (code, libellé) depuis l'onglet « produits » du
    modèle association — même liste que la fiche produits papier envoyée
    aux associations, lue dynamiquement pour rester à jour si le modèle
    évolue (nombre de lignes variable)."""
    chemin = _modele_gardee("Modele association.xlsx")
    if not os.path.exists(chemin):
        return []
    wb = load_workbook(chemin, data_only=True)
    ws = wb["produits"]
    derniere = _derniere_ligne_produits(ws)
    produits = []
    for r in range(LIGNE_PREMIER_PRODUIT_MODELE, derniere + 1):
        code = ws.cell(r, 1).value
        produits.append({"code": str(code).strip(), "libelle": str(ws.cell(r, 2).value or "").strip()})
    return produits


def _lire_produits_modele_complet():
    """Comme _lire_produits_modele(), mais avec toutes les colonnes (libellé
    VIF, type calcul) pour l'éditeur en ligne du modèle."""
    chemin = _modele_gardee("Modele association.xlsx")
    if not os.path.exists(chemin):
        return []
    wb = load_workbook(chemin, data_only=True)
    ws = wb["produits"]
    derniere = _derniere_ligne_produits(ws)
    produits = []
    for r in range(LIGNE_PREMIER_PRODUIT_MODELE, derniere + 1):
        produits.append({
            "code": str(ws.cell(r, 1).value).strip(),
            "libelle": str(ws.cell(r, 2).value or "").strip(),
            "libelle_vif": str(ws.cell(r, 4).value or "").strip(),
            "type_calcul": str(ws.cell(r, 5).value or "").strip(),
        })
    return produits


def _enregistrer_produits_modele(produits):
    """Réécrit la liste des produits (code, libellé, libellé VIF, type
    calcul) dans le modèle Excel partagé — insère ou supprime des lignes
    Excel (ws.insert_rows/delete_rows) si le nombre de produits change,
    pour que les lignes suivantes (« Autres », « Total », « Type calcul= »)
    restent immédiatement après la liste, à leur position relative
    d'origine, sans jamais toucher la mise en page (logo, bordures,
    en-têtes) ni les lignes situées avant la liste. Sauvegarde l'ancienne
    version avant d'écrire."""
    chemin = _modele_gardee("Modele association.xlsx")
    if not os.path.exists(chemin):
        raise FileNotFoundError("Modèle introuvable")

    wb = load_workbook(chemin)
    ws = wb["produits"]

    nb_actuels = _derniere_ligne_produits(ws) - LIGNE_PREMIER_PRODUIT_MODELE + 1
    nb_nouveaux = len(produits)

    if nb_nouveaux > nb_actuels:
        ws.insert_rows(LIGNE_PREMIER_PRODUIT_MODELE + nb_actuels, amount=nb_nouveaux - nb_actuels)
    elif nb_nouveaux < nb_actuels:
        ws.delete_rows(LIGNE_PREMIER_PRODUIT_MODELE + nb_nouveaux, amount=nb_actuels - nb_nouveaux)

    for i, produit in enumerate(produits):
        r = LIGNE_PREMIER_PRODUIT_MODELE + i
        ws.cell(r, 1).value = produit["code"]
        ws.cell(r, 2).value = produit["libelle"]
        ws.cell(r, 4).value = produit.get("libelle_vif", "")
        ws.cell(r, 5).value = produit.get("type_calcul", "")

    horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy(chemin, f"{chemin}.bak_avant_edition_produits_{horodatage}")
    wb.save(chemin)


def _nom_fichier_association(nom, annee):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", nom).strip("_").lower() or "association"
    return f"association_gardant_{annee}_{slug}.xlsx"


def _date_collecte(annee):
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT date_debut, date_fin FROM collecte_campagnes WHERE annee = ?",
            (annee,),
        ).fetchone()
    if not campagne or not campagne["date_debut"]:
        return str(annee)
    debut = campagne["date_debut"]
    fin = campagne["date_fin"]
    try:
        debut = datetime.strptime(debut, "%Y-%m-%d").strftime("%d/%m/%Y")
        fin = datetime.strptime(fin, "%Y-%m-%d").strftime("%d/%m/%Y") if fin else None
    except ValueError:
        pass
    return f"du {debut} au {fin}" if fin else debut


def _inserer_logo(ws, largeur_px):
    """Insère le logo BAI en haut de la feuille, toujours depuis le fichier
    statique de l'application (le même que celui utilisé pour les PDF), et
    retire toute image héritée du modèle avant. Le modèle Excel partagé
    peut ainsi être édité librement (y compris via Google Sheets) sans que
    son propre logo — sujet à un format de dessin que certaines versions
    d'Excel refusent d'ouvrir après un aller-retour Google Sheets — ne soit
    jamais utilisé tel quel dans les fichiers générés."""
    chemin_logo = os.path.join(current_app.root_path, "static", "images", "logo_ba_complet.png")
    if not os.path.exists(chemin_logo):
        return
    ws._images = []
    image = ImageOpenpyxl(chemin_logo)
    ratio = image.height / image.width
    image.width = largeur_px
    image.height = round(largeur_px * ratio)
    ws.add_image(image, "A1")



# Le modèle Excel partagé (Modele association.xlsx) utilise des jetons texte
# ("àassociation", "àcode", "àdatecollecte", "àannéecollecte") pour toutes
# les valeurs à substituer, SAUF l'année de collecte qui apparaît par endroits
# comme un nombre/texte "2025" brut — reliquat d'avant la convention par
# jetons. ANNEE_PLACEHOLDER_MODELE_ASSOCIATION nomme explicitement cette
# valeur plutôt que de la laisser en dur dans le code ci-dessous : si le
# modèle est un jour retouché pour utiliser "àannéecollecte" partout, cette
# constante (et les deux lignes qui la référencent) pourra être retirée.
ANNEE_PLACEHOLDER_MODELE_ASSOCIATION = 2025


def _creer_fichier_association(asso, annee, dossier):
    source = _modele_gardee("Modele association.xlsx")
    if not os.path.exists(source):
        raise FileNotFoundError("Modèle Excel association introuvable")

    chemin = os.path.join(dossier, _nom_fichier_association(asso["nom"], annee))
    wb = load_workbook(source)
    code = asso.get("code_vif") or ""
    date_collecte = _date_collecte(annee)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value == ANNEE_PLACEHOLDER_MODELE_ASSOCIATION:
                    cell.value = annee
                if isinstance(cell.value, str):
                    cell.value = cell.value.replace("àassociation", asso["nom"])
                    cell.value = re.sub(r"àcode(?!\d)", code, cell.value)
                    cell.value = cell.value.replace("àdatecollecte", date_collecte)
                    cell.value = cell.value.replace("àannéecollecte", str(annee))
                    cell.value = cell.value.replace(str(ANNEE_PLACEHOLDER_MODELE_ASSOCIATION), str(annee))

    ws = wb["magasins"]
    for index, magasin in enumerate(asso["magasins"], start=1):
        row = 14 + index
        if row >= 25:
            ws.insert_rows(row)
        ws.cell(row=row, column=1).value = _vif_fmt(magasin.get("Code VIF"))
        ws.cell(row=row, column=2).value = _valeur_propre(magasin.get("Nom"))
        ws.cell(row=row, column=3).value = None
    for row in range(15 + len(asso["magasins"]), 24):
        for column in range(1, 10):
            ws.cell(row=row, column=column).value = None
    ws["C25"] = "=SUM(C15:C23)"
    feuille_produits = wb["produits"]
    zone_produits = f"A1:E{max(_derniere_ligne_utilisee(feuille_produits), 44)}"
    for feuille, zone in ((feuille_produits, zone_produits), (ws, "A1:I27")):
        feuille.page_setup.orientation = "portrait"
        feuille.page_setup.fitToWidth = 1
        feuille.page_setup.fitToHeight = 1
        feuille.sheet_properties.pageSetUpPr.fitToPage = True
        feuille.print_area = zone
    _inserer_logo(feuille_produits, 588)
    _inserer_logo(ws, 444)
    wb.save(chemin)
    return chemin


def _evaluer_formule_somme(ws, formule):
    """Évalue une formule Excel du type =SUM(C15:C23) en relisant les valeurs
    déjà présentes dans la feuille — openpyxl ne recalcule pas les formules,
    mais Excel/LibreOffice afficheraient 0 (pas une case vide) tant
    qu'aucune donnée n'est saisie dans la plage : on reproduit ce
    comportement pour les lignes Total des modèles, qui n'utilisent que des
    SUM simples sur une colonne ou une ligne. Retourne None si la formule
    n'est pas un SUM reconnu (laissée telle quelle, affichée vide ailleurs)."""
    m = re.fullmatch(r"=SUM\(([A-Z]+)(\d+):([A-Z]+)(\d+)\)", str(formule).strip(), re.IGNORECASE)
    if not m:
        return None
    from openpyxl.utils import column_index_from_string
    col1, row1, col2, row2 = m.groups()
    c1, c2 = column_index_from_string(col1.upper()), column_index_from_string(col2.upper())
    r1, r2 = int(row1), int(row2)
    total = 0
    for r in range(min(r1, r2), max(r1, r2) + 1):
        for c in range(min(c1, c2), max(c1, c2) + 1):
            v = ws.cell(r, c).value
            if isinstance(v, (int, float)):
                total += v
    return total


def _ligne_bordee(ws, r, colonnes):
    """Une ligne Excel a un quadrillage imprimable si sa première colonne
    visible porte une bordure — c'est ce qui distingue, dans les modèles
    collecte gardée, le tableau proprement dit (avec ses lignes vides
    laissées pour écrire d'autres magasins à la main) de la ligne Total et
    des notes qui suivent (non bordées, simple texte, parfois surlignées)."""
    b = ws.cell(r, colonnes[0]).border
    return any(side is not None and side.style for side in (b.left, b.right, b.top, b.bottom))


def _couleur_depuis_argb(argb):
    if not argb or len(argb) < 6 or argb == "00000000":
        return None
    hexa = argb[-6:]
    try:
        return colors.Color(int(hexa[0:2], 16) / 255.0, int(hexa[2:4], 16) / 255.0, int(hexa[4:6], 16) / 255.0)
    except ValueError:
        return None


def _lire_tableau_feuille_association(ws):
    """Reconstruit dynamiquement la structure d'un tableau (en-têtes,
    largeurs, données) depuis la feuille Excel RÉELLEMENT générée — plutôt
    que des numéros de ligne/colonne figés dans le code, qui se
    désynchronisent dès que quelqu'un modifie le modèle (ex. ajout d'une
    colonne) sans toucher au code Python. Convention du modèle, utilisée
    pour détecter la structure sans rien supposer d'autre : la première
    colonne du tableau contient 'code' (ou 'code produit') sur la ligne
    d'en-tête, et une éventuelle 2e ligne d'en-tête (sous-colonnes, ex.
    'Bénévoles' réparti en 4 demi-journées) a sa première cellule vide. Les
    colonnes masquées dans Excel (ex. 'libellé VIF', technique) sont
    ignorées, comme le ferait une impression native du classeur.

    Retourne (entetes, lignes_tableau, lignes_hors_tableau, largeurs_mm) :
    - lignes_tableau = lignes quadrillées (header compris plus loin), y
      compris les lignes vides du modèle (prévues pour écrire d'autres
      magasins/produits à la main) ;
    - lignes_hors_tableau = lignes suivantes non quadrillées (Total, notes),
      sous forme de (valeurs, couleurs_fond) — formules SUM déjà évaluées
      (voir _evaluer_formule_somme)."""
    from openpyxl.utils import get_column_letter, range_boundaries

    zone = ws.print_area or ws.dimensions
    zone = str(zone).split(",")[0] if zone else "A1:A1"
    zone = zone.split("!")[-1].replace("$", "")
    min_col, min_row, max_col, max_row = range_boundaries(zone)

    colonnes = [
        c for c in range(min_col, max_col + 1)
        if not getattr(ws.column_dimensions.get(get_column_letter(c)), "hidden", False)
    ]

    ligne_entete = None
    for r in range(min_row, max_row + 1):
        v = ws.cell(r, colonnes[0]).value
        if isinstance(v, str) and v.strip().lower().startswith("code"):
            ligne_entete = r
            break
    if ligne_entete is None:
        ligne_entete = min_row

    # Le print_area du modèle peut inclure, en bout de tableau, des colonnes
    # redevenues inutilisées (fusionnées dans une colonne précédente, ex.
    # l'ancienne colonne 'KG Brut' absorbée dans 'Poids en kg brut') : ni
    # bordées ni masquées, elles n'apparaîtraient pas davantage à
    # l'impression native puisque sans bordure. On ne garde que les
    # colonnes dont l'en-tête est réellement bordée.
    colonnes = [c for c in colonnes if _ligne_bordee(ws, ligne_entete, [c])]

    sous_entete = ligne_entete + 1
    a_sous_entete = (
        sous_entete <= max_row
        and ws.cell(sous_entete, colonnes[0]).value is None
        and any(isinstance(ws.cell(sous_entete, c).value, str) and ws.cell(sous_entete, c).value.strip()
                for c in colonnes)
    )

    entetes = []
    for c in colonnes:
        principal = ws.cell(ligne_entete, c).value
        secondaire = ws.cell(sous_entete, c).value if a_sous_entete else None
        entetes.append(secondaire if isinstance(secondaire, str) and secondaire.strip() else principal)

    def valeurs_ligne(r):
        valeurs = []
        for c in colonnes:
            v = ws.cell(r, c).value
            if isinstance(v, str) and v.strip().startswith("="):
                calcul = _evaluer_formule_somme(ws, v)
                v = calcul if calcul is not None else v
            valeurs.append(v)
        return valeurs

    # Le print_area peut aussi se figer en retard sur de nouvelles lignes de
    # note ajoutées sous le tableau (ex. texte en jaune) : on continue donc
    # la recherche des lignes hors tableau jusqu'à la fin réelle de la
    # feuille, pas seulement jusqu'à la borne du print_area.
    limite = max(max_row, ws.max_row)

    debut_donnees = (sous_entete if a_sous_entete else ligne_entete) + 1
    lignes_tableau = []
    r = debut_donnees
    while r <= limite and _ligne_bordee(ws, r, colonnes):
        lignes_tableau.append(valeurs_ligne(r))
        r += 1

    lignes_hors_tableau = []
    for r2 in range(r, limite + 1):
        valeurs = valeurs_ligne(r2)
        if not any(v is not None for v in valeurs):
            continue
        couleurs_fond = []
        for c in colonnes:
            cell = ws.cell(r2, c)
            argb = None
            try:
                if cell.fill and cell.fill.patternType:
                    argb = cell.fill.fgColor.rgb
            except Exception:
                argb = None
            couleurs_fond.append(_couleur_depuis_argb(argb) if isinstance(argb, str) else None)
        lignes_hors_tableau.append((valeurs, couleurs_fond))

    largeurs_excel = []
    for c in colonnes:
        dim = ws.column_dimensions.get(get_column_letter(c))
        largeurs_excel.append(dim.width if dim and dim.width else 10.0)
    disponible_mm = 190.0
    total_excel = sum(largeurs_excel) or 1
    largeurs_mm = [max(15.0, largeur / total_excel * disponible_mm) for largeur in largeurs_excel]

    return entetes, lignes_tableau, lignes_hors_tableau, largeurs_mm


def _creer_pdf_association(fichier_excel, asso, annee, dossier):
    """Crée les deux fiches PDF en reprenant la structure RÉELLE du fichier
    Excel généré (en-têtes, nombre de colonnes, largeurs) — voir
    _lire_tableau_feuille_association. Ce n'est pas une impression native
    du classeur (pas de moteur LibreOffice/Excel disponible sur ce serveur),
    mais la mise en page s'adapte désormais automatiquement si le modèle
    Excel est modifié (colonne ajoutée/retirée/renommée), sans plus jamais
    nécessiter de toucher ce code."""
    wb = load_workbook(fichier_excel, data_only=False)
    nom = os.path.splitext(os.path.basename(fichier_excel))[0] + ".pdf"
    chemin = os.path.join(dossier, nom)
    page_width, page_height = A4
    pdf = pdf_canvas.Canvas(chemin, pagesize=A4)
    logo = os.path.join(current_app.root_path, "static", "images", "logo_ba_complet.png")

    def texte(valeur):
        s = ("" if valeur is None else str(valeur)).replace("\n", " ").strip()
        if s.startswith("="):
            # Formule Excel non calculée (openpyxl ne recalcule pas) —
            # afficher la formule brute serait trompeur, mieux vaut une
            # case vide (l'association ouvrira le classeur Excel joint
            # pour voir le vrai total calculé).
            return ""
        return s

    def decouper_texte(s, largeur_dispo, police, taille):
        if not s:
            return [""]
        mots = s.split(" ")
        lignes_decoupees, courante = [], ""
        for mot in mots:
            essai = (courante + " " + mot).strip()
            if pdf.stringWidth(essai, police, taille) <= largeur_dispo or not courante:
                courante = essai
            else:
                lignes_decoupees.append(courante)
                courante = mot
        if courante:
            lignes_decoupees.append(courante)
        return lignes_decoupees or [""]

    def entete(titre):
        if os.path.exists(logo):
            pdf.drawImage(ImageReader(logo), 12 * mm, page_height - 25 * mm, width=72 * mm, height=8 * mm, preserveAspectRatio=True, mask="auto")
        pdf.setFont("Helvetica", 8)
        pdf.drawString(24 * mm, page_height - 32 * mm, "11 allée de la Pinéa - 38600 FONTAINE")
        pdf.drawString(24 * mm, page_height - 38 * mm, "Tél. : 04.76.85.92.50")
        pdf.drawString(24 * mm, page_height - 44 * mm, "Mail : ba380.collecte@banquealimentaire.org")
        y = page_height - 53 * mm
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(70 * mm, y, "COLLECTE")
        pdf.drawString(105 * mm, y, str(annee))
        pdf.drawString(24 * mm, y - 8 * mm, _date_collecte(annee))
        pdf.drawString(105 * mm, y - 8 * mm, titre)
        pdf.line(24 * mm, y - 11 * mm, 175 * mm, y - 11 * mm)
        code_vif = asso.get("code_vif") or ""
        pdf.drawString(24 * mm, y - 19 * mm, asso["nom"])
        if code_vif:
            pdf.drawString(70 * mm, y - 19 * mm, code_vif)
        return y - 25 * mm

    def grille(lignes, largeurs, x, y, hauteur_min, gras_premiere=False):
        yy = y
        for index, ligne in enumerate(lignes):
            gras = gras_premiere and index == 0
            police = "Helvetica-Bold" if gras else "Helvetica"
            taille = 6.5 if gras else 6
            cellules = []
            for col, largeur in enumerate(largeurs):
                dispo = largeur - 2.4 * mm
                cellules.append(decouper_texte(texte(ligne[col]), dispo, police, taille))
            nb_lignes_texte = max(len(c) for c in cellules)
            interligne = taille * 1.25
            hauteur_ligne = max(hauteur_min, nb_lignes_texte * interligne + 2 * mm)
            xx = x
            for col, largeur in enumerate(largeurs):
                pdf.rect(xx, yy - hauteur_ligne, largeur, hauteur_ligne)
                pdf.setFont(police, taille)
                for num_ligne, morceau in enumerate(cellules[col]):
                    pdf.drawString(xx + 1.2 * mm, yy - 1 * mm - (num_ligne + 1) * interligne, morceau)
                xx += largeur
            yy -= hauteur_ligne
        return yy

    def lignes_libres(lignes_et_couleurs, largeurs, x, y):
        yy = y
        police, taille = "Helvetica", 6.5
        interligne = taille * 1.25
        for ligne, couleurs_fond in lignes_et_couleurs:
            cellules = []
            for col, largeur in enumerate(largeurs):
                dispo = largeur - 2.4 * mm
                cellules.append(decouper_texte(texte(ligne[col]), dispo, police, taille))
            nb_lignes_texte = max(len(c) for c in cellules)
            hauteur_ligne = nb_lignes_texte * interligne + 2 * mm
            xx = x
            for col, largeur in enumerate(largeurs):
                if couleurs_fond[col] is not None:
                    pdf.setFillColor(couleurs_fond[col])
                    pdf.rect(xx, yy - hauteur_ligne, largeur, hauteur_ligne, stroke=0, fill=1)
                    pdf.setFillColor(colors.black)
                pdf.setFont(police, taille)
                for num_ligne, morceau in enumerate(cellules[col]):
                    pdf.drawString(xx + 1.2 * mm, yy - 1 * mm - (num_ligne + 1) * interligne, morceau)
                xx += largeur
            yy -= hauteur_ligne
        return yy

    for nom_feuille, titre in (("produits", "FICHE PRODUITS"), ("magasins", "FICHE MAGASINS")):
        ws = wb[nom_feuille]
        entetes, lignes_tableau, lignes_hors_tableau, largeurs_mm = _lire_tableau_feuille_association(ws)
        y = entete(titre)
        lignes = [entetes] + lignes_tableau
        hauteur_ligne = max(5.5, min(8.0, 160 / max(len(lignes), 1))) * mm
        largeurs = [l * mm for l in largeurs_mm]
        y = grille(lignes, largeurs, 10 * mm, y, hauteur_ligne, True)
        if lignes_hors_tableau:
            lignes_libres(lignes_hors_tableau, largeurs, 10 * mm, y - 2 * mm)
        pdf.showPage()

    pdf.save()
    return chemin


GARDEE_TEXTE_MAIL_DEFAUT = (
    "De la Banque Alimentaire de l'Isère à <<association>> (code <<code>>)\n\n"
    "COLLECTE NATIONALE BANQUE ALIMENTAIRE des <<datecollecte>>\n\n"
    "Vous collectez au titre de la Banque Alimentaire de l'Isère et conservez les produits collectés "
    "pour les redistribuer à vos bénéficiaires.\n\n"
    "Nous avons besoin, avec la Fédération des Banques Alimentaires, de connaitre le tonnage collecté "
    "avec deux axes :\n"
    "-le tonnage par magasin qui est communiqué aux grandes enseignes pour leurs magasins\n"
    "-le tonnage par produit qui sert aussi à communiquer au niveau national sur les résultats de la "
    "collecte\n\n"
    "Pour cela nous souhaitons :\n"
    "-idéalement le poids en kg brut de chaque produit\n"
    "-et aussi idéalement le poids total par magasin en kg brut soit réel soit une estimation\n\n"
    "Nous vous demandons de nous retourner le plus rapidement possible après la collecte le fichier "
    "excel joint complété directement en l'envoyant par mail à ba380.collecte@banquealimentaire.org.\n\n"
    "Nous souhaitons aussi avoir un décompte le plus précis du nombre de bénévoles par demi-journée "
    "ayant participé à la collecte.\n\n"
    "Si vous ne pouvez pas compléter ce fichier sur un ordinateur, envoyez-nous une version papier "
    "manuscrite de ce fichier.\n\n"
    "Ce fichier a 2 feuilles, une « produits » et une « magasins ».\n"
    "Les magasins que vous collectez sont :\n"
    "<<magasins>>\n\n"
    "<<lien_saisie>>\n\n"
    "Merci de bien vouloir accuser réception de ce message à l'adresse mail : "
    "ba380.collecte@banquealimentaire.org.\n\n"
    "Vous pouvez nous contacter si vous avez besoin de précisions ou d'explications.\n\n"
    "Merci d'avance pour votre implication dans cette collecte.\n\n"
    "Responsable collecte de la BA38"
)

CHEMIN_TEXTE_MAIL_GARDEE = os.path.join(MODELES_GARDEE_DIR, "gardee_mail_texte.txt")

_MOTIF_BALISE_HTML = re.compile(r"<(p|br|div|span|strong|b|em|i|u|ul|ol|li|a)[ >/]", re.I)


def _texte_brut_vers_html(texte):
    """Convertit un texte brut (\\n\\n = paragraphe, \\n = retour à la ligne)
    en HTML équivalent — sert à migrer en douceur un texte legacy (saisi
    avant l'éditeur riche Quill) sans perdre sa mise en page ; les repères
    <<...>> sont échappés comme le ferait Quill si on les tapait au clavier
    (voir _remplacer_placeholder, qui sait retrouver les deux formes)."""
    paragraphes = texte.strip().split("\n\n")
    return "".join(
        "<p>" + html_lib.escape(p).replace("\n", "<br>") + "</p>"
        for p in paragraphes
    )


def _lire_texte_mail_gardee():
    """Texte (HTML, édité avec Quill) du mail envoyé aux associations
    gardant leur collecte, dans un fichier PARTAGÉ (comme l'image et les
    pièces jointes du mail chauffeurs/équipiers) — modifiable en ligne, sans
    passer par un modèle Word, et disponible d'une campagne et d'une session
    à l'autre sans redépôt annuel. Un texte brut déposé avant l'éditeur
    riche est converti à la volée (voir _texte_brut_vers_html)."""
    if os.path.exists(CHEMIN_TEXTE_MAIL_GARDEE):
        with open(CHEMIN_TEXTE_MAIL_GARDEE, "r", encoding="utf-8") as f:
            texte = f.read()
        if not _MOTIF_BALISE_HTML.search(texte):
            return _texte_brut_vers_html(texte)
        return texte
    return GARDEE_TEXTE_MAIL_DEFAUT_HTML


def _ecrire_texte_mail_gardee(texte):
    with open(CHEMIN_TEXTE_MAIL_GARDEE, "w", encoding="utf-8") as f:
        f.write(texte)


GARDEE_TEXTE_MAIL_DEFAUT_HTML = _texte_brut_vers_html(GARDEE_TEXTE_MAIL_DEFAUT)


def _remplacer_placeholder(texte, nom, valeur):
    """Remplace un repère <<nom>> dans un texte HTML édité avec Quill : quand
    l'utilisateur tape <<nom>> comme texte visible dans l'éditeur, le
    navigateur échappe les chevrons en &lt;/&gt; en enregistrant le HTML —
    on cherche donc les deux formes (littérale, pour un texte legacy pas
    encore repassé par Quill, et échappée)."""
    valeur = valeur or ""
    for motif in (f"<<{nom}>>", f"&lt;&lt;{nom}&gt;&gt;"):
        texte = texte.replace(motif, valeur)
    return texte


def _texte_modele_gardee(asso, annee, lien_saisie=None):
    """Repères <<association>>, <<code>>, <<datecollecte>>, <<magasins>> et
    <<lien_saisie>> — ce dernier substitué seulement si présent dans le
    texte, sinon ajouté en dernier paragraphe (compatibilité avec un texte
    personnalisé plus ancien qui ne l'aurait pas encore). Le texte est au
    format HTML (éditeur riche Quill, mis en forme possible : gras, couleur,
    taille) ; chaque valeur insérée est échappée, et la liste des magasins
    passe par des <br> plutôt que des retours à la ligne bruts."""
    magasins = "<br>".join(
        html_lib.escape(f"- {_vif_fmt(m.get('Code VIF'))} — {m.get('Nom', '')} ({m.get('Ville', '')})")
        for m in asso["magasins"]
    )
    texte = _lire_texte_mail_gardee()
    texte = _remplacer_placeholder(texte, "association", html_lib.escape(asso["nom"]))
    texte = _remplacer_placeholder(texte, "code", html_lib.escape(asso.get("code_vif") or ""))
    texte = _remplacer_placeholder(texte, "datecollecte", html_lib.escape(_date_collecte(annee)))
    texte = _remplacer_placeholder(texte, "magasins", magasins)

    lien_html = f'<a href="{lien_saisie}">{html_lib.escape(lien_saisie)}</a>' if lien_saisie else ""
    if "<<lien_saisie>>" in texte or "&lt;&lt;lien_saisie&gt;&gt;" in texte:
        texte = _remplacer_placeholder(texte, "lien_saisie", lien_html)
    elif lien_saisie:
        texte = texte.rstrip() + (
            "<p>Vous pouvez aussi saisir directement en ligne le poids par magasin et la répartition "
            f"par produit, sans avoir besoin de renvoyer les fichiers joints : {lien_html}</p>"
        )

    return texte.strip()


def _entier_ou_zero(valeur):
    """Convertit en entier une valeur venant d'un Excel partagé (liste_groupes)
    en tolérant les valeurs non numériques (texte libre, cellule vide...) —
    renvoie 0 plutôt que de planter toute la page (audit 2026-10-23, #4)."""
    nombre = pd.to_numeric(valeur, errors="coerce")
    return int(nombre) if pd.notna(nombre) else 0


def _construire_associations(df_mag, df_groupes):
    """Rattache chaque magasin gardé à son (ou ses) association(s), à partir
    de la colonne 'Gardée par' (normalisée — voir _normaliser_gardee_par) et
    d'une éventuelle seconde association révélée par 'Stockage' (voir
    _associations_secondaires_stockage) : la liste des associations vient
    donc de df_mag, pas de df_groupes. Chaque association trouvée dans
    df_groupes est enrichie de ses métadonnées (type, contacts, fiche
    groupe) ; sinon ('trouvee': False) elle apparaît quand même, avec juste
    son nom et ses magasins — jamais de magasin perdu."""
    df_mag = df_mag.fillna("")
    gardee_par = df_mag.get("Gardée par", pd.Series(dtype=str)).map(_normaliser_gardee_par)

    groupes_par_nom = {}
    groupes_par_nom_lower = {}
    groupes_par_nom_norm = {}
    if not df_groupes.empty and "Nom" in df_groupes.columns:
        for _, g in df_groupes.iterrows():
            # Certaines associations sont enregistrées dans liste_groupes.xlsx
            # avec le préfixe 'BAI+' collé au nom (ex. 'BAI+CCAS DOMENE') —
            # même normalisation que pour 'Gardée par' pour que la
            # correspondance fonctionne malgré ce préfixe.
            nom = _normaliser_gardee_par(g.get("Nom", ""))
            groupes_par_nom[nom] = g
            groupes_par_nom_lower[nom.lower()] = g
            # Repli supplémentaire (casse/accents/abréviations/alias — voir
            # _cle_recherche_association) pour les libellés trop différents
            # même après le nettoyage 'BAI+' ci-dessus (ex. 'ASAT Association
            # Solidaire Active De Tignieu' côté magasins vs 'ASAT' seul ici).
            groupes_par_nom_norm[_normaliser_nom_association(nom)] = g

    lignes_par_asso = {}
    for idx, row in df_mag.iterrows():
        primaire = gardee_par.loc[idx]
        if primaire:
            lignes_par_asso.setdefault(primaire, []).append(idx)
        for secondaire in _associations_secondaires_stockage(row.get("Stockage", ""), primaire):
            grp_sec = groupes_par_nom_lower.get(secondaire.lower())
            # Nom normalisé (sans préfixe 'BAI+'), pas le nom brut de
            # liste_groupes.xlsx qui le porte parfois lui aussi (ex. 'BAI+CCAS
            # DOMENE') — sinon l'association apparaît sous ce nom préfixé.
            nom_cle = _normaliser_gardee_par(grp_sec.get("Nom")) if grp_sec is not None else secondaire
            lignes_par_asso.setdefault(nom_cle, []).append(idx)

    associations = []
    for nom_asso in sorted(lignes_par_asso.keys(), key=str.lower):
        magasins_grp = df_mag.loc[lignes_par_asso[nom_asso]]
        grp = groupes_par_nom.get(nom_asso)
        if grp is None:
            grp = groupes_par_nom_norm.get(_cle_recherche_association(nom_asso))
        associations.append({
            "nom": nom_asso,
            "type": _valeur_propre(grp.get("Type")) if grp is not None else "",
            "membre_bai": _valeur_propre(grp.get("Membre BAI")) if grp is not None else "",
            "nb_contacts": _entier_ou_zero(grp.get("Nombre contacts")) if grp is not None else 0,
            "nb_leaders": _entier_ou_zero(grp.get("Nombre leaders")) if grp is not None else 0,
            "fiche_groupe": _valeur_propre(grp.get("Fiche groupe")) if grp is not None else "",
            "trouvee": grp is not None,
            "magasins": magasins_grp[["Code VIF", "Nom", "Ville", "Stockage", "Contact", "Email"]].to_dict("records"),
        })

    idx_attribues = {idx for indices in lignes_par_asso.values() for idx in indices}
    mask_sans_association = (gardee_par == "") & ~df_mag.index.isin(idx_attribues)
    sans_association = df_mag[mask_sans_association][["Code VIF", "Nom", "Ville", "Stockage"]].to_dict("records")
    return associations, sans_association


@collecte_bp.route("/collecte/gardee/modele-excel")
@login_required
@require_access("collecte", "lecture")
def telecharger_modele_association():
    """Télécharge le modèle Excel utilisé pour générer le fichier
    personnalisé de chaque association (feuilles « produits » et
    « magasins ») — à modifier puis redéposer via « 🔄 Remplacer le modèle »
    sur la page, sans connaissance technique ni accès serveur requis."""
    nom = "Modele association.xlsx"
    chemin = _modele_gardee(nom)
    if not os.path.exists(chemin):
        flash("❌ Modèle introuvable", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=request.args.get("annee", type=int)))
    return send_file(
        chemin,
        as_attachment=True,
        download_name=nom,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/gardee/modele-excel", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_modele_association():
    """Remplace le modèle Excel des associations — sauvegarde l'ancienne
    version avant d'écraser (cf. l'incident de logo dupliqué du modèle
    précédent), et vérifie que le fichier déposé est un classeur Excel
    valide avec les deux feuilles attendues avant de l'accepter, pour ne
    pas casser la génération des fichiers association."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    fichier = request.files.get("modele_excel")

    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier sélectionné.", "warning")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    if not fichier.filename.lower().endswith(".xlsx"):
        flash("⛔ Format non accepté (.xlsx uniquement).", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    tampon = io.BytesIO(fichier.read())
    try:
        wb = load_workbook(tampon)
    except Exception:
        flash("⛔ Fichier Excel invalide ou corrompu — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    manquantes = [feuille for feuille in ("produits", "magasins") if feuille not in wb.sheetnames]
    if manquantes:
        flash(f"⛔ Feuille(s) manquante(s) dans le fichier déposé : {', '.join(manquantes)} — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    nom = "Modele association.xlsx"
    chemin = _modele_gardee(nom)
    if os.path.exists(chemin):
        horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy(chemin, f"{chemin}.bak_avant_remplacement_{horodatage}")

    tampon.seek(0)
    with open(chemin, "wb") as f:
        f.write(tampon.read())

    flash("✅ Modèle Excel remplacé.", "success")
    write_log(f"📊 Modèle association.xlsx remplacé par {current_user.email}")
    return redirect(url_for("collecte.gardee_envoi", annee=annee))


CHEMIN_URL_DRIVE_MODELE_ASSOCIATION = os.path.join(MODELES_GARDEE_DIR, "modele_association_drive_url.txt")


def _lire_url_drive_modele_association():
    """Lien Google Sheets du modèle association, s'il a été renseigné —
    fichier partagé (pas de campagne associée, le modèle est indépendant
    de l'année)."""
    if not os.path.exists(CHEMIN_URL_DRIVE_MODELE_ASSOCIATION):
        return ""
    with open(CHEMIN_URL_DRIVE_MODELE_ASSOCIATION, "r", encoding="utf-8") as f:
        return f.read().strip()


def _ecrire_url_drive_modele_association(url):
    os.makedirs(MODELES_GARDEE_DIR, exist_ok=True)
    with open(CHEMIN_URL_DRIVE_MODELE_ASSOCIATION, "w", encoding="utf-8") as f:
        f.write(url.strip())


@collecte_bp.route("/collecte/gardee/modele-excel/lien-drive", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_url_drive_modele_association():
    """Enregistre le lien Google Sheets utilisé pour synchroniser le modèle
    association — permet ensuite de l'éditer directement dans Google
    Sheets (titres, mise en page, commentaires, formules) sans avoir à
    déposer manuellement un fichier .xlsx à chaque modification."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    url = request.form.get("url_drive", "").strip()
    _ecrire_url_drive_modele_association(url)
    flash("✅ Lien Google Sheets enregistré." if url else "🗑️ Lien Google Sheets effacé.", "success")
    return redirect(url_for("collecte.gardee_envoi", annee=annee))


@collecte_bp.route("/collecte/gardee/modele-excel/synchroniser", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def synchroniser_modele_association():
    """Télécharge la version actuelle du modèle depuis Google Sheets
    (même mécanisme que pour la liste des magasins/véhicules) et remplace
    le modèle Excel partagé — avec sauvegarde préalable et vérification
    que le fichier obtenu est exploitable (feuilles attendues présentes,
    logo toujours détecté), car un export Google Sheets peut, dans de
    rares cas, mal réencoder une image intégrée."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    url = _lire_url_drive_modele_association()
    url_export = _url_export_drive(url)
    if not url_export:
        flash("⛔ Aucun lien Google Sheets valide n'est enregistré.", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    try:
        reponse = requests.get(url_export, timeout=30)
        reponse.raise_for_status()
    except Exception:
        flash("⛔ Échec du téléchargement depuis Google Sheets — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    if not reponse.content.startswith(b"PK"):
        flash("⛔ Contenu invalide reçu de Google Sheets — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    tampon = io.BytesIO(reponse.content)
    try:
        wb = load_workbook(tampon)
    except Exception:
        flash("⛔ Fichier reçu illisible par Excel — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    manquantes = [feuille for feuille in ("produits", "magasins") if feuille not in wb.sheetnames]
    if manquantes:
        flash(f"⛔ Feuille(s) manquante(s) dans la version Google Sheets : {', '.join(manquantes)} — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    nb_images = sum(len(feuille._images) for feuille in wb.worksheets)
    for feuille in wb.worksheets:
        _tronquer_grille_google_sheets(feuille)

    nom = "Modele association.xlsx"
    chemin = _modele_gardee(nom)
    if os.path.exists(chemin):
        horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy(chemin, f"{chemin}.bak_avant_sync_drive_{horodatage}")

    # Un export Google Sheets écrit ses dessins (logo) avec un préfixe de
    # namespace (<xdr:wsDr>...) qu'Excel accepte à l'ouverture mais qui a
    # déjà provoqué un « contenu illisible » une fois reconstruit par des
    # opérations ultérieures (cf. incident logo dupliqué du 15/09). On ne
    # sauvegarde donc jamais les octets bruts reçus : on les fait d'abord
    # repasser par une écriture openpyxl, qui régénère systématiquement
    # les dessins dans la forme canonique sans préfixe, sûre à re-relire
    # et re-sauvegarder ensuite (génération des fichiers association).
    tampon_normalise = io.BytesIO()
    wb.save(tampon_normalise)
    tampon_normalise.seek(0)

    with open(chemin, "wb") as f:
        f.write(tampon_normalise.read())

    write_log(f"📊 Modèle association.xlsx synchronisé depuis Google Sheets par {current_user.email}")
    if nb_images == 0:
        flash(
            "⚠️ Modèle synchronisé, mais aucune image détectée dans le fichier reçu — "
            "si le logo doit apparaître, vérifiez le fichier généré pour une association "
            "(l'ancienne version reste disponible en sauvegarde).",
            "warning",
        )
    else:
        flash("✅ Modèle synchronisé depuis Google Sheets.", "success")
    return redirect(url_for("collecte.gardee_envoi", annee=annee))


@collecte_bp.route("/collecte/gardee/modele-produits")
@login_required
@require_access("collecte", "lecture")
def gardee_modele_produits():
    """Éditeur en ligne de la liste des produits du modèle association —
    ajoute/modifie/supprime des lignes directement dans le fichier Excel
    partagé, sans avoir besoin d'ouvrir Excel sur un poste."""
    annee = request.args.get("annee", type=int) or datetime.now().year
    return render_template(
        "collecte/gardee_modele_produits.html",
        annee=annee,
        produits=_lire_produits_modele_complet(),
    )


@collecte_bp.route("/collecte/gardee/modele-produits/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_modele_produits_route():
    donnees = request.get_json(silent=True) or {}
    produits = donnees.get("produits") or []
    produits_valides = [
        p for p in produits
        if str(p.get("code", "")).strip() and str(p.get("libelle", "")).strip()
    ]
    if not produits_valides:
        return jsonify({"success": False, "erreur": "La liste ne peut pas être vide"}), 400
    try:
        _enregistrer_produits_modele(produits_valides)
    except Exception as erreur:
        return jsonify({"success": False, "erreur": str(erreur)}), 500
    write_log(f"📦 Modèle produits association modifié ({len(produits_valides)} produits) par {current_user.email}")
    return jsonify({"success": True, "nb": len(produits_valides)})


@collecte_bp.route("/collecte/gardee")
@login_required
@require_access("collecte", "lecture")
def gardee():
    annee = request.args.get("annee", type=int) or datetime.now().year

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)

    manquants = []
    if df_mag.empty:
        manquants.append(FICHIERS["magasins"]["label"])
    if df_groupes.empty:
        manquants.append(FICHIERS["groupes"]["label"])

    associations, sans_association = ([], [])
    if not manquants:
        associations, sans_association = _construire_associations(df_mag, df_groupes)
        df_participants = _lire_participants(annee)
        index_code_vif = _table_code_vif_associations()
        email_overrides = _email_overrides()
        gratuit_assos = _gratuit_associations()
        for asso in associations:
            asso["referents"] = _referents_association(asso["nom"], df_participants)
            asso["code_vif"], asso["ecart_code_vif"], asso["code_vif_force"] = _code_vif_association(asso["nom"], index_code_vif)
            asso["email_force"] = email_overrides.get(_cle_recherche_association(asso["nom"]), "")
            asso["gratuit"] = gratuit_assos.get(_cle_recherche_association(asso["nom"]), True)

    return render_template(
        "collecte/gardee.html",
        annee=annee,
        manquants=manquants,
        associations=associations,
        sans_association=sans_association,
        nb_magasins=len(df_mag),
        fichier_excel=os.path.exists(
            os.path.join(_dossier_annee(annee), f"associations_gardant_{annee}.xlsx")
        ),
    )


@collecte_bp.route("/collecte/gardee/code_vif_override", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_code_vif_override():
    """Force (ou efface) le Code VIF utilisé pour une association dans la
    liste 'Associations gardant' uniquement — n'écrit jamais dans la table
    associations (partagée avec indicateurs, cotisations, etc.), voir
    _ensure_table_code_vif_overrides."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    nom_association = (request.form.get("nom_association") or "").strip()
    code_vif = "" if request.form.get("reset") else (request.form.get("code_vif") or "").strip()
    cle = _cle_recherche_association(nom_association)

    with get_db_connection() as conn:
        _ensure_table_code_vif_overrides(conn)
        if code_vif:
            conn.execute("""
                INSERT INTO collecte_gardee_code_vif_overrides
                    (nom_normalise, nom_association, code_vif, defini_par, defini_le)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(nom_normalise) DO UPDATE SET
                    code_vif = excluded.code_vif,
                    nom_association = excluded.nom_association,
                    defini_par = excluded.defini_par,
                    defini_le = excluded.defini_le
            """, (cle, nom_association, code_vif, current_user.email, datetime.now().strftime("%Y-%m-%d %H:%M")))
            conn.commit()
            flash(f"✅ Code VIF forcé pour « {nom_association} » : {code_vif} (liste Associations gardant uniquement)", "success")
            write_log(f"✏️ Code VIF gardant forcé : {nom_association!r} -> {code_vif!r} par {current_user.email}")
        else:
            conn.execute(
                "DELETE FROM collecte_gardee_code_vif_overrides WHERE nom_normalise = ?", (cle,)
            )
            conn.commit()
            flash(f"↩️ Code VIF automatique rétabli pour « {nom_association} »", "success")
            write_log(f"↩️ Code VIF gardant : correction manuelle effacée pour {nom_association!r} par {current_user.email}")

    # Fichier Excel contrôlé déjà généré : le rafraîchir tout de suite pour
    # qu'un « Télécharger le fichier contrôlé » immédiat après la correction
    # ne renvoie jamais une version obsolète (sans la correction) — sans
    # bloquer sur une erreur ici, « Préparer l'envoi » le régénère de toute
    # façon à chaque ouverture.
    if os.path.exists(os.path.join(_dossier_annee(annee), f"associations_gardant_{annee}.xlsx")):
        _generer_fichier_excel_gardee(annee)

    return redirect(url_for("collecte.gardee", annee=annee))


@collecte_bp.route("/collecte/gardee/email_override", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_email_override():
    """Force (ou efface) l'email utilisé pour envoyer à une association dans
    la liste 'Associations gardant' uniquement — par défaut (sans
    correction), l'email reste celui du contact go-on-web marqué GR (ou de
    tous les participants du groupe) dans liste_participants.xlsx ; cette
    correction n'écrit jamais dans ce fichier ni dans la table associations
    (Partenaires). Plusieurs adresses peuvent être séparées par ';' ou ','."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    nom_association = (request.form.get("nom_association") or "").strip()
    email = "" if request.form.get("reset") else (request.form.get("email") or "").strip()
    cle = _cle_recherche_association(nom_association)

    if email and not _emails_magasin(email):
        flash("❌ Aucune adresse email valide reconnue", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    with get_db_connection() as conn:
        _ensure_table_email_overrides(conn)
        if email:
            conn.execute("""
                INSERT INTO collecte_gardee_email_overrides
                    (nom_normalise, nom_association, email, defini_par, defini_le)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(nom_normalise) DO UPDATE SET
                    email = excluded.email,
                    nom_association = excluded.nom_association,
                    defini_par = excluded.defini_par,
                    defini_le = excluded.defini_le
            """, (cle, nom_association, email, current_user.email, datetime.now().strftime("%Y-%m-%d %H:%M")))
            conn.commit()
            flash(f"✅ Email d'envoi forcé pour « {nom_association} » : {email} (liste Associations gardant uniquement)", "success")
            write_log(f"✏️ Email gardant forcé : {nom_association!r} -> {email!r} par {current_user.email}")
        else:
            conn.execute(
                "DELETE FROM collecte_gardee_email_overrides WHERE nom_normalise = ?", (cle,)
            )
            conn.commit()
            flash(f"↩️ Email automatique (go-on-web) rétabli pour « {nom_association} »", "success")
            write_log(f"↩️ Email gardant : correction manuelle effacée pour {nom_association!r} par {current_user.email}")

    # Cf. gardee_code_vif_override : rafraîchir tout de suite le fichier
    # Excel contrôlé déjà généré, pour qu'un téléchargement immédiat après
    # la correction reflète bien le nouvel email.
    if os.path.exists(os.path.join(_dossier_annee(annee), f"associations_gardant_{annee}.xlsx")):
        _generer_fichier_excel_gardee(annee)

    return redirect(url_for("collecte.gardee", annee=annee))


@collecte_bp.route("/collecte/gardee/gratuit", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_gratuit():
    """Enregistre le statut Gratuit (O/N) d'une association gardant."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    nom_association = (request.form.get("nom_association") or "").strip()
    gratuit = 0 if request.form.get("gratuit") == "N" else 1
    if not nom_association:
        flash("❌ Association manquante", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))
    with get_db_connection() as conn:
        _ensure_table_gratuit_gardee(conn)
        conn.execute("""
            INSERT INTO collecte_gardee_gratuit (nom_normalise, nom_association, gratuit, defini_par, defini_le)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(nom_normalise) DO UPDATE SET
                gratuit = excluded.gratuit,
                nom_association = excluded.nom_association,
                defini_par = excluded.defini_par,
                defini_le = excluded.defini_le
        """, (_cle_recherche_association(nom_association), nom_association, gratuit,
              current_user.email, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
    flash(f"✅ « {nom_association} » : Gratuit = {'Oui' if gratuit else 'Non'}", "success")
    write_log(f"✏️ Gratuit gardant : {nom_association!r} -> {'O' if gratuit else 'N'} par {current_user.email}")
    return redirect(url_for("collecte.gardee", annee=annee))


def _generer_fichier_excel_gardee(annee):
    """Reconstruit le fichier Excel combiné des associations gardant leur
    collecte, ainsi que la fiche Excel/PDF personnalisée de chacune —
    utilisé à la fois par la route dédiée (accès direct) et systématiquement
    à l'ouverture de « Préparer l'envoi », pour que la page reflète toujours
    les dernières données go-on-web sans bouton « Régénérer » à penser à
    cliquer. Retourne (True, None) si tout s'est bien passé, ou
    (False, message_erreur) sinon — jamais d'exception laissée remonter."""
    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)

    if df_mag.empty or df_groupes.empty:
        return False, "❌ Fichier(s) manquant(s) (liste des magasins et/ou des groupes)"

    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)

    associations, sans_association = _construire_associations(df_mag, df_groupes)
    df_participants = _lire_participants(annee)
    index_code_vif = _table_code_vif_associations()
    email_overrides = _email_overrides()
    for asso in associations:
        asso["referents"] = _referents_association(asso["nom"], df_participants)
        asso["code_vif"], asso["ecart_code_vif"], asso["code_vif_force"] = _code_vif_association(asso["nom"], index_code_vif)
        asso["email_force"] = email_overrides.get(_cle_recherche_association(asso["nom"]), "")

    def _fmt_referents(referents):
        return " ; ".join(
            f"{r['nom']}" + (f" <{r['email']}>" if r["email"] else "") + (f" ({r['telephone']})" if r["telephone"] else "")
            for r in referents
        )

    df_associations = pd.DataFrame([{
        "Nom": a["nom"],
        "Type": a["type"],
        "Membre BAI": a["membre_bai"],
        "Nombre contacts": a["nb_contacts"],
        "Nombre leaders": a["nb_leaders"],
        "Nombre magasins": len(a["magasins"]),
        "Trouvée dans liste des groupes": "Oui" if a["trouvee"] else "Non — nom absent de liste_groupes.xlsx",
        "Code VIF de l'association": a["code_vif"],
        "Écart Code VIF": a["ecart_code_vif"],
        "Référent - Nom": a["referents"][0]["nom"] if a["referents"] else "",
        "Référent - Email": a["referents"][0]["email"] if a["referents"] else "",
        "Référent - Téléphone": a["referents"][0]["telephone"] if a["referents"] else "",
        "Autres contacts": _fmt_referents(a["referents"][1:]),
        "Email d'envoi forcé": a["email_force"],
        "Fiche groupe": a["fiche_groupe"],
    } for a in associations])

    df_mag_flat = df_mag.fillna("")
    df_mag_flat["Association"] = df_mag_flat.apply(_association_effective, axis=1)
    colonnes_mag = ["Association", "Gardée par", "Code VIF", "Nom", "Ville", "Stockage",
                    "Contact", "Email", "Accord", "Commentaire", "Fiche magasin"]
    colonnes_mag = [c for c in colonnes_mag if c in df_mag_flat.columns]
    df_magasins = df_mag_flat[colonnes_mag].sort_values(["Association", "Nom"])

    # Onglet pivot : une association par ligne, ses magasins en colonnes
    # (même présentation que l'onglet 'Tournees' du module Production).
    nb_max_mag = max((len(a["magasins"]) for a in associations), default=0)
    colonnes_mag_pivot = [f"Magasin {i + 1}" for i in range(nb_max_mag)]
    rows_pivot = []
    for a in associations:
        noms_mags = [f"{m['Code VIF']} — {m['Nom']}" for m in a["magasins"]]
        row = {"Association": a["nom"], "Type": a["type"], "Nb magasins": len(noms_mags)}
        for i, col in enumerate(colonnes_mag_pivot):
            row[col] = noms_mags[i] if i < len(noms_mags) else ""
        rows_pivot.append(row)
    df_pivot = pd.DataFrame(rows_pivot, columns=["Association", "Type", "Nb magasins"] + colonnes_mag_pivot)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_associations.to_excel(writer, sheet_name="Associations", index=False)
        df_magasins.to_excel(writer, sheet_name="Magasins gardés", index=False)
        df_pivot.to_excel(writer, sheet_name="Associations - Magasins", index=False)
    buffer.seek(0)

    dossier = _dossier_annee(annee)
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, f"associations_gardant_{annee}.xlsx")
    try:
        for asso in associations:
            fichier_association = _creer_fichier_association(asso, annee, dossier)
            _creer_pdf_association(fichier_association, asso, annee, dossier)
    except RuntimeError as erreur:
        return False, f"❌ Création PDF impossible : {erreur}"
    with open(chemin, "wb") as fichier:
        fichier.write(buffer.getvalue())

    write_log(f"📥 Export Associations gardant {annee} par {current_user.email}")
    return True, None


@collecte_bp.route("/collecte/gardee/excel")
@login_required
@require_access("collecte", "lecture")
def gardee_excel():
    """Accès direct depuis la page d'accueil du module (« 🏠 Créer la
    liste ») — génère le fichier combiné et les fiches par association,
    puis renvoie sur la page liste (« Associations gardant »), à ne pas
    confondre avec « 📧 Préparer l'envoi » qui régénère aussi automatiquement
    à chaque ouverture (voir _generer_fichier_excel_gardee)."""
    annee = request.args.get("annee", type=int) or datetime.now().year
    ok, erreur = _generer_fichier_excel_gardee(annee)
    if not ok:
        flash(erreur, "danger")
    else:
        flash(
            f"✅ Fichier associations_gardant_{annee}.xlsx créé. Contrôlez-le avant de préparer l'envoi.",
            "success",
        )
    return redirect(url_for("collecte.gardee", annee=annee))


@collecte_bp.route("/collecte/gardee/telecharger")
@login_required
@require_access("collecte", "lecture")
def gardee_telecharger():
    annee = request.args.get("annee", type=int) or datetime.now().year
    nom = f"associations_gardant_{annee}.xlsx"
    chemin = os.path.join(_dossier_annee(annee), nom)
    if not os.path.exists(chemin):
        flash("❌ Le fichier Excel n'a pas encore été créé", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))
    return send_file(
        chemin,
        as_attachment=True,
        download_name=nom,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/gardee/telecharger_zip")
@login_required
@require_access("collecte", "lecture")
def gardee_telecharger_zip():
    """Zip du fichier Excel combiné et des fiches Excel/PDF par association
    (générés par « 🏠 Créer la liste »)."""
    annee = request.args.get("annee", type=int) or datetime.now().year
    dossier = _dossier_annee(annee)

    chemins = []
    combine = os.path.join(dossier, f"associations_gardant_{annee}.xlsx")
    if os.path.exists(combine):
        chemins.append(combine)
    chemins += sorted(glob.glob(os.path.join(dossier, f"association_gardant_{annee}_*.xlsx")))
    chemins += sorted(glob.glob(os.path.join(dossier, f"association_gardant_{annee}_*.pdf")))

    if not chemins:
        flash("❌ Aucun fichier à télécharger — créez la liste d'abord", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as archive:
        for chemin in chemins:
            archive.write(chemin, arcname=os.path.basename(chemin))
    tampon.seek(0)

    return send_file(
        tampon,
        as_attachment=True,
        download_name=f"associations_gardant_{annee}_documents.zip",
        mimetype="application/zip",
    )


# ============================================================================
# ⚖️ SAISIE DES POIDS PAR MAGASIN GARDÉ ET DES QUANTITÉS PAR PRODUIT — même
# principe de grille autosave que les cagettes (ba38_collecte_cagettes), mais
# une seule valeur en kg par ligne (pas de découpage par demi-journée) :
# ces poids/quantités sont relevés une fois, par pesée, sur les fiches
# renvoyées par les associations gardant leur collecte.
# ============================================================================

# La grille globale (une ligne par magasin, toutes associations confondues)
# a été supprimée le 2026-11-19 — orpheline depuis le 2026-11-16 (plus
# aucun bouton de menu ne pointait vers elle), et redondante avec la
# saisie par association (✏️ Saisir, même table collecte_poids_magasins_gardee,
# cf. gardee_saisie). Les 2 redirections d'erreur de
# exporter_poids_magasins_gardee pointent désormais vers gardee_envoi.


def _paires_magasin_association_a_exclure_export_poids(annee):
    """Paires (code_vif, nom_association) à exclure de l'export poids par
    magasin : association déjà cochée « Poids exporté » (confirmée importée
    dans VIF, cf. confirmer_vif_gardee) OU déjà dans un CSV généré pour
    cette étape en attente de cette confirmation (cf.
    _noms_associations_gardee_en_attente_confirmation) — permet de
    relancer cet export plusieurs fois sans redoubler les mêmes paires, au
    fil des associations qui répondent, et sans jamais regénérer un CSV
    déjà en attente d'import VIF. Exclusion par PAIRE (magasin,
    association), pas par magasin seul : un magasin collecté par plusieurs
    associations (ex. stockage partagé) donne lieu à une réception VIF par
    association, et l'export de l'une ne doit jamais exclure celui de
    l'autre (demande utilisateur 2026-11-23, suite au cas réel Intermarché
    La Mure, partagé Beurrepinard / Coll Entraide du Trièves)."""
    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if df_mag.empty:
        return set()
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    noms_exclus = (
        _noms_associations_gardee_avec_champ(annee, "poids_exporte")
        | _noms_associations_gardee_en_attente_confirmation(annee, "poids_exporte")
    )
    return {
        (_vif_fmt(m.get("Code VIF")), asso["nom"])
        for asso in associations
        if asso["nom"] in noms_exclus
        for m in asso["magasins"]
    }


@collecte_bp.route("/collecte/<int:annee>/poids-magasins-gardee/export")
@login_required
@require_access("collecte", "lecture")
def exporter_poids_magasins_gardee(annee):
    """Export CSV « Saisie des poids par magasin gardé » pour import VIF —
    même format que exporter_cagettes, quantité = poids en kg brut saisi,
    mais article 5010010 (collecte gardée) au lieu de 5010000 (collecte
    directe) — ces magasins peuvent aussi être suivis côté cagettes (camions
    BAI, cf. magasins Collecte BAI à stockage partagé avec une association),
    l'article différent évite que les deux exports comptent le même tonnage
    en double dans VIF. Une ligne par (magasin, association) : un magasin
    collecté par PLUSIEURS associations (stockage partagé) donne lieu à
    PLUSIEURS réceptions VIF distinctes, chacune avec le poids propre saisi
    par cette association (cf. _ensure_table_poids_magasins_gardee) — ce
    n'est PAS le même poids dupliqué, mais bien 2 pesées réelles différentes
    (demande utilisateur 2026-11-23, suite au cas réel Intermarché La Mure,
    partagé Beurrepinard / Coll Entraide du Trièves). Une association dont
    la saisie n'est pas complète (pas le badge vert « X/X complet », cf.
    _noms_associations_completes — mêmes magasins ET produits que pour le BL
    VIF) est exclue de CET export (ses magasins restent en attente, aucun
    blocage des autres) plutôt que de bloquer l'export entier comme avant
    (demande utilisateur 2026-11-23, suite à un cas réel : CCAS du Val de
    Virieu et Coll Entraide du Trièves complets bloqués par 32 magasins
    d'autres associations pas encore prêtes)."""
    df_mag = _lire_magasins_gardes(annee)
    if df_mag.empty:
        flash(f"⛔ Liste des magasins gardés {annee} indisponible", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)

    paires_deja_traitees = _paires_magasin_association_a_exclure_export_poids(annee)

    df_groupes = _lire_groupes(annee)
    associations, _ = _construire_associations(df_mag, df_groupes)
    noms_par_code_vif = {}
    for asso in associations:
        for m in asso["magasins"]:
            noms_par_code_vif.setdefault(_vif_fmt(m.get("Code VIF")), []).append(asso["nom"])

    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        saisies = {
            (r["code_vif"], r["nom_association"]): r["poids_kg"]
            for r in conn.execute(
                "SELECT code_vif, nom_association, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ?",
                (annee,),
            ).fetchall()
        }

    # Une association est exclue de CET export si sa saisie n'est pas
    # complète (magasins ET produits, même règle que le BL VIF) — les
    # autres associations déjà complètes ne sont jamais bloquées par
    # celles qui ne le sont pas encore.
    noms_completes = _noms_associations_completes(annee)
    noms_incomplets = {asso["nom"] for asso in associations if asso["nom"] not in noms_completes}

    date_reception = datetime.now().strftime("%d/%m/%Y")

    lignes = []
    magasins_en_attente = []
    paires_deja_exportees = []
    for _, row in df_mag.iterrows():
        code_vif = str(row.get("Code VIF", "")).strip()
        if not code_vif or code_vif.lower() == "nan":
            continue
        noms_asso_magasin = noms_par_code_vif.get(code_vif, [])
        if not noms_asso_magasin:
            magasins_en_attente.append(str(row.get("Nom", "")).strip())
            continue
        for nom_asso in noms_asso_magasin:
            paire = (code_vif, nom_asso)
            if paire in paires_deja_traitees:
                paires_deja_exportees.append(f"{row.get('Nom', '')} ({nom_asso})")
                continue
            if nom_asso in noms_incomplets:
                magasins_en_attente.append(f"{row.get('Nom', '')} ({nom_asso})")
                continue
            poids_kg = saisies.get(paire)
            if poids_kg is None:
                continue
            lignes.append((code_vif, poids_kg, nom_asso))

    noms_inclus = {nom_asso for _, _, nom_asso in lignes}

    if not lignes:
        if magasins_en_attente:
            flash(
                f"⚠️ Rien à exporter : {len(magasins_en_attente)} magasin(s) en attente d'une saisie "
                "complète dans leur association (export relançable dès que prêt).",
                "warning",
            )
        else:
            flash("⚠️ Aucun poids à exporter.", "warning")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    tampon = StringIO()
    ecrivain = csv.writer(tampon, delimiter=";")
    for code_vif, poids_kg, _ in lignes:
        ecrivain.writerow([
            CAGETTES_EXPORT_SOCIETE,
            CAGETTES_EXPORT_ETAB,
            date_reception,
            code_vif,
            CAGETTES_EXPORT_LIEU,
            CAGETTES_EXPORT_DEPOT,
            CAGETTES_EXPORT_ARTICLE_GARDEE,
            round(poids_kg),
            CAGETTES_EXPORT_UNITE,
            "", "", "", "",
            CAGETTES_EXPORT_ORIGINE,
        ])

    for nom in noms_inclus:
        _mettre_en_attente_confirmation_vif(annee, "poids_exporte", nom, current_user.email)

    write_log(
        f"📤 Export CSV poids magasins gardée {annee} ({len(lignes)} réception(s), "
        f"{len(noms_inclus)} association(s) en attente de confirmation d'import VIF, "
        f"{len(paires_deja_exportees)} paire(s) magasin/association déjà exclue(s), "
        f"{len(magasins_en_attente)} en attente de saisie) par {current_user.email}"
    )
    # Ce téléchargement ne recharge pas la page : le message n'apparaîtra
    # qu'au prochain chargement (ex. retour sur Suivi envoi), mais c'est la
    # seule confirmation visible qu'on puisse donner ici (demande
    # utilisateur 2026-11-26 : aucun retour visible après génération).
    flash(
        f"📤 Export poids par magasin généré : {len(noms_inclus)} association(s) en attente de "
        "confirmation d'import VIF (bouton « Confirmer l'import VIF OK » sur cette page).",
        "info",
    )

    return Response(
        tampon.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=saisie_poids_magasins_gardee_{annee}.csv"},
    )


def _donnees_quantites_produits(annee):
    """Données de la page Quantités par produit, factorisées pour être
    partagées entre l'affichage (quantites_produits) et l'export Excel
    serveur (quantites_produits_export_excel) — ce dernier a besoin des
    mêmes lignes avec les mêmes totaux, générés côté Python plutôt que
    dans le JS de la page (openpyxl, contrairement à SheetJS en version
    gratuite, sait figer des volets dans le fichier généré)."""
    produits = _lire_produits_modele()
    if not produits:
        return None

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if df_mag.empty:
        return None
    associations, _ = _construire_associations(df_mag, df_groupes)
    noms_associations = sorted({a["nom"] for a in associations}, key=str.lower)
    index_code_vif = _table_code_vif_associations()
    code_vif_associations = {}
    for nom in noms_associations:
        code_vif_asso, _, _ = _code_vif_association(nom, index_code_vif)
        code_vif_associations[nom] = code_vif_asso or ""
    totaux_n1 = _totaux_gardee_annee_precedente(annee)
    totaux_associations_n1 = {
        nom: _total_gardee_annee_precedente_association(totaux_n1, nom, code_vif_associations.get(nom))
        for nom in noms_associations
    }

    with get_db_connection() as conn:
        saisies = {
            (r["association"], r["code_produit"]): r["poids_kg"]
            for r in conn.execute(
                "SELECT association, code_produit, poids_kg FROM collecte_quantites_produits WHERE annee = ?", (annee,)
            ).fetchall()
        }
        # Valorisation en mercuriale — même référentiel de prix (par Code VIF)
        # que la pesée palox (collecte_palox_mercuriale), les codes produit du
        # modèle association étant les mêmes codes article VIF.
        _ensure_tables_palox(conn)
        prix_mercuriale = {
            r["code_vif"]: r["prix_kg"] for r in conn.execute(
                "SELECT code_vif, prix_kg FROM collecte_palox_mercuriale WHERE annee = ?", (annee,)
            ).fetchall()
        }

    total_transfere = sum(v or 0 for v in saisies.values())
    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        total_stock_gardee = conn.execute(
            "SELECT COALESCE(SUM(poids_kg), 0) FROM collecte_poids_magasins_gardee WHERE annee = ?", (annee,)
        ).fetchone()[0]

    lignes = []
    for produit in produits:
        poids_kg = prix_mercuriale.get(produit["code"])
        ligne = {
            "code_produit": produit["code"],
            "libelle": produit["libelle"],
            "prix_kg": poids_kg,
        }
        total_poids = 0.0
        for nom in noms_associations:
            v = saisies.get((nom, produit["code"]))
            ligne[nom] = v
            total_poids += v or 0
        ligne["total_poids"] = total_poids
        ligne["valeur"] = (total_poids * poids_kg) if poids_kg is not None else None
        lignes.append(ligne)

    return {
        "lignes": lignes,
        "noms_associations": noms_associations,
        "code_vif_associations": code_vif_associations,
        "totaux_associations_n1": totaux_associations_n1,
        "prix_mercuriale": prix_mercuriale,
        "total_transfere": total_transfere,
        "total_stock_gardee": total_stock_gardee,
    }


@collecte_bp.route("/collecte/<int:annee>/quantites-produits")
@login_required
@require_access("collecte", "lecture")
def quantites_produits(annee):
    donnees = _donnees_quantites_produits(annee)
    if donnees is None:
        if not _lire_produits_modele():
            flash("⛔ Liste des produits indisponible — modèle association introuvable", "danger")
        else:
            flash(f"⛔ Liste des magasins gardés {annee} indisponible — importez d'abord le fichier magasins", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    return render_template(
        "collecte/quantites_produits.html",
        annee=annee,
        lignes=donnees["lignes"],
        associations=donnees["noms_associations"],
        code_vif_associations=donnees["code_vif_associations"],
        totaux_associations_n1=donnees["totaux_associations_n1"],
        annee_precedente=annee - 1,
        mercuriale_importee=bool(donnees["prix_mercuriale"]),
        total_transfere=donnees["total_transfere"],
        total_stock_gardee=donnees["total_stock_gardee"],
    )


@collecte_bp.route("/collecte/<int:annee>/quantites-produits/export-excel")
@login_required
@require_access("collecte", "lecture")
def quantites_produits_export_excel(annee):
    """Export Excel de la page Quantités par produit — généré côté serveur
    (openpyxl) plutôt que dans le navigateur (SheetJS gratuit ne sait pas
    écrire de volets figés) : ligne « Total » et ligne « Total
    {annee-1} » conservées dans le fichier (contrairement à l'affichage
    où elles sont des repères visuels), première ligne (en-têtes) figée."""
    donnees = _donnees_quantites_produits(annee)
    if donnees is None:
        flash("⛔ Export impossible — données indisponibles", "danger")
        return redirect(url_for("collecte.quantites_produits", annee=annee))

    lignes = donnees["lignes"]
    noms_associations = donnees["noms_associations"]
    code_vif_associations = donnees["code_vif_associations"]
    totaux_associations_n1 = donnees["totaux_associations_n1"]

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = f"Quantités {annee}"[:31]

    entetes_associations = [
        nom + (f" ({code_vif_associations[nom]})" if code_vif_associations.get(nom) else "")
        for nom in noms_associations
    ]
    ws.append(["Code", "Produit"] + entetes_associations + ["Total poids (kg)", "Valeur (€)"])

    gras = Font(bold=True)
    fond_gris = PatternFill("solid", fgColor="F1F3F5")

    ligne_total = ["", "Total"]
    for nom in noms_associations:
        ligne_total.append(round(sum((l.get(nom) or 0) for l in lignes), 2))
    ligne_total.append(round(sum((l.get("total_poids") or 0) for l in lignes), 2))
    ligne_total.append(round(sum((l.get("valeur") or 0) for l in lignes), 2))
    ws.append(ligne_total)
    for cell in ws[ws.max_row]:
        cell.font = gras

    ligne_n1 = ["", f"Total {annee - 1}"]
    for nom in noms_associations:
        ligne_n1.append(round(totaux_associations_n1.get(nom) or 0))
    ligne_n1.append(round(sum((totaux_associations_n1.get(nom) or 0) for nom in noms_associations), 2))
    ligne_n1.append(None)
    ws.append(ligne_n1)
    for cell in ws[ws.max_row]:
        cell.font = gras
        cell.fill = fond_gris

    for ligne in lignes:
        row = [ligne["code_produit"], ligne["libelle"]]
        for nom in noms_associations:
            row.append(ligne.get(nom))
        row.append(round(ligne["total_poids"], 2))
        row.append(round(ligne["valeur"], 2) if ligne["valeur"] is not None else None)
        ws.append(row)

    ws.freeze_panes = "A2"

    tampon = io.BytesIO()
    wb.save(tampon)
    tampon.seek(0)
    nom_fichier = f"quantites_produits_{annee}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return send_file(
        tampon, as_attachment=True, download_name=nom_fichier,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _date_vif_lundi_jeudi():
    """Date des documents VIF de la collecte gardée : lundi au jeudi
    uniquement ; du vendredi au dimanche, le jeudi précédent."""
    d = datetime.now()
    if d.weekday() > 3:
        d -= timedelta(days=d.weekday() - 3)
    return d


def _lignes_transferts_vif(annee):
    """Pour chaque article de la page Quantités par produit, total à
    transférer dans VIF (sortie du stock « collecte gardée » 5010010,
    entrée sur l'article) — associations déjà cochées « Transferts
    exportés » exclues (déjà transférées lors d'un précédent export, cf.
    _noms_associations_gardee_avec_champ), ET
    associations dont la saisie n'est pas complète exclues aussi (mêmes
    magasins ET produits que le BL VIF, cf. _noms_associations_completes),
    ET associations pas encore cochées « Poids exporté » exclues aussi —
    les 3 CSV (poids par magasin / transferts / BL) doivent être générés
    dans cet ordre, une association ne passe à cet export que si l'export
    poids par magasin a déjà été fait pour elle (demande utilisateur
    2026-11-23) — pour ne porter que sur les associations qui ont vraiment
    fini de répondre, et pouvoir relancer cet export plusieurs fois sans
    redoubler un transfert déjà fait. « Poids exporté » et « Transferts
    exportés » s'entendent ici au sens confirmé import VIF (cf.
    confirmer_vif_gardee) — une association dont le CSV transferts est
    généré mais pas encore confirmé reste exclue d'une régénération (cf.
    _noms_associations_gardee_en_attente_confirmation) sans pour autant
    compter comme éligible pour le BL. Partagé par l'affichage écran et
    l'export CSV TRANCOG."""
    noms_traites = (
        _noms_associations_gardee_avec_champ(annee, "transferts_exportes")
        | _noms_associations_gardee_en_attente_confirmation(annee, "transferts_exportes")
    )
    noms_completes = _noms_associations_completes(annee)
    noms_poids_exporte = _noms_associations_gardee_avec_champ(annee, "poids_exporte")
    noms_eligibles = noms_completes & noms_poids_exporte
    with get_db_connection() as conn:
        placeholders_exclus = ",".join("?" * len(noms_traites))
        exclusion_sql = f"AND q.association NOT IN ({placeholders_exclus})" if noms_traites else ""
        placeholders_completes = ",".join("?" * len(noms_eligibles))
        return [dict(r) for r in conn.execute(f"""
            SELECT q.code_produit AS article, a.libelle, SUM(q.poids_kg) AS total_kg
            FROM collecte_quantites_produits q
            LEFT JOIN articles a ON a.article = q.code_produit
            WHERE q.annee = ? AND q.poids_kg > 0 {exclusion_sql}
            AND q.association IN ({placeholders_completes})
            GROUP BY q.code_produit HAVING SUM(q.poids_kg) > 0
            ORDER BY q.code_produit
        """, (annee, *noms_traites, *noms_eligibles)).fetchall()]


@collecte_bp.route("/collecte/<int:annee>/quantites-produits/transferts")
@login_required
@require_access("collecte", "lecture")
def quantites_produits_transferts(annee):
    """Liste des mouvements de transfert à saisir à la main dans VIF —
    voir _lignes_transferts_vif."""
    lignes = _lignes_transferts_vif(annee)
    return render_template(
        "collecte/quantites_produits_transferts.html",
        annee=annee, lignes=lignes,
        total=sum(l["total_kg"] for l in lignes),
        article_source=CAGETTES_EXPORT_ARTICLE_GARDEE,
        depot_source=CAGETTES_EXPORT_DEPOT,
        depot_dest="03",
        date_mvt=_date_vif_lundi_jeudi(),
    )


def _ligne_trancog_vif(article, quantite_kg, date_mvt, article_sortie, nature_mouvement="TRANCOG", libelle=""):
    """Une ligne (48 colonnes, `;` séparateur) du format TRANCOG/TRANSCOL
    VIF (transfert manuel), reproduisant exactement ce que donnerait le
    modèle /srv/ba38/uploads/matrice TRANCOG(1).xlsx rempli à la main
    puis enregistré en CSV en retirant ses 3 premières lignes (en-têtes
    + instructions) --- seuls l'article, la quantité, la date et
    l'article sortie (5010010 collecte gardée ou 5010000 collecte
    directe, cf. appelants) varient, tout le reste est constant dans le
    modèle. Nature de mouvement TRANCOG par défaut (collecte gardée,
    comme le modèle), TRANSCOL pour la pesée palox (collecte directe,
    demande utilisateur 2026-11-18). Dépôt entrée = 03 (demande
    utilisateur 2026-11-17, pas la valeur 02 du modèle --- déjà la
    valeur affichée sur ces pages avant ce changement) ; émplacement
    entrée = B011 (demande utilisateur 2026-11-17). Lot entrée vide pour
    TRANSCOL (collecte directe, comme le modèle réel) ; pour TRANCOG
    (collecte gardée), lot entrée = « COLGA » + code produit, ex.
    COLGA0110041, vide si article « non loti » (demande utilisateur
    2026-11-24) --- même lot que celui posé par generer_bl_collecte_gardee
    (ba38_vif.generation), pour que le BL VIF consomme le même lot que
    celui créé par ce transfert."""
    qte = str(int(round(quantite_kg)))
    non_loti = "non loti" in (libelle or "").lower()
    lot_entree = f"COLGA{article}" if nature_mouvement == "TRANCOG" and not non_loti else ""
    champs = [
        "01", "38", "1OD", "0", "0", nature_mouvement,
        date_mvt.strftime("%d/%m/%Y"), "120000", "transfert collecte", "",
        "AUC", "", "",
        article, lot_entree, "03", "B011", qte, "Kg",
    ] + [""] * 4 + [
        article_sortie, "", CAGETTES_EXPORT_DEPOT, "", qte, "Kg",
    ] + [""] * 19
    return ";".join(champs)


@collecte_bp.route("/collecte/<int:annee>/quantites-produits/transferts/export-trancog")
@login_required
@require_access("collecte", "lecture")
def quantites_produits_transferts_export_trancog(annee):
    """Export CSV prêt à importer dans VIF (format TRANCOG, cf.
    _ligne_trancog_vif) --- équivalent du modèle Excel rempli à la main
    puis réenregistré en CSV en retirant les 3 premières lignes, mais
    généré directement depuis les quantités saisies, pour les seules
    associations à la saisie complète (mêmes magasins ET produits que le
    BL VIF). Associations ayant contribué à cet export mises en attente
    de confirmation d'import VIF (cf. confirmer_vif_gardee) — la case
    « Transferts exportés » ne se coche qu'une fois cette confirmation
    faite, pour ne jamais débloquer le BL sur un CSV généré mais jamais
    réellement importé dans VIF (incident réel 2026-11-24, stocks VIF
    négatifs après un essai non importé)."""
    lignes = _lignes_transferts_vif(annee)
    if not lignes:
        flash(f"⛔ Aucune quantité à transférer pour {annee}.", "warning")
        return redirect(url_for("collecte.quantites_produits_transferts", annee=annee))

    date_mvt = _date_vif_lundi_jeudi()
    contenu = "\r\n".join(
        _ligne_trancog_vif(l["article"], l["total_kg"], date_mvt, CAGETTES_EXPORT_ARTICLE_GARDEE, libelle=l.get("libelle"))
        for l in lignes
    ) + "\r\n"

    noms_exclus = (
        _noms_associations_gardee_avec_champ(annee, "transferts_exportes")
        | _noms_associations_gardee_en_attente_confirmation(annee, "transferts_exportes")
    )
    noms_completes = _noms_associations_completes(annee)
    noms_poids_exporte = _noms_associations_gardee_avec_champ(annee, "poids_exporte")
    with get_db_connection() as conn:
        noms_inclus = {
            r["association"] for r in conn.execute(
                "SELECT DISTINCT association FROM collecte_quantites_produits "
                "WHERE annee = ? AND poids_kg > 0", (annee,)
            ).fetchall()
            if r["association"] not in noms_exclus
            and r["association"] in noms_completes
            and r["association"] in noms_poids_exporte
        }
    for nom in noms_inclus:
        _mettre_en_attente_confirmation_vif(annee, "transferts_exportes", nom, current_user.email)

    write_log(
        f"📤 Export CSV TRANCOG VIF {annee} ({len(lignes)} article(s), "
        f"{len(noms_inclus)} association(s) en attente de confirmation d'import VIF) par {current_user.email}"
    )
    flash(
        f"📤 Export transferts VIF généré : {len(noms_inclus)} association(s) en attente de "
        "confirmation d'import VIF (bouton « Confirmer l'import VIF OK » sur la page Suivi envoi).",
        "info",
    )
    return Response(
        contenu.encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trancog_vif_{annee}.csv"},
    )


@collecte_bp.route("/collecte/<int:annee>/quantites-produits/bl-vif")
@login_required
@require_access("collecte", "lecture")
def quantites_produits_bl_vif(annee):
    """BL VIF de la collecte gardée : une ligne par association × article
    dont la quantité saisie est > 0, même format que le BL des ramasses
    partenaires (ba38_vif.generation). Colonne Gratuit d'après le statut
    de l'association (Associations gardant) ; si non gratuit, prix = mercuriale.
    Associations déjà cochées « BL généré » exclues (déjà dans un
    précédent BL) ; celles incluses dans CE BL mises en attente de
    confirmation d'import VIF (cf. confirmer_vif_gardee) — la case
    « BL généré » ne se coche qu'une fois cette confirmation faite, pour
    ne pas les réinclure si le BL est relancé plus tard pour les
    associations qui répondent en retard (demande utilisateur 2026-11-16).
    Également exclues : les associations dont la saisie n'est pas complète
    (pas le badge vert « X/X complet », cf. _noms_associations_completes)
    — jamais de BL sur une saisie partielle (demande utilisateur
    2026-11-17) — ET les associations pas encore cochées « Transferts
    exportés » (au sens confirmé import VIF) : les 3 CSV (poids par
    magasin / transferts / BL) doivent être générés et CONFIRMÉS importés
    dans VIF dans cet ordre, le BL ne se génère pour une association que
    si son transfert VIF a déjà été réellement confirmé importé (demande
    utilisateur 2026-11-23, renforcée le 2026-11-24 après un BL généré
    pour des associations dont le transfert n'avait en réalité jamais été
    importé dans VIF, causant des stocks négatifs)."""
    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if df_mag.empty:
        flash(f"⛔ Liste des magasins gardés {annee} indisponible", "danger")
        return redirect(url_for("collecte.quantites_produits", annee=annee))
    associations, _ = _construire_associations(df_mag, df_groupes)
    noms_traites_avant = (
        _noms_associations_gardee_avec_champ(annee, "bl_genere")
        | _noms_associations_gardee_en_attente_confirmation(annee, "bl_genere")
    )
    noms_completes = _noms_associations_completes(annee)
    noms_transferts_exportes = _noms_associations_gardee_avec_champ(annee, "transferts_exportes")
    noms_eligibles = noms_completes & noms_transferts_exportes
    noms_pas_encore_eligibles = {
        a["nom"] for a in associations
        if a["nom"] not in noms_traites_avant and a["nom"] not in noms_eligibles
    }
    noms = sorted(
        {a["nom"] for a in associations if a["nom"] not in noms_traites_avant and a["nom"] in noms_eligibles},
        key=str.lower,
    )
    index_code_vif = _table_code_vif_associations()
    gratuit_assos = _gratuit_associations()

    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        saisies = conn.execute(
            "SELECT association, code_produit, poids_kg FROM collecte_quantites_produits "
            "WHERE annee = ? AND poids_kg > 0", (annee,)
        ).fetchall()
        prix = {r["code_vif"]: r["prix_kg"] for r in conn.execute(
            "SELECT code_vif, prix_kg FROM collecte_palox_mercuriale WHERE annee = ?", (annee,)
        ).fetchall()}

        par_asso = {}
        for r in saisies:
            par_asso.setdefault(r["association"], []).append(r)

        lignes, erreurs, noms_inclus = [], [], []
        for nom in noms:
            if nom not in par_asso:
                continue
            code_asso, _, _ = _code_vif_association(nom, index_code_vif)
            if not code_asso:
                erreurs.append(f"Association sans code VIF : {nom}")
                continue
            gratuit = gratuit_assos.get(_cle_recherche_association(nom), True)
            for r in sorted(par_asso[nom], key=lambda x: x["code_produit"]):
                p = prix.get(r["code_produit"])
                if not gratuit and not p:
                    erreurs.append(f"Prix mercuriale manquant pour {nom} / article {r['code_produit']} (association non gratuite)")
                    continue
                lignes.append({
                    "code_vif": str(code_asso), "article": r["code_produit"],
                    "total_kg": r["poids_kg"], "gratuit": gratuit, "prix_kg": p or 0,
                })
            noms_inclus.append(nom)

        if erreurs:
            for e in erreurs:
                flash(f"❌ {e}", "danger")
            return redirect(url_for("collecte.quantites_produits", annee=annee))
        if not lignes:
            flash("⚠️ Aucune quantité saisie : rien à exporter", "warning")
            return redirect(url_for("collecte.quantites_produits", annee=annee))

        from ba38_vif.generation import generer_bl_collecte_gardee
        date_bl = _date_vif_lundi_jeudi()
        contenu = generer_bl_collecte_gardee(conn, lignes, date_bl)

    for nom in noms_inclus:
        _mettre_en_attente_confirmation_vif(annee, "bl_genere", nom, current_user.email)

    write_log(
        f"📤 BL VIF collecte gardée {annee} ({len(lignes)} ligne(s), {len(noms_inclus)} association(s) "
        f"en attente de confirmation d'import VIF, {len(noms_pas_encore_eligibles)} association(s) incomplète(s) "
        f"ou pas encore exportée(s) (transferts) exclue(s)) par {current_user.email}"
    )
    flash(
        f"📤 BL VIF généré : {len(noms_inclus)} association(s) en attente de confirmation d'import VIF "
        "(bouton « Confirmer l'import VIF OK » sur la page Suivi envoi).",
        "info",
    )
    return Response(
        contenu, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=BL_collecte_gardee_{annee}.CSV"},
    )


@collecte_bp.route("/collecte/quantites-produits/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_quantites_produits():
    donnees = request.get_json(silent=True) or {}
    annee = donnees.get("annee")
    association = str(donnees.get("association", "")).strip()
    code_produit = str(donnees.get("code_produit", "")).strip()
    poids_brut = donnees.get("poids_kg")

    if not annee or not association or not code_produit:
        return jsonify({"success": False, "erreur": "Paramètres manquants"}), 400

    try:
        poids_kg = round(float(poids_brut), 2) if poids_brut not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

    if poids_kg is not None and poids_kg < 0:
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

    verrou = _resultat_gardee_verrouille(annee)
    if verrou:
        return jsonify({
            "success": False,
            "erreur": f"🔒 Résultat collecte gardée {annee} verrouillé depuis le {verrou['le']} — déverrouillez-le d'abord.",
        }), 423

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO collecte_quantites_produits (annee, association, code_produit, poids_kg, saisi_le, saisi_par)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(annee, association, code_produit)
            DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
        """, (annee, association, code_produit, poids_kg, maintenant, current_user.email))
        conn.commit()

    return jsonify({"success": True})


def _controle_renforce_ok(annee):
    """Contrôle renforcé exigé pour une action sensible (remise à zéro,
    déverrouillage) : case à cocher ET année tapée exactement, vérifiés ici
    côté serveur — jamais seulement un confirm() JS, contournable par un
    POST direct (même principe que gardee_relancer, audit 2026-10-28 ;
    demande utilisateur 2026-11-10 : "sous contrôle renforcé")."""
    return (
        request.form.get("confirmation") == "oui"
        and request.form.get("mot_confirmation_saisi", "").strip() == str(annee)
    )


def _ensure_colonnes_verrouillage_resultat_bai(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "resultat_bai_verrouille_le" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN resultat_bai_verrouille_le TEXT")
    if "resultat_bai_verrouille_par" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN resultat_bai_verrouille_par TEXT")


def _resultat_bai_verrouille(annee):
    """None si la saisie Résultat collecte BAI (cagettes + palox) de
    l'année est déverrouillée, sinon {"le":..., "par":...} — vérifié par
    chaque route d'écriture de enregistrer_cagettes/palox_* (demande
    utilisateur 2026-11-10 : "pouvoir bloquer la saisie une fois que tout
    est fini afin d'éviter des erreurs")."""
    with get_db_connection() as conn:
        _ensure_colonnes_verrouillage_resultat_bai(conn)
        conn.commit()
        row = conn.execute(
            "SELECT resultat_bai_verrouille_le, resultat_bai_verrouille_par FROM collecte_campagnes WHERE annee = ?",
            (annee,),
        ).fetchone()
    if row and row["resultat_bai_verrouille_le"]:
        return {"le": row["resultat_bai_verrouille_le"], "par": row["resultat_bai_verrouille_par"]}
    return None


@collecte_bp.route("/collecte/<int:annee>/resultat-bai/verrouiller", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def resultat_bai_verrouiller(annee):
    """Verrouille la saisie Résultat collecte BAI (cagettes + palox) pour
    l'année — geste volontairement simple (pas de contrôle renforcé) :
    verrouiller ne risque jamais de perdre des données, contrairement au
    déverrouillage."""
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_colonnes_verrouillage_resultat_bai(conn)
        conn.execute(
            "UPDATE collecte_campagnes SET resultat_bai_verrouille_le = ?, resultat_bai_verrouille_par = ? WHERE annee = ?",
            (maintenant, current_user.email, annee),
        )
        conn.commit()
    write_log(f"🔒 Résultat collecte BAI {annee} verrouillé par {current_user.email}")
    flash(f"🔒 Saisie Résultat collecte BAI verrouillée pour {annee}.", "success")
    return redirect(url_for("collecte.collecte_main", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/resultat-bai/deverrouiller", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def resultat_bai_deverrouiller(annee):
    """Déverrouille — contrôle renforcé obligatoire (cf. _controle_renforce_ok)."""
    if not _controle_renforce_ok(annee):
        flash("❌ Confirmation incomplète — rien n'a été déverrouillé.", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    with get_db_connection() as conn:
        _ensure_colonnes_verrouillage_resultat_bai(conn)
        conn.execute(
            "UPDATE collecte_campagnes SET resultat_bai_verrouille_le = NULL, resultat_bai_verrouille_par = NULL WHERE annee = ?",
            (annee,),
        )
        conn.commit()
    write_log(f"🔓 Résultat collecte BAI {annee} déverrouillé par {current_user.email}")
    flash(f"🔓 Saisie Résultat collecte BAI déverrouillée pour {annee}.", "warning")
    return redirect(url_for("collecte.collecte_main", annee=annee))


def _ensure_colonnes_verrouillage_resultat_gardee(conn):
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(collecte_campagnes)").fetchall()}
    if "resultat_gardee_verrouille_le" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN resultat_gardee_verrouille_le TEXT")
    if "resultat_gardee_verrouille_par" not in colonnes:
        conn.execute("ALTER TABLE collecte_campagnes ADD COLUMN resultat_gardee_verrouille_par TEXT")


def _resultat_gardee_verrouille(annee):
    """None si la saisie Résultat collecte gardée (poids par magasin gardé +
    quantités par produit — pages internes ET lien public associations)
    de l'année est déverrouillée, sinon {"le":..., "par":...} — même
    principe que _resultat_bai_verrouille (demande utilisateur 2026-11-10 :
    "faire la même chose pour Résultat collecte gardée")."""
    with get_db_connection() as conn:
        _ensure_colonnes_verrouillage_resultat_gardee(conn)
        conn.commit()
        row = conn.execute(
            "SELECT resultat_gardee_verrouille_le, resultat_gardee_verrouille_par FROM collecte_campagnes WHERE annee = ?",
            (annee,),
        ).fetchone()
    if row and row["resultat_gardee_verrouille_le"]:
        return {"le": row["resultat_gardee_verrouille_le"], "par": row["resultat_gardee_verrouille_par"]}
    return None


@collecte_bp.route("/collecte/<int:annee>/resultat-gardee/verrouiller", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def resultat_gardee_verrouiller(annee):
    """Verrouille la saisie Résultat collecte gardée (poids par magasin
    gardé + quantités par produit, y compris via le lien public envoyé aux
    associations) pour l'année — geste volontairement simple, comme pour
    le verrouillage BAI."""
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_colonnes_verrouillage_resultat_gardee(conn)
        conn.execute(
            "UPDATE collecte_campagnes SET resultat_gardee_verrouille_le = ?, resultat_gardee_verrouille_par = ? WHERE annee = ?",
            (maintenant, current_user.email, annee),
        )
        conn.commit()
    write_log(f"🔒 Résultat collecte gardée {annee} verrouillé par {current_user.email}")
    flash(f"🔒 Saisie Résultat collecte gardée verrouillée pour {annee}.", "success")
    return redirect(url_for("collecte.collecte_main", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/resultat-gardee/deverrouiller", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def resultat_gardee_deverrouiller(annee):
    """Déverrouille — contrôle renforcé obligatoire (cf. _controle_renforce_ok)."""
    if not _controle_renforce_ok(annee):
        flash("❌ Confirmation incomplète — rien n'a été déverrouillé.", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    with get_db_connection() as conn:
        _ensure_colonnes_verrouillage_resultat_gardee(conn)
        conn.execute(
            "UPDATE collecte_campagnes SET resultat_gardee_verrouille_le = NULL, resultat_gardee_verrouille_par = NULL WHERE annee = ?",
            (annee,),
        )
        conn.commit()
    write_log(f"🔓 Résultat collecte gardée {annee} déverrouillé par {current_user.email}")
    flash(f"🔓 Saisie Résultat collecte gardée déverrouillée pour {annee}.", "warning")
    return redirect(url_for("collecte.collecte_main", annee=annee))


RESULTAT_COLLECTE_TABLES_RESET = {
    "cagettes": ("collecte_cagettes", "Saisie des cagettes"),
    "palox": ("collecte_palox_pesees", "Pesée palox"),
    "poids-gardee": ("collecte_poids_magasins_gardee", "Poids par magasin gardé"),
    "quantites": ("collecte_quantites_produits", "Quantités par produit"),
}


@collecte_bp.route("/collecte/<int:annee>/resultat-collecte/remise-a-zero/<cle>", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def resultat_collecte_remise_a_zero(annee, cle):
    """Efface, UNE saisie à la fois (pas les 4 d'un coup), une table de
    saisie de l'année choisie — utilisée à la fois depuis le bandeau
    dépliable « Remise à zéro des saisies » de la page Résultat collecte
    ET (cle="quantites") depuis le bouton « Repartir à zéro » de la page
    Quantités par produit, qui avait jusqu'ici son propre bouton+confirm()
    JS sans contrôle renforcé (demande utilisateur 2026-11-16). Volontairement
    utilisable en prod comme en dev (même principe que
    poids_magasins_gardee_reinitialiser), pour repartir à zéro après des
    essais réels, pas réservé aux tests dev. Contrôle renforcé obligatoire
    (cf. _controle_renforce_ok) — demande utilisateur 2026-11-10."""
    info = RESULTAT_COLLECTE_TABLES_RESET.get(cle)
    if not info:
        abort(404)
    table, libelle = info
    # Retour sur la page d'où l'action a été lancée plutôt que toujours la
    # page Résultat collecte — "quantites" est aussi déclenchable depuis
    # Quantités par produit, une page à part.
    retour_endpoint = "collecte.quantites_produits" if cle == "quantites" else "collecte.collecte_main"

    if cle in ("cagettes", "palox") and _resultat_bai_verrouille(annee):
        flash(f"🔒 Résultat collecte BAI {annee} verrouillé — déverrouillez-le d'abord.", "danger")
        return redirect(url_for(retour_endpoint, annee=annee))
    if cle in ("poids-gardee", "quantites") and _resultat_gardee_verrouille(annee):
        flash(f"🔒 Résultat collecte gardée {annee} verrouillé — déverrouillez-le d'abord.", "danger")
        return redirect(url_for(retour_endpoint, annee=annee))

    if not _controle_renforce_ok(annee):
        flash("❌ Confirmation incomplète — rien n'a été effacé.", "danger")
        return redirect(url_for(retour_endpoint, annee=annee))

    with get_db_connection() as conn:
        cur = conn.execute(f"DELETE FROM {table} WHERE annee = ?", (annee,))
        conn.commit()

    write_log(f"🗑️ Remise à zéro {libelle} {annee} : {cur.rowcount} ligne(s) par {current_user.email}")
    flash(f"🗑️ Remise à zéro « {libelle} » : {cur.rowcount} saisie(s) effacée(s) pour {annee}.", "success")
    return redirect(url_for(retour_endpoint, annee=annee))


# ============================================================================
# 📈 Évolution magasins (suivi pluriannuel, hors campagne annuelle)
# ============================================================================
FICHIER_EVOLUTION_MAGASINS = "/srv/ba38/uploads/collectes magasins evolution.xlsx"
FICHIER_REFERENTIEL_MAGASINS = "/srv/ba38/uploads/liste-magasins-réferentiel.xlsx"
# Les résultats jusqu'en 2025 inclus proviennent tels quels du fichier
# historique (repris une fois par le script d'import) — le premier extrait
# VIF importable dans l'application concerne la collecte 2026.
PREMIERE_ANNEE_IMPORT_VIF = 2026


def _ensure_tables_evolution_magasins(conn):
    """Tables du suivi pluriannuel des magasins, reprises une fois depuis les
    deux fichiers Excel maintenus à la main jusqu'ici (cf.
    scripts/importer_historique_magasins_evolution.py) — indépendantes de
    l'année de campagne, contrairement au reste du module."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_magasins_referentiel (
            code_vif TEXT PRIMARY KEY,
            nom TEXT,
            etat TEXT,
            adresse TEXT,
            ville TEXT,
            code_postal TEXT,
            telephone TEXT,
            email TEXT,
            stockage TEXT,
            gardee_par TEXT,
            ajoute_le TEXT,
            ajoute_par TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_magasins_resultats (
            code_vif TEXT NOT NULL,
            annee INTEGER NOT NULL,
            resultat_kg REAL,
            importe_le TEXT,
            importe_par TEXT,
            PRIMARY KEY (code_vif, annee)
        )
    """)


def _lire_referentiel_evolution():
    with get_db_connection() as conn:
        _ensure_tables_evolution_magasins(conn)
        magasins = [dict(r) for r in conn.execute(
            "SELECT * FROM collecte_magasins_referentiel ORDER BY nom COLLATE NOCASE"
        ).fetchall()]
        resultats = conn.execute("SELECT code_vif, annee, resultat_kg FROM collecte_magasins_resultats").fetchall()
    par_magasin = {}
    annees = set()
    for r in resultats:
        par_magasin.setdefault(r["code_vif"], {})[r["annee"]] = r["resultat_kg"]
        annees.add(r["annee"])
    for m in magasins:
        m["resultats"] = par_magasin.get(m["code_vif"], {})
    return magasins, sorted(annees, reverse=True)


def _lire_fichier_magasins_brut(annee):
    """Relit le fichier magasins de la campagne (Drive ou upload), sans
    filtrage par état — contrairement à _lire_referentiel_magasins_bai,
    utilisé ici pour détecter TOUS les magasins (y compris non collectés ou
    gardés) absents du référentiel pluriannuel."""
    with get_db_connection() as conn:
        campagne = conn.execute("SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)).fetchone()
    if not campagne:
        return []
    chemin = _fichier_drive(annee, "magasins")
    if not chemin:
        return []

    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]
    for col in ["Code VIF", "Nom", "État", "Adresse", "Ville", "C.P.", "Téléphone", "Email", "Stockage", "Gardée par"]:
        if col not in df.columns:
            df[col] = ""

    magasins = []
    for _, row in df.iterrows():
        code_vif = _vif_fmt(row["Code VIF"])
        if not code_vif or code_vif.lower() == "nan":
            continue
        magasins.append({
            "code_vif": code_vif,
            "nom": str(_valeur_propre(row["Nom"])).strip(),
            "etat": str(_valeur_propre(row["État"])).strip(),
            "adresse": str(_valeur_propre(row["Adresse"])).strip(),
            "ville": str(_valeur_propre(row["Ville"])).strip(),
            "code_postal": str(_valeur_propre(row["C.P."])).strip(),
            "telephone": str(_valeur_propre(row["Téléphone"])).strip(),
            "email": str(_valeur_propre(row["Email"])).strip(),
            "stockage": str(_valeur_propre(row["Stockage"])).strip(),
            "gardee_par": str(_valeur_propre(row["Gardée par"])).strip(),
        })
    return magasins


def _parser_extrait_vif_collecte(contenu):
    """Parse un extrait VIF (rapport texte tabulé, encodage cp1252) listant
    les quantités par magasin (colonne « Fournisseur » = Code VIF) — somme
    toutes les lignes trouvées pour un même Code VIF, quel que soit
    l'article (5010000 collecte directe / 5010010 collecte gardée) ou
    l'enseigne, pour couvrir le cas où un magasin apparaît sur plusieurs
    lignes. Les lignes de sous-total (« TOTAL Enseigne »/« TOTAL Article »,
    Fournisseur vide) sont naturellement ignorées, leur premier champ ne
    correspondant jamais à un Code VIF."""
    resultats = {}
    for ligne in contenu.splitlines():
        champs = ligne.split("\t")
        if len(champs) < 5:
            continue
        code_vif_brut = champs[0].strip()
        if not re.match(r"^\d{6,10}$", code_vif_brut):
            continue
        code_vif = _vif_fmt(code_vif_brut)
        brut = champs[2].strip()
        if not brut:
            continue
        try:
            valeur = float(brut.replace(".", "").replace(",", "."))
        except ValueError:
            continue
        resultats[code_vif] = resultats.get(code_vif, 0.0) + valeur
    return resultats


@collecte_bp.route("/collecte/evolution-magasins")
@login_required
@require_access("collecte", "lecture")
def evolution_magasins():
    annee = request.args.get("annee", type=int) or datetime.now().year
    magasins, annees = _lire_referentiel_evolution()
    return render_template(
        "collecte/evolution_magasins.html",
        annee=annee,
        magasins=magasins,
        annees=annees,
    )


@collecte_bp.route("/collecte/evolution-magasins/export")
@login_required
@require_access("collecte", "lecture")
def evolution_magasins_export():
    """Export Excel du référentiel pluriannuel — une ligne par magasin, une
    colonne par année, avec l'évolution et le % d'évolution calculés entre
    les deux années les plus récentes (mêmes règles que le tableau à
    l'écran)."""
    magasins, annees = _lire_referentiel_evolution()

    wb_export = Workbook()
    ws = wb_export.active
    ws.title = "magasins"
    entetes = ["Code VIF", "Nom", "État", "Ville", "Stockage", "Évolution /an-1 (kg)", "% évolution"] + [str(a) for a in annees]
    ws.append(entetes)

    lignes = []
    somme_evolution = 0.0
    somme_annee_prec = 0.0
    sommes_annees = {a: 0.0 for a in annees}
    for m in magasins:
        resultats = m["resultats"]
        evolution = pct = None
        if len(annees) >= 2:
            cette_annee = resultats.get(annees[0])
            annee_prec = resultats.get(annees[1])
            if cette_annee is not None or annee_prec is not None:
                c = cette_annee or 0
                p = annee_prec or 0
                evolution = c - p
                pct = (evolution / p) if p else None
                somme_evolution += evolution
                somme_annee_prec += p
        for a in annees:
            valeur = resultats.get(a)
            if valeur is not None:
                sommes_annees[a] += valeur
        lignes.append([
            m["code_vif"], m["nom"], m["etat"], m["ville"], m["stockage"],
            round(evolution, 1) if evolution is not None else None,
            round(pct * 100, 1) if pct is not None else None,
        ] + [resultats.get(a) for a in annees])

    pct_global = (somme_evolution / somme_annee_prec * 100) if somme_annee_prec else None
    ligne_total = ["", "Total", "", "", "",
                   round(somme_evolution, 1),
                   round(pct_global, 1) if pct_global is not None else None,
                   ] + [round(sommes_annees[a], 1) for a in annees]
    ws.append(ligne_total)
    for ligne in lignes:
        ws.append(ligne)

    for colonne in ws.columns:
        lettre = colonne[0].column_letter
        ws.column_dimensions[lettre].width = 14 if lettre != "B" else 32

    ws.freeze_panes = "A3"
    ws.auto_filter.ref = ws.dimensions

    tampon = io.BytesIO()
    wb_export.save(tampon)
    tampon.seek(0)
    nom_fichier = f"evolution_magasins_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(
        tampon,
        as_attachment=True,
        download_name=nom_fichier,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/evolution-magasins/export-gow")
@login_required
@require_access("collecte", "lecture")
def evolution_magasins_export_gow():
    """Export du tonnage par magasin au format attendu par l'import
    « tonnage collecté année -1 » de Go on the Web --- mêmes colonnes et
    même ordre que l'ancien fichier maintenu à la main
    « tonnage-magasins.xlsx » (État, Code VIF, Nom, C.P., Ville, puis une
    colonne par année, valeurs arrondies au kg) : remplace le report manuel
    Drive + RECHERCHEV par un export direct depuis les résultats déjà
    importés dans l'application (collecte_magasins_resultats). Les codes
    camion fictifs (« 999... », staffing entrepôt) sont exclus, ce ne sont
    pas des magasins."""
    magasins, annees = _lire_referentiel_evolution()
    annees_asc = sorted(annees)

    wb_export = Workbook()
    ws = wb_export.active
    ws.title = "Sheet1"
    ws.append(["État", "Code VIF", "Nom", "C.P.", "Ville"] + annees_asc)

    for m in magasins:
        if m["code_vif"].startswith("999"):
            continue
        resultats = m["resultats"]
        code_postal = m["code_postal"]
        if code_postal and code_postal.endswith(".0"):
            code_postal = code_postal[:-2]  # nettoyage cosmétique : certains codes postaux ont été stockés en flottant ("38000.0") lors de la reprise historique
        ligne = [m["etat"], m["code_vif"], m["nom"], code_postal, m["ville"]]
        for a in annees_asc:
            valeur = resultats.get(a)
            ligne.append(round(valeur) if valeur is not None else None)
        ws.append(ligne)

    for colonne in ws.columns:
        lettre = colonne[0].column_letter
        ws.column_dimensions[lettre].width = 14 if lettre != "C" else 32

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    tampon = io.BytesIO()
    wb_export.save(tampon)
    tampon.seek(0)
    return send_file(
        tampon,
        as_attachment=True,
        download_name="tonnage-magasins.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/<int:annee>/evolution-magasins/detecter-nouveaux")
@login_required
@require_access("collecte", "lecture")
def evolution_magasins_detecter(annee):
    magasins_annee = _lire_fichier_magasins_brut(annee)
    if not magasins_annee:
        return jsonify({"success": False, "erreur": f"Fichier magasins {annee} indisponible"}), 400
    with get_db_connection() as conn:
        _ensure_tables_evolution_magasins(conn)
        codes_connus = {r[0] for r in conn.execute("SELECT code_vif FROM collecte_magasins_referentiel").fetchall()}
    nouveaux = [m for m in magasins_annee if m["code_vif"] not in codes_connus]
    return jsonify({"success": True, "nouveaux": nouveaux})


@collecte_bp.route("/collecte/evolution-magasins/ajouter", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def evolution_magasins_ajouter():
    donnees = request.get_json(silent=True) or {}
    magasins = donnees.get("magasins") or []
    if not magasins:
        return jsonify({"success": False, "erreur": "Aucun magasin sélectionné"}), 400

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_tables_evolution_magasins(conn)
        for m in magasins:
            code_vif = str(m.get("code_vif", "")).strip()
            if not code_vif:
                continue
            conn.execute("""
                INSERT INTO collecte_magasins_referentiel
                    (code_vif, nom, etat, adresse, ville, code_postal, telephone, email, stockage, gardee_par, ajoute_le, ajoute_par)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code_vif) DO NOTHING
            """, (
                code_vif, m.get("nom"), m.get("etat"), m.get("adresse"), m.get("ville"),
                m.get("code_postal"), m.get("telephone"), m.get("email"), m.get("stockage"), m.get("gardee_par"),
                maintenant, current_user.email,
            ))
        conn.commit()
    write_log(f"📈 Évolution magasins : {len(magasins)} magasin(s) ajouté(s) au référentiel par {current_user.email}")
    return jsonify({"success": True, "nb": len(magasins)})


@collecte_bp.route("/collecte/<int:annee>/evolution-magasins/importer-vif", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def evolution_magasins_importer_vif(annee):
    if annee < PREMIERE_ANNEE_IMPORT_VIF:
        flash(
            f"⛔ Les résultats {annee} proviennent du fichier historique (repris tels quels) — "
            f"l'import d'extrait VIF n'est disponible qu'à partir de {PREMIERE_ANNEE_IMPORT_VIF}.",
            "danger",
        )
        return redirect(url_for("collecte.evolution_magasins", annee=annee))

    fichier = request.files.get("extrait_vif")
    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier sélectionné.", "warning")
        return redirect(url_for("collecte.evolution_magasins", annee=annee))

    contenu_brut = fichier.read()
    try:
        contenu = contenu_brut.decode("cp1252")
    except UnicodeDecodeError:
        contenu = contenu_brut.decode("utf-8", errors="replace")

    resultats = _parser_extrait_vif_collecte(contenu)
    if not resultats:
        flash("⛔ Aucune ligne de résultat reconnue dans ce fichier.", "danger")
        return redirect(url_for("collecte.evolution_magasins", annee=annee))

    with get_db_connection() as conn:
        _ensure_tables_evolution_magasins(conn)
        codes_connus = {r[0] for r in conn.execute("SELECT code_vif FROM collecte_magasins_referentiel").fetchall()}
        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nb_maj, inconnus = 0, []
        for code_vif, kg in resultats.items():
            if code_vif not in codes_connus:
                inconnus.append(code_vif)
                continue
            conn.execute("""
                INSERT INTO collecte_magasins_resultats (code_vif, annee, resultat_kg, importe_le, importe_par)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code_vif, annee) DO UPDATE SET
                    resultat_kg = excluded.resultat_kg, importe_le = excluded.importe_le, importe_par = excluded.importe_par
            """, (code_vif, annee, round(kg, 2), maintenant, current_user.email))
            nb_maj += 1
        conn.commit()

    message = f"✅ Résultats {annee} importés pour {nb_maj} magasin(s)."
    if inconnus:
        message += f" ⚠️ {len(inconnus)} Code VIF absent(s) du référentiel, ignorés : {', '.join(inconnus[:10])}" + (
            "…" if len(inconnus) > 10 else ""
        )
    flash(message, "success" if not inconnus else "warning")
    write_log(f"📈 Évolution magasins : import extrait VIF {annee}, {nb_maj} magasin(s) par {current_user.email}")
    return redirect(url_for("collecte.evolution_magasins", annee=annee))


def _calculer_tonnage_basilic(annee):
    """Calcule le tonnage par magasin directement depuis les données saisies
    dans Basilic (cagettes pesées pour la collecte directe + poids saisis
    pour la collecte gardée), sans passer par un aller-retour VIF — même
    calcul et même séparation par article que les CSV envoyés à VIF
    (exporter_cagettes / exporter_poids_magasins_gardee), sommés par Code
    VIF comme le ferait VIF lui-même à l'import. Basilic est la source
    d'origine de ces chiffres (VIF n'en est qu'une réexpédition), d'où ce
    calcul direct plutôt qu'un extrait VIF réimporté."""
    resultats = {}
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT poids_cagette_pese FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    poids_cagette = (campagne["poids_cagette_pese"] if campagne else None) or 0
    if poids_cagette:
        for ligne in _charger_lignes_cagettes(annee):
            total = ligne.get("total") or 0
            if total:
                code_vif = ligne["code_vif"]
                resultats[code_vif] = resultats.get(code_vif, 0.0) + total * poids_cagette

    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        for r in conn.execute(
            "SELECT code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ?", (annee,)
        ).fetchall():
            if r["poids_kg"]:
                resultats[r["code_vif"]] = resultats.get(r["code_vif"], 0.0) + r["poids_kg"]

    return resultats


@collecte_bp.route("/collecte/<int:annee>/evolution-magasins/calculer-depuis-basilic", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def evolution_magasins_calculer_basilic(annee):
    if annee < PREMIERE_ANNEE_IMPORT_VIF:
        flash(
            f"⛔ Les résultats {annee} proviennent du fichier historique (repris tels quels) — "
            f"le calcul depuis Basilic n'est disponible qu'à partir de {PREMIERE_ANNEE_IMPORT_VIF}.",
            "danger",
        )
        return redirect(url_for("collecte.evolution_magasins", annee=annee))

    resultats = _calculer_tonnage_basilic(annee)
    if not resultats:
        flash(f"⛔ Aucune cagette pesée ni poids gardé trouvé pour {annee}.", "danger")
        return redirect(url_for("collecte.evolution_magasins", annee=annee))

    with get_db_connection() as conn:
        _ensure_tables_evolution_magasins(conn)
        codes_connus = {r[0] for r in conn.execute("SELECT code_vif FROM collecte_magasins_referentiel").fetchall()}
        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nb_maj, inconnus = 0, []
        for code_vif, kg in resultats.items():
            if code_vif not in codes_connus:
                inconnus.append(code_vif)
                continue
            conn.execute("""
                INSERT INTO collecte_magasins_resultats (code_vif, annee, resultat_kg, importe_le, importe_par)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code_vif, annee) DO UPDATE SET
                    resultat_kg = excluded.resultat_kg, importe_le = excluded.importe_le, importe_par = excluded.importe_par
            """, (code_vif, annee, round(kg, 2), maintenant, current_user.email))
            nb_maj += 1
        conn.commit()

    message = f"✅ Résultats {annee} calculés pour {nb_maj} magasin(s) depuis Basilic (cagettes + gardée)."
    if inconnus:
        message += f" ⚠️ {len(inconnus)} Code VIF absent(s) du référentiel, ignorés : {', '.join(inconnus[:10])}" + (
            "…" if len(inconnus) > 10 else ""
        )
    flash(message, "success" if not inconnus else "warning")
    write_log(f"📈 Évolution magasins : calcul depuis Basilic {annee}, {nb_maj} magasin(s) par {current_user.email}")
    return redirect(url_for("collecte.evolution_magasins", annee=annee))


# ============================================================================
# ⚖️ Pesée palox (poste de pesée + synthèse valorisée)
# ============================================================================
# Reprise du classeur "palox-<année> définitif pesée.xlsm" tenu à la main
# jusqu'ici : une feuille par catégorie de produit pour saisir le poids de
# chaque palox à la pesée, une feuille "Articles" de synthèse valorisée en
# mercuriale (prix €/kg fourni chaque année par la fédération).
CATEGORIES_PALOX_INITIALES = [
    # (ordre, libellé, code VIF, libellé VIF, poids min (kg), poids max (kg))
    # Bornes reprises telles quelles de la validation de données Excel de
    # chaque onglet catégorie du classeur (nombre entier compris entre min
    # et max) — 10-700 partout sauf le Sucre (10-750).
    (1, "Patisserie", "0110041", "Patisserie / Viennoiserie", 10, 700),
    (2, "Biscottes", "0210001", "Biscottes/Pain grillé", 10, 700),
    (3, "Biscuits et autres sucrés", "0210031", "Biscuits sucrés", 10, 700),
    (4, "Café", "0310001", "Café grain/moulu", 10, 700),
    (5, "Thé, tisane", "0310031", "Thé/Infusion/Chicorée", 10, 700),
    (6, "Poudre petit déjeuner", "0410001", "Poudre petit déjeuner", 10, 700),
    (7, "Céréales", "0410011", "Céréales", 10, 700),
    (8, "Chocolat, confiserie", "0610001", "Chocolat noir/lait/noisettes..", 10, 700),
    (9, "Lait", "0910051", "Lait ambiant", 10, 700),
    (10, "Farine de blé", "1010001", "Farine/maizena", 10, 700),
    (11, "Pomme de terre en flocons", "1010031", "Pomme de terre en flocons", 10, 700),
    (12, "Pâtes", "1110001", "Pâtes", 10, 700),
    (13, "Couscous", "1110031", "Couscous/Semoule/Autre fécul.", 10, 700),
    (14, "Riz", "1110051", "Riz long/rond", 10, 700),
    (15, "Fruits secs", "1210001", "Fruits secs", 10, 700),
    (16, "Légumes secs", "1220001", "Légumes secs sous vide", 10, 700),
    (17, "Potages liquide", "1310001", "Potages /soupes ambiant", 10, 700),
    (18, "Potages déshydratés", "1310011", "Potages déshydratés", 10, 700),
    (19, "Condiments, mayo, sauces, sel, poivre", "1410021", "moutardes/mayo/ketchup", 10, 700),
    (20, "Huiles", "1710001", "Huiles", 10, 700),
    (21, "Sucre en morceaux, en poudre", "1910001", "Sucre en morceaux", 10, 750),
    (22, "Confitures, compote, conserve fruits, miel", "2010001", "Confitures/miel/pates a tartiner", 10, 700),
    (23, "Petits pots et assimilés BB", "2510001", "Petits pots et assimilés BB", 10, 700),
    (24, "Sodas/boissons sucrées/arom.", "2810011", "Sodas/boissons sucrés", 10, 700),
    (25, "Conserve plats cuisinés", "4210011", "Platcuisinés viande ambiant", 10, 700),
    (26, "Conserve de légumes", "4510001", "Conserve de légumes", 10, 700),
    (27, "Conserve poisson crustacé", "4910001", "Conserves  poisson/ crustacé", 10, 700),
    (28, "Puériculture", "6010000", "Puériculture", 10, 700),
    (29, "Pdts Hygiène Famille", "6010010", "Pdts Hygiene Famille", 10, 700),
    (30, "Pdts. Entretien/Lessive", "6010030", "Pdts. Entretien/Lessive", 10, 700),
    (31, "Autres", None, None, 10, 700),
]


TARES_PALOX_INITIALES = [
    # (ordre, libellé, poids de tare à vide (kg)) — repris du tableau TARES
    # du classeur historique, pour impression de référence près de la balance.
    (1, "palox Europe", 57),
    (2, "palox noir", 50),
    (3, "palox rouge", 40),
    (4, "1/2 palox rouge", 25),
    (5, "CV6413:10 en hauteur avec palette", 73),
    (6, "CV6413:7 en hauteur avec palette", 61),
]


def _ensure_tables_palox(conn):
    """Tables du module pesée palox — le référentiel des catégories est
    indépendant de l'année de campagne (repris une fois du classeur), les
    pesées et la mercuriale sont propres à chaque année."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_palox_categories (
            id INTEGER PRIMARY KEY,
            ordre INTEGER NOT NULL,
            libelle TEXT NOT NULL,
            code_vif TEXT,
            libelle_vif TEXT,
            poids_min REAL DEFAULT 10,
            poids_max REAL DEFAULT 700
        )
    """)
    colonnes_categories = {r[1] for r in conn.execute("PRAGMA table_info(collecte_palox_categories)").fetchall()}
    if "poids_min" not in colonnes_categories:
        conn.execute("ALTER TABLE collecte_palox_categories ADD COLUMN poids_min REAL DEFAULT 10")
    if "poids_max" not in colonnes_categories:
        conn.execute("ALTER TABLE collecte_palox_categories ADD COLUMN poids_max REAL DEFAULT 700")
        conn.execute("UPDATE collecte_palox_categories SET poids_max = 750 WHERE libelle = 'Sucre en morceaux, en poudre'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_palox_pesees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            annee INTEGER NOT NULL,
            categorie_id INTEGER NOT NULL,
            numero_palox INTEGER NOT NULL,
            poids_kg REAL,
            poids_corrige_kg REAL,
            saisi_le TEXT,
            saisi_par TEXT,
            corrige_le TEXT,
            corrige_par TEXT,
            UNIQUE(annee, categorie_id, numero_palox)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_palox_mercuriale (
            annee INTEGER NOT NULL,
            code_vif TEXT NOT NULL,
            libelle TEXT,
            prix_kg REAL,
            importe_le TEXT,
            importe_par TEXT,
            PRIMARY KEY (annee, code_vif)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_palox_mercuriale_import (
            annee INTEGER PRIMARY KEY,
            nom_fichier TEXT,
            importe_le TEXT,
            importe_par TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_palox_parametres (
            cle TEXT PRIMARY KEY,
            valeur TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collecte_palox_tares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ordre INTEGER NOT NULL,
            libelle TEXT NOT NULL,
            poids_kg REAL NOT NULL
        )
    """)
    nb = conn.execute("SELECT COUNT(*) FROM collecte_palox_categories").fetchone()[0]
    if nb == 0:
        for ordre, libelle, code_vif, libelle_vif, poids_min, poids_max in CATEGORIES_PALOX_INITIALES:
            conn.execute("""
                INSERT INTO collecte_palox_categories (ordre, libelle, code_vif, libelle_vif, poids_min, poids_max)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ordre, libelle, code_vif, libelle_vif, poids_min, poids_max))
    nb_tares = conn.execute("SELECT COUNT(*) FROM collecte_palox_tares").fetchone()[0]
    if nb_tares == 0:
        for ordre, libelle, poids_kg in TARES_PALOX_INITIALES:
            conn.execute(
                "INSERT INTO collecte_palox_tares (ordre, libelle, poids_kg) VALUES (?, ?, ?)",
                (ordre, libelle, poids_kg),
            )


LIGNES_ETIQUETTE_PALOX_DEFAUT = [
    {"texte": "COLLECTE >>annee<<", "taille": 22, "gras": True},
    {"texte": ">>code_vif<<", "taille": 48, "gras": True},
    {"texte": ">>categorie<<", "taille": 54, "gras": True},
    {"texte": "Palette >>numero<<", "taille": 42, "gras": True},
    {"texte": "Poids >>poids:150<< kg", "taille": 42, "gras": True},
]


def _gabarit_etiquette_palox(conn):
    """Mise en page de l'étiquette imprimée à chaque pesée — reprise au
    départ de l'onglet "imp" du classeur, sous forme d'une simple liste de
    lignes de texte (pas de HTML à connaître) : chaque ligne a son texte,
    sa taille de police et un style gras ou non, modifiables sans toucher
    au code. Espaces réservés remplacés à l'impression, entre >> et << :
    >>annee<<, >>code_vif<<, >>categorie<<, >>numero<<, >>poids<<,
    >>date_heure<<."""
    r = conn.execute("SELECT valeur FROM collecte_palox_parametres WHERE cle = 'gabarit_lignes'").fetchone()
    if r and r["valeur"]:
        try:
            return json.loads(r["valeur"])
        except (TypeError, ValueError):
            pass
    return LIGNES_ETIQUETTE_PALOX_DEFAUT


def _categories_palox(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM collecte_palox_categories ORDER BY ordre"
    ).fetchall()]


def _parser_mercuriale(contenu):
    """Parse le rapport texte mercuriale (export VIF, encodage cp1252,
    tabulé) : ignore l'en-tête jusqu'à la ligne de titres de colonnes
    (« Article » / « Libellé »), puis lit code / libellé / coût EUR/Kgn pour
    chaque ligne — prix en notation française (virgule décimale).

    Lève ValueError si la colonne 4 de l'en-tête ne ressemble pas à une
    colonne de prix (« EUR » attendu) — sans ce contrôle, un export VIF dont
    l'ordre des colonnes aurait changé serait importé silencieusement avec
    de mauvais prix, sans qu'aucune ligne ne soit rejetée pour le signaler
    (audit 2026-10-28)."""
    prix = {}
    lignes_apres_entete = False
    for ligne in contenu.splitlines():
        champs = ligne.split("\t")
        if not lignes_apres_entete:
            if champs[0].strip() == "Article" and len(champs) > 1 and champs[1].strip().startswith("Libell"):
                if len(champs) < 4 or "eur" not in champs[3].strip().lower():
                    raise ValueError(
                        "Format de fichier non reconnu : la 4e colonne de l'en-tête "
                        f"(« {champs[3].strip() if len(champs) > 3 else ''} ») ne ressemble pas à une colonne de prix (EUR)."
                    )
                lignes_apres_entete = True
            continue
        if len(champs) < 4:
            continue
        code = champs[0].strip()
        if not code:
            continue
        try:
            valeur = float(champs[3].strip().replace(",", "."))
        except ValueError:
            continue
        prix[code] = (champs[1].strip(), valeur)
    return prix


@collecte_bp.route("/collecte/palox/categories")
@login_required
@require_access("collecte", "lecture")
def palox_categories():
    """Gestion du référentiel des catégories de produit (pluriannuel,
    indépendant de l'année de campagne) — code VIF utilisé pour retrouver
    le prix mercuriale, à tenir à jour si un code change ou si une
    catégorie est ajoutée/retirée d'une année sur l'autre."""
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        categories = _categories_palox(conn)
        utilisees = {r[0] for r in conn.execute(
            "SELECT DISTINCT categorie_id FROM collecte_palox_pesees"
        ).fetchall()}
        gabarit_lignes = _gabarit_etiquette_palox(conn)
    for c in categories:
        c["utilisee"] = c["id"] in utilisees
    return render_template(
        "collecte/palox_categories.html",
        categories=categories,
        gabarit_lignes=gabarit_lignes,
        gabarit_defaut=LIGNES_ETIQUETTE_PALOX_DEFAUT,
    )


@collecte_bp.route("/collecte/palox/gabarit-etiquette/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_gabarit_etiquette_enregistrer():
    """Mise en page de l'étiquette imprimée (liste de lignes de texte avec
    taille de police), modifiable sans toucher au code ni au HTML."""
    donnees = request.get_json(force=True) or {}
    lignes_brutes = donnees.get("lignes") or []

    lignes = []
    for ligne in lignes_brutes:
        texte = str(ligne.get("texte", "")).strip()
        if not texte:
            continue
        try:
            taille = int(ligne.get("taille"))
        except (TypeError, ValueError):
            taille = 24
        lignes.append({"texte": texte, "taille": taille, "gras": bool(ligne.get("gras"))})
    if not lignes:
        return jsonify({"success": False, "erreur": "Modèle vide"}), 400

    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        conn.execute("""
            INSERT INTO collecte_palox_parametres (cle, valeur) VALUES ('gabarit_lignes', ?)
            ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur
        """, (json.dumps(lignes),))
        conn.commit()

    write_log(f"⚖️ Mise en page de l'étiquette pesée palox modifiée par {current_user.email}")
    return jsonify({"success": True})


@collecte_bp.route("/collecte/palox/categories/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_categories_enregistrer():
    donnees = request.get_json(force=True) or {}
    lignes = donnees.get("categories") or []

    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        for ligne in lignes:
            libelle = str(ligne.get("libelle", "")).strip()
            if not libelle:
                continue
            code_vif = str(ligne.get("code_vif") or "").strip() or None
            libelle_vif = str(ligne.get("libelle_vif") or "").strip() or None
            try:
                ordre = int(ligne.get("ordre"))
            except (TypeError, ValueError):
                ordre = 999
            try:
                poids_min = float(str(ligne.get("poids_min", "")).replace(",", "."))
            except (TypeError, ValueError):
                poids_min = 10
            try:
                poids_max = float(str(ligne.get("poids_max", "")).replace(",", "."))
            except (TypeError, ValueError):
                poids_max = 700
            categorie_id = ligne.get("id")
            if categorie_id:
                conn.execute("""
                    UPDATE collecte_palox_categories
                    SET ordre = ?, libelle = ?, code_vif = ?, libelle_vif = ?, poids_min = ?, poids_max = ?
                    WHERE id = ?
                """, (ordre, libelle, code_vif, libelle_vif, poids_min, poids_max, categorie_id))
            else:
                conn.execute("""
                    INSERT INTO collecte_palox_categories (ordre, libelle, code_vif, libelle_vif, poids_min, poids_max)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ordre, libelle, code_vif, libelle_vif, poids_min, poids_max))
        conn.commit()

    write_log(f"⚖️ Référentiel catégories palox mis à jour par {current_user.email} ({len(lignes)} ligne(s))")
    return jsonify({"success": True})


@collecte_bp.route("/collecte/palox/categories/<int:categorie_id>/supprimer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_categories_supprimer(categorie_id):
    with get_db_connection() as conn:
        utilisee = conn.execute(
            "SELECT 1 FROM collecte_palox_pesees WHERE categorie_id = ? LIMIT 1", (categorie_id,)
        ).fetchone()
        if utilisee:
            return jsonify({"success": False, "erreur": "Catégorie déjà utilisée dans une pesée, suppression impossible"}), 400
        conn.execute("DELETE FROM collecte_palox_categories WHERE id = ?", (categorie_id,))
        conn.commit()

    write_log(f"⚖️ Catégorie palox {categorie_id} supprimée par {current_user.email}")
    return jsonify({"success": True})


@collecte_bp.route("/collecte/palox/tares")
@login_required
@require_access("collecte", "lecture")
def palox_tares():
    """Référentiel imprimable des tares (poids à vide) par type de
    conditionnement — simple feuille de référence pour l'opérateur au poste
    de pesée (soustraction faite à la main, pas de calcul automatique)."""
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        tares = [dict(r) for r in conn.execute(
            "SELECT * FROM collecte_palox_tares ORDER BY ordre"
        ).fetchall()]
    return render_template("collecte/palox_tares.html", tares=tares)


@collecte_bp.route("/collecte/palox/tares/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_tares_enregistrer():
    donnees = request.get_json(force=True) or {}
    lignes = donnees.get("tares") or []

    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        for ligne in lignes:
            libelle = str(ligne.get("libelle", "")).strip()
            if not libelle:
                continue
            try:
                ordre = int(ligne.get("ordre"))
            except (TypeError, ValueError):
                ordre = 999
            try:
                poids_kg = float(str(ligne.get("poids_kg", "")).replace(",", "."))
            except (TypeError, ValueError):
                continue
            tare_id = ligne.get("id")
            if tare_id:
                conn.execute(
                    "UPDATE collecte_palox_tares SET ordre = ?, libelle = ?, poids_kg = ? WHERE id = ?",
                    (ordre, libelle, poids_kg, tare_id),
                )
            else:
                conn.execute(
                    "INSERT INTO collecte_palox_tares (ordre, libelle, poids_kg) VALUES (?, ?, ?)",
                    (ordre, libelle, poids_kg),
                )
        conn.commit()

    write_log(f"⚖️ Référentiel tares palox mis à jour par {current_user.email} ({len(lignes)} ligne(s))")
    return jsonify({"success": True})


@collecte_bp.route("/collecte/palox/tares/<int:tare_id>/supprimer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_tares_supprimer(tare_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM collecte_palox_tares WHERE id = ?", (tare_id,))
        conn.commit()

    write_log(f"⚖️ Tare palox {tare_id} supprimée par {current_user.email}")
    return jsonify({"success": True})


@collecte_bp.route("/collecte/<int:annee>/palox")
@login_required
@require_access("collecte", "lecture")
def palox_accueil(annee):
    """Ancienne liste des catégories, fusionnée dans la Synthèse (qui
    permet désormais aussi de cliquer une catégorie pour la peser) —
    route conservée pour ne pas casser d'éventuels liens existants."""
    return redirect(url_for("collecte.palox_synthese", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/palox/<int:categorie_id>")
@login_required
@require_access("collecte", "lecture")
def palox_saisie(annee, categorie_id):
    """Poste de pesée pour une catégorie donnée — pensé pour aller vite :
    un champ poids, impression immédiate de la feuille A4 dès l'enregistrement
    (gabarit dans le template, sans confirmation manuelle — nécessite que le
    navigateur du poste soit lancé en mode kiosque d'impression, sans quoi
    le navigateur affichera sa boîte de dialogue d'impression standard)."""
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        categorie = conn.execute(
            "SELECT * FROM collecte_palox_categories WHERE id = ?", (categorie_id,)
        ).fetchone()
        if not categorie:
            abort(404)
        historique = [dict(r) for r in conn.execute("""
            SELECT * FROM collecte_palox_pesees
            WHERE annee = ? AND categorie_id = ?
            ORDER BY numero_palox DESC LIMIT 15
        """, (annee, categorie_id)).fetchall()]
        dernier_numero = conn.execute("""
            SELECT COALESCE(MAX(numero_palox), 0) FROM collecte_palox_pesees
            WHERE annee = ? AND categorie_id = ?
        """, (annee, categorie_id)).fetchone()[0]
        gabarit_lignes = _gabarit_etiquette_palox(conn)
    return render_template(
        "collecte/palox_saisie.html",
        annee=annee,
        categorie=dict(categorie),
        prochain_numero=dernier_numero + 1,
        historique=historique,
        gabarit_lignes=gabarit_lignes,
    )


@collecte_bp.route("/collecte/<int:annee>/palox/categories-actives")
@login_required
@require_access("collecte", "lecture")
def palox_categories_actives(annee):
    """Liste {id, libelle} des catégories palox, pour que le service worker
    (sw_palox.js) sache quelles pages/données mettre en cache à l'avance —
    cf. documentation_interne/Securisation_Saisie_Pesee_Palox.docx, mode
    hors-ligne 4.3. `annee` n'intervient pas dans le filtrage (catégories
    pluriannuelles) mais garde l'URL cohérente avec le reste du module."""
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        categories = _categories_palox(conn)
    return jsonify({
        "success": True,
        "categories": [{"id": c["id"], "libelle": c["libelle"]} for c in categories],
    })


@collecte_bp.route("/collecte/<int:annee>/palox/<int:categorie_id>/donnees")
@login_required
@require_access("collecte", "lecture")
def palox_donnees_saisie(annee, categorie_id):
    """Version JSON de palox_saisie — alimente la fenêtre de pesée affichée
    par-dessus la Synthèse (sans changement de page, pour ne pas couper le
    plein écran à chaque pesée)."""
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        categorie = conn.execute(
            "SELECT * FROM collecte_palox_categories WHERE id = ?", (categorie_id,)
        ).fetchone()
        if not categorie:
            return jsonify({"success": False, "erreur": "Catégorie inconnue"}), 404
        historique = [dict(r) for r in conn.execute("""
            SELECT * FROM collecte_palox_pesees
            WHERE annee = ? AND categorie_id = ?
            ORDER BY numero_palox DESC LIMIT 15
        """, (annee, categorie_id)).fetchall()]
        dernier_numero = conn.execute("""
            SELECT COALESCE(MAX(numero_palox), 0) FROM collecte_palox_pesees
            WHERE annee = ? AND categorie_id = ?
        """, (annee, categorie_id)).fetchone()[0]
        gabarit_lignes = _gabarit_etiquette_palox(conn)
    return jsonify({
        "success": True,
        "categorie": dict(categorie),
        "prochain_numero": dernier_numero + 1,
        "historique": historique,
        "gabarit_lignes": gabarit_lignes,
    })


def _valider_poids_categorie(poids_kg, categorie):
    """Reprend la validation de données Excel de l'onglet catégorie
    (nombre entier compris entre poids_min et poids_max) — bornes propres
    à chaque catégorie, modifiables sur l'écran ⚙️ Catégories. "nan"/"inf"
    sont acceptés par float() mais feraient planter int(poids_kg) plus bas
    (ValueError/OverflowError non catchée) — rejetés ici explicitement
    (audit 2026-10-28)."""
    if not math.isfinite(poids_kg):
        return "Poids invalide"
    if poids_kg != int(poids_kg):
        return "Le poids doit être un nombre entier"
    if not (categorie["poids_min"] <= poids_kg <= categorie["poids_max"]):
        return f"Le poids doit être compris entre {int(categorie['poids_min'])} et {int(categorie['poids_max'])} kg"
    return None


@collecte_bp.route("/collecte/<int:annee>/palox/<int:categorie_id>/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_enregistrer(annee, categorie_id):
    verrou = _resultat_bai_verrouille(annee)
    if verrou:
        return jsonify({
            "success": False,
            "erreur": f"🔒 Résultat collecte BAI {annee} verrouillé depuis le {verrou['le']} — déverrouillez-le d'abord.",
        }), 423
    donnees = request.get_json(force=True) or {}
    try:
        poids_kg = float(str(donnees.get("poids_kg", "")).replace(",", "."))
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400
    if poids_kg <= 0:
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

    numero_force = donnees.get("numero_palox")
    if numero_force not in (None, ""):
        try:
            numero_force = int(numero_force)
        except (TypeError, ValueError):
            return jsonify({"success": False, "erreur": "N° palox invalide"}), 400
        if numero_force <= 0:
            return jsonify({"success": False, "erreur": "N° palox invalide"}), 400
    else:
        numero_force = None

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        categorie = conn.execute(
            "SELECT * FROM collecte_palox_categories WHERE id = ?", (categorie_id,)
        ).fetchone()
        if not categorie:
            return jsonify({"success": False, "erreur": "Catégorie inconnue"}), 404

        erreur = _valider_poids_categorie(poids_kg, categorie)
        if erreur:
            return jsonify({"success": False, "erreur": erreur}), 400

        # SELECT MAX+INSERT séparés : 2 postes de pesée simultanés (scénario
        # réel, plusieurs tablettes en parallèle) peuvent calculer le même
        # prochain numéro avant que l'un des deux n'ait validé le sien —
        # la contrainte UNIQUE(annee, categorie_id, numero_palox) l'empêche
        # en base, mais levait jusqu'ici une IntegrityError non catchée
        # (500 brut). On retente quelques fois avec un nouveau numéro plutôt
        # que de planter (audit 2026-10-28).
        for tentative in range(5):
            if numero_force is not None:
                deja_pris = conn.execute("""
                    SELECT 1 FROM collecte_palox_pesees WHERE annee = ? AND categorie_id = ? AND numero_palox = ?
                """, (annee, categorie_id, numero_force)).fetchone()
                if deja_pris:
                    return jsonify({"success": False, "erreur": f"Le n° {numero_force} est déjà utilisé"}), 409
                numero_palox = numero_force
            else:
                numero_palox = conn.execute("""
                    SELECT COALESCE(MAX(numero_palox), 0) + 1 FROM collecte_palox_pesees
                    WHERE annee = ? AND categorie_id = ?
                """, (annee, categorie_id)).fetchone()[0]

            try:
                conn.execute("""
                    INSERT INTO collecte_palox_pesees (annee, categorie_id, numero_palox, poids_kg, saisi_le, saisi_par)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (annee, categorie_id, numero_palox, poids_kg, maintenant, current_user.email))
                conn.commit()
                break
            except sqlite3.IntegrityError:
                conn.rollback()
                if numero_force is not None:
                    return jsonify({"success": False, "erreur": f"Le n° {numero_force} est déjà utilisé"}), 409
                if tentative == 4:
                    return jsonify({"success": False, "erreur": "Numéro de palox pris par un autre poste de pesée au même instant — réessayez."}), 409

    return jsonify({
        "success": True,
        "numero_palox": numero_palox,
        "prochain_numero": numero_palox + 1,
        "poids_kg": poids_kg,
        "date_heure": maintenant,
        "categorie": {
            "libelle": categorie["libelle"],
            "code_vif": categorie["code_vif"],
            "libelle_vif": categorie["libelle_vif"],
        },
        "annee": annee,
    })


@collecte_bp.route("/collecte/<int:annee>/palox/<int:categorie_id>/supprimer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_supprimer(annee, categorie_id):
    """Supprime une pesée entièrement (erreur de saisie sur la mauvaise
    catégorie par exemple) — contrairement à la correction, qui ne fait que
    remplacer le poids d'un palox existant."""
    verrou = _resultat_bai_verrouille(annee)
    if verrou:
        return jsonify({
            "success": False,
            "erreur": f"🔒 Résultat collecte BAI {annee} verrouillé depuis le {verrou['le']} — déverrouillez-le d'abord.",
        }), 423
    donnees = request.get_json(force=True) or {}
    numero_palox = donnees.get("numero_palox")
    if not numero_palox:
        return jsonify({"success": False, "erreur": "N° palox manquant"}), 400

    with get_db_connection() as conn:
        curseur = conn.execute("""
            DELETE FROM collecte_palox_pesees WHERE annee = ? AND categorie_id = ? AND numero_palox = ?
        """, (annee, categorie_id, numero_palox))
        conn.commit()
    if curseur.rowcount == 0:
        return jsonify({"success": False, "erreur": "Palox introuvable"}), 404

    write_log(f"⚖️ Collecte {annee} : palox {categorie_id}/{numero_palox} supprimé par {current_user.email}")
    return jsonify({"success": True})


@collecte_bp.route("/collecte/<int:annee>/palox/<int:categorie_id>/reinitialiser", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_reinitialiser(annee, categorie_id):
    """Efface toutes les pesées de la catégorie pour l'année — équivalent
    de la macro "reinit" du classeur (remise à zéro de la numérotation en
    début de collecte)."""
    verrou = _resultat_bai_verrouille(annee)
    if verrou:
        return jsonify({
            "success": False,
            "erreur": f"🔒 Résultat collecte BAI {annee} verrouillé depuis le {verrou['le']} — déverrouillez-le d'abord.",
        }), 423
    with get_db_connection() as conn:
        curseur = conn.execute(
            "DELETE FROM collecte_palox_pesees WHERE annee = ? AND categorie_id = ?", (annee, categorie_id)
        )
        conn.commit()

    write_log(f"⚖️ Collecte {annee} : numérotation catégorie {categorie_id} réinitialisée "
              f"({curseur.rowcount} pesée(s) effacée(s)) par {current_user.email}")
    return jsonify({"success": True, "nb_effacees": curseur.rowcount})


@collecte_bp.route("/collecte/<int:annee>/palox/<int:categorie_id>/corriger", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_corriger(annee, categorie_id):
    verrou = _resultat_bai_verrouille(annee)
    if verrou:
        return jsonify({
            "success": False,
            "erreur": f"🔒 Résultat collecte BAI {annee} verrouillé depuis le {verrou['le']} — déverrouillez-le d'abord.",
        }), 423
    donnees = request.get_json(force=True) or {}
    numero_palox = donnees.get("numero_palox")
    try:
        poids_corrige = float(str(donnees.get("poids_corrige_kg", "")).replace(",", "."))
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400
    if not numero_palox or poids_corrige <= 0:
        return jsonify({"success": False, "erreur": "Paramètres invalides"}), 400

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        categorie = conn.execute(
            "SELECT * FROM collecte_palox_categories WHERE id = ?", (categorie_id,)
        ).fetchone()
        if not categorie:
            return jsonify({"success": False, "erreur": "Catégorie inconnue"}), 404
        erreur = _valider_poids_categorie(poids_corrige, categorie)
        if erreur:
            return jsonify({"success": False, "erreur": erreur}), 400

        curseur = conn.execute("""
            UPDATE collecte_palox_pesees
            SET poids_corrige_kg = ?, corrige_le = ?, corrige_par = ?
            WHERE annee = ? AND categorie_id = ? AND numero_palox = ?
        """, (poids_corrige, maintenant, current_user.email, annee, categorie_id, numero_palox))
        conn.commit()
    if curseur.rowcount == 0:
        return jsonify({"success": False, "erreur": "Palox introuvable"}), 404

    write_log(f"⚖️ Collecte {annee} : palox {categorie_id}/{numero_palox} corrigé "
              f"({poids_corrige} kg) par {current_user.email}")
    return jsonify({
        "success": True,
        "numero_palox": numero_palox,
        "poids_kg": poids_corrige,
        "date_heure": maintenant,
    })


def _synthese_palox(annee):
    """Équivalent de la feuille "Articles" du classeur : poids et % de
    répartition par catégorie (poids corrigé si présent, sinon poids
    saisi), valorisation en mercuriale (seulement si la mercuriale de
    l'année a été importée) et comparaison au poids de l'année précédente,
    calculée directement à partir de notre propre historique."""
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        categories = _categories_palox(conn)
        pesees = conn.execute(
            "SELECT categorie_id, poids_kg, poids_corrige_kg, saisi_le FROM collecte_palox_pesees WHERE annee = ?",
            (annee,),
        ).fetchall()
        pesees_prec = conn.execute(
            "SELECT categorie_id, poids_kg, poids_corrige_kg FROM collecte_palox_pesees WHERE annee = ?",
            (annee - 1,),
        ).fetchall()
        prix = {r["code_vif"]: r["prix_kg"] for r in conn.execute(
            "SELECT code_vif, prix_kg FROM collecte_palox_mercuriale WHERE annee = ?", (annee,)
        ).fetchall()}
        mercuriale_importee = conn.execute(
            "SELECT COUNT(*) FROM collecte_palox_mercuriale WHERE annee = ?", (annee,)
        ).fetchone()[0] > 0

    def _poids_effectif(r):
        return r["poids_corrige_kg"] if r["poids_corrige_kg"] is not None else (r["poids_kg"] or 0)

    poids_par_categorie, nb_par_categorie, poids_prec_par_categorie, poids_par_jour_heure = {}, {}, {}, {}
    for r in pesees:
        poids_par_categorie[r["categorie_id"]] = poids_par_categorie.get(r["categorie_id"], 0) + _poids_effectif(r)
        nb_par_categorie[r["categorie_id"]] = nb_par_categorie.get(r["categorie_id"], 0) + 1
        if r["saisi_le"]:
            # (jour, heure) et pas seulement l'heure : sinon les mêmes heures
            # de jours différents de la campagne se cumulaient entre elles au
            # lieu de rester des lignes distinctes (signalé 2026-11-10).
            cle = (r["saisi_le"][:10], r["saisi_le"][11:13])
            poids_par_jour_heure[cle] = poids_par_jour_heure.get(cle, 0) + _poids_effectif(r)
    for r in pesees_prec:
        poids_prec_par_categorie[r["categorie_id"]] = poids_prec_par_categorie.get(r["categorie_id"], 0) + _poids_effectif(r)

    cumul_horaire, cumul = [], 0.0
    for jour, heure in sorted(poids_par_jour_heure):
        poids_heure = poids_par_jour_heure[(jour, heure)]
        cumul += poids_heure
        cumul_horaire.append({
            "jour": jour,
            "heure": heure + "h",
            "poids_heure": round(poids_heure, 1),
            "cumul_kg": round(cumul, 1),
        })

    poids_total = sum(poids_par_categorie.values())
    lignes, valeur_totale = [], 0.0
    for cat in categories:
        poids = poids_par_categorie.get(cat["id"], 0)
        prix_kg = prix.get(cat["code_vif"]) if cat["code_vif"] else None
        valeur = poids * prix_kg if prix_kg is not None else None
        if valeur is not None:
            valeur_totale += valeur
        poids_prec = poids_prec_par_categorie.get(cat["id"])
        lignes.append({
            "id": cat["id"], "libelle": cat["libelle"], "code_vif": cat["code_vif"],
            "nb_palox": nb_par_categorie.get(cat["id"], 0),
            "poids_kg": round(poids, 1),
            "pct_poids": round(poids / poids_total * 100, 1) if poids_total else 0,
            "prix_kg": prix_kg,
            "valeur": round(valeur, 2) if valeur is not None else None,
            "poids_annee_precedente": round(poids_prec, 1) if poids_prec is not None else None,
        })

    return {
        "annee": annee,
        "lignes": lignes,
        "poids_total": round(poids_total, 1),
        "valeur_totale": round(valeur_totale, 2),
        "mercuriale_importee": mercuriale_importee,
        "cumul_horaire": cumul_horaire,
    }


@collecte_bp.route("/collecte/<int:annee>/palox/synthese")
@login_required
@require_access("collecte", "lecture")
def palox_synthese(annee):
    return render_template("collecte/palox_synthese.html", verrou_bai=_resultat_bai_verrouille(annee), **_synthese_palox(annee))


def _lignes_transferts_palox_vif(annee):
    """Pour chaque catégorie pesée au palox, total à transférer dans VIF
    (sortie du stock « collecte directe » 5010000, entrée sur le code
    VIF de la catégorie) --- mêmes principes que
    _lignes_transferts_vif (quantités par produit, collecte gardée),
    mais source = pesée palox plutôt que les quantités saisies par les
    associations gardant leur collecte. Catégories sans code VIF ou
    sans poids ignorées."""
    lignes = _synthese_palox(annee)["lignes"]
    return [
        {"article": l["code_vif"], "libelle": l["libelle"], "total_kg": l["poids_kg"]}
        for l in lignes if l["code_vif"] and l["poids_kg"]
    ]


@collecte_bp.route("/collecte/<int:annee>/palox/transferts")
@login_required
@require_access("collecte", "lecture")
def palox_transferts(annee):
    """Liste des mouvements de transfert à saisir dans VIF pour la pesée
    palox --- voir _lignes_transferts_palox_vif."""
    lignes = _lignes_transferts_palox_vif(annee)
    return render_template(
        "collecte/palox_transferts.html",
        annee=annee, lignes=lignes,
        total=sum(l["total_kg"] for l in lignes),
        article_source=CAGETTES_EXPORT_ARTICLE,
        depot_source=CAGETTES_EXPORT_DEPOT,
        depot_dest="03",
        date_mvt=_date_vif_lundi_jeudi(),
    )


@collecte_bp.route("/collecte/<int:annee>/palox/transferts/export-trancog")
@login_required
@require_access("collecte", "lecture")
def palox_transferts_export_trancog(annee):
    """Export CSV prêt à importer dans VIF (format TRANCOG, cf.
    _ligne_trancog_vif) pour la pesée palox --- même principe que
    quantites_produits_transferts_export_trancog, mais article sortie
    5010000 (collecte directe) au lieu de 5010010 (collecte gardée)."""
    lignes = _lignes_transferts_palox_vif(annee)
    if not lignes:
        flash(f"⛔ Aucun poids à transférer pour {annee}.", "warning")
        return redirect(url_for("collecte.palox_transferts", annee=annee))

    date_mvt = _date_vif_lundi_jeudi()
    contenu = "\r\n".join(
        _ligne_trancog_vif(l["article"], l["total_kg"], date_mvt, CAGETTES_EXPORT_ARTICLE, nature_mouvement="TRANSCOL")
        for l in lignes
    ) + "\r\n"

    write_log(f"📤 Export CSV TRANCOG VIF palox {annee} ({len(lignes)} catégorie(s)) par {current_user.email}")
    return Response(
        contenu.encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trancog_vif_palox_{annee}.csv"},
    )


@collecte_bp.route("/collecte/<int:annee>/palox/synthese/donnees")
@login_required
@require_access("collecte", "lecture")
def palox_synthese_donnees(annee):
    """Version JSON de _synthese_palox — pour rafraîchir le tableau sans
    recharger la page après une pesée (fenêtre de pesée par-dessus la
    Synthèse, voir palox_donnees_saisie)."""
    return jsonify(_synthese_palox(annee))


@collecte_bp.route("/collecte/<int:annee>/palox/affichage")
def palox_affichage(annee):
    """Écran kiosk (2e écran, sans connexion) affichant en direct la
    synthèse de la pesée palox — même principe que l'affichage cuisine
    (ba38_cuisine/routes_kiosk.py + production_cuisine/affichage.html) :
    polling JSON, passage en plein écran au premier clic si ouvert avec
    ?plein_ecran=1 (bouton « Affichage TV » de palox_synthese.html)."""
    return render_template("collecte/palox_affichage.html", annee=annee)


@collecte_bp.route("/collecte/<int:annee>/palox/affichage/donnees")
def palox_affichage_donnees(annee):
    """Version publique (sans connexion) de _synthese_palox, pour l'écran
    kiosk ci-dessus — pas de données plus sensibles que ce que montre déjà
    l'écran cuisine équivalent."""
    return jsonify(_synthese_palox(annee))


@collecte_bp.route("/collecte/<int:annee>/palox/synthese/export")
@login_required
@require_access("collecte", "lecture")
def palox_synthese_export(annee):
    synthese = _synthese_palox(annee)

    wb_export = Workbook()
    ws = wb_export.active
    ws.title = "pesée palox"
    entetes = ["Catégorie", "Code VIF", "Nb palox", "Poids (kg)", "% du total",
               "Prix mercuriale (€/kg)", "Valeur (€)", "Poids année précédente (kg)"]

    ws.append([f"Pesée palox {annee} — synthèse valorisée"])
    ws.merge_cells(f"A1:{chr(ord('A') + len(entetes) - 1)}1")
    titre = ws["A1"]
    titre.font = Font(name="Arial", bold=True, size=12, color="1F3864")
    titre.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    ws.append(entetes)
    ws.append([
        "Total", "", sum(l["nb_palox"] for l in synthese["lignes"]), synthese["poids_total"], 100,
        "", synthese["valeur_totale"],
        round(sum(l["poids_annee_precedente"] or 0 for l in synthese["lignes"]), 1),
    ])
    for l in synthese["lignes"]:
        ws.append([
            l["libelle"], l["code_vif"], l["nb_palox"], l["poids_kg"], l["pct_poids"],
            l["prix_kg"], l["valeur"], l["poids_annee_precedente"],
        ])

    for i in range(len(entetes)):
        lettre = chr(ord("A") + i)
        ws.column_dimensions[lettre].width = 34 if lettre == "A" else 14

    derniere_colonne = chr(ord("A") + len(entetes) - 1)
    derniere_ligne = 3 + len(synthese["lignes"])

    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A2:{derniere_colonne}{derniere_ligne}"

    # Mise en forme conditionnelle (dégradé blanc → vert, les poids/valeurs
    # les plus importants ressortent) — seulement sur les lignes de détail
    # (à partir de la ligne 4), la ligne "Total" fausserait l'échelle en
    # étant systématiquement la plus grande valeur (demande utilisateur
    # 2026-11-16).
    if derniere_ligne >= 4:
        for colonne in ("D", "E", "G"):
            ws.conditional_formatting.add(
                f"{colonne}4:{colonne}{derniere_ligne}",
                ColorScaleRule(start_type="min", start_color="FFFFFF", end_type="max", end_color="63BE7B"),
            )

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.gridLines = True

    tampon = io.BytesIO()
    wb_export.save(tampon)
    tampon.seek(0)
    nom_fichier = f"pesee_palox_{annee}.xlsx"
    return send_file(
        tampon,
        as_attachment=True,
        download_name=nom_fichier,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@collecte_bp.route("/collecte/<int:annee>/palox/mercuriale/importer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def palox_mercuriale_importer(annee):
    """Import du rapport de prix mercuriale — n'a de sens qu'une fois la
    pesée terminée, pour valoriser les résultats (la saisie au poste de
    pesée ne dépend pas de la mercuriale). Déposé depuis Paramètres (année
    en cours ET année précédente) — retour sur la page Paramètres de
    l'année affichée (retour_annee), pas forcément celle du fichier importé
    puisque les deux formulaires y cohabitent."""
    annee_retour = request.form.get("retour_annee", type=int) or annee

    fichier = request.files.get("fichier_mercuriale")
    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier sélectionné.", "warning")
        return redirect(url_for("collecte.collecte_main", annee=annee_retour))

    # Rapport texte mercuriale, normalement quelques dizaines de Ko — plafond
    # large mais explicite pour éviter de décoder/parcourir plusieurs fois en
    # mémoire un fichier énorme déposé par erreur, sur un serveur à 4 Go de
    # RAM (audit 2026-10-28, cf. consigne CLAUDE.md sur les commandes/fichiers
    # lourds).
    LIMITE_MERCURIALE_OCTETS = 5 * 1024 * 1024
    contenu_brut = fichier.read(LIMITE_MERCURIALE_OCTETS + 1)
    if len(contenu_brut) > LIMITE_MERCURIALE_OCTETS:
        flash("⛔ Fichier trop volumineux (plus de 5 Mo) — ce n'est probablement pas le bon fichier.", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee_retour))
    try:
        contenu = contenu_brut.decode("cp1252")
    except UnicodeDecodeError:
        contenu = contenu_brut.decode("utf-8", errors="replace")

    try:
        prix = _parser_mercuriale(contenu)
    except ValueError as erreur:
        flash(f"⛔ {erreur}", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee_retour))
    if not prix:
        flash("⛔ Aucune ligne de prix reconnue dans ce fichier.", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee_retour))

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_tables_palox(conn)
        for code, (libelle, valeur) in prix.items():
            conn.execute("""
                INSERT INTO collecte_palox_mercuriale (annee, code_vif, libelle, prix_kg, importe_le, importe_par)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(annee, code_vif) DO UPDATE SET
                    libelle = excluded.libelle, prix_kg = excluded.prix_kg,
                    importe_le = excluded.importe_le, importe_par = excluded.importe_par
            """, (annee, code, libelle, valeur, maintenant, current_user.email))
        conn.execute("""
            INSERT INTO collecte_palox_mercuriale_import (annee, nom_fichier, importe_le, importe_par)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(annee) DO UPDATE SET
                nom_fichier = excluded.nom_fichier, importe_le = excluded.importe_le, importe_par = excluded.importe_par
        """, (annee, fichier.filename, maintenant, current_user.email))
        conn.commit()

    flash(f"✅ Mercuriale {annee} importée ({len(prix)} article(s)).", "success")
    write_log(f"⚖️ Collecte {annee} : mercuriale importée ({len(prix)} article(s)) par {current_user.email}")
    return redirect(url_for("collecte.collecte_main", annee=annee_retour))


@collecte_bp.route("/collecte/<int:annee>/saisie-association/<token>", methods=["GET", "POST"])
def saisie_association(annee, token):
    """Formulaire public (sans compte) permettant à une association de
    saisir elle-même ses poids par magasin et ses quantités par produit
    — lien envoyé par mail en même temps que ses fichiers Excel/PDF
    personnalisés (cf. generer_token_saisie_association, gardee_envoi)."""
    payload = verifier_token_saisie_association(token)
    if not payload or payload.get("annee") != annee:
        return render_template("collecte/saisie_association_lien_invalide.html"), 403
    nom_association = payload.get("association", "")

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    association = next((a for a in associations if a["nom"] == nom_association), None)
    if association is None:
        return render_template("collecte/saisie_association_lien_invalide.html"), 403
    index_code_vif = _table_code_vif_associations()
    association_code_vif, _, _ = _code_vif_association(nom_association, index_code_vif)

    produits = _lire_produits_modele()

    if request.method == "POST":
        if _resultat_gardee_verrouille(annee):
            flash("🔒 La saisie est actuellement verrouillée par la Banque Alimentaire — merci de réessayer plus tard.", "warning")
            return redirect(url_for("collecte.saisie_association", annee=annee, token=token))

        def _poids_valide_ou_ignore(brut, libelle_erreur, champs_invalides):
            """Comme les champs du formulaire sont en texte libre (saisie
            possible depuis un téléphone), une valeur non numérique ou
            négative est ignorée (pas enregistrée) plutôt que de faire
            planter toute la page en 500 — contrairement à l'ancien
            comportement (audit 2026-10-23/28, lien public sans connexion,
            donc pas de contrôle navigateur fiable à attendre)."""
            if not brut:
                return None
            try:
                valeur = float(brut)
            except ValueError:
                champs_invalides.append(libelle_erreur)
                return "IGNORER"
            if valeur < 0:
                champs_invalides.append(libelle_erreur)
                return "IGNORER"
            return round(valeur, 2)

        action = request.form.get("action", "enregistrer")
        champs_invalides = []
        une_saisie = False
        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        magasins_manquants_termine = []

        if action == "terminer":
            # « J'ai terminé ma saisie » : ne retraite PAS les champs du
            # formulaire (chaque case est déjà enregistrée via l'autosave
            # AJAX au fil de la saisie — cf. champ-autosave côté template) :
            # un bouton qui réécrirait tous les champs présenterait un vrai
            # risque, une case non resoumise pour une raison quelconque
            # (bug JS, champ désactivé...) écraserait silencieusement une
            # valeur déjà bonne avec du vide. Exige que tous les magasins
            # aient un poids (jamais déduit à 0, un magasin a toujours une
            # pesée réelle), puis enregistre explicitement à 0 tout produit
            # du catalogue resté vide pour cette association — visible
            # ensuite dans la grille Quantités par produit — pour que le
            # calcul de complétude (_saisie_association_avancement, qui
            # exige une valeur sur chaque produit) atteigne 100% sans que
            # l'association ait à taper un 0 sur chaque produit qu'elle n'a
            # pas (demande utilisateur 2026-11-23).
            with get_db_connection() as conn:
                _ensure_table_poids_magasins_gardee(conn)
                codes_magasins = [_vif_fmt(m.get("Code VIF")) for m in association["magasins"]]
                poids_magasins_actuels = {}
                if codes_magasins:
                    marque = ",".join("?" * len(codes_magasins))
                    poids_magasins_actuels = {
                        r["code_vif"]: r["poids_kg"] for r in conn.execute(
                            f"SELECT code_vif, poids_kg FROM collecte_poids_magasins_gardee "
                            f"WHERE annee = ? AND nom_association = ? AND code_vif IN ({marque})",
                            (annee, nom_association, *codes_magasins)
                        ).fetchall()
                    }
                magasins_manquants_termine = [
                    m.get("Nom", c) for m, c in zip(association["magasins"], codes_magasins)
                    if poids_magasins_actuels.get(c) is None
                ]
                if not magasins_manquants_termine:
                    for produit in produits:
                        existant = conn.execute(
                            "SELECT poids_kg FROM collecte_quantites_produits "
                            "WHERE annee = ? AND association = ? AND code_produit = ?",
                            (annee, nom_association, produit["code"])
                        ).fetchone()
                        if existant is not None and existant["poids_kg"] is not None:
                            continue
                        conn.execute("""
                            INSERT INTO collecte_quantites_produits (annee, association, code_produit, poids_kg, saisi_le, saisi_par)
                            VALUES (?, ?, ?, 0, ?, ?)
                            ON CONFLICT(annee, association, code_produit)
                            DO UPDATE SET poids_kg = 0, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
                        """, (annee, nom_association, produit["code"], maintenant, f"association:{nom_association}:terminer"))
                conn.commit()
        else:
            with get_db_connection() as conn:
                _ensure_table_poids_magasins_gardee(conn)
                for magasin in association["magasins"]:
                    code_vif = _vif_fmt(magasin.get("Code VIF"))
                    brut = request.form.get(f"poids_magasin_{code_vif}", "").strip().replace(",", ".")
                    poids_kg = _poids_valide_ou_ignore(brut, magasin.get("Nom", code_vif), champs_invalides)
                    if poids_kg == "IGNORER":
                        continue
                    une_saisie = True
                    conn.execute("""
                        INSERT INTO collecte_poids_magasins_gardee (annee, code_vif, nom_association, poids_kg, saisi_le, saisi_par)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(annee, code_vif, nom_association)
                        DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
                    """, (annee, code_vif, nom_association, poids_kg, maintenant, f"association:{nom_association}"))
                for produit in produits:
                    brut = request.form.get(f"poids_produit_{produit['code']}", "").strip().replace(",", ".")
                    poids_kg = _poids_valide_ou_ignore(brut, produit.get("libelle", produit["code"]), champs_invalides)
                    if poids_kg == "IGNORER":
                        continue
                    une_saisie = True
                    conn.execute("""
                        INSERT INTO collecte_quantites_produits (annee, association, code_produit, poids_kg, saisi_le, saisi_par)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(annee, association, code_produit)
                        DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
                    """, (annee, nom_association, produit["code"], poids_kg, maintenant, f"association:{nom_association}"))
                conn.commit()

        write_log(f"⚖️ Saisie en ligne association « {nom_association} » {annee} enregistrée")
        if une_saisie:
            _notifier_saisie_association(annee, nom_association)
        if action == "terminer":
            if magasins_manquants_termine:
                flash(
                    "⛔ Impossible de terminer : il manque le poids de "
                    f"{len(magasins_manquants_termine)} magasin(s) — {', '.join(magasins_manquants_termine)}.",
                    "danger",
                )
            else:
                _marquer_suivi_gardee(annee, nom_association, "saisie_en_ligne_terminee", True, f"association:{nom_association}")
                write_log(f"✅ Saisie en ligne « {nom_association} » {annee} marquée terminée (produits restants comptés à 0)")
                flash("✅ Saisie marquée terminée — merci ! Les produits non indiqués ont été comptés à 0.", "success")
        elif champs_invalides:
            flash(
                "⚠️ Enregistré, sauf " + str(len(champs_invalides)) + " valeur(s) invalide(s) ignorée(s) : "
                + ", ".join(champs_invalides), "warning",
            )
        else:
            flash("✅ Merci, votre saisie a bien été enregistrée.", "success")
        return redirect(url_for("collecte.saisie_association", annee=annee, token=token))

    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        poids_magasins = {
            r["code_vif"]: r["poids_kg"]
            for r in conn.execute(
                "SELECT code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ? AND nom_association = ?",
                (annee, nom_association),
            ).fetchall()
        }
        poids_produits = {
            r["code_produit"]: r["poids_kg"]
            for r in conn.execute(
                "SELECT code_produit, poids_kg FROM collecte_quantites_produits WHERE annee = ? AND association = ?",
                (annee, nom_association),
            ).fetchall()
        }

    magasins = [
        {
            "code_vif": _vif_fmt(m.get("Code VIF")),
            "nom": m.get("Nom", ""),
            "ville": m.get("Ville", ""),
            "poids_kg": poids_magasins.get(_vif_fmt(m.get("Code VIF"))),
        }
        for m in association["magasins"]
    ]
    for produit in produits:
        produit["poids_kg"] = poids_produits.get(produit["code"])

    return render_template(
        "collecte/saisie_association.html",
        annee=annee,
        token=token,
        association=nom_association,
        association_code_vif=association_code_vif,
        magasins=magasins,
        produits=produits,
        verrou_gardee=_resultat_gardee_verrouille(annee),
        suivi_terminee=_lire_suivi_gardee(annee).get(nom_association, {}).get("saisie_en_ligne_terminee", False),
    )


@collecte_bp.route("/collecte/<int:annee>/saisie-association/<token>/champ", methods=["POST"])
def saisie_association_enregistrer_champ(annee, token):
    """Autosave AJAX d'une seule case du formulaire public saisie_association
    (en plus du bouton « Enregistrer » qui soumet tout le formulaire) — même
    authentification par token que la page elle-même, pas de compte requis."""
    payload = verifier_token_saisie_association(token)
    if not payload or payload.get("annee") != annee:
        return jsonify({"success": False, "erreur": "Lien invalide ou expiré"}), 403
    nom_association = payload.get("association", "")

    donnees = request.get_json(silent=True) or {}
    type_champ = donnees.get("type")
    code = str(donnees.get("code", "")).strip()
    poids_brut = donnees.get("poids_kg")

    if type_champ not in ("magasin", "produit") or not code:
        return jsonify({"success": False, "erreur": "Paramètres manquants"}), 400

    try:
        poids_kg = round(float(poids_brut), 2) if poids_brut not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

    if poids_kg is not None and poids_kg < 0:
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

    verrou = _resultat_gardee_verrouille(annee)
    if verrou:
        return jsonify({
            "success": False,
            "erreur": "🔒 La saisie est actuellement verrouillée par la Banque Alimentaire — merci de réessayer plus tard.",
        }), 423

    if type_champ == "magasin":
        # Le code_vif vient du client (JSON posté) : sans ce contrôle, une
        # association pourrait écraser le poids déclaré d'une AUTRE
        # association (remontées officielles VIF/FFBA) simplement en postant
        # un autre code_vif depuis sa propre page de saisie (audit 2026-10-28).
        df_mag_verif = _lire_magasins_gardes(annee)
        df_groupes_verif = _lire_groupes(annee)
        if "Code VIF" in df_mag_verif.columns:
            df_mag_verif["Code VIF"] = df_mag_verif["Code VIF"].map(_vif_fmt)
        associations_verif, _ = _construire_associations(df_mag_verif, df_groupes_verif)
        association_verif = next((a for a in associations_verif if a["nom"] == nom_association), None)
        codes_vif_association = {
            _vif_fmt(m.get("Code VIF")) for m in (association_verif or {}).get("magasins", [])
        }
        if _vif_fmt(code) not in codes_vif_association:
            return jsonify({"success": False, "erreur": "Magasin non autorisé pour cette association"}), 403
    else:
        # Même principe que pour la branche magasin ci-dessus : le code
        # produit vient du client, sans contrôle il permettait d'insérer des
        # lignes avec un code_produit inventé dans collecte_quantites_produits
        # (pas d'écrasement croisé entre associations possible ici — chaque
        # ligne reste scopée à nom_association, qui vient du token — mais la
        # cohérence des données n'était pas garantie) (audit 2026-10-29).
        codes_produits_valides = {p["code"] for p in _lire_produits_modele()}
        if code not in codes_produits_valides:
            return jsonify({"success": False, "erreur": "Produit inconnu"}), 400

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    saisi_par = f"association:{nom_association}"
    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        if type_champ == "magasin":
            code_vif = _vif_fmt(code)
            conn.execute("""
                INSERT INTO collecte_poids_magasins_gardee (annee, code_vif, nom_association, poids_kg, saisi_le, saisi_par)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(annee, code_vif, nom_association)
                DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
            """, (annee, code_vif, nom_association, poids_kg, maintenant, saisi_par))
        else:
            conn.execute("""
                INSERT INTO collecte_quantites_produits (annee, association, code_produit, poids_kg, saisi_le, saisi_par)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(annee, association, code_produit)
                DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
            """, (annee, nom_association, code, poids_kg, maintenant, saisi_par))
        conn.commit()

    _notifier_saisie_association(annee, nom_association)
    return jsonify({"success": True})


@collecte_bp.route("/collecte/gardee/saisie/choisir")
@login_required
@require_access("collecte", "ecriture")
def gardee_saisie_choisir():
    """Ancien point d'entrée dédié (tableau dupliqué de gardee_envoi) —
    fusionné dans gardee_envoi le 2026-11-18 (demande utilisateur : les 2
    tableaux quasi identiques entre "Préparer l'envoi" et "Saisir"
    rendaient le cycle "association gardant sa collecte" confus). Cette
    route ne fait plus que rediriger, pour ne pas casser les favoris/liens
    existants (ex. bouton Retour de gardee_saisie.html)."""
    annee = request.args.get("annee", type=int) or datetime.now().year
    return redirect(url_for("collecte.gardee_envoi", annee=annee))


@collecte_bp.route("/collecte/gardee/saisie", methods=["GET", "POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_saisie():
    """Équivalent interne (compte connecté) de saisie_association : regroupe
    en une seule page, pour UNE association, la saisie du poids par magasin
    et des quantités par produit — au lieu d'obliger l'utilisateur à
    naviguer entre les 2 grilles globales (poids_magasins_gardee,
    quantites_produits) pour y repérer la même association (demande
    2026-11-10). Mêmes conventions que les autres routes gardee/* : annee
    en querystring/form, jamais dans le chemin."""
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    nom_association = (request.form.get("association", "") if request.method == "POST"
                        else request.args.get("association", "")).strip()

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    association = next((a for a in associations if a["nom"] == nom_association), None)
    if association is None:
        flash("⛔ Association introuvable.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))
    index_code_vif = _table_code_vif_associations()
    association_code_vif, _, _ = _code_vif_association(nom_association, index_code_vif)

    produits = _lire_produits_modele()

    if request.method == "POST":
        if _resultat_gardee_verrouille(annee):
            flash(f"🔒 Résultat collecte gardée {annee} verrouillé — déverrouillez-le d'abord.", "danger")
            return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

        def _poids_valide_ou_ignore(brut, libelle_erreur, champs_invalides):
            if not brut:
                return None
            try:
                valeur = float(brut)
            except ValueError:
                champs_invalides.append(libelle_erreur)
                return "IGNORER"
            if valeur < 0:
                champs_invalides.append(libelle_erreur)
                return "IGNORER"
            return round(valeur, 2)

        champs_invalides = []
        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db_connection() as conn:
            _ensure_table_poids_magasins_gardee(conn)
            for magasin in association["magasins"]:
                code_vif = _vif_fmt(magasin.get("Code VIF"))
                brut = request.form.get(f"poids_magasin_{code_vif}", "").strip().replace(",", ".")
                poids_kg = _poids_valide_ou_ignore(brut, magasin.get("Nom", code_vif), champs_invalides)
                if poids_kg == "IGNORER":
                    continue
                conn.execute("""
                    INSERT INTO collecte_poids_magasins_gardee (annee, code_vif, nom_association, poids_kg, saisi_le, saisi_par)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(annee, code_vif, nom_association)
                    DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
                """, (annee, code_vif, nom_association, poids_kg, maintenant, current_user.email))
            for produit in produits:
                brut = request.form.get(f"poids_produit_{produit['code']}", "").strip().replace(",", ".")
                poids_kg = _poids_valide_ou_ignore(brut, produit.get("libelle", produit["code"]), champs_invalides)
                if poids_kg == "IGNORER":
                    continue
                conn.execute("""
                    INSERT INTO collecte_quantites_produits (annee, association, code_produit, poids_kg, saisi_le, saisi_par)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(annee, association, code_produit)
                    DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
                """, (annee, nom_association, produit["code"], poids_kg, maintenant, current_user.email))
            conn.execute(
                "UPDATE collecte_detection_pdf_association SET statut = 'appliquee' "
                "WHERE annee = ? AND association = ? AND statut IN ('termine', 'erreur')",
                (annee, nom_association),
            )
            conn.commit()
        write_log(f"⚖️ Saisie collecte gardée {annee} ({nom_association}) enregistrée par {current_user.email}")
        if champs_invalides:
            flash(
                "⚠️ Enregistré, sauf " + str(len(champs_invalides)) + " valeur(s) invalide(s) ignorée(s) : "
                + ", ".join(champs_invalides), "warning",
            )
        else:
            flash("✅ Saisie enregistrée.", "success")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    with get_db_connection() as conn:
        _ensure_table_detection_pdf_association(conn)
        _ensure_table_poids_magasins_gardee(conn)
        conn.commit()
        poids_magasins = {
            r["code_vif"]: r["poids_kg"]
            for r in conn.execute(
                "SELECT code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ? AND nom_association = ?",
                (annee, nom_association),
            ).fetchall()
        }
        poids_produits = {
            r["code_produit"]: r["poids_kg"]
            for r in conn.execute(
                "SELECT code_produit, poids_kg FROM collecte_quantites_produits WHERE annee = ? AND association = ?",
                (annee, nom_association),
            ).fetchall()
        }
        detection = conn.execute(
            "SELECT * FROM collecte_detection_pdf_association WHERE annee = ? AND association = ? "
            "AND statut IN ('en_cours', 'erreur', 'termine') ORDER BY id DESC LIMIT 1",
            (annee, nom_association),
        ).fetchone()

    magasins_detectes, produits_detectes = {}, {}
    if detection and detection["statut"] == "termine":
        magasins_detectes = json.loads(detection["magasins_detectes"] or "{}")
        produits_detectes = json.loads(detection["produits_detectes"] or "{}")

    magasins = [
        {
            "code_vif": _vif_fmt(m.get("Code VIF")),
            "nom": m.get("Nom", ""),
            "ville": m.get("Ville", ""),
            "poids_kg": magasins_detectes.get(_vif_fmt(m.get("Code VIF")), poids_magasins.get(_vif_fmt(m.get("Code VIF")))),
            "detecte": _vif_fmt(m.get("Code VIF")) in magasins_detectes,
        }
        for m in association["magasins"]
    ]
    for produit in produits:
        produit["poids_kg"] = produits_detectes.get(produit["code"], poids_produits.get(produit["code"]))
        produit["detecte"] = produit["code"] in produits_detectes

    return render_template(
        "collecte/gardee_saisie.html",
        annee=annee,
        association=nom_association,
        association_code_vif=association_code_vif,
        magasins=magasins,
        produits=produits,
        detection=detection,
        verrou_gardee=_resultat_gardee_verrouille(annee),
    )


@collecte_bp.route("/collecte/gardee/saisie/champ", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_saisie_enregistrer_champ():
    """Autosave AJAX pour gardee_saisie — équivalent interne (compte connecté)
    de saisie_association_enregistrer_champ, mêmes contrôles de cohérence
    (magasin/produit appartenant bien à l'association choisie)."""
    donnees = request.get_json(silent=True) or {}
    annee = donnees.get("annee")
    try:
        annee = int(annee)
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Année invalide"}), 400
    nom_association = str(donnees.get("association", "")).strip()
    type_champ = donnees.get("type")
    code = str(donnees.get("code", "")).strip()
    poids_brut = donnees.get("poids_kg")

    if not nom_association or type_champ not in ("magasin", "produit") or not code:
        return jsonify({"success": False, "erreur": "Paramètres manquants"}), 400

    try:
        poids_kg = round(float(poids_brut), 2) if poids_brut not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400
    if poids_kg is not None and poids_kg < 0:
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

    verrou = _resultat_gardee_verrouille(annee)
    if verrou:
        return jsonify({
            "success": False,
            "erreur": f"🔒 Résultat collecte gardée {annee} verrouillé depuis le {verrou['le']} — déverrouillez-le d'abord.",
        }), 423

    if type_champ == "magasin":
        df_mag_verif = _lire_magasins_gardes(annee)
        df_groupes_verif = _lire_groupes(annee)
        if "Code VIF" in df_mag_verif.columns:
            df_mag_verif["Code VIF"] = df_mag_verif["Code VIF"].map(_vif_fmt)
        associations_verif, _ = _construire_associations(df_mag_verif, df_groupes_verif)
        association_verif = next((a for a in associations_verif if a["nom"] == nom_association), None)
        codes_vif_association = {
            _vif_fmt(m.get("Code VIF")) for m in (association_verif or {}).get("magasins", [])
        }
        if _vif_fmt(code) not in codes_vif_association:
            return jsonify({"success": False, "erreur": "Magasin non autorisé pour cette association"}), 403
    else:
        codes_produits_valides = {p["code"] for p in _lire_produits_modele()}
        if code not in codes_produits_valides:
            return jsonify({"success": False, "erreur": "Produit inconnu"}), 400

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        if type_champ == "magasin":
            code_vif = _vif_fmt(code)
            conn.execute("""
                INSERT INTO collecte_poids_magasins_gardee (annee, code_vif, nom_association, poids_kg, saisi_le, saisi_par)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(annee, code_vif, nom_association)
                DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
            """, (annee, code_vif, nom_association, poids_kg, maintenant, current_user.email))
        else:
            conn.execute("""
                INSERT INTO collecte_quantites_produits (annee, association, code_produit, poids_kg, saisi_le, saisi_par)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(annee, association, code_produit)
                DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
            """, (annee, nom_association, code, poids_kg, maintenant, current_user.email))
        conn.execute(
            "UPDATE collecte_detection_pdf_association SET statut = 'appliquee' "
            "WHERE annee = ? AND association = ? AND statut IN ('termine', 'erreur')",
            (annee, nom_association),
        )
        conn.commit()

    return jsonify({"success": True})


@collecte_bp.route("/collecte/gardee/saisie/importer-pdf", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_saisie_importer_pdf():
    """Upload d'un PDF reçu d'une association (fiche qu'on lui a envoyée,
    remplie à la main puis scannée), analysé par l'IA (arrière-plan) pour
    pré-remplir gardee_saisie — jamais enregistré directement, voir
    _detecter_pdf_association_background et _ensure_table_detection_pdf_association."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    nom_association = request.form.get("association", "").strip()

    if _resultat_gardee_verrouille(annee):
        flash(f"🔒 Résultat collecte gardée {annee} verrouillé — déverrouillez-le d'abord.", "danger")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    fichier = request.files.get("pdf")
    if not fichier or not fichier.filename:
        flash("⛔ Aucun PDF fourni", "warning")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    if (fichier.mimetype or "") != "application/pdf":
        flash("⛔ Format non supporté (PDF attendu)", "warning")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    pdf_bytes = fichier.read()
    if len(pdf_bytes) > 15 * 1024 * 1024:
        flash("⛔ Fichier trop volumineux (15 Mo max)", "warning")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    association = next((a for a in associations if a["nom"] == nom_association), None)
    if association is None:
        flash("⛔ Association introuvable.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    magasins_connus = [
        {"code_vif": _vif_fmt(m.get("Code VIF")), "nom": m.get("Nom", "")}
        for m in association["magasins"]
    ]
    produits_connus = _lire_produits_modele()

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_table_detection_pdf_association(conn)
        cur = conn.execute("""
            INSERT INTO collecte_detection_pdf_association (annee, association, statut, lance_le, lance_par, pdf_nom)
            VALUES (?, ?, 'en_cours', ?, ?, ?)
        """, (annee, nom_association, maintenant, current_user.email, fichier.filename))
        detection_id = cur.lastrowid
        conn.commit()

    # Fichier conservé sur disque pour historique (même principe que les
    # captures d'écran de sans_vendredi_detecter_image), pas relu ensuite.
    dossier_pdf = _dossier_gardee_pdf(annee)
    os.makedirs(dossier_pdf, exist_ok=True)
    with open(os.path.join(dossier_pdf, f"detection_{detection_id}.pdf"), "wb") as f:
        f.write(pdf_bytes)

    app_reel = current_app._get_current_object()
    lancer_tache_fond(
        target=_detecter_pdf_association_background,
        args=(app_reel, detection_id, annee, nom_association, pdf_bytes, magasins_connus, produits_connus),
        nom="Collecte gardée : detection PDF association",
        utilisateur=current_user.email,
    )
    write_log(f"🤖 Collecte gardée {annee} ({nom_association}) : détection PDF #{detection_id} lancée par {current_user.email}")
    flash("🤖 Analyse du PDF lancée (compter 15 à 30 secondes) — rechargez la page pour voir le résultat", "info")
    return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))


def _nom_association_dans_classeur(wb, nom_association):
    """Vérifie que le nom de l'association apparaît tel quel dans une
    cellule du classeur — _creer_fichier_association écrit ce nom en toutes
    lettres dans une cellule dédiée (B10 feuille 'produits', B11 feuille
    'magasins' du modèle actuel, mais on scanne tout le classeur pour rester
    robuste à une réorganisation du modèle) au moment de générer la fiche
    envoyée à CETTE association précise. Sert à détecter un fichier déposé
    par erreur sur la page d'une autre association : les codes magasins
    sont déjà filtrés par association (donc sans risque), mais les codes
    produits sont communs à toutes et seraient sinon importés sans la
    moindre alerte même venant du mauvais fichier."""
    cible = _normaliser_nom_association(nom_association)
    if not cible:
        return True
    for ws in wb.worksheets:
        for ligne in ws.iter_rows():
            for cellule in ligne:
                if isinstance(cellule.value, str) and _normaliser_nom_association(cellule.value) == cible:
                    return True
    return False


def _lire_resultats_excel_association(fichier_excel, association):
    """Lit un classeur Excel reçu d'une association (même structure que
    _creer_fichier_association, le modèle envoyé par mail : feuilles
    'magasins' et 'produits', code en colonne A, poids en colonne
    C — repéré par le code présent sur la ligne, pas par un numéro de ligne
    fixe, pour tolérer les lignes insérées/déplacées) — contrairement à
    l'import PDF, pas besoin de l'IA : la structure est déjà directement
    exploitable. Retourne (magasins_detectes, produits_detectes,
    association_confirmee), ne retient que les lignes dont le code
    correspond à un magasin RÉEL de cette association ou à un produit du
    modèle (même garde-fou que l'import PDF : jamais de code inventé ou
    d'une autre association) ; association_confirmee (voir
    _nom_association_dans_classeur) signale en plus si CE fichier semble
    bien concerner cette association précise."""
    wb = load_workbook(fichier_excel, data_only=True)
    codes_vif_association = {_vif_fmt(m.get("Code VIF")) for m in association["magasins"]}
    codes_produits_valides = {p["code"] for p in _lire_produits_modele()}
    association_confirmee = _nom_association_dans_classeur(wb, association["nom"])

    magasins_detectes = {}
    if "magasins" in wb.sheetnames:
        for ligne in wb["magasins"].iter_rows():
            if len(ligne) < 3 or not ligne[0].value:
                continue
            code = _vif_fmt(ligne[0].value)
            valeur = ligne[2].value
            if code in codes_vif_association and isinstance(valeur, (int, float)) and valeur >= 0:
                magasins_detectes[code] = round(valeur, 2)

    produits_detectes = {}
    if "produits" in wb.sheetnames:
        for ligne in wb["produits"].iter_rows():
            if len(ligne) < 3 or not ligne[0].value:
                continue
            code = str(ligne[0].value).strip()
            valeur = ligne[2].value
            if code in codes_produits_valides and isinstance(valeur, (int, float)) and valeur >= 0:
                produits_detectes[code] = round(valeur, 2)

    return magasins_detectes, produits_detectes, association_confirmee


@collecte_bp.route("/collecte/gardee/saisie/importer-excel", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_saisie_importer_excel():
    """Upload d'un Excel reçu d'une association (fiche qu'on lui a envoyée,
    remplie puis renvoyée) — lu directement (cf.
    _lire_resultats_excel_association, pas besoin de l'IA contrairement au
    PDF scanné), pré-remplit gardee_saisie pour relecture avant
    enregistrement — jamais écrit directement dans
    collecte_poids_magasins_gardee / collecte_quantites_produits (même
    garde-fou que l'import PDF, table de détection partagée)."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    nom_association = request.form.get("association", "").strip()

    if _resultat_gardee_verrouille(annee):
        flash(f"🔒 Résultat collecte gardée {annee} verrouillé — déverrouillez-le d'abord.", "danger")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    fichier = request.files.get("excel")
    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier Excel fourni", "warning")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    extension = os.path.splitext(fichier.filename)[1].lower()
    if extension not in (".xlsx", ".xlsm"):
        flash("⛔ Format non supporté (Excel .xlsx attendu)", "warning")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    fichier_bytes = fichier.read()
    if len(fichier_bytes) > 15 * 1024 * 1024:
        flash("⛔ Fichier trop volumineux (15 Mo max)", "warning")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    association = next((a for a in associations if a["nom"] == nom_association), None)
    if association is None:
        flash("⛔ Association introuvable.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    try:
        magasins_detectes, produits_detectes, association_confirmee = _lire_resultats_excel_association(
            io.BytesIO(fichier_bytes), association
        )
    except Exception as e:
        flash(f"⛔ Impossible de lire ce fichier Excel : {e}", "danger")
        return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))

    avertissement = (
        None if association_confirmee else
        f"⚠️ Le nom de « {nom_association} » n'a pas été retrouvé dans ce fichier Excel — "
        "vérifiez qu'il s'agit bien du bon fichier avant d'enregistrer."
    )

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        _ensure_table_detection_pdf_association(conn)
        conn.execute("""
            INSERT INTO collecte_detection_pdf_association
                (annee, association, statut, magasins_detectes, produits_detectes, avertissement, lance_le, lance_par, pdf_nom)
            VALUES (?, ?, 'termine', ?, ?, ?, ?, ?, ?)
        """, (annee, nom_association, json.dumps(magasins_detectes, ensure_ascii=False),
              json.dumps(produits_detectes, ensure_ascii=False), avertissement,
              maintenant, current_user.email, fichier.filename))
        conn.commit()

    write_log(
        f"📊 Collecte gardée {annee} ({nom_association}) : import Excel "
        f"({len(magasins_detectes)} magasin(s), {len(produits_detectes)} produit(s)"
        f"{', association non confirmée' if not association_confirmee else ''}) par {current_user.email}"
    )
    if avertissement:
        flash(avertissement, "warning")
    flash(
        f"✅ Fichier Excel lu : {len(magasins_detectes)} magasin(s), {len(produits_detectes)} produit(s) "
        f"détecté(s) — vérifiez puis enregistrez.", "success",
    )
    return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))


@collecte_bp.route("/collecte/gardee/saisie/detection/<int:detection_id>/ignorer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_saisie_detection_ignorer(detection_id):
    """Écarte une détection PDF sans l'appliquer (valeurs détectées jugées
    inutilisables) — les champs de gardee_saisie reviennent aux valeurs déjà
    enregistrées."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    nom_association = request.form.get("association", "").strip()
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE collecte_detection_pdf_association SET statut = 'ignoree' WHERE id = ? AND annee = ? AND association = ?",
            (detection_id, annee, nom_association),
        )
        conn.commit()
    flash("Valeurs détectées écartées.", "secondary")
    return redirect(url_for("collecte.gardee_saisie", annee=annee, association=nom_association))


def _envoyer_mail_association(asso, annee, dossier, destinataires, mode_test, libelle_supplementaire=""):
    """Génère (si besoin) les fichiers Excel/PDF de l'association et lui
    envoie le mail — factorisé pour être réutilisé par l'envoi initial
    (à toutes) et par la relance individuelle (à une seule). Le libellé
    supplémentaire (ex. « Rappel — en attente de résultats »), saisi
    librement sur la page au moment d'une relance, est ajouté à la fin
    de l'objet du mail ---jamais utilisé pour l'envoi initial, seul
    _envoyer_mail_association appelé depuis gardee_relancer en passe un
    (demande utilisateur 2026-11-18)."""
    fichier_association = os.path.join(dossier, _nom_fichier_association(asso["nom"], annee))
    if not os.path.exists(fichier_association):
        fichier_association = _creer_fichier_association(asso, annee, dossier)
    fichier_pdf = os.path.splitext(fichier_association)[0] + ".pdf"
    if not os.path.exists(fichier_pdf):
        fichier_pdf = _creer_pdf_association(fichier_association, asso, annee, dossier)
    token_saisie = generer_token_saisie_association(annee, asso["nom"])
    lien_saisie = url_for("collecte.saisie_association", annee=annee, token=token_saisie, _external=True)
    sujet = ("[TEST] " if mode_test else "") + f"Collecte nationale Banque Alimentaire {annee} — {asso['nom']}"
    if libelle_supplementaire:
        sujet += f" — {libelle_supplementaire}"
    return envoyer_mail(
        sujet=sujet,
        destinataires=destinataires,
        texte=_texte_modele_gardee(asso, annee, lien_saisie=lien_saisie),
        is_html=True,
        sender_override="ba380.collecte@banquealimentaire.org",
        cc=None if mode_test else ["ba380.collecte@banquealimentaire.org"],
        attachment_path=fichier_association,
        attachment_paths=[fichier_pdf],
    )


def _chemin_gardee_produits_annee_precedente(annee):
    """Fichier Excel « bilan produits » annuel des associations gardant,
    importé depuis la page (bouton dédié) dans le dossier partagé dev/prod
    MODELES_GARDEE_DIR — convention de nommage
    associationgardantproduitsAAAA.xlsx où AAAA est l'année des résultats
    qu'il contient. Relocalisé le 2026-11-19 depuis /srv/ba38/uploads/
    (dossier générique, vidé par erreur le 2026-11-18, perte du fichier
    2025)."""
    return os.path.join(MODELES_GARDEE_DIR, f"associationgardantproduits{annee - 1}.xlsx")


def _lire_totaux_gardee_depuis_fichier(chemin, annee_resultats):
    """Lit les totaux par association (clé code VIF + repli nom normalisé)
    dans l'onglet « detail » du fichier donné — factorisé entre
    _totaux_gardee_annee_precedente (fichier déjà en place) et la
    validation d'un fichier tout juste importé depuis la page (cf.
    gardee_produits_annee_precedente_importer).
    Retourne (totaux, erreur) — erreur=None si tout est allé bien.

    Le total par association est lu directement sur la ligne « Total AAAA »
    du fichier, valeur mise en cache par Excel (même si c'est le résultat
    d'une formule côté fichier) — PAS recalculé en sommant les lignes
    produit : demande explicite utilisateur 2026-11-16 suite à un total
    recalculé faux ("il faut prendre la ligne 30 total 2025 même si c'est
    une somme"), le total du fichier n'est pas une simple somme des lignes
    visibles de l'onglet detail. Le repérage de cette ligne reste
    volontairement tolérant (nom d'onglet insensible à la casse/espaces,
    ligne reconnue par "commence par total" + l'année en sous-chaîne
    plutôt qu'une égalité stricte) : c'est ce repérage, pas la lecture de
    la valeur, qui avait échoué lors de l'audit du même jour."""
    vide = {"code_vif": {}, "nom": {}}
    try:
        wb = load_workbook(chemin, data_only=True)
    except Exception as erreur:
        return vide, f"fichier illisible ({erreur})"
    nom_feuille = next((n for n in wb.sheetnames if n.strip().lower() == "detail"), None)
    if nom_feuille is None:
        return vide, f"onglet « detail » introuvable (onglets présents : {', '.join(wb.sheetnames)})"
    ws = wb[nom_feuille]
    cible = str(annee_resultats)
    ligne_total = None
    for r in range(1, ws.max_row + 1):
        valeur = str(ws.cell(row=r, column=1).value or "").strip().lower()
        if valeur.startswith("total") and cible in valeur:
            ligne_total = r
            break
    if ligne_total is None:
        return vide, f"ligne « Total {annee_resultats} » introuvable dans l'onglet « {nom_feuille} »"
    par_code_vif, par_nom = {}, {}
    for c in range(8, ws.max_column + 1):
        code = ws.cell(row=2, column=c).value
        nom = ws.cell(row=3, column=c).value
        total = ws.cell(row=ligne_total, column=c).value
        if not code or not nom or total is None:
            continue
        par_code_vif[_vif_fmt(code)] = float(total)
        par_nom[_cle_recherche_association(str(nom))] = float(total)
    if not par_code_vif:
        return vide, (
            f"ligne « Total {annee_resultats} » trouvée (ligne {ligne_total}) mais aucune colonne "
            "association valide (code + nom + total non vide, à partir de la colonne H) n'a pu être lue"
        )
    return {"code_vif": par_code_vif, "nom": par_nom}, None


def _totaux_gardee_annee_precedente(annee):
    """Total par association de l'année précédente (cf.
    _lire_totaux_gardee_depuis_fichier), seulement le total, pas le détail
    par produit (demande utilisateur 2026-11-14 : afficher ce total en
    repère dans Quantités par produit et Saisie collecte gardée, sans
    réimporter tout le détail). Retourne {"code_vif": {...}, "nom": {...}}
    — le nom normalisé sert de repli quand le Code VIF d'une association a
    changé d'une année sur l'autre (ça arrive, cf.
    ecart_code_vif/_code_vif_association)."""
    chemin = _chemin_gardee_produits_annee_precedente(annee)
    if not os.path.exists(chemin):
        return {"code_vif": {}, "nom": {}}
    try:
        totaux, erreur = _lire_totaux_gardee_depuis_fichier(chemin, annee - 1)
    except Exception as erreur_inattendue:
        write_log(f"⚠️ Collecte {annee} : lecture totaux gardée {annee - 1} impossible ({erreur_inattendue})")
        return {"code_vif": {}, "nom": {}}
    if erreur:
        write_log(f"⚠️ Collecte {annee} : fichier repère {annee - 1} en place mais invalide ({erreur})")
    return totaux


def _info_fichier_gardee_annee_precedente(annee):
    """Statut du fichier repère pour l'UI (import depuis la page, cf.
    gardee_produits_annee_precedente_importer) — None si rien d'importé."""
    chemin = _chemin_gardee_produits_annee_precedente(annee)
    if not os.path.exists(chemin):
        return None
    totaux = _totaux_gardee_annee_precedente(annee)
    return {
        "nom": os.path.basename(chemin),
        "modifie_le": datetime.fromtimestamp(os.path.getmtime(chemin)).strftime("%Y-%m-%d %H:%M"),
        "nb_associations": len(totaux["code_vif"]),
    }


def _total_gardee_annee_precedente_association(totaux_n1, nom, code_vif):
    """Associe une association au total N-1 — code VIF en priorité, repli
    sur le nom normalisé (cf. _totaux_gardee_annee_precedente)."""
    total = totaux_n1["code_vif"].get(_vif_fmt(code_vif)) if code_vif else None
    if total is None:
        total = totaux_n1["nom"].get(_cle_recherche_association(nom))
    return total


@collecte_bp.route("/collecte/<int:annee>/gardee/produits-annee-precedente/importer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_produits_annee_precedente_importer(annee):
    """Dépôt du fichier repère « année précédente », depuis la section
    Paramètres de la page principale (déplacé depuis la page Saisie
    collecte gardée le 2026-11-17, demande utilisateur), à la place d'une
    copie manuelle sur le serveur (demande utilisateur 2026-11-16 : "ne
    plus partir du fichier uploadé [à la main] mais proposer un
    téléchargement [import] du fichier dans la page")."""
    fichier = request.files.get("fichier")
    if not fichier or not fichier.filename:
        flash("⛔ Aucun fichier fourni", "warning")
        return redirect(url_for("collecte.collecte_main", annee=annee))
    if os.path.splitext(fichier.filename)[1].lower() != ".xlsx":
        flash("⛔ Format attendu : Excel (.xlsx)", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    chemin = _chemin_gardee_produits_annee_precedente(annee)
    # Suffixe .tmp (plutôt que de garder .xlsx à la fin) : openpyxl refuse
    # d'ouvrir un fichier dont le nom ne se termine pas par une extension
    # Excel reconnue, peu importe son contenu réel (bug trouvé 2026-11-16).
    chemin_temporaire = os.path.join(os.path.dirname(chemin), "_import_" + os.path.basename(chemin))
    fichier.save(chemin_temporaire)
    totaux, erreur = _lire_totaux_gardee_depuis_fichier(chemin_temporaire, annee - 1)
    if erreur:
        os.remove(chemin_temporaire)
        flash(f"⛔ Fichier invalide — {erreur}, rien n'a été importé.", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))
    os.replace(chemin_temporaire, chemin)
    write_log(
        f"📥 Collecte {annee} : fichier repère {annee - 1} importé par {current_user.email} "
        f"({len(totaux['code_vif'])} association(s))"
    )
    flash(f"✅ Fichier importé — {len(totaux['code_vif'])} association(s) {annee - 1} trouvée(s).", "success")
    return redirect(url_for("collecte.collecte_main", annee=annee))


def _associations_gardee_enrichies(annee):
    """Reconstruit la liste des associations gardant leur collecte, avec
    emails et Code VIF — factorisé entre gardee_envoi et les routes de
    suivi/relance qui ont besoin des mêmes données."""
    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    df_participants = _lire_participants(annee)
    if df_mag.empty or df_groupes.empty:
        return None
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    index_code_vif = _table_code_vif_associations()
    email_overrides = _email_overrides()
    for asso in associations:
        asso["referents"] = _referents_association(asso["nom"], df_participants)
        asso["email_force"] = email_overrides.get(_cle_recherche_association(asso["nom"]), "")
        if asso["email_force"]:
            asso["emails"] = _emails_magasin(asso["email_force"])
        else:
            asso["emails"] = sorted({r["email"] for r in asso["referents"] if "@" in r["email"]})
        asso["code_vif"], asso["ecart_code_vif"], asso["code_vif_force"] = _code_vif_association(asso["nom"], index_code_vif)
    return associations


def _associations_ayant_repondu_en_ligne(annee):
    """Noms des associations ayant saisi au moins un champ (poids magasin ou
    quantité produit) via le formulaire public saisie_association (lien
    envoyé par mail, sans compte) plutôt que par saisie interne — ces
    saisies sont enregistrées avec saisi_par = "association:<nom>" (cf.
    saisie_association, saisie_association_enregistrer_champ), contre
    l'adresse email du compte connecté pour une saisie interne. nom_association
    identifie directement l'association sur collecte_poids_magasins_gardee
    depuis que sa clé inclut cette colonne (cf.
    _ensure_table_poids_magasins_gardee) — plus besoin de retrouver
    l'association par code_vif, ce qui aurait pu attribuer à tort une saisie
    en ligne à une association qui partage le même magasin."""
    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        noms = {
            r["nom_association"] for r in conn.execute(
                "SELECT DISTINCT nom_association FROM collecte_poids_magasins_gardee "
                "WHERE annee = ? AND saisi_par LIKE 'association:%'", (annee,)
            ).fetchall()
        }
        noms |= {
            r["association"] for r in conn.execute(
                "SELECT DISTINCT association FROM collecte_quantites_produits "
                "WHERE annee = ? AND saisi_par LIKE 'association:%'", (annee,)
            ).fetchall()
        }
    return noms


def _associations_gardee_avec_suivi(annee):
    """Associations gardant enrichies (cf. _associations_gardee_enrichies)
    + suivi manuel (répondu/résultat envoyé), avancement de la saisie en
    ligne et statut Mailjet du dernier envoi — factorisé entre gardee_envoi
    (qui y ajoute la régénération Excel et l'UI d'envoi) et
    gardee_saisie_choisir (page allégée dupliquée dans Résultat collecte
    gardée pour choisir rapidement une association à saisir, sans
    régénérer les fichiers ni afficher les contrôles d'envoi — demande
    utilisateur 2026-11-10 : "simplifier la saisie et le contrôle").

    Coche aussi automatiquement « Résultat envoyé » (si pas déjà coché) pour
    toute association ayant répondu via le lien public ET dont la saisie
    est désormais complète (même calcul que le badge vert, cf.
    _noms_associations_completes) — une association qui saisit elle-même
    ses résultats en ligne, jusqu'au bout, n'a jamais besoin qu'on coche
    cette case à la main (demande utilisateur 2026-11-17, ex. EQUILIBRE)."""
    associations = _associations_gardee_enrichies(annee)
    if associations is None:
        return None
    suivi = _lire_suivi_gardee(annee)
    produits = _lire_produits_modele()
    with get_db_connection() as conn:
        _ensure_table_poids_magasins_gardee(conn)
        poids_magasins = {}
        for r in conn.execute(
            "SELECT nom_association, code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ?", (annee,)
        ).fetchall():
            poids_magasins.setdefault(r["nom_association"], {})[r["code_vif"]] = r["poids_kg"]
        poids_produits = {}
        for r in conn.execute(
            "SELECT association, code_produit, poids_kg FROM collecte_quantites_produits WHERE annee = ?", (annee,)
        ).fetchall():
            poids_produits.setdefault(r["association"], {})[r["code_produit"]] = r["poids_kg"]
    statuts = _statuts_envois_mail_collecte(annee, "gardee_envoi")
    totaux_n1 = _totaux_gardee_annee_precedente(annee)
    repondu_en_ligne = _associations_ayant_repondu_en_ligne(annee)
    verrou = _resultat_gardee_verrouille(annee)
    for asso in associations:
        asso["suivi"] = suivi.get(asso["nom"], {
            "repondu": False, "resultat_envoye": False, "traite": False,
            "poids_exporte": False, "transferts_exportes": False, "bl_genere": False,
            "saisie_en_ligne_terminee": False,
        })
        asso["avancement_saisie"] = _saisie_association_avancement(
            annee, asso["nom"], asso["magasins"], produits, poids_magasins, poids_produits
        )
        remplis, total = asso["avancement_saisie"]
        if (
            not verrou and total and remplis == total
            and asso["nom"] in repondu_en_ligne
            and not asso["suivi"]["resultat_envoye"]
        ):
            _marquer_suivi_gardee(annee, asso["nom"], "resultat_envoye", True, f"association:{asso['nom']}")
            asso["suivi"]["resultat_envoye"] = True
        premier_email = asso["emails"][0] if asso.get("emails") else None
        asso["statut_mailjet"] = statuts.get(premier_email) if premier_email else None
        # Poids total collecté = somme des poids par magasin saisis (la
        # fiche "poids par magasin" est la mesure directe du tonnage ;
        # "quantités par produit" n'en est qu'une répartition détaillée,
        # pas une 2e source à additionner — demande utilisateur 2026-11-10).
        asso["poids_total"] = sum(
            poids_magasins.get(asso["nom"], {})[_vif_fmt(m.get("Code VIF"))]
            for m in asso["magasins"]
            if poids_magasins.get(asso["nom"], {}).get(_vif_fmt(m.get("Code VIF"))) is not None
        )
        asso["poids_total_n1"] = _total_gardee_annee_precedente_association(
            totaux_n1, asso["nom"], asso.get("code_vif")
        )
    return associations


@collecte_bp.route("/collecte/gardee/envoi", methods=["GET", "POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_envoi():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    dossier = _dossier_annee(annee)
    chemin = os.path.join(dossier, f"associations_gardant_{annee}.xlsx")

    if request.method == "GET":
        # Régénération systématique à l'ouverture de la page : plus besoin
        # d'un bouton « Créer »/« Régénérer » séparé, la liste et les
        # fiches par association reflètent toujours go-on-web à l'instant
        # où on prépare l'envoi (~9s, acceptée sciemment par l'utilisateur
        # contre le risque d'oublier de régénérer).
        ok, erreur = _generer_fichier_excel_gardee(annee)
        if not ok:
            flash(erreur, "danger")
            return redirect(url_for("collecte.gardee", annee=annee))

    associations = _associations_gardee_enrichies(annee)
    if associations is None:
        flash("❌ Fichier(s) source(s) manquant(s)", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    if request.method == "POST":
        if request.form.get("confirmation") != "oui":
            flash("❌ Confirmez le contrôle du fichier avant l'envoi", "danger")
            return redirect(url_for("collecte.gardee_envoi", annee=annee))
        mode_test = request.form.get("mode_test") == "on"
        test_une_association = request.form.get("test_une_association") == "on"
        nom_test = request.form.get("association_test_nom", "")
        associations_a_traiter = associations
        if test_une_association:
            associations_a_traiter = [a for a in associations if a["nom"] == nom_test]
            if not associations_a_traiter:
                flash("❌ Sélectionnez une association pour le test", "danger")
                return redirect(url_for("collecte.gardee_envoi", annee=annee))
        if mode_test and not getattr(current_user, "email", ""):
            flash("❌ Votre compte n’a pas d’adresse email pour le test", "danger")
            return redirect(url_for("collecte.gardee_envoi", annee=annee))
        envoyes = 0
        sans_email = []
        echecs = []
        for asso in associations_a_traiter:
            if not asso["emails"] and not mode_test:
                sans_email.append(asso["nom"])
                continue
            destinataires = [current_user.email] if mode_test else asso["emails"]
            try:
                resultat = _envoyer_mail_association(asso, annee, dossier, destinataires, mode_test)
                _enregistrer_envoi_mail_collecte(annee, "gardee_envoi", destinataires[0], asso["nom"], resultat=resultat)
            except Exception as e:
                echecs.append(asso["nom"])
                write_log(f"❌ Collecte {annee} : échec envoi associations gardant à {asso['nom']} — {e}")
                _enregistrer_envoi_mail_collecte(annee, "gardee_envoi", destinataires[0], asso["nom"], erreur=str(e))
                continue
            envoyes += 1

        portee = "pour une association" if test_une_association else "pour toutes les associations"
        message = f"✅ {envoyes} mail(s) {'de test ' if mode_test else ''}envoyé(s) {portee} avec les fichiers Excel personnalisés"
        if echecs:
            message += f" — ⛔ {len(echecs)} échec(s) : {', '.join(echecs)}"
        if sans_email:
            message += f" ; sans adresse : {', '.join(sans_email)}"
        flash(message, "success" if not (sans_email or echecs) else "warning")
        write_log(
            f"📧 Envoi Associations gardant {annee} ({portee}{' réel' if test_une_association and not mode_test else ''}) : "
            f"{envoyes} envoyé(s) par {current_user.email}"
        )
        return redirect(url_for("collecte.gardee", annee=annee))

    associations = _associations_gardee_avec_suivi(annee)
    associations = sorted(associations, key=lambda a: a["nom"])
    total_magasins = sum(len(a["magasins"]) for a in associations)
    total_poids = sum(a["poids_total"] for a in associations)
    total_poids_n1 = sum(a["poids_total_n1"] for a in associations if a["poids_total_n1"] is not None)
    en_attente_vif = {
        etape: sorted(_noms_associations_gardee_en_attente_confirmation(annee, etape))
        for etape in ETAPES_VIF_GARDEE
    }

    return render_template(
        "collecte/gardee_envoi.html",
        annee=annee,
        annee_precedente=annee - 1,
        associations=associations,
        total_magasins=total_magasins,
        total_poids=total_poids,
        total_poids_n1=total_poids_n1,
        info_fichier_n1=_info_fichier_gardee_annee_precedente(annee),
        verrou_gardee=_resultat_gardee_verrouille(annee),
        fichier_nom=os.path.basename(chemin),
        texte_mail=_lire_texte_mail_gardee(),
        url_drive_modele_association=_lire_url_drive_modele_association(),
        en_attente_vif=en_attente_vif,
        feature="gardee_envoi",
    )


@collecte_bp.route("/collecte/gardee/envoi/suivi", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_marquer_suivi():
    """Coche/décoche à la main l'une des cases de suivi pour une
    association. « repondu » et « resultat_envoye » : vérification faite
    par l'équipe hors application (mail reçu, fichier reçu, saisie en
    ligne consultée), jamais automatique. « poids_exporte »,
    « transferts_exportes », « bl_genere » : depuis le 2026-11-24, se
    cochent normalement via le bouton « Confirmer l'import VIF » de
    chaque étape (cf. confirmer_vif_gardee), pas automatiquement à la
    génération du CSV (ancien comportement à l'origine d'un BL généré
    pour des associations jamais réellement importées dans VIF, stocks
    négatifs) — modifiables ici à la main seulement pour corriger une
    erreur ou forcer un nouvel export. « traite » retiré du tableau et de
    cette liste le 2026-11-23 (redondant avec les 3 cases dédiées, devenu
    source de confusion — colonne conservée en base mais plus modifiable
    depuis l'écran)."""
    payload = request.get_json(silent=True) or {}
    annee = payload.get("annee")
    nom_association = payload.get("association", "")
    champ = payload.get("champ", "")
    valeur = bool(payload.get("valeur"))
    champs_valides = (
        "repondu", "resultat_envoye",
        "poids_exporte", "transferts_exportes", "bl_genere",
    )
    if champ not in champs_valides or not annee or not nom_association:
        return jsonify({"success": False, "erreur": "Paramètres invalides"}), 400
    _marquer_suivi_gardee(annee, nom_association, champ, valeur, current_user.email)
    return jsonify({"success": True})


@collecte_bp.route("/collecte/<int:annee>/gardee/confirmer-vif/<etape>", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def confirmer_vif_gardee(annee, etape):
    """Confirme que le CSV généré pour UNE étape (poids_exporte /
    transferts_exportes / bl_genere) a bien été importé dans VIF et que
    tout est OK — coche la case de suivi pour chaque association en
    attente de cette confirmation (ce qui débloque l'étape suivante) et
    vide la file d'attente (cf. _confirmer_vif_gardee,
    _mettre_en_attente_confirmation_vif). Tant que cette confirmation
    n'est pas faite, l'association reste bloquée à cette étape : évite
    qu'un CSV généré « pour test » ou jamais réellement importé dans VIF
    ne débloque l'étape suivante (incident réel 2026-11-24, BL généré
    pour 5 associations dont le transfert n'avait en fait jamais été
    importé, causant des stocks VIF négatifs)."""
    if etape not in ETAPES_VIF_GARDEE:
        flash("❌ Étape invalide", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))
    noms = _confirmer_vif_gardee(annee, etape, current_user.email)
    if noms:
        flash(f"✅ Import VIF confirmé pour {len(noms)} association(s) : {', '.join(noms)}", "success")
        write_log(
            f"✅ Confirmation import VIF ({etape}) {annee} : {len(noms)} association(s) "
            f"({', '.join(noms)}) par {current_user.email}"
        )
    else:
        flash("⚠️ Aucune association en attente de confirmation pour cette étape.", "warning")
    return redirect(request.referrer or url_for("collecte.gardee_envoi", annee=annee))


@collecte_bp.route("/collecte/gardee/envoi/relancer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_relancer():
    """Renvoie le mail (avec les mêmes fichiers Excel/PDF) à une ou
    plusieurs associations — relance individuelle (une association) ou
    groupée (toutes celles où la case correspondante n'est pas cochée).
    Pour une relance groupée, action=test envoie un seul mail d'exemple
    (la 1ère association ciblée) à l'adresse du compte connecté au lieu
    d'un envoi réel (demande utilisateur 2026-11-18)."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    dossier = _dossier_annee(annee)
    associations = _associations_gardee_enrichies(annee)
    if associations is None:
        flash("❌ Fichier(s) source(s) manquant(s)", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    nom_association = request.form.get("association", "")
    groupe = request.form.get("groupe", "")
    action = request.form.get("action", "relancer")
    libelle_supplementaire = request.form.get("libelle_supplementaire", "").strip()
    if nom_association:
        cible = [a for a in associations if a["nom"] == nom_association]
        libelle_portee = f"« {nom_association} »"
    elif groupe in ("repondu", "resultat_envoye"):
        # Relance GROUPÉE (potentiellement toutes les associations) : jamais
        # de mode test sur l'envoi RÉEL de cette route (mode_test=False plus
        # bas, en dur) — seule une confirm() JS protégeait l'envoi de masse,
        # contournable par un POST direct (audit 2026-10-28). La relance
        # individuelle ci-dessus reste protégée par la seule confirm() JS,
        # un envoi ciblé à une seule association étant d'une tout autre
        # ampleur. Le bouton « 🧪 Tester sur mon adresse » (action=test,
        # demande utilisateur 2026-11-18) n'est, lui, jamais concerné par
        # cette confirmation : mode_test=True plus bas force de toute façon
        # la redirection vers l'adresse du compte connecté dans envoyer_mail,
        # aucun risque d'envoi réel.
        if action != "test" and request.form.get("confirmation_envoi") != "oui":
            flash("❌ Cochez d'abord « confirmer » — rien n'a été envoyé.", "danger")
            return redirect(url_for("collecte.gardee_envoi", annee=annee))
        suivi = _lire_suivi_gardee(annee)
        cible = [a for a in associations if not suivi.get(a["nom"], {}).get(groupe)]
        libelle_portee = "sans réponse" if groupe == "repondu" else "sans résultat envoyé"
    else:
        flash("❌ Rien à relancer — précisez une association ou un groupe", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    if action == "test":
        if not cible:
            flash(f"⚠️ Aucune association {libelle_portee} à tester.", "warning")
            return redirect(url_for("collecte.gardee_envoi", annee=annee))
        adresse_test = getattr(current_user, "email", "") or ""
        if not adresse_test:
            flash("❌ Votre compte n'a pas d'adresse email pour le test.", "danger")
            return redirect(url_for("collecte.gardee_envoi", annee=annee))
        exemple = cible[0]
        try:
            _envoyer_mail_association(
                exemple, annee, dossier, [adresse_test], mode_test=True,
                libelle_supplementaire=libelle_supplementaire,
            )
        except Exception as e:
            flash(f"⛔ Échec de l'envoi test : {e}", "danger")
            return redirect(url_for("collecte.gardee_envoi", annee=annee))
        flash(
            f"✅ Mail de test envoyé à {adresse_test} (exemple : « {exemple['nom']} », "
            f"{len(cible)} association(s) {libelle_portee} au total).",
            "success",
        )
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    envoyes = 0
    sans_email = []
    echecs = []
    for asso in cible:
        if not asso["emails"]:
            sans_email.append(asso["nom"])
            continue
        try:
            resultat = _envoyer_mail_association(
                asso, annee, dossier, asso["emails"], mode_test=False,
                libelle_supplementaire=libelle_supplementaire,
            )
            _enregistrer_envoi_mail_collecte(annee, "gardee_envoi", asso["emails"][0], asso["nom"], resultat=resultat)
        except Exception as e:
            echecs.append(asso["nom"])
            write_log(f"❌ Collecte {annee} : échec relance associations gardant à {asso['nom']} — {e}")
            _enregistrer_envoi_mail_collecte(annee, "gardee_envoi", asso["emails"][0], asso["nom"], erreur=str(e))
            continue
        envoyes += 1

    message = f"✅ Relance envoyée à {envoyes} association(s) {libelle_portee}"
    if echecs:
        message += f" — ⛔ {len(echecs)} échec(s) : {', '.join(echecs)}"
    if sans_email:
        message += f" ; sans adresse (non relancées) : {', '.join(sans_email)}"
    flash(message, "success" if not (sans_email or echecs) else "warning")
    suffixe_log = f", libellé « {libelle_supplementaire} »" if libelle_supplementaire else ""
    write_log(f"🔁 Relance Associations gardant {annee} ({libelle_portee}{suffixe_log}) : {envoyes} envoyé(s) par {current_user.email}")
    return redirect(url_for("collecte.gardee_envoi", annee=annee))


@collecte_bp.route("/collecte/gardee/envoi/texte", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_texte_mail_gardee():
    """Enregistre le texte du mail envoyé aux associations gardant leur
    collecte, dans le fichier partagé (cf. _ecrire_texte_mail_gardee) —
    même principe que le texte du mail chauffeurs/équipiers ou de la
    demande d'autorisation, plus de modèle Word à redéposer."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    texte = request.form.get("texte_mail", "").strip()
    if texte:
        _ecrire_texte_mail_gardee(texte)
        flash("✅ Texte du mail enregistré.", "success")
    else:
        flash("⛔ Le texte ne peut pas être vide.", "warning")
    return redirect(url_for("collecte.gardee_envoi", annee=annee))


@collecte_bp.route("/collecte/gardee/envoi/apercu")
@login_required
@require_access("collecte", "lecture")
def gardee_envoi_apercu():
    """Affiche directement dans le navigateur (pas d'envoi, même de test)
    le texte exact du mail pour l'association sélectionnée — même principe
    que les aperçus PDF/mail des autres envois du module."""
    annee = request.args.get("annee", type=int) or datetime.now().year
    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if df_mag.empty or df_groupes.empty:
        abort(404)
    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)
    associations, _ = _construire_associations(df_mag, df_groupes)
    if not associations:
        abort(404)
    index_code_vif = _table_code_vif_associations()
    nom = request.args.get("association", "")
    asso = next((a for a in associations if a["nom"] == nom), associations[0])
    asso["code_vif"], _, _ = _code_vif_association(asso["nom"], index_code_vif)
    lien_saisie = url_for("collecte.saisie_association", annee=annee,
                           token=generer_token_saisie_association(annee, asso["nom"]), _external=True)
    texte = _texte_modele_gardee(asso, annee, lien_saisie=lien_saisie)
    return Response(texte, mimetype="text/html; charset=utf-8")
