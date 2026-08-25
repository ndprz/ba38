import os
import re
from datetime import datetime
from collections import OrderedDict

from flask import request, render_template, flash, redirect, url_for
from flask_login import login_required
from googleapiclient.http import MediaFileUpload

from ba38_utilitaires.core import get_google_services, write_log, require_access

from ba38_tresorerie import tresorerie_bp


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
    """
    import io, os, re
    from datetime import datetime
    from collections import OrderedDict

    DOSSIER_PARTICIPATION = os.getenv("DOSSIER_PARTICIPATION")

    if not DOSSIER_PARTICIPATION:
        flash("❌ Variable d’environnement DOSSIER_PARTICIPATION manquante.", "danger")
        return redirect(url_for("tresorerie.tresorerie"))

    client, service, creds = get_google_services()
    if service is None:
        flash("❌ Connexion Google Drive impossible", "danger")
        return redirect(url_for("tresorerie.tresorerie"))

    DOSSIER_PARTICIPATION = os.getenv("DOSSIER_PARTICIPATION")
    if not DOSSIER_PARTICIPATION:
        flash("❌ Variable d’environnement DOSSIER_PARTICIPATION manquante.", "danger")
        return redirect(url_for("tresorerie.tresorerie"))

    # ✅ AJOUT ICI
    client, service, creds = get_google_services()
    if service is None:
        flash("❌ Connexion Google Drive impossible", "danger")
        return redirect(url_for("tresorerie.tresorerie"))

    # -------- Helpers Drive --------
    def ensure_clean_trim_folder(parent_id: str, folder_name: str) -> str:
        """
        - Cherche tous les dossiers nommés `folder_name` sous `parent_id`
        - S'il y en a plusieurs: conserve le plus récent, supprime les autres
        - Vide le contenu du dossier conservé (supprime tous les fichiers)
        - S'il n'existe pas: le crée
        - Retourne l'id du dossier propre prêt à l'emploi
        """
        # Lister les dossiers homonymes
        query = (
            f"'{parent_id}' in parents and "
            f"name='{folder_name}' and "
            f"mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        res = service.files().list(
            q=query,
            fields="files(id, name, createdTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        folders = res.get("files", [])

        folder_id = None
        if folders:
            # Garde le plus récent
            folders.sort(key=lambda x: x.get("createdTime", ""), reverse=True)
            folder_id = folders[0]["id"]
            # Supprime les doublons homonymes plus anciens
            for dup in folders[1:]:
                try:
                    service.files().delete(
                        fileId=dup["id"], supportsAllDrives=True
                    ).execute()
                    write_log(f"🗑️ Dossier dupliqué supprimé: {dup['id']}")
                except Exception as e:
                    write_log(f"⚠️ Impossible de supprimer un doublon: {e}")
        else:
            # Créer le dossier s'il n'existe pas
            meta = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            folder = service.files().create(
                body=meta, fields="id", supportsAllDrives=True
            ).execute()
            folder_id = folder["id"]
            write_log(f"📂 Dossier créé: {folder_name} ({folder_id})")

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
                    service.files().delete(
                        fileId=f["id"], supportsAllDrives=True
                    ).execute()
                    write_log(f"🧹 Supprimé du dossier {folder_name}: {f['name']}")
                except Exception as e:
                    write_log(f"⚠️ Impossible de supprimer {f['name']}: {e}")
        except Exception as e:
            write_log(f"⚠️ Purge du dossier échouée: {e}")

        return folder_id

    if request.method == "POST":
        uploaded_file = request.files.get("fichier")
        if not uploaded_file or not uploaded_file.filename:
            flash("❌ Aucun fichier sélectionné", "danger")
            return redirect(url_for("tresorerie.traitement_participation"))

        from werkzeug.utils import secure_filename
        fichier_nom = secure_filename(uploaded_file.filename) or "parsol2l.txt"

        # -------- 2) Lecture contenu (UTF-8, fallback CP1252) --------
        contenu_bytes = uploaded_file.read()
        try:
            contenu = contenu_bytes.decode("utf-8")
        except UnicodeDecodeError:
            contenu = contenu_bytes.decode("cp1252")

        lignes = contenu.splitlines(keepends=True)

        # -------- 4) Suffixe & dossier cible TrimN_YYYY --------
        premiere_date = None
        for l in lignes:
            m = re.match(r"^\s*(\d{2}/\d{2}/\d{4})", l)
            if m:
                try:
                    premiere_date = datetime.strptime(m.group(1), "%d/%m/%Y")
                    break
                except Exception:
                    pass

        suffixe = ""
        folder_id_cible = DOSSIER_PARTICIPATION
        if premiere_date:
            trimestre = (premiere_date.month - 1) // 3 + 1
            suffixe = f"_{premiere_date.year}_T{trimestre}"
            folder_name = f"{premiere_date.year}_T{trimestre}"
            # 👉 Ici on n’efface plus/ recrée pas le dossier : on le nettoie et supprime les doublons
            folder_id_cible = ensure_clean_trim_folder(DOSSIER_PARTICIPATION, folder_name)
        else:
            folder_name = "(SansDate)"  # info pour le flash


        # -------- 5) Découper en factures --------
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

        # -------- 6) Traiter & cumuler les totaux --------
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

        # -------- 7) Construire les sorties --------
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

        # -------- 8) Upload dans le dossier cible (UTF-8) --------
        def upload_txt(nom: str, contenu_txt: str, folder_id: str):
            chemin_tmp = f"/tmp/{nom}"
            with open(chemin_tmp, "w", encoding="utf-8", newline="") as f:
                f.write(contenu_txt)
            media = MediaFileUpload(chemin_tmp, mimetype="text/plain", resumable=False)
            meta = {"name": nom, "parents": [folder_id]}
            service.files().create(
                body=meta, media_body=media, fields="id", supportsAllDrives=True
            ).execute()

        def upload_bytes(nom: str, data: bytes, folder_id: str):
            chemin_tmp = f"/tmp/{nom}"
            with open(chemin_tmp, "wb") as f:
                f.write(data)
            media = MediaFileUpload(chemin_tmp, mimetype="text/plain", resumable=False)
            meta = {"name": nom, "parents": [folder_id]}
            service.files().create(
                body=meta, media_body=media, fields="id", supportsAllDrives=True
            ).execute()

        base = fichier_nom[:-4] if fichier_nom.lower().endswith(".txt") else fichier_nom

        # Le dossier cible vient d'être purgé (ensure_clean_trim_folder) : on y
        # redépose le fichier original pour qu'il reste disponible à côté des sorties.
        upload_bytes(fichier_nom, contenu_bytes, folder_id_cible)
        upload_txt(f"{base}_corrigé{suffixe}.txt", txt_corrige, folder_id_cible)
        upload_txt(f"{base}_lignes_supprimees{suffixe}.txt", txt_suppr, folder_id_cible)
        upload_txt(f"{base}_analyse{suffixe}.txt", txt_analyse, folder_id_cible)

        flash(
            f"✅ Traitement terminé — {total_general_suppr:.2f} € supprimés. "
            f"Fichiers déposés dans « {folder_name} ».",
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
