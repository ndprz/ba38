import os
import re
import unicodedata
from datetime import datetime
from collections import OrderedDict

from flask import request, render_template, flash, redirect, url_for, session
from flask_login import login_required
from googleapiclient.http import MediaFileUpload

from ba38_utilitaires.core import get_google_services, write_log, require_access

from ba38_tresorerie import tresorerie_bp


# Nom du sous-dossier de « Participations » utilisé à la place des vrais
# dossiers AAAA_TN quand on n'est pas en vrai PROD (instance DEV ou compte
# test_only) : DEV et PROD partagent le même DOSSIER_PARTICIPATION, et le
# traitement VIDE le dossier du trimestre avant d'y redéposer les fichiers —
# un test ne doit jamais effacer les fichiers réels du trésorier.
SOUS_DOSSIER_TEST = "_TEST_BASILIC"

# Chemin lisible (pour le trésorier) du dossier DOSSIER_PARTICIPATION
CHEMIN_DRIVE_PARTICIPATION = "Drive partagé « BA380 - TRESORERIE » › Participations"


# ===============================
# 📅 Utilitaire jour de semaine
# ===============================
def jour_semaine(date_str):
    try:
        d = datetime.strptime(date_str, "%d/%m/%Y")
        return d.weekday()  # 0=lundi … 6=dimanche
    except:
        return None


# ===============================
# 🧮 Calcul des 3 fichiers EBP
# ===============================
def traiter_contenu_parsol_ebp(contenu):
    """
    À partir du texte PARSOL : supprime les lignes ven/sam/dim, recalcule les
    totaux et construit les 3 sorties texte (corrigé = fichier importé dans
    EBP, lignes supprimées, analyse).
    """
    lignes = contenu.splitlines(keepends=True)

    # -------- Découper en factures --------
    factures, facture = [], []
    for ligne in lignes:
        if ligne.strip().startswith("BA. de l'Isère"):
            if facture:
                factures.append(facture)
                facture = []
        facture.append(ligne)
    if facture:
        factures.append(facture)

    pat_detail = re.compile(
        r"^\s*(\d{2}/\d{2}/\d{4})\s+(\d+)\s+([\d\s.,]+)\s+([\d\s.,]+)\s*$"
    )

    # -------- Traiter & cumuler les totaux --------
    factures_corrigees = []
    suppr_par_assoc = OrderedDict()
    total_general_suppr = 0.0
    total_general_corrige = 0.0

    for facture in factures:
        nouvelle_facture = []
        assoc = ""
        garder_facture = False
        total_assoc_suppr = 0.0

        for l in facture:
            ls = l.strip()

            if ls.startswith("Association"):
                assoc = ls
                if assoc not in suppr_par_assoc:
                    suppr_par_assoc[assoc] = []

            m = pat_detail.match(ls)
            if m:
                date_str, nb_ben, participation, total = m.groups()
                try:
                    total_val = float(total.replace(" ", "").replace(",", "."))
                except Exception:
                    total_val = 0.0

                wd = jour_semaine(date_str)
                if wd in (4, 5, 6):  # ven/sam/dim => suppression
                    suppr_par_assoc.setdefault(assoc, []).append(ls)
                    total_assoc_suppr += total_val
                    total_general_suppr += total_val
                    continue
                else:
                    garder_facture = True
                    total_general_corrige += total_val
                    nouvelle_facture.append(l)
            else:
                nouvelle_facture.append(l)

        if garder_facture:
            factures_corrigees.append(nouvelle_facture)

        if assoc and total_assoc_suppr > 0:
            suppr_par_assoc[assoc].append(
                f"TOTAL supprimé {assoc} : {total_assoc_suppr:.2f} €"
            )

    # -------- Construire les sorties --------
    txt_corrige = "".join("".join(f) for f in factures_corrigees)
    txt_corrige += f"\n=== TOTAL GÉNÉRAL (corrigé) : {total_general_corrige:.2f} € ===\n"

    blocs = []
    for a, lignes_s in suppr_par_assoc.items():
        if not lignes_s:
            continue
        blocs.append(a + "\n" + "\n".join("  " + s for s in lignes_s) + "\n")
    blocs.append(f"\n=== TOTAL GÉNÉRAL SUPPRIMÉ : {total_general_suppr:.2f} € ===\n")
    txt_suppr = "".join(blocs)

    txt_analyse = (
        f"Total général corrigé : {total_general_corrige:.2f} €\n"
        f"Total supprimé : {total_general_suppr:.2f} €\n"
    )

    return {
        "txt_corrige": txt_corrige,
        "txt_suppr": txt_suppr,
        "txt_analyse": txt_analyse,
        "total_corrige": round(total_general_corrige, 2),
        "total_supprime": round(total_general_suppr, 2),
    }


def annee_trimestre_parsol(contenu):
    """(année, trimestre) de la 1ère date trouvée dans le fichier, ou (None, None)."""
    for l in contenu.splitlines():
        m = re.match(r"^\s*(\d{2}/\d{2}/\d{4})", l)
        if m:
            try:
                d = datetime.strptime(m.group(1), "%d/%m/%Y")
                return d.year, (d.month - 1) // 3 + 1
            except Exception:
                pass
    return None, None


# ===============================
# 📂 Helpers Drive
# ===============================
def _mode_test_drive():
    """True hors vrai PROD : instance DEV ou compte test_only (base de test)."""
    if os.getenv("ENVIRONMENT", "").upper() == "DEV":
        return True
    try:
        return bool(session.get("test_user"))
    except RuntimeError:
        return False


def _trouver_dossier(service, parent_id, nom):
    """Dossiers nommés `nom` sous `parent_id`, du plus récent au plus ancien."""
    res = service.files().list(
        q=(
            f"'{parent_id}' in parents and name='{nom}' and "
            f"mimeType='application/vnd.google-apps.folder' and trashed=false"
        ),
        fields="files(id, name, createdTime)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    dossiers = res.get("files", [])
    dossiers.sort(key=lambda x: x.get("createdTime", ""), reverse=True)
    return dossiers


def _creer_dossier(service, parent_id, nom):
    meta = {"name": nom, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    dossier = service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
    write_log(f"📂 Dossier créé: {nom} ({dossier['id']})")
    return dossier["id"]


def dossier_racine_participation(service, creer=True):
    """
    (id, chemin lisible) du dossier sous lequel se trouvent les AAAA_TN :
    DOSSIER_PARTICIPATION en vrai PROD, son sous-dossier _TEST_BASILIC sinon.
    """
    racine = os.getenv("DOSSIER_PARTICIPATION")
    if not racine:
        raise RuntimeError("Variable d’environnement DOSSIER_PARTICIPATION manquante.")

    if not _mode_test_drive():
        return racine, CHEMIN_DRIVE_PARTICIPATION

    existants = _trouver_dossier(service, racine, SOUS_DOSSIER_TEST)
    if existants:
        dossier_id = existants[0]["id"]
    elif creer:
        dossier_id = _creer_dossier(service, racine, SOUS_DOSSIER_TEST)
    else:
        dossier_id = None
    return dossier_id, f"{CHEMIN_DRIVE_PARTICIPATION} › {SOUS_DOSSIER_TEST}"


def ensure_clean_trim_folder(service, parent_id: str, folder_name: str) -> str:
    """
    - Cherche tous les dossiers nommés `folder_name` sous `parent_id`
    - S'il y en a plusieurs: conserve le plus récent, supprime les autres
    - Vide le contenu du dossier conservé (supprime tous les fichiers)
    - S'il n'existe pas: le crée
    - Retourne l'id du dossier propre prêt à l'emploi
    """
    folders = _trouver_dossier(service, parent_id, folder_name)

    if folders:
        folder_id = folders[0]["id"]
        # Supprime les doublons homonymes plus anciens
        for dup in folders[1:]:
            try:
                service.files().delete(fileId=dup["id"], supportsAllDrives=True).execute()
                write_log(f"🗑️ Dossier dupliqué supprimé: {dup['id']}")
            except Exception as e:
                write_log(f"⚠️ Impossible de supprimer un doublon: {e}")
    else:
        folder_id = _creer_dossier(service, parent_id, folder_name)

    # Purger le contenu du dossier retenu (pas le dossier lui-même)
    try:
        res_children = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for f in res_children.get("files", []):
            try:
                service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                write_log(f"🧹 Supprimé du dossier {folder_name}: {f['name']}")
            except Exception as e:
                write_log(f"⚠️ Impossible de supprimer {f['name']}: {e}")
    except Exception as e:
        write_log(f"⚠️ Purge du dossier échouée: {e}")

    return folder_id


def _upload(service, nom, data: bytes, folder_id):
    chemin_tmp = f"/tmp/{nom}"
    with open(chemin_tmp, "wb") as f:
        f.write(data)
    media = MediaFileUpload(chemin_tmp, mimetype="text/plain", resumable=False)
    meta = {"name": nom, "parents": [folder_id]}
    fichier = service.files().create(
        body=meta, media_body=media, fields="id", supportsAllDrives=True
    ).execute()
    return fichier["id"]


def deposer_fichiers_ebp(service, contenu_bytes, fichier_nom, annee=None, trimestre=None):
    """
    Calcule les 3 fichiers EBP et les dépose (avec l'original) dans le
    dossier AAAA_TN du trimestre, après l'avoir vidé. Sans année/trimestre
    (aucune date dans le fichier) : dépôt à la racine, sans purge.
    Retourne les infos utiles pour retrouver le fichier corrigé.
    """
    try:
        contenu = contenu_bytes.decode("utf-8")
    except UnicodeDecodeError:
        contenu = contenu_bytes.decode("cp1252")

    if not annee or not trimestre:
        annee, trimestre = annee_trimestre_parsol(contenu)

    racine_id, racine_chemin = dossier_racine_participation(service)

    if annee and trimestre:
        suffixe = f"_{annee}_T{trimestre}"
        folder_name = f"{annee}_T{trimestre}"
        folder_id = ensure_clean_trim_folder(service, racine_id, folder_name)
        dossier_chemin = f"{racine_chemin} › {folder_name}"
    else:
        suffixe = ""
        folder_name = "(SansDate)"  # info pour le flash
        folder_id = racine_id
        dossier_chemin = racine_chemin

    sorties = traiter_contenu_parsol_ebp(contenu)

    base = fichier_nom[:-4] if fichier_nom.lower().endswith(".txt") else fichier_nom
    nom_corrige = f"{base}_corrigé{suffixe}.txt"

    # Le dossier cible vient d'être purgé : on y redépose le fichier original
    # pour qu'il reste disponible à côté des sorties.
    _upload(service, fichier_nom, contenu_bytes, folder_id)
    corrige_id = _upload(service, nom_corrige, sorties["txt_corrige"].encode("utf-8"), folder_id)
    _upload(service, f"{base}_lignes_supprimees{suffixe}.txt", sorties["txt_suppr"].encode("utf-8"), folder_id)
    _upload(service, f"{base}_analyse{suffixe}.txt", sorties["txt_analyse"].encode("utf-8"), folder_id)

    return {
        "dossier_id": folder_id,
        "dossier_nom": folder_name,
        "dossier_chemin": dossier_chemin,
        "fichier_corrige_nom": nom_corrige,
        "fichier_corrige_id": corrige_id,
        "total_corrige": sorties["total_corrige"],
        "total_supprime": sorties["total_supprime"],
    }


def rechercher_fichier_corrige(service, annee, trimestre):
    """
    Retrouve sur Drive un fichier corrigé déjà produit (par l'ancien menu
    « Traitement fichier participation EBP » par exemple) dans le dossier
    AAAA_TN. Retourne le même dict que deposer_fichiers_ebp, ou None.
    """
    racine_id, racine_chemin = dossier_racine_participation(service, creer=False)
    if not racine_id:
        return None

    folder_name = f"{annee}_T{trimestre}"
    dossiers = _trouver_dossier(service, racine_id, folder_name)
    if not dossiers:
        return None
    folder_id = dossiers[0]["id"]

    res = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    # Drive peut stocker le « é » décomposé (NFD) : comparaison normalisée
    fin_attendue = unicodedata.normalize("NFC", f"_corrigé_{annee}_T{trimestre}.txt")
    corrige = next(
        (f for f in res.get("files", [])
         if unicodedata.normalize("NFC", f["name"]).endswith(fin_attendue)),
        None
    )
    if not corrige:
        return None

    total_corrige = None
    try:
        texte = service.files().get_media(fileId=corrige["id"], supportsAllDrives=True).execute().decode("utf-8")
        m = re.search(r"TOTAL GÉNÉRAL \(corrigé\) : ([\d.]+) €", texte)
        if m:
            total_corrige = float(m.group(1))
    except Exception as e:
        write_log(f"⚠️ Lecture du fichier corrigé {corrige['name']} impossible : {e}")

    return {
        "dossier_id": folder_id,
        "dossier_nom": folder_name,
        "dossier_chemin": f"{racine_chemin} › {folder_name}",
        "fichier_corrige_nom": corrige["name"],
        "fichier_corrige_id": corrige["id"],
        "total_corrige": total_corrige,
        "total_supprime": None,
    }


# ===============================
# 📂 Traitement fichier participation
# ===============================
@tresorerie_bp.route("/traitement_participation", methods=["GET", "POST"])
@login_required
@require_access("tresorerie", "ecriture")
def traitement_participation():
    """
    - Lit le .txt envoyé depuis le poste de l'utilisateur (upload local)
    - Supprime les lignes ven/sam/dim, recalcule les totaux
    - Crée/choisit un sous-dossier TrimN_YYYY sous DOSSIER_PARTICIPATION
      * S'il existe déjà : on le garde, on SUPPRIME TOUT SON CONTENU
      * On supprime aussi d'éventuels DOUBLONS de dossiers homonymes
    - Dépose 3 fichiers dedans (corrigé, lignes_supprimées, analyse),
      suffixés par _TrimN_YYYY
    Même traitement que celui lancé automatiquement par Participation V2
    (participation.py::traiter) — voir deposer_fichiers_ebp.
    """
    if not os.getenv("DOSSIER_PARTICIPATION"):
        flash("❌ Variable d’environnement DOSSIER_PARTICIPATION manquante.", "danger")
        return redirect(url_for("tresorerie.tresorerie"))

    client, service, creds = get_google_services()
    if service is None:
        flash("❌ Connexion Google Drive impossible", "danger")
        return redirect(url_for("tresorerie.tresorerie"))

    if request.method == "POST":
        uploaded_file = request.files.get("fichier")
        if not uploaded_file or not uploaded_file.filename:
            flash("❌ Aucun fichier sélectionné", "danger")
            return redirect(url_for("tresorerie.traitement_participation"))

        from werkzeug.utils import secure_filename
        fichier_nom = secure_filename(uploaded_file.filename) or "parsol2l.txt"

        infos = deposer_fichiers_ebp(service, uploaded_file.read(), fichier_nom)

        flash(
            f"✅ Traitement terminé — {infos['total_supprime']:.2f} € supprimés. "
            f"Fichiers déposés dans « {infos['dossier_chemin']} ».",
            "success"
        )
        return redirect(url_for("tresorerie.traitement_participation"))

    return render_template("tresorerie/traitement_participation.html")


# ===============================
# 🗑️ Ancienne fonction simple (conservée pour tests)
# ===============================
def traiter_parsol(contenu):
    lignes = contenu.splitlines(keepends=True)
    factures, facture = [], []
    for ligne in lignes:
        if ligne.strip().startswith("BA. de l'Isère"):
            if facture:
                factures.append(facture)
                facture = []
        facture.append(ligne)
    if facture:
        factures.append(facture)

    factures_corrigees, lignes_supprimees = [], []
    total_general = 0.0

    for facture in factures:
        nouvelle_facture = []
        assoc = ""
        garder_facture = False

        for l in facture:
            if l.strip().startswith("Association"):
                assoc = l.strip()

            match = re.match(r"(\d{2}/\d{2}/\d{4})\s+(\d+)\s+([\d,]+)\s+([\d,]+)", l.strip())
            if match:
                date_str, nb, prix, total = match.groups()
                total = float(total.replace(",", "."))
                total_general += total
                wd = jour_semaine(date_str)
                if wd in (4, 5, 6):
                    lignes_supprimees.append(f"{assoc} → {l.strip()}\n")
                    continue
                else:
                    garder_facture = True
            nouvelle_facture.append(l)

        if garder_facture:
            factures_corrigees.append(nouvelle_facture)

    txt_corrige = "".join("".join(f) for f in factures_corrigees)
    txt_suppr = "".join(lignes_supprimees)
    txt_analyse = f"Total général du fichier original : {total_general:.2f} €\n"
    return txt_corrige, txt_suppr, txt_analyse
