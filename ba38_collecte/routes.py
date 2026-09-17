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
import shutil
import re
import csv
import glob
import json
import copy
import sqlite3
import zipfile
import argparse
import subprocess
from io import StringIO
from datetime import datetime, timedelta
from threading import Thread

from werkzeug.utils import secure_filename

import anthropic
import markdown
import requests
import pandas as pd
from docx import Document
from openpyxl import load_workbook
from openpyxl.drawing.image import Image as ImageOpenpyxl
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
)
from ba38_utilitaires.organisation import get_organisation
from ba38_collecte import collecte_bp
from ba38_collecte import moteur_tournees as moteur
from ba38_collecte import moteur_carte_secteurs as carte_secteurs

EXTENSIONS_EXCEL = {".xlsx", ".xls"}
EXTENSIONS_PDF = {".pdf"}
MODELES_GARDEE_DIR = "/srv/ba38/uploads/collecte_fichiers_source"

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
    "magasins":  {"champ": "drive_magasins",  "label": "Liste des magasins"},
    "vehicules": {"champ": "drive_vehicules", "label": "Liste des véhicules / planning"},
    "cagettes":  {"champ": "drive_cagettes",  "label": "Historique cagettes"},
    "groupes":   {"champ": "drive_groupes",   "label": "Liste des groupes"},
    "participants": {"champ": "drive_participants", "label": "Liste des participants"},
    "participants_mailing": {"champ": "drive_participants_mailing", "label": "Liste des participants pour mailing"},
}
DRIVE_CHAMPS_PRODUCTION = {cle: DRIVE_CHAMPS[cle] for cle in ("magasins", "vehicules", "cagettes")}


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
    Drive reconnaissable."""
    fid = _id_drive(url)
    return f"https://docs.google.com/spreadsheets/d/{fid}/export?format=xlsx" if fid else None


def _fichier_drive(annee, cle):
    """Télécharge un export Google Sheets et retourne son chemin local."""
    conf = DRIVE_CHAMPS[cle]
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    url = _url_export_drive(campagne[conf["champ"]]) if campagne else None
    if not url:
        return None
    dossier = _dossier_annee(annee)
    os.makedirs(dossier, exist_ok=True)
    noms_stockage = {"vehicules": "liste_vehicules.xlsx"}
    nom_stockage = FICHIERS.get(cle, {}).get("nom_stockage") or noms_stockage[cle]
    chemin = os.path.join(dossier, nom_stockage)
    reponse = requests.get(url, timeout=30)
    reponse.raise_for_status()
    if not reponse.content.startswith(b"PK"):
        raise ValueError(f"contenu invalide pour « {conf['label']} »")
    with open(chemin, "wb") as fichier:
        fichier.write(reponse.content)
    return chemin


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
        if not code or code == "nan" or not personne or personne == "nan" or not jour:
            continue
        if not _est_camion_reel(code):
            continue
        match = re.match(r"(\d+)", debut)
        periode = "Matin" if not match or int(match.group(1)) < 13 else "Après-midi"
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
        if not code or code == "nan" or not jour or not _est_camion_reel(code):
            continue
        code_vif_brut = ligne.get("Code VIF")
        if code_vif_brut is None or (isinstance(code_vif_brut, float) and pd.isna(code_vif_brut)):
            continue
        code_vif = _vif_fmt(code_vif_brut)
        if not code_vif:
            continue
        match = re.match(r"(\d+)", debut)
        periode = "Matin" if not match or int(match.group(1)) < 13 else "Apres Midi"
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

PRODUCTION_FICHIERS_SORTIE = {
    "excel":     {"nom": "Tournees_BAI38_{annee}_GOTW.xlsx", "label": "Classeur Excel (tournées + contrôles)"},
    "fiches":    {"nom": "fiches_jour_vehicule_magasin_{annee}_GOTW.pdf", "label": "Fiches de collecte"},
    "pointage":  {"nom": "pointage_vehicules_{annee}_GOTW.pdf", "label": "Pointage véhicules"},
    "equipier":  {"nom": "fiches_jour_vehicule_magasin_equipier_{annee}_GOTW.pdf", "label": "Fiches équipier"},
    "index":     {"nom": "fiches_equipier_jour_vehicule_{annee}_GOTW.pdf", "label": "Index alphabétique équipiers"},
    "consignes": {"nom": "vehicule_consignes.xlsx", "label": "Consignes véhicules (1 ligne/camion)"},
    "carte":     {"nom": "carte_tournees_production.html", "label": "Carte interactive des tournées"},
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


def _generer_tournees(campagne, params):
    """Exécute le pipeline complet (extraction PDF + magasins + optimisation
    + export Excel) via collecte_moteur_tournees, tel qu'orchestré par
    main() dans le script d'origine. Retourne le nom du fichier Excel généré
    et quelques compteurs pour l'affichage/la BDD."""
    annee = campagne["annee"]
    dossier = _dossier_annee(annee)
    pdf_path = os.path.join(dossier, campagne["fichier_pdf_precedent"])
    magasins_path = _fichier_drive(annee, "magasins") or os.path.join(dossier, campagne["fichier_magasins"])

    args_ns = argparse.Namespace(
        camions_supp=params["camions_supp"],
        poids_nouveaux=params["poids_nouveaux"],
        max_magasins=params["max_magasins"],
        corriger_mal_places=params["corriger_mal_places"],
        fusionner_legeres=params["fusionner_legeres"],
        optimiser_anciens=params["optimiser_anciens"],
        output=None,
        pdf=pdf_path,
        magasins=magasins_path,
        nouveaux=None,
        annee=annee,
    )

    fiches = moteur.extraire_pdf(pdf_path)

    vifs_pdf_2025 = set()
    for f in fiches:
        for v in f["vif_codes"]:
            vifs_pdf_2025.add(str(v).lstrip("0"))

    df_mag = moteur.lire_magasins(magasins_path, vifs_pdf_2025, params["poids_nouveaux"], annee)
    df_t = moteur.optimiser_tournees(fiches, df_mag, args_ns)

    dossier_resultats = _dossier_resultats(annee)
    os.makedirs(dossier_resultats, exist_ok=True)
    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    nom_fichier = _nom_fichier_genere(annee, params, horodatage)
    args_ns.output = os.path.join(dossier_resultats, nom_fichier)

    moteur.generer_excel(df_t, df_mag, args_ns, args_ns.output, fiches, moteur.non_affectes_global, "")

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

    return {
        "nom_fichier": nom_fichier,
        "nom_carte_secteurs": nom_carte_secteurs,
        "nom_carte_tournees": nom_carte_tournees,
        "nb_tournees": len(df_t),
        "nb_magasins": len(df_mag),
        "nb_nouveaux_magasins": int(df_mag["Nouveau"].sum()),
    }


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
            "camion": str(row["Camion"]),
            "nom_camion": "" if pd.isna(row.get("Nom camion")) else str(row["Nom camion"]),
            "tonnage": 0.0 if pd.isna(row.get("Tonnage")) else float(row["Tonnage"]),
            "km": "" if pd.isna(row.get("Km estimes")) else float(row["Km estimes"]),
            "duree": "" if pd.isna(row.get("Duree estimee")) else str(row["Duree estimee"]),
            "secteur": "" if pd.isna(row.get("Secteur")) else str(row["Secteur"]),
            "magasins": ", ".join(magasins),
            "commentaire": "" if pd.isna(row.get("Commentaire optimisation")) else str(row["Commentaire optimisation"]),
            "figee": bool(row["Camion"] in moteur.VEHICULES_FIGES),
        })
    return lignes


def _lire_referentiel_magasins_bai(annee, campagne):
    """Relit le fichier magasins de l'année (Drive ou upload) et renvoie la
    liste des magasins du périmètre bai (État='Collecté par la BAI', +
    'Collecte gardée' avec Stockage BAI+, cf. moteur.lire_magasins) avec leurs
    demi-journées d'ouverture (colonne Créneaux, cf. moteur.parse_creneaux).
    Reflète l'état courant du fichier — utilisé uniquement pour (ré)initialiser
    la liste figée de collecte_cagettes_magasins, jamais directement par la
    page de saisie (qui doit rester stable même si le fichier est remplacé en
    cours de campagne)."""
    chemin = _fichier_drive(annee, "magasins") or os.path.join(_dossier_annee(annee), campagne["fichier_magasins"])

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
    try:
        chemin = _fichier_drive(annee, "magasins") or os.path.join(_dossier_annee(annee), FICHIERS["magasins"]["nom_stockage"])
    except Exception as erreur:
        write_log(f"⚠️ Lecture Drive magasins {annee} impossible : {erreur}")
        chemin = os.path.join(_dossier_annee(annee), FICHIERS["magasins"]["nom_stockage"])
    if not os.path.exists(chemin):
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

        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        envoyes = 0
        for magasin in magasins_a_traiter:
            fichier_lettre = _creer_pdf_autorisation(magasin, annee, dossier)
            destinataires = [current_user.email] if mode_test else magasin["emails"]
            envoyer_mail(
                sujet=("[TEST] " if mode_test else "") + f"Collecte nationale Banque Alimentaire {annee} — Demande d'autorisation — {magasin['nom']}",
                destinataires=destinataires,
                texte=texte_mail_modele.replace("<<Nom>>", magasin["nom"]),
                sender_override=os.getenv("MAILJET_SENDER"),
                cc=None if mode_test else ["ba380.collecte@banquealimentaire.org"],
                attachment_path=fichier_lettre,
            )
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

        flash(f"✅ {envoyes} demande(s) d'autorisation {'de test ' if mode_test else ''}envoyée(s)", "success")
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
    for magasin in magasins:
        magasin["deja_envoye"] = magasin["code_vif"] in deja_envoyes

    image_produits_url = url_for("collecte.autorisations_image") if _chemin_image_produits_autorisation() else None

    return render_template(
        "collecte/autorisations.html",
        annee=annee,
        magasins=magasins,
        texte_lettre=(campagne["autorisation_texte_lettre"] if campagne else None) or AUTORISATION_TEXTE_LETTRE_DEFAUT,
        texte_coupon=(campagne["autorisation_texte_coupon"] if campagne else None) or AUTORISATION_TEXTE_COUPON_DEFAUT,
        texte_mail=(campagne["autorisation_texte_mail"] if campagne else None) or AUTORISATION_TEXTE_MAIL_DEFAUT,
        image_produits_url=image_produits_url,
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
    chemin = _fichier_drive(annee, "magasins") or os.path.join(_dossier_annee(annee), campagne["fichier_magasins"])

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
    grille) vs applicable mais pas encore saisie (None)."""
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

    lignes = []
    for magasin in magasins:
        code_vif = magasin["code_vif"]
        ligne = {"code_vif": code_vif, "nom_magasin": magasin["nom_magasin"], "total": 0}
        for dj in moteur.DEMI_JOURNEES:
            applicable = (code_vif, dj) in applicables
            valeur = saisies.get((code_vif, dj)) if applicable else None
            ligne[dj] = valeur
            ligne["_appl_" + dj] = applicable
            if valeur:
                ligne["total"] += valeur
        lignes.append(ligne)
    return lignes


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

            resultats = []
            for camions_supp, max_magasins in SCENARIOS_ANALYSE:
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
                df_t = moteur.optimiser_tournees(copy.deepcopy(fiches), df_mag.copy(), args_ns)
                resultats.append(_calculer_indicateurs_scenario(df_t, camions_supp, max_magasins))
                write_log(f"🔬 Analyse 8 scénarios #{analyse_id} : VX{camions_supp}-MAX{max_magasins} terminé")

            conn = sqlite3.connect(db_path)
            conn.execute("""
                UPDATE collecte_analyses SET statut = 'termine', resultat = ?, termine_le = ?
                WHERE id = ?
            """, (json.dumps(resultats), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), analyse_id))
            conn.commit()
            conn.close()
            write_log(f"✅ Analyse 8 scénarios #{analyse_id} terminée")

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
    "N" pour un titre "9.N ..." (dernier chiffre après le point), ou sous
    son texte exact sinon. Utilisé pour répartir la section « 9. Règles
    fonctionnelles détaillées » (numérotée 9.1 à 9.5) vers les 5 mêmes
    clés que le reste du manuel, plutôt que de rester isolée sous la clé
    "9" où aucune modale ne l'afficherait."""
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
            correspondance = re.match(r"9\.(\d+)", element.text_content().strip())
            cle_courante = correspondance.group(1) if correspondance else element.text_content().strip()
            elements_courants = [element]
        else:
            elements_courants.append(element)
    sections[cle_courante] = serialiser(elements_courants)
    return sections


def _sections_aide_collecte():
    """Aide utilisateur du module Collecte, découpée par sujet (clés "1" à
    "5", une par section de la page d'accueil) — la clé "intro" regroupe la
    présentation générale, l'accès, la légende des couleurs et les bonnes
    pratiques (§6), pour n'afficher qu'une présentation générale sur la
    page d'accueil et l'aide propre à chaque sujet dans ce sujet."""
    chemin = os.path.join(
        current_app.root_path, "templates", "docsHtml", DOCUMENTS_AIDE_COLLECTE["aide"]["html"]
    )
    brut = _decouper_html_par_h1(chemin)
    return {
        "intro": brut.get("intro", "") + brut.get("6", ""),
        "1": brut.get("1", ""),
        "2": brut.get("2", ""),
        "3": brut.get("3", ""),
        "4": brut.get("4", ""),
        "5": brut.get("5", ""),
    }


def _sections_manuel_collecte():
    """Manuel d'utilisation détaillé, découpé de la même façon que l'aide
    (cf. _sections_aide_collecte) — la clé "intro" regroupe ici
    l'introduction générale, le déroulé type d'une campagne (§6) et le
    glossaire (§8) ; les bonnes pratiques (§7) ne sont pas reprises ici
    (déjà dans l'aide générale, pour éviter la redite). La section
    « 9. Règles fonctionnelles détaillées » (numérotée 9.1 à 9.5) est
    répartie dans les mêmes clés "1" à "5" que le reste du manuel — sans
    quoi elle resterait isolée sous la clé "9", jamais affichée nulle
    part (cf. _decouper_html_par_h2)."""
    chemin = os.path.join(
        current_app.root_path, "templates", "docsHtml", DOCUMENTS_AIDE_COLLECTE["manuel"]["html"]
    )
    brut = _decouper_html_par_h1(chemin)
    regles = _decouper_html_par_h2(brut.get("9", ""))
    return {
        "intro": brut.get("Introduction", "") + brut.get("6", "") + brut.get("8", ""),
        "1": brut.get("1", "") + regles.get("1", ""),
        "2": brut.get("2", "") + regles.get("2", ""),
        "3": brut.get("3", "") + regles.get("3", ""),
        "4": brut.get("4", "") + regles.get("4", ""),
        "5": brut.get("5", "") + regles.get("5", ""),
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

    return render_template(
        "collecte/index.html",
        annee=annee,
        campagne=campagne,
        annees_existantes=annees_existantes,
        fichiers=FICHIERS,
        derniere_generation=generations[0] if generations else None,
        nb_generations=len(generations),
        derniere_analyse=derniere_analyse,
        aide=_sections_aide_collecte(),
        manuel=_sections_manuel_collecte(),
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
            for personne in destinataires:
                envoyer_mail(
                    f"Vos affectations tournées {annee}",
                    [personne["email"]],
                    _corps_mail_affectations_html(personne, mail_texte, image_url),
                    sender_override="ba380.directeur@banquealimentaire.org",
                    attachment_paths=chemins_pj or None,
                    is_html=True,
                )
            flash(f"✅ {len(destinataires)} mail(s) préparé(s).", "success")

    return render_template(
        "collecte/chauffeurs_equipiers.html",
        annee=annee,
        destinataires=destinataires,
        personnes_sans_email=personnes_sans_email,
        mail_texte=mail_texte,
        image_url=image_url,
        pieces_jointes=pieces_jointes,
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
        existante = conn.execute(
            "SELECT id FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

        if existante:
            conn.execute(
                "UPDATE collecte_campagnes SET drive_magasins = ?, drive_vehicules = ?, "
                 "drive_cagettes = ?, drive_groupes = ?, drive_participants = ?, "
                 "drive_participants_mailing = ? WHERE annee = ?",
                (valeurs["drive_magasins"], valeurs["drive_vehicules"], valeurs["drive_cagettes"],
                  valeurs["drive_groupes"], valeurs["drive_participants"],
                  valeurs["drive_participants_mailing"], annee)
            )
        else:
            maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT INTO collecte_campagnes
                    (annee, drive_magasins, drive_vehicules, drive_cagettes, drive_groupes,
                                         drive_participants, drive_participants_mailing, date_creation, cree_par)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (annee, valeurs["drive_magasins"], valeurs["drive_vehicules"], valeurs["drive_cagettes"],
                                    valeurs["drive_groupes"], valeurs["drive_participants"],
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

    if not campagne or not (campagne["fichier_magasins"] or campagne["drive_magasins"]) or not campagne["fichier_pdf_precedent"]:
        flash(
            "⛔ Liste des magasins et PDF des tournées précédentes requis avant de générer "
            f"les tournées {annee}",
            "danger"
        )
        return None
    return campagne


@collecte_bp.route("/collecte/analyse", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def lancer_analyse():
    annee = request.form.get("annee", type=int)

    campagne = _get_campagne_ou_redirect(annee)
    if campagne is None:
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

    dossier = _dossier_annee(annee)
    pdf_path = os.path.join(dossier, campagne["fichier_pdf_precedent"])
    magasins_path = _fichier_drive(annee, "magasins") or os.path.join(dossier, campagne["fichier_magasins"])
    app_reel = current_app._get_current_object()

    Thread(
        target=_lancer_analyse_8configs_background,
        args=(app_reel, analyse_id, pdf_path, magasins_path, params_communs, annee)
    ).start()

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
    Thread(target=_generer_redaction_background, args=(app_reel, analyse_id, prompt)).start()

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
    if campagne and (campagne["fichier_magasins"] or campagne["drive_magasins"]):
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


@collecte_bp.route("/collecte/<int:annee>/cagettes")
@login_required
@require_access("collecte", "lecture")
def cagettes(annee):
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

    if not campagne or not (campagne["fichier_magasins"] or campagne["drive_magasins"]):
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
    )


# Constantes fixes du format d'import VIF « Saisie des cagettes par magasin »
# (réception marchandise) — colonnes et valeurs figées communiquées par
# l'utilisateur, pas de logique métier derrière, juste le gabarit attendu.
CAGETTES_EXPORT_SOCIETE = "01"
CAGETTES_EXPORT_ETAB = "38"
CAGETTES_EXPORT_LIEU = "01"
CAGETTES_EXPORT_DEPOT = "05"
CAGETTES_EXPORT_ARTICLE = "5010000"
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

    magasins_zero = [ligne["nom_magasin"] for ligne in lignes if not (ligne.get("total") or 0)]
    if magasins_zero and not forcer:
        flash(
            f"⛔ Export impossible : {len(magasins_zero)} magasin(s) sans aucune cagette saisie — "
            f"{', '.join(magasins_zero)}",
            "danger",
        )
        return redirect(url_for("collecte.cagettes", annee=annee))

    if magasins_zero and forcer:
        write_log(f"🧪 Export cagettes {annee} FORCÉ (dev) malgré {len(magasins_zero)} magasin(s) à zéro par {current_user.email}")

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
    return redirect(url_for("collecte.cagettes", annee=annee))


@collecte_bp.route("/collecte/<int:annee>/cagettes/initialiser", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def initialiser_cagettes(annee):
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()

    if not campagne or not (campagne["fichier_magasins"] or campagne["drive_magasins"]):
        flash(f"⛔ Liste des magasins {annee} requise avant d'initialiser la saisie cagettes", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    magasins = _lire_referentiel_magasins_bai(annee, campagne)
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


@collecte_bp.route("/collecte/resultats/<int:generation_id>/pdf")
@login_required
@require_access("collecte", "lecture")
def resultats_pdf(generation_id):
    generation = _get_generation_ou_redirect(generation_id)
    if generation is None:
        return redirect(url_for("collecte.collecte_main"))

    lignes = _charger_tournees(generation)
    html = render_template(
        "collecte/resultats_pdf.html", annee=generation["annee"], lignes=lignes,
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
        if not (campagne and _url_export_drive(campagne[conf["champ"]]))
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

    # Date du jeudi + liens Drive : renseignés une fois pour toutes sur la
    # page principale du module (un nouveau dossier/jeu de 3 liens est créé
    # par le club chaque année), plus besoin de les ressaisir ici.
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM collecte_campagnes WHERE annee = ?", (annee,)
        ).fetchone()
    date_jeudi = _date_fr_courte(campagne["date_debut"]) if campagne else None

    urls_drive = {}
    liens_manquants = []
    for cle, conf in DRIVE_CHAMPS_PRODUCTION.items():
        url = _url_export_drive(campagne[conf["champ"]]) if campagne else None
        if url:
            urls_drive[cle] = url
        else:
            liens_manquants.append(conf["label"])
    if liens_manquants:
        flash(
            f"❌ Lien(s) Drive non configuré(s) pour {annee} : {', '.join(liens_manquants)} — "
            f"à renseigner sur la page principale du module (section « Fichiers Drive »)",
            "danger"
        )
        return redirect(url_for("collecte.production", annee=annee))

    dossier = _dossier_production(annee)
    os.makedirs(dossier, exist_ok=True)

    # Téléchargement des 3 fichiers depuis le drive (partagés "Toute personne
    # disposant du lien") : la page production doit toujours refléter le
    # dernier état du planning go-on-web, pas un import ponctuel.
    try:
        for cle, url in urls_drive.items():
            reponse = requests.get(url, timeout=30)
            reponse.raise_for_status()
            if not reponse.content.startswith(b"PK"):
                raise ValueError(
                    f"contenu invalide pour « {cle} » — vérifier que le fichier est "
                    f"bien partagé en \"Toute personne disposant du lien\""
                )
            with open(os.path.join(dossier, f"{cle}.xlsx"), "wb") as f:
                f.write(reponse.content)
    except Exception as e:
        flash(f"❌ Échec du téléchargement des fichiers depuis le drive : {e}", "danger")
        write_log(f"❌ Génération documents production {annee} : échec téléchargement drive ({e})")
        return redirect(url_for("collecte.production", annee=annee))

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

        resultat = subprocess.run(cmd, capture_output=True, text=True)
        sortie = (resultat.stdout or "") + "\n" + (resultat.stderr or "")

        if resultat.returncode != 0 or not os.path.exists(chemin_fiche):
            flash(f"❌ Échec de la génération de la fiche du camion {camion}", "danger")
            write_log(
                f"❌ Fiche camion {camion} ({annee}) en échec par {current_user.email}\n{sortie[-4000:]}"
            )
            return redirect(url_for("collecte.production", annee=annee))

        write_log(f"📄 Fiche camion {camion} ({annee}) générée par {current_user.email}")
        return send_file(chemin_fiche, mimetype="application/pdf", as_attachment=False)

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
        "--annee", str(annee),
        "--output-excel", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["excel"]["nom"].format(annee=annee)),
        "--output-fiches", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["fiches"]["nom"].format(annee=annee)),
        "--output-pointage", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["pointage"]["nom"].format(annee=annee)),
        "--output-equipier", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["equipier"]["nom"].format(annee=annee)),
        "--output-index", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["index"]["nom"].format(annee=annee)),
        "--output-vehicule-consignes", os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["consignes"]["nom"]),
    ]
    if date_jeudi:
        cmd += ["--date-jeudi", date_jeudi]

    resultat = subprocess.run(cmd, capture_output=True, text=True)
    sortie = (resultat.stdout or "") + "\n" + (resultat.stderr or "")

    with open(os.path.join(dossier, "dernier_journal.log"), "w", encoding="utf-8") as f:
        f.write(sortie)

    # Nom fixe pour la carte (générée sous un nom horodaté par le script) afin
    # d'offrir un lien de téléchargement stable.
    cartes = sorted(glob.glob(os.path.join(dossier, "carte_tournees_bai38_*.html")))
    if cartes:
        os.replace(cartes[-1], os.path.join(dossier, PRODUCTION_FICHIERS_SORTIE["carte"]["nom"]))

    if resultat.returncode != 0:
        flash("❌ Échec de la génération — voir le journal ci-dessous", "danger")
        write_log(
            f"❌ Génération documents production {annee} en échec par {current_user.email}\n"
            f"{sortie[-4000:]}"
        )
    else:
        flash("✅ Documents générés avec succès", "success")
        write_log(f"📄 Génération documents production {annee} par {current_user.email}")

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
    """Magasins État='Collecte gardée' du référentiel de l'année (DataFrame
    vide si liste_magasins.xlsx est absent)."""
    try:
        chemin = _fichier_drive(annee, "magasins") or os.path.join(_dossier_annee(annee), FICHIERS["magasins"]["nom_stockage"])
    except Exception as erreur:
        write_log(f"⚠️ Lecture Drive magasins {annee} impossible : {erreur}")
        chemin = os.path.join(_dossier_annee(annee), FICHIERS["magasins"]["nom_stockage"])
    if not os.path.exists(chemin):
        return pd.DataFrame()
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    if "État" not in df.columns:
        return pd.DataFrame()
    return df[df["État"].astype(str).str.strip() == "Collecte gardée"].reset_index(drop=True)


def _lire_groupes(annee):
    """Tous les groupes (associations) go-on-web de l'année, colonnes
    normalisées (DataFrame vide si liste_groupes.xlsx est absent). Pas
    filtré sur 'Nombre magasins' : cette colonne, calculée côté go-on-web,
    accuse parfois un retard par rapport à la colonne 'Gardée par' du
    référentiel magasins (ex. une association vient de reprendre un magasin
    mais son compteur n'est pas encore remonté) — s'y fier pour décider
    qu'une association « n'existe pas » ferait disparaître des associations
    bien réelles de la liste."""
    try:
        chemin = _fichier_drive(annee, "groupes") or os.path.join(_dossier_annee(annee), FICHIERS["groupes"]["nom_stockage"])
    except Exception as erreur:
        write_log(f"⚠️ Lecture Drive groupes {annee} impossible : {erreur}")
        chemin = os.path.join(_dossier_annee(annee), FICHIERS["groupes"]["nom_stockage"])
    if not os.path.exists(chemin):
        return pd.DataFrame()
    df = pd.read_excel(chemin)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _lire_participants(annee):
    """Tous les participants/contacts go-on-web de l'année (toutes années de
    collecte confondues dans l'export), colonnes normalisées (DataFrame vide
    si liste_participants.xlsx est absent)."""
    try:
        chemin = _fichier_drive(annee, "participants") or os.path.join(_dossier_annee(annee), FICHIERS["participants"]["nom_stockage"])
    except Exception as erreur:
        write_log(f"⚠️ Lecture Drive participants {annee} impossible : {erreur}")
        chemin = os.path.join(_dossier_annee(annee), FICHIERS["participants"]["nom_stockage"])
    if not os.path.exists(chemin):
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


def _lire_suivi_gardee(annee):
    """{nom_association: {'repondu': bool, 'resultat_envoye': bool}}"""
    with get_db_connection() as conn:
        _ensure_table_suivi_gardee(conn)
        rows = conn.execute(
            "SELECT nom_association, repondu, resultat_envoye FROM collecte_gardee_suivi WHERE annee = ?",
            (annee,),
        ).fetchall()
    return {
        r["nom_association"]: {"repondu": bool(r["repondu"]), "resultat_envoye": bool(r["resultat_envoye"])}
        for r in rows
    }


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


def _saisie_association_avancement(annee, nom_association, magasins, produits, poids_magasins, poids_produits):
    """Indice automatique (nb rempli / nb attendu) de la saisie en ligne
    pour une association — affiché en aide à la décision à côté des cases
    à cocher manuelles, jamais utilisé pour les cocher automatiquement
    (cf. _ensure_table_suivi_gardee)."""
    codes_magasins = [_vif_fmt(m.get("Code VIF")) for m in magasins]
    remplis_magasins = sum(1 for c in codes_magasins if poids_magasins.get(c) is not None)
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
    en tête."""
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
        })
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
                if cell.value == 2025:
                    cell.value = annee
                if isinstance(cell.value, str):
                    cell.value = cell.value.replace("àassociation", asso["nom"])
                    cell.value = re.sub(r"àcode(?!\d)", code, cell.value)
                    cell.value = cell.value.replace("àdatecollecte", date_collecte)
                    cell.value = cell.value.replace("àannéecollecte", str(annee))
                    cell.value = cell.value.replace("2025", str(annee))

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


def _creer_pdf_association(fichier_excel, asso, annee, dossier):
    """Crée directement les deux fiches PDF avec la présentation du modèle Excel."""
    wb = load_workbook(fichier_excel, data_only=False)
    nom = os.path.splitext(os.path.basename(fichier_excel))[0] + ".pdf"
    chemin = os.path.join(dossier, nom)
    page_width, page_height = A4
    pdf = pdf_canvas.Canvas(chemin, pagesize=A4)
    logo = os.path.join(current_app.root_path, "static", "images", "logo_ba_complet.png")

    def texte(valeur):
        return str(valeur or "").replace("\n", " ")

    def entete(ws, titre):
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
        nom_asso = asso["nom"] + (f" (code {code_vif})" if code_vif else "")
        pdf.drawString(24 * mm, y - 19 * mm, nom_asso)
        return y - 25 * mm

    def grille(lignes, largeurs, x, y, hauteur, gras_premiere=False):
        total = sum(largeurs)
        for index, ligne in enumerate(lignes):
            yy = y - index * hauteur
            pdf.setFont("Helvetica-Bold" if gras_premiere and index == 0 else "Helvetica", 6 if index else 6.5)
            xx = x
            for col, largeur in enumerate(largeurs):
                pdf.rect(xx, yy - hauteur, largeur, hauteur)
                pdf.drawString(xx + 1.2 * mm, yy - hauteur + 2.2 * mm, texte(ligne[col])[:38])
                xx += largeur
        return y - len(lignes) * hauteur

    ws = wb["produits"]
    y = entete(ws, "FICHE PRODUITS")
    derniere_ligne_produits = _derniere_ligne_produits(ws)
    lignes = [[ws.cell(14, c).value for c in range(1, 6)]]
    lignes.extend([[ws.cell(r, c).value for c in range(1, 6)] for r in range(16, derniere_ligne_produits + 2) if any(ws.cell(r, c).value is not None for c in range(1, 6))])
    lignes.append(["", "Total", "", "", ""])
    grille(lignes, [25 * mm, 46 * mm, 22 * mm, 62 * mm, 22 * mm], 10 * mm, y, 6.2 * mm, True)
    pdf.showPage()

    ws = wb["magasins"]
    y = entete(ws, "FICHE MAGASINS")
    lignes = [[ws.cell(13, c).value for c in range(1, 9)]]
    lignes.extend([[ws.cell(r, c).value for c in range(1, 9)] for r in range(15, 24) if ws.cell(r, 1).value or ws.cell(r, 2).value])
    lignes.append(["", "Total", "", "", "", "", "", ""])
    grille(lignes, [18 * mm, 39 * mm, 20 * mm, 20 * mm, 20 * mm, 22 * mm, 20 * mm, 22 * mm], 5 * mm, y, 8 * mm, True)
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


def _lire_texte_mail_gardee():
    """Texte du mail envoyé aux associations gardant leur collecte, dans un
    fichier texte PARTAGÉ (comme l'image et les pièces jointes du mail
    chauffeurs/équipiers) — modifiable en ligne, sans passer par un modèle
    Word, et disponible d'une campagne et d'une session à l'autre sans
    redépôt annuel."""
    if os.path.exists(CHEMIN_TEXTE_MAIL_GARDEE):
        with open(CHEMIN_TEXTE_MAIL_GARDEE, "r", encoding="utf-8") as f:
            return f.read()
    return GARDEE_TEXTE_MAIL_DEFAUT


def _ecrire_texte_mail_gardee(texte):
    with open(CHEMIN_TEXTE_MAIL_GARDEE, "w", encoding="utf-8") as f:
        f.write(texte)


def _texte_modele_gardee(asso, annee, lien_saisie=None):
    """Repères <<association>>, <<code>>, <<datecollecte>>, <<magasins>> et
    <<lien_saisie>> — ce dernier substitué seulement si présent dans le
    texte, sinon ajouté en dernier paragraphe (compatibilité avec un texte
    personnalisé plus ancien qui ne l'aurait pas encore)."""
    magasins = "\n".join(
        f"- {_vif_fmt(m.get('Code VIF'))} — {m.get('Nom', '')} ({m.get('Ville', '')})"
        for m in asso["magasins"]
    )
    texte = _lire_texte_mail_gardee()
    texte = texte.replace("<<association>>", asso["nom"])
    texte = texte.replace("<<code>>", asso.get("code_vif") or "")
    texte = texte.replace("<<datecollecte>>", _date_collecte(annee))
    texte = texte.replace("<<magasins>>", magasins)

    if "<<lien_saisie>>" in texte:
        texte = texte.replace("<<lien_saisie>>", lien_saisie or "")
    elif lien_saisie:
        texte = texte.rstrip() + (
            "\n\nVous pouvez aussi saisir directement en ligne le poids par magasin et la répartition "
            f"par produit, sans avoir besoin de renvoyer les fichiers joints : {lien_saisie}"
        )

    return texte.strip()


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
            nom_cle = str(grp_sec.get("Nom")).strip() if grp_sec is not None else secondaire
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
            "nb_contacts": int(grp.get("Nombre contacts")) if grp is not None and pd.notna(grp.get("Nombre contacts")) else 0,
            "nb_leaders": int(grp.get("Nombre leaders")) if grp is not None and pd.notna(grp.get("Nombre leaders")) else 0,
            "fiche_groupe": _valeur_propre(grp.get("Fiche groupe")) if grp is not None else "",
            "trouvee": grp is not None,
            "magasins": magasins_grp[["Code VIF", "Nom", "Ville", "Stockage", "Contact", "Email"]].to_dict("records"),
        })

    sans_association = df_mag[gardee_par == ""][["Code VIF", "Nom", "Ville", "Stockage"]].to_dict("records")
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
        return redirect(url_for("collecte.gardee", annee=request.args.get("annee", type=int)))
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
        return redirect(url_for("collecte.gardee", annee=annee))

    if not fichier.filename.lower().endswith(".xlsx"):
        flash("⛔ Format non accepté (.xlsx uniquement).", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    tampon = io.BytesIO(fichier.read())
    try:
        wb = load_workbook(tampon)
    except Exception:
        flash("⛔ Fichier Excel invalide ou corrompu — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    manquantes = [feuille for feuille in ("produits", "magasins") if feuille not in wb.sheetnames]
    if manquantes:
        flash(f"⛔ Feuille(s) manquante(s) dans le fichier déposé : {', '.join(manquantes)} — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

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
    return redirect(url_for("collecte.gardee", annee=annee))


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
    return redirect(url_for("collecte.gardee", annee=annee))


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
        return redirect(url_for("collecte.gardee", annee=annee))

    try:
        reponse = requests.get(url_export, timeout=30)
        reponse.raise_for_status()
    except Exception:
        flash("⛔ Échec du téléchargement depuis Google Sheets — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    if not reponse.content.startswith(b"PK"):
        flash("⛔ Contenu invalide reçu de Google Sheets — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    tampon = io.BytesIO(reponse.content)
    try:
        wb = load_workbook(tampon)
    except Exception:
        flash("⛔ Fichier reçu illisible par Excel — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    manquantes = [feuille for feuille in ("produits", "magasins") if feuille not in wb.sheetnames]
    if manquantes:
        flash(f"⛔ Feuille(s) manquante(s) dans la version Google Sheets : {', '.join(manquantes)} — rien n'a été remplacé.", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

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
    return redirect(url_for("collecte.gardee", annee=annee))


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
        for asso in associations:
            asso["referents"] = _referents_association(asso["nom"], df_participants)
            asso["code_vif"], asso["ecart_code_vif"], asso["code_vif_force"] = _code_vif_association(asso["nom"], index_code_vif)

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
        url_drive_modele_association=_lire_url_drive_modele_association(),
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

    return redirect(url_for("collecte.gardee", annee=annee))


@collecte_bp.route("/collecte/gardee/excel")
@login_required
@require_access("collecte", "lecture")
def gardee_excel():
    annee = request.args.get("annee", type=int) or datetime.now().year

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)

    if df_mag.empty or df_groupes.empty:
        flash("❌ Fichier(s) manquant(s) (liste des magasins et/ou des groupes)", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))

    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)

    associations, sans_association = _construire_associations(df_mag, df_groupes)
    df_participants = _lire_participants(annee)
    index_code_vif = _table_code_vif_associations()
    for asso in associations:
        asso["referents"] = _referents_association(asso["nom"], df_participants)
        asso["code_vif"], asso["ecart_code_vif"], asso["code_vif_force"] = _code_vif_association(asso["nom"], index_code_vif)

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
        "Fiche groupe": a["fiche_groupe"],
    } for a in associations])

    df_mag_flat = df_mag.copy()
    df_mag_flat["Association"] = df_mag_flat.get("Gardée par", "").map(_normaliser_gardee_par)
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
        flash(f"❌ Création PDF impossible : {erreur}", "danger")
        return redirect(url_for("collecte.gardee", annee=annee))
    with open(chemin, "wb") as fichier:
        fichier.write(buffer.getvalue())

    write_log(f"📥 Export Associations gardant {annee} par {current_user.email}")
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

@collecte_bp.route("/collecte/<int:annee>/poids-magasins-gardee")
@login_required
@require_access("collecte", "lecture")
def poids_magasins_gardee(annee):
    df_mag = _lire_magasins_gardes(annee)
    if df_mag.empty:
        flash(f"⛔ Liste des magasins gardés {annee} indisponible — importez d'abord le fichier magasins", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)

    with get_db_connection() as conn:
        saisies = {
            r["code_vif"]: r["poids_kg"]
            for r in conn.execute(
                "SELECT code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ?", (annee,)
            ).fetchall()
        }

    index_code_vif = _table_code_vif_associations()
    code_vif_par_association = {}

    lignes = []
    for _, row in df_mag.iterrows():
        code_vif = str(row.get("Code VIF", "")).strip()
        if not code_vif or code_vif.lower() == "nan":
            continue
        nom_association = _normaliser_gardee_par(row.get("Gardée par", ""))
        if nom_association not in code_vif_par_association:
            code_vif_asso, _, _ = _code_vif_association(nom_association, index_code_vif)
            code_vif_par_association[nom_association] = code_vif_asso
        lignes.append({
            "code_vif": code_vif,
            "nom_magasin": str(row.get("Nom", "")).strip(),
            "association": nom_association,
            "association_code_vif": code_vif_par_association[nom_association] or "",
            "poids_kg": saisies.get(code_vif),
        })
    lignes.sort(key=lambda l: (l["association"].lower(), l["nom_magasin"].lower()))

    return render_template(
        "collecte/poids_magasins_gardee.html",
        annee=annee,
        lignes=lignes,
    )


@collecte_bp.route("/collecte/poids-magasins-gardee/enregistrer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def enregistrer_poids_magasins_gardee():
    donnees = request.get_json(silent=True) or {}
    annee = donnees.get("annee")
    code_vif = str(donnees.get("code_vif", "")).strip()
    poids_brut = donnees.get("poids_kg")

    if not annee or not code_vif:
        return jsonify({"success": False, "erreur": "Paramètres manquants"}), 400

    try:
        poids_kg = float(poids_brut) if poids_brut not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO collecte_poids_magasins_gardee (annee, code_vif, poids_kg, saisi_le, saisi_par)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(annee, code_vif)
            DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
        """, (annee, code_vif, poids_kg, maintenant, current_user.email))
        conn.commit()

    return jsonify({"success": True})


@collecte_bp.route("/collecte/<int:annee>/poids-magasins-gardee/export")
@login_required
@require_access("collecte", "lecture")
def exporter_poids_magasins_gardee(annee):
    """Export CSV « Saisie des poids par magasin gardé » pour import VIF —
    même format que exporter_cagettes (une ligne par magasin), quantité =
    poids en kg brut saisi pour ce magasin."""
    df_mag = _lire_magasins_gardes(annee)
    if df_mag.empty:
        flash(f"⛔ Liste des magasins gardés {annee} indisponible", "danger")
        return redirect(url_for("collecte.poids_magasins_gardee", annee=annee))

    if "Code VIF" in df_mag.columns:
        df_mag["Code VIF"] = df_mag["Code VIF"].map(_vif_fmt)

    with get_db_connection() as conn:
        saisies = {
            r["code_vif"]: r["poids_kg"]
            for r in conn.execute(
                "SELECT code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ?", (annee,)
            ).fetchall()
        }

    date_reception = datetime.now().strftime("%d/%m/%Y")

    lignes = []
    magasins_manquants = []
    for _, row in df_mag.iterrows():
        code_vif = str(row.get("Code VIF", "")).strip()
        if not code_vif or code_vif.lower() == "nan":
            continue
        poids_kg = saisies.get(code_vif)
        if not poids_kg:
            magasins_manquants.append(str(row.get("Nom", "")).strip())
            continue
        lignes.append((code_vif, poids_kg))

    est_dev = os.getenv("ENVIRONMENT", "DEV").upper() != "PROD"
    forcer = est_dev and request.args.get("forcer") == "1"

    if magasins_manquants and not forcer:
        flash(
            f"⛔ Export impossible : {len(magasins_manquants)} magasin(s) sans poids saisi — "
            f"{', '.join(magasins_manquants)}",
            "danger",
        )
        return redirect(url_for("collecte.poids_magasins_gardee", annee=annee))

    if magasins_manquants and forcer:
        write_log(f"🧪 Export poids magasins gardée {annee} FORCÉ (dev) malgré {len(magasins_manquants)} magasin(s) sans poids par {current_user.email}")

    tampon = StringIO()
    ecrivain = csv.writer(tampon, delimiter=";")
    for code_vif, poids_kg in lignes:
        ecrivain.writerow([
            CAGETTES_EXPORT_SOCIETE,
            CAGETTES_EXPORT_ETAB,
            date_reception,
            code_vif,
            CAGETTES_EXPORT_LIEU,
            CAGETTES_EXPORT_DEPOT,
            CAGETTES_EXPORT_ARTICLE,
            round(poids_kg),
            CAGETTES_EXPORT_UNITE,
            "", "", "", "",
            CAGETTES_EXPORT_ORIGINE,
        ])

    write_log(f"📤 Export CSV poids magasins gardée {annee} par {current_user.email}")

    return Response(
        tampon.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=saisie_poids_magasins_gardee_{annee}.csv"},
    )


@collecte_bp.route("/collecte/<int:annee>/quantites-produits")
@login_required
@require_access("collecte", "lecture")
def quantites_produits(annee):
    produits = _lire_produits_modele()
    if not produits:
        flash("⛔ Liste des produits indisponible — modèle association introuvable", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))

    df_mag = _lire_magasins_gardes(annee)
    df_groupes = _lire_groupes(annee)
    if df_mag.empty:
        flash(f"⛔ Liste des magasins gardés {annee} indisponible — importez d'abord le fichier magasins", "danger")
        return redirect(url_for("collecte.collecte_main", annee=annee))
    associations, _ = _construire_associations(df_mag, df_groupes)
    noms_associations = sorted({a["nom"] for a in associations}, key=str.lower)
    index_code_vif = _table_code_vif_associations()
    code_vif_associations = {}
    for nom in noms_associations:
        code_vif_asso, _, _ = _code_vif_association(nom, index_code_vif)
        code_vif_associations[nom] = code_vif_asso or ""

    with get_db_connection() as conn:
        saisies = {
            (r["association"], r["code_produit"]): r["poids_kg"]
            for r in conn.execute(
                "SELECT association, code_produit, poids_kg FROM collecte_quantites_produits WHERE annee = ?", (annee,)
            ).fetchall()
        }

    lignes = []
    for produit in produits:
        ligne = {"code_produit": produit["code"], "libelle": produit["libelle"]}
        for nom in noms_associations:
            ligne[nom] = saisies.get((nom, produit["code"]))
        lignes.append(ligne)

    return render_template(
        "collecte/quantites_produits.html",
        annee=annee,
        lignes=lignes,
        associations=noms_associations,
        code_vif_associations=code_vif_associations,
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
        poids_kg = float(poids_brut) if poids_brut not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

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


# ============================================================================
# 📈 Évolution magasins (suivi pluriannuel, hors campagne annuelle)
# ============================================================================
FICHIER_EVOLUTION_MAGASINS = "/srv/ba38/uploads/collectes magasins evolution.xlsx"
FICHIER_REFERENTIEL_MAGASINS = "/srv/ba38/uploads/liste-magasins-réferentiel.xlsx"


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
    chemin = _fichier_drive(annee, "magasins") or (
        os.path.join(_dossier_annee(annee), campagne["fichier_magasins"]) if campagne["fichier_magasins"] else None
    )
    if not chemin or not os.path.exists(chemin):
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
            "nom": str(row["Nom"] or "").strip(),
            "etat": str(row["État"] or "").strip(),
            "adresse": str(row["Adresse"] or "").strip(),
            "ville": str(row["Ville"] or "").strip(),
            "code_postal": str(row["C.P."] or "").strip(),
            "telephone": str(row["Téléphone"] or "").strip(),
            "email": str(row["Email"] or "").strip(),
            "stockage": str(row["Stockage"] or "").strip(),
            "gardee_par": str(row["Gardée par"] or "").strip(),
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
        maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db_connection() as conn:
            for magasin in association["magasins"]:
                code_vif = _vif_fmt(magasin.get("Code VIF"))
                brut = request.form.get(f"poids_magasin_{code_vif}", "").strip().replace(",", ".")
                poids_kg = float(brut) if brut else None
                conn.execute("""
                    INSERT INTO collecte_poids_magasins_gardee (annee, code_vif, poids_kg, saisi_le, saisi_par)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(annee, code_vif)
                    DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
                """, (annee, code_vif, poids_kg, maintenant, f"association:{nom_association}"))
            for produit in produits:
                brut = request.form.get(f"poids_produit_{produit['code']}", "").strip().replace(",", ".")
                poids_kg = float(brut) if brut else None
                conn.execute("""
                    INSERT INTO collecte_quantites_produits (annee, association, code_produit, poids_kg, saisi_le, saisi_par)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(annee, association, code_produit)
                    DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
                """, (annee, nom_association, produit["code"], poids_kg, maintenant, f"association:{nom_association}"))
            conn.commit()
        write_log(f"⚖️ Saisie en ligne association « {nom_association} » {annee} enregistrée")
        flash("✅ Merci, votre saisie a bien été enregistrée.", "success")
        return redirect(url_for("collecte.saisie_association", annee=annee, token=token))

    with get_db_connection() as conn:
        poids_magasins = {
            r["code_vif"]: r["poids_kg"]
            for r in conn.execute(
                "SELECT code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ?", (annee,)
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
        poids_kg = float(poids_brut) if poids_brut not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"success": False, "erreur": "Poids invalide"}), 400

    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    saisi_par = f"association:{nom_association}"
    with get_db_connection() as conn:
        if type_champ == "magasin":
            code_vif = _vif_fmt(code)
            conn.execute("""
                INSERT INTO collecte_poids_magasins_gardee (annee, code_vif, poids_kg, saisi_le, saisi_par)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(annee, code_vif)
                DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
            """, (annee, code_vif, poids_kg, maintenant, saisi_par))
        else:
            conn.execute("""
                INSERT INTO collecte_quantites_produits (annee, association, code_produit, poids_kg, saisi_le, saisi_par)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(annee, association, code_produit)
                DO UPDATE SET poids_kg = excluded.poids_kg, saisi_le = excluded.saisi_le, saisi_par = excluded.saisi_par
            """, (annee, nom_association, code, poids_kg, maintenant, saisi_par))
        conn.commit()

    return jsonify({"success": True})


def _envoyer_mail_association(asso, annee, dossier, destinataires, mode_test):
    """Génère (si besoin) les fichiers Excel/PDF de l'association et lui
    envoie le mail — factorisé pour être réutilisé par l'envoi initial
    (à toutes) et par la relance individuelle (à une seule)."""
    fichier_association = os.path.join(dossier, _nom_fichier_association(asso["nom"], annee))
    if not os.path.exists(fichier_association):
        fichier_association = _creer_fichier_association(asso, annee, dossier)
    fichier_pdf = os.path.splitext(fichier_association)[0] + ".pdf"
    if not os.path.exists(fichier_pdf):
        fichier_pdf = _creer_pdf_association(fichier_association, asso, annee, dossier)
    token_saisie = generer_token_saisie_association(annee, asso["nom"])
    lien_saisie = url_for("collecte.saisie_association", annee=annee, token=token_saisie, _external=True)
    envoyer_mail(
        sujet=("[TEST] " if mode_test else "") + f"Collecte nationale Banque Alimentaire {annee} — {asso['nom']}",
        destinataires=destinataires,
        texte=_texte_modele_gardee(asso, annee, lien_saisie=lien_saisie),
        sender_override=os.getenv("MAILJET_SENDER"),
        cc=None if mode_test else ["ba380.collecte@banquealimentaire.org"],
        attachment_path=fichier_association,
        attachment_paths=[fichier_pdf],
    )


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
    for asso in associations:
        asso["referents"] = _referents_association(asso["nom"], df_participants)
        asso["emails"] = sorted({r["email"] for r in asso["referents"] if "@" in r["email"]})
        asso["code_vif"], asso["ecart_code_vif"], asso["code_vif_force"] = _code_vif_association(asso["nom"], index_code_vif)
    return associations


@collecte_bp.route("/collecte/gardee/envoi", methods=["GET", "POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_envoi():
    annee = request.args.get("annee", type=int) or request.form.get("annee", type=int) or datetime.now().year
    dossier = _dossier_annee(annee)
    chemin = os.path.join(dossier, f"associations_gardant_{annee}.xlsx")
    if not os.path.exists(chemin):
        flash("❌ Créez et contrôlez d'abord le fichier Excel", "danger")
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
        if test_une_association and not mode_test:
            flash("❌ L'option sur une seule association nécessite le mode Test", "danger")
            return redirect(url_for("collecte.gardee_envoi", annee=annee))
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
        for asso in associations_a_traiter:
            if not asso["emails"] and not mode_test:
                sans_email.append(asso["nom"])
                continue
            destinataires = [current_user.email] if mode_test else asso["emails"]
            _envoyer_mail_association(asso, annee, dossier, destinataires, mode_test)
            envoyes += 1

        portee = "pour une association" if test_une_association else "pour toutes les associations"
        message = f"✅ {envoyes} mail(s) {'de test ' if mode_test else ''}envoyé(s) {portee} avec les fichiers Excel personnalisés"
        if sans_email:
            message += f" ; sans adresse : {', '.join(sans_email)}"
        flash(message, "success" if not sans_email else "warning")
        write_log(f"📧 Envoi Associations gardant {annee} : {envoyes} envoyé(s) par {current_user.email}")
        return redirect(url_for("collecte.gardee", annee=annee))

    suivi = _lire_suivi_gardee(annee)
    produits = _lire_produits_modele()
    with get_db_connection() as conn:
        poids_magasins = {
            r["code_vif"]: r["poids_kg"]
            for r in conn.execute(
                "SELECT code_vif, poids_kg FROM collecte_poids_magasins_gardee WHERE annee = ?", (annee,)
            ).fetchall()
        }
        poids_produits = {}
        for r in conn.execute(
            "SELECT association, code_produit, poids_kg FROM collecte_quantites_produits WHERE annee = ?", (annee,)
        ).fetchall():
            poids_produits.setdefault(r["association"], {})[r["code_produit"]] = r["poids_kg"]
    for asso in associations:
        asso["suivi"] = suivi.get(asso["nom"], {"repondu": False, "resultat_envoye": False})
        asso["avancement_saisie"] = _saisie_association_avancement(
            annee, asso["nom"], asso["magasins"], produits, poids_magasins, poids_produits
        )

    return render_template(
        "collecte/gardee_envoi.html",
        annee=annee,
        associations=associations,
        fichier_nom=os.path.basename(chemin),
        texte_mail=_lire_texte_mail_gardee(),
    )


@collecte_bp.route("/collecte/gardee/envoi/suivi", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_marquer_suivi():
    """Coche/décoche à la main l'une des deux cases de suivi (réponse au
    1er message, résultat envoyé) pour une association — vérification
    faite par l'équipe hors application (mail reçu, fichier reçu, ou
    saisie en ligne consultée), jamais automatique."""
    payload = request.get_json(silent=True) or {}
    annee = payload.get("annee")
    nom_association = payload.get("association", "")
    champ = payload.get("champ", "")
    valeur = bool(payload.get("valeur"))
    if champ not in ("repondu", "resultat_envoye") or not annee or not nom_association:
        return jsonify({"success": False, "erreur": "Paramètres invalides"}), 400
    _marquer_suivi_gardee(annee, nom_association, champ, valeur, current_user.email)
    return jsonify({"success": True})


@collecte_bp.route("/collecte/gardee/envoi/relancer", methods=["POST"])
@login_required
@require_access("collecte", "ecriture")
def gardee_relancer():
    """Renvoie le mail (avec les mêmes fichiers Excel/PDF) à une ou
    plusieurs associations — relance individuelle (une association) ou
    groupée (toutes celles où la case correspondante n'est pas cochée)."""
    annee = request.form.get("annee", type=int) or datetime.now().year
    dossier = _dossier_annee(annee)
    associations = _associations_gardee_enrichies(annee)
    if associations is None:
        flash("❌ Fichier(s) source(s) manquant(s)", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    nom_association = request.form.get("association", "")
    groupe = request.form.get("groupe", "")
    if nom_association:
        cible = [a for a in associations if a["nom"] == nom_association]
        libelle_portee = f"« {nom_association} »"
    elif groupe in ("repondu", "resultat_envoye"):
        suivi = _lire_suivi_gardee(annee)
        cible = [a for a in associations if not suivi.get(a["nom"], {}).get(groupe)]
        libelle_portee = "sans réponse" if groupe == "repondu" else "sans résultat envoyé"
    else:
        flash("❌ Rien à relancer — précisez une association ou un groupe", "danger")
        return redirect(url_for("collecte.gardee_envoi", annee=annee))

    envoyes = 0
    sans_email = []
    for asso in cible:
        if not asso["emails"]:
            sans_email.append(asso["nom"])
            continue
        _envoyer_mail_association(asso, annee, dossier, asso["emails"], mode_test=False)
        envoyes += 1

    message = f"✅ Relance envoyée à {envoyes} association(s) {libelle_portee}"
    if sans_email:
        message += f" ; sans adresse (non relancées) : {', '.join(sans_email)}"
    flash(message, "success" if not sans_email else "warning")
    write_log(f"🔁 Relance Associations gardant {annee} ({libelle_portee}) : {envoyes} envoyé(s) par {current_user.email}")
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
    return Response(texte, mimetype="text/plain; charset=utf-8")
