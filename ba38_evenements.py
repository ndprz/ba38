# ============================================================
# 📁 ba38_evenements.py — Écran d’événements BAI
# ============================================================
# Fonctionnalités :
# - Création / modification / suppression d'événements
# - Upload de fichiers (vidéo, image, PDF, PPTX)
# - Conversion automatique PPTX -> PDF (LibreOffice), puis PDF -> images (pdf2image)
# - Conversion automatique PDF -> images
# - Nettoyage des fichiers liés lors d'une suppression ou d'un remplacement
# - API /affichage_evenement + /api/evenements_actifs pour le front
# - Logs via write_log(), chemins dynamiques DEV/PROD via get_static_event_dir()


import os
import re
import glob
import sqlite3
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
import shutil

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, abort
)
from flask_login import login_required
from werkzeug.utils import secure_filename
from pdf2image import convert_from_path

import subprocess

# Utils maison
from utils import (
    get_db_connection, upload_database, write_log, get_static_event_dir,is_admin_global, require_access, has_access
)



evenements_bp = Blueprint("evenements", __name__, template_folder="templates")

# ============================================================
# 🧱 Routes : gestion
# ============================================================
@evenements_bp.route("/gestion_evenements", methods=["GET", "POST"])
@login_required
@require_access("evenements", "lecture")
def gestion_evenements():
    """
    Gestion complète des événements (CRUD).

    Règles fonctionnelles :
    - Création : sauvegarde du fichier, aucune suppression.
    - Modification avec nouveau fichier : suppression de l’ancien média AVANT remplacement.
    - Suppression événement : suppression complète (vidéo, image, dérivés, sous-titres).

    Fonctionnement :
    - POST  → traitement des actions (ajout, modification, suppression, activation).
    - GET   → affichage de la liste des événements et bénévoles.
    """

    # Debug upload (utile pour diagnostiquer les problèmes d’upload)
    write_log(f"DEBUG files keys = {list(request.files.keys())}")


    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ======================================================================
    # POST : actions
    # ======================================================================
    if request.method == "POST":

        if not has_access("evenements", "ecriture"):
            abort(403)

        action = request.form.get("action", "ajouter")

        # ------------------------------------------------------------------
        # 🗑️ SUPPRESSION D’UN ÉVÉNEMENT (nettoyage COMPLET)
        # ------------------------------------------------------------------
        if action == "supprimer":

            eid = request.form.get("id")

            if eid:

                row = cur.execute(
                    "SELECT fichier_path, image_path FROM evenements WHERE id = ?",
                    (eid,)
                ).fetchone()

                if row:

                    paths = []

                    if row["fichier_path"]:
                        paths.append(to_abs_path(row["fichier_path"]))

                    if row["image_path"]:
                        paths.append(to_abs_path(row["image_path"]))

                    # suppression des fichiers
                    for p in paths:

                        if p and os.path.exists(p):
                            try:
                                os.remove(p)
                                write_log(f"🗑️ Fichier supprimé : {p}")
                            except Exception as e:
                                write_log(f"❌ Erreur suppression fichier {p} : {e}")

                        # suppression dérivés + sous-titres
                        if p:
                            base = base_noext(p)
                            remove_all_files_for_base(base)

                # suppression BDD
                cur.execute(
                    "DELETE FROM evenements WHERE id = ?",
                    (eid,)
                )

                conn.commit()
                upload_database()

                flash("🗑️ Événement supprimé (fichiers nettoyés).", "info")

            return redirect(url_for("evenements.gestion_evenements"))

        # ------------------------------------------------------------------
        # 🔁 BASCULE ACTIF / INACTIF
        # ------------------------------------------------------------------
        if action == "basculer_actif":

            eid = request.form.get("id")

            if eid:

                cur.execute(
                    "UPDATE evenements SET actif = 1 - actif WHERE id = ?",
                    (eid,)
                )

                conn.commit()
                upload_database()

                flash("🔁 Statut mis à jour.", "info")

            return redirect(url_for("evenements.gestion_evenements"))

        # ------------------------------------------------------------------
        # 🗓️ RÉGLAGES DU PLANNING AUTO DES PASSAGES
        # ------------------------------------------------------------------
        if action == "config_planning":

            actif = request.form.get("planning_actif") == "on"

            try:
                duree = int(request.form.get("planning_duree_affichage") or 30)
            except ValueError:
                duree = 30
            duree = max(5, duree)

            set_planning_config(actif, duree)

            flash("🗓️ Réglages du planning des passages mis à jour.", "success")

            return redirect(url_for("evenements.gestion_evenements"))

        # ------------------------------------------------------------------
        # CHAMPS COMMUNS (ajout / modification)
        # ------------------------------------------------------------------
        type_ev = request.form.get("type")
        titre = (request.form.get("titre") or "").strip()
        contenu = (request.form.get("contenu") or "").strip()

        benevole_id = request.form.get("benevole_id") or None
        image_path = (request.form.get("image_path") or "").strip() or None

        date_debut = parse_dt_local(request.form.get("date_debut"))
        date_fin = parse_dt_local(request.form.get("date_fin"))

        recurrence = request.form.get("recurrence") or "aucune"
        duree = int(request.form.get("duree_affichage") or 15)

        # 🎥 Règle métier : pour une vidéo, la durée est ignorée
        if type_ev == "video":
            duree = None

        # ------------------------------------------------------------------
        # 📸 IMAGE AUTOMATIQUE depuis photo bénévole
        # ------------------------------------------------------------------
        if not image_path and benevole_id:

            try:

                bid = int(benevole_id)

                src_dir = os.getenv("PHOTOS_BENEVOLES_DIR", "/srv/ba38/photos_benevoles")

                for ext in (".jpg", ".jpeg", ".png"):

                    src = os.path.join(src_dir, f"{bid}{ext}")

                    if os.path.exists(src):

                        dest = os.path.join(
                            get_static_event_dir(),
                            f"benevole_{bid}{ext}"
                        )

                        shutil.copy2(src, dest)

                        image_path = f"/static/evenements/benevole_{bid}{ext}"

                        write_log(f"📸 Photo bénévole copiée : {image_path}")

                        break

            except Exception as e:
                write_log(f"❌ Erreur copie photo bénévole : {e}")

        write_log(
            f"🧾 Traitement événement → image_path={image_path}, benevole_id={benevole_id}"
        )

        # ------------------------------------------------------------------
        # 📎 UPLOAD FICHIER
        # ------------------------------------------------------------------
        new_file_web = None

        if "fichier" in request.files and request.files["fichier"].filename:

            f = request.files["fichier"]

            if not allowed_file(f.filename):
                flash("❌ Extension non autorisée.", "danger")
                return redirect(url_for("evenements.gestion_evenements"))

            new_file_web = save_uploaded_file(f)

        # ------------------------------------------------------------------
        # 🔁 MODIFICATION
        # ------------------------------------------------------------------
        if action == "modifier":

            eid = request.form.get("id")

            if not eid:
                flash("❌ Identifiant manquant.", "danger")
                return redirect(url_for("evenements.gestion_evenements"))

            # suppression ancien média si nouveau fichier
            if new_file_web:

                old = cur.execute(
                    "SELECT fichier_path FROM evenements WHERE id = ?",
                    (eid,)
                ).fetchone()

                if old and old["fichier_path"]:

                    base = base_noext(
                        to_abs_path(old["fichier_path"])
                    )

                    remove_all_files_for_base(base)

            champs = [
                "type",
                "titre",
                "contenu",
                "benevole_id",
                "image_path",
                "date_debut",
                "date_fin",
                "recurrence",
                "duree_affichage"
            ]

            params = [
                type_ev,
                titre,
                contenu,
                benevole_id,
                image_path,
                date_debut,
                date_fin,
                recurrence,
                duree
            ]

            sql = f"UPDATE evenements SET {', '.join(c + '=?' for c in champs)}"

            if new_file_web:
                sql += ", fichier_path=?"
                params.append(new_file_web)

            sql += " WHERE id=?"
            params.append(eid)

            cur.execute(sql, params)

            conn.commit()
            upload_database()

            flash("💾 Événement modifié.", "success")

            return redirect(url_for("evenements.gestion_evenements"))

        # ------------------------------------------------------------------
        # ➕ CRÉATION
        # ------------------------------------------------------------------
        cur.execute(
            """
            INSERT INTO evenements
              (type, titre, contenu, fichier_path, benevole_id, image_path,
               date_debut, date_fin, recurrence, duree_affichage, actif)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                type_ev,
                titre,
                contenu,
                new_file_web,
                benevole_id,
                image_path,
                date_debut,
                date_fin,
                recurrence,
                duree
            )
        )

        conn.commit()
        upload_database()

        flash("✅ Événement ajouté.", "success")

        return redirect(url_for("evenements.gestion_evenements"))

    # ======================================================================
    # GET : affichage
    # ======================================================================

    ev_rows = cur.execute(
        "SELECT * FROM evenements ORDER BY date_debut DESC, id DESC"
    ).fetchall()

    ben_rows = cur.execute(
        "SELECT id, nom, prenom FROM benevoles ORDER BY nom, prenom"
    ).fetchall()

    conn.close()

    # conversion Row → dict
    evenements = [dict(r) for r in ev_rows]
    benevoles = [dict(r) for r in ben_rows]

    # premier événement actif
    evenement_actif = next(
        (e for e in evenements if e.get("actif")),
        None
    )

    return render_template(
        "evenements/gestion_evenements.html",
        evenements=evenements,
        evenement_actif=evenement_actif,
        benevoles=benevoles,
        planning_config=get_planning_config()
    )


# =====================================================
# Récupérer la durée de la vidéo
# =====================================================
def get_video_duration(video_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f"Erreur lors de la récupération de la durée : {e}")
        return None



# ============================================================
# 🔧 Config & constantes
# ============================================================

WEB_PREFIX = "/static/evenements"

def get_upload_dir():
    """Retourne dynamiquement le chemin vers le dossier des événements (DEV/PROD)."""
    return get_static_event_dir()

ALLOWED_EXTENSIONS = {"pdf", "pptx", "mp4", "webm", "mov", "jpg", "jpeg", "png", "gif", "webp"}

# ============================================================
# 🧰 Utilitaires
# ============================================================

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_dt_local(val: str) -> str:
    try:
        return datetime.fromisoformat(val).isoformat(timespec="minutes")
    except Exception:
        return (val or "").strip()

def to_web_path(abs_path: str) -> str:
    """Convertit un chemin absolu UPLOAD_DIR -> chemin web /static/evenements/..."""
    if not abs_path:
        return ""
    upload_dir = get_upload_dir()
    abs_path = os.path.abspath(abs_path)
    if abs_path.startswith(upload_dir):
        rel = abs_path[len(upload_dir):].lstrip(os.sep)
        return f"{WEB_PREFIX}/{rel}"
    if abs_path.startswith("/static/"):
        return abs_path
    return f"{WEB_PREFIX}/{os.path.basename(abs_path)}"

def to_abs_path(web_path: str) -> str:
    """Convertit un /static/evenements/... -> chemin absolu dans UPLOAD_DIR."""
    if not web_path:
        return ""
    upload_dir = get_upload_dir()
    if web_path.startswith(WEB_PREFIX):
        rel = web_path[len(WEB_PREFIX):].lstrip("/")
        return os.path.join(upload_dir, rel)
    if web_path.startswith(upload_dir):
        return web_path
    return os.path.join(upload_dir, os.path.basename(web_path))

def base_noext(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def remove_all_files_for_base(base: str):
    """
    Supprime TOUS les fichiers liés à un événement
    (vidéo incluse) — à utiliser UNIQUEMENT lors
    de la suppression d’un événement.
    """
    upload_dir = get_upload_dir()
    patterns = [
        f"{base}.*",
        f"{base}_page_*.jpg",
        f"{base}_slide_*.jpg",
        f"{base}_*.jpg",
    ]
    for pat in patterns:
        for fp in glob.glob(os.path.join(upload_dir, pat)):
            try:
                os.remove(fp)
                write_log(f"🗑️ Fichier supprimé : {fp}")
            except Exception as e:
                write_log(f"⚠️ Suppression échouée {fp} : {e}")

def remove_derived_files_for_base(base: str):
    """
    Supprime UNIQUEMENT les fichiers dérivés
    (PDF, images, sous-titres), JAMAIS la vidéo source.
    """
    upload_dir = get_upload_dir()
    patterns = [
        f"{base}.pdf",
        f"{base}.pptx",
        f"{base}.vtt",
        f"{base}.srt",
        f"{base}_page_*.jpg",
        f"{base}_slide_*.jpg",
        f"{base}_*.jpg",
    ]
    for pat in patterns:
        for fp in glob.glob(os.path.join(upload_dir, pat)):
            try:
                os.remove(fp)
                write_log(f"🧹 Fichier dérivé supprimé : {fp}")
            except Exception as e:
                write_log(f"⚠️ Suppression échouée {fp} : {e}")


def remove_files_for_base(base: str):
    """
    Supprime tous les fichiers liés à une base (sans extension) :
    - .pptx / .pdf / .mp4 / .webm / .mov / .jpg / .jpeg / .png / .gif / .webp
    - _page_*.jpg / _slide_*.jpg / *_*.jpg dérivés
    """
    upload_dir = get_upload_dir()
    deleted = 0
    extensions = ["pptx", "pdf", "mp4", "webm", "mov", "jpg", "jpeg", "png", "gif", "webp"]
    patterns = [os.path.join(upload_dir, f"{base}.{ext}") for ext in extensions]
    patterns += [
        os.path.join(upload_dir, base + "_page_*.jpg"),
        os.path.join(upload_dir, base + "_slide_*.jpg"),
        os.path.join(upload_dir, base + "_*.jpg"),
    ]
    for pat in patterns:
        for fp in glob.glob(pat):
            try:
                os.remove(fp)
                deleted += 1
            except FileNotFoundError:
                pass
            except Exception as e:
                write_log(f"⚠️ Suppression échouée {fp} : {e}")
    if deleted:
        write_log(f"🧹 {deleted} fichier(s) supprimé(s) pour base '{base}'.")

# ============================================================
# 🔄 Conversions
# ============================================================

def convertir_pdf_en_images(pdf_abs_path: str) -> list[str]:
    web_images = []
    upload_dir = get_upload_dir()
    try:
        write_log(f"🖨️ Conversion PDF -> images : {pdf_abs_path}")
        images = convert_from_path(pdf_abs_path, dpi=150)
        base = base_noext(pdf_abs_path)
        for i, img in enumerate(images, start=1):
            out_abs = os.path.join(upload_dir, f"{base}_page_{i}.jpg")
            img.save(out_abs, "JPEG")
            web_images.append(to_web_path(out_abs))
        write_log(f"✅ PDF -> {len(web_images)} image(s).")
    except Exception as e:
        import traceback
        write_log(f"❌ Erreur PDF->images : {e}\n{traceback.format_exc()}")
    return web_images

def libreoffice_available() -> bool:
    try:
        r = subprocess.run(["which", "libreoffice"], capture_output=True, text=True, check=False)
        return bool(r.stdout.strip())
    except Exception:
        return False

def convertir_pptx_en_pdf(pptx_abs_path: str) -> str | None:
    pdf_abs = os.path.splitext(pptx_abs_path)[0] + ".pdf"
    if not libreoffice_available():
        write_log("⚠️ LibreOffice indisponible — conversion PPTX->PDF impossible.")
        return None
    try:
        write_log(f"📑 Conversion PPTX -> PDF via LibreOffice : {pptx_abs_path}")
        outdir = os.path.dirname(pptx_abs_path)
        res = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", outdir, pptx_abs_path],
            capture_output=True, text=True, check=False
        )
        if res.returncode != 0:
            write_log(f"❌ LibreOffice retour {res.returncode} : {res.stderr or res.stdout}")
            return None
        if not os.path.exists(pdf_abs):
            write_log("❌ PDF attendu non trouvé après conversion.")
            return None
        write_log(f"✅ PPTX -> PDF OK : {pdf_abs}")
        return pdf_abs
    except Exception as e:
        import traceback
        write_log(f"❌ Erreur PPTX->PDF : {e}\n{traceback.format_exc()}")
        return None

# ============================================================
# 💾 Gestion fichiers uploadés
# ============================================================

def save_uploaded_file(file_storage) -> str:
    upload_dir = get_upload_dir()
    filename = secure_filename(file_storage.filename)
    abs_path = os.path.join(upload_dir, filename)
    os.makedirs(upload_dir, exist_ok=True)
    file_storage.save(abs_path)
    write_log(f"💾 Fichier sauvegardé : {abs_path}")

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pptx":
        pdf_abs = convertir_pptx_en_pdf(abs_path)
        if pdf_abs:
            convertir_pdf_en_images(pdf_abs)
            return to_web_path(pdf_abs)
        return to_web_path(abs_path)
    if ext == ".pdf":
        convertir_pdf_en_images(abs_path)
        return to_web_path(abs_path)
    return to_web_path(abs_path)

# ============================================================
# 👥 Photo bénévole
# ============================================================

def get_benevole_photo_path(benevole_id) -> str | None:
    if not benevole_id:
        return None
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT filename FROM photos_benevoles WHERE benevole_id = ? ORDER BY id DESC LIMIT 1",
            (benevole_id,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        filename = row["filename"] if isinstance(row, sqlite3.Row) else row[0]
        return f"/static/photos_benevoles/{filename}"
    except Exception as e:
        write_log(f"⚠️ get_benevole_photo_path : {e}")
        return None





# ============================================================
# 🗓️ Planning des passages associations (généré à la volée)
# ============================================================

JOURS_SEMAINE_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _minutes_depuis_heure(heure: str):
    """Extrait les minutes depuis minuit d'un texte type '14h30' ou '14h-14h20'.
    Retourne None si le texte n'est pas exploitable (ex: 'matin', 'au quai')."""
    if not heure:
        return None
    m = re.search(r"(\d{1,2})\s*[hH]\s*(\d{2})?", heure)
    if not m:
        return None
    h = int(m.group(1))
    mn = int(m.group(2)) if m.group(2) else 0
    if h > 23 or mn > 59:
        return None
    return h * 60 + mn


def get_planning_config() -> dict:
    """Réglages (actif / durée d'affichage) du planning auto des passages,
    modifiables depuis gestion_evenements. Table créée à la volée si absente."""

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS planning_passages_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            actif INTEGER NOT NULL DEFAULT 1,
            duree_affichage INTEGER NOT NULL DEFAULT 30
        )
    """)
    cur.execute(
        "INSERT OR IGNORE INTO planning_passages_config (id, actif, duree_affichage) VALUES (1, 1, 30)"
    )
    conn.commit()
    row = cur.execute(
        "SELECT actif, duree_affichage FROM planning_passages_config WHERE id = 1"
    ).fetchone()
    conn.close()

    return {"actif": bool(row[0]), "duree_affichage": row[1]}


def set_planning_config(actif: bool, duree_affichage: int) -> None:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS planning_passages_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            actif INTEGER NOT NULL DEFAULT 1,
            duree_affichage INTEGER NOT NULL DEFAULT 30
        )
    """)
    cur.execute("""
        INSERT INTO planning_passages_config (id, actif, duree_affichage)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET actif = excluded.actif, duree_affichage = excluded.duree_affichage
    """, (int(bool(actif)), duree_affichage))
    conn.commit()
    conn.close()


def _rang_heure(heure: str):
    """Ordre d'affichage : le matin d'abord, puis les heures connues triées
    chronologiquement, puis le reste (texte non exploitable) à la fin."""
    h = heure.lower()
    if "matin" in h:
        return (0, 0, heure)
    minutes = _minutes_depuis_heure(heure)
    if minutes is not None:
        return (1, minutes, heure)
    return (2, 0, heure)


def generer_planning_du_jour(jour: str, duree_affichage: int = 30) -> dict | None:
    """Construit un événement 'planning' virtuel (non stocké en base) listant les
    associations dont jour_de_passage_a_la_BAI correspond au jour donné, regroupées
    par heure_de_passage et triées au mieux chronologiquement."""

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT nom_association, heure_de_passage, Emplacement
        FROM associations
        WHERE jour_de_passage_a_la_BAI IS NOT NULL
          AND LOWER(jour_de_passage_a_la_BAI) LIKE ?
          AND (validite IS NULL OR LOWER(TRIM(validite)) != 'non')
    """, (f"%{jour}%",)).fetchall()
    conn.close()

    groupes = {}

    for r in rows:
        nom = (r["nom_association"] or "").strip()
        if not nom:
            continue

        emplacement = (r["Emplacement"] or "").strip()
        libelle = f"{nom} ({emplacement})" if emplacement else nom

        heure = (r["heure_de_passage"] or "").strip()
        cle = heure.lower() or "￿"

        if cle not in groupes:
            groupes[cle] = {"heure": heure or "—", "lignes": []}

        groupes[cle]["lignes"].append(libelle)

    if not groupes:
        return None

    passages = sorted(groupes.values(), key=lambda g: _rang_heure(g["heure"]))

    return {
        "id": f"planning_{jour}",
        "type": "planning",
        "titre": f"Passages {jour.capitalize()}",
        "duree_affichage": duree_affichage,
        "passages": passages,
    }


# ============================================================
# 🌍 API : événements actifs
# ============================================================

@evenements_bp.route("/api/evenements_actifs")
@login_required
@require_access("evenements", "lecture")
def api_evenements_actifs():

    now_dt = datetime.now(ZoneInfo("Europe/Paris"))
    now = now_dt.strftime("%Y-%m-%dT%H:%M")
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM evenements
        WHERE actif = 1
          AND date_debut <= ?
          AND date_fin   >= ?
        ORDER BY date_debut, id
    """, (now, now)).fetchall()


    conn.close()

    data = []
    for r in rows:
        d = dict(r)
        d["actif"] = int(d.get("actif", 0))
        fichier_web = (d.get("fichier_path") or "").strip()
        images = []

        if fichier_web:
            base = base_noext(to_abs_path(fichier_web))
            upload_dir = get_upload_dir()
            patterns = [
                os.path.join(upload_dir, f"{base}_page_*.jpg"),
                os.path.join(upload_dir, f"{base}_slide_*.jpg"),
                os.path.join(upload_dir, f"{base}_*.jpg"),
            ]
            found = []
            for pat in patterns:
                found.extend(sorted(glob.glob(pat)))
            seen = set()
            for fp in found:
                if fp not in seen:
                    seen.add(fp)
                    images.append(to_web_path(fp))
        if images:
            d["images"] = images
        data.append(d)

    planning_config = get_planning_config()
    if planning_config["actif"]:
        jour_courant = JOURS_SEMAINE_FR[now_dt.weekday()]
        planning = generer_planning_du_jour(jour_courant, planning_config["duree_affichage"])
        if planning:
            data.append(planning)

    return jsonify(data)



# ============================================================
# 🎙 Génération automatique des sous-titres (API Whisper)
# ============================================================
from flask import jsonify
from openai import OpenAI
import os
from utils import write_log, get_db_connection, get_static_event_dir

@evenements_bp.route("/evenements/generer_sous_titres/<int:event_id>", methods=["POST"])
@login_required
@require_access("evenements", "ecriture")
def generer_sous_titres(event_id):

    api_key = os.getenv("OPENAI_API_KEY")
    write_log(f"🔑 OPENAI_API_KEY utilisée = {api_key[:8]}...{api_key[-4:]}")

    if not api_key:
        write_log("❌ OPENAI_API_KEY absente – génération sous-titres impossible")
        return jsonify({"error": "Service de sous-titres indisponible"}), 503

    write_log(f"🔑 OPENAI_API_KEY chargée ? {'OUI' if api_key else 'NON'}")

    client = OpenAI(api_key=api_key)

    conn = get_db_connection()
    row = conn.execute("SELECT fichier_path FROM evenements WHERE id = ?", (event_id,)).fetchone()
    conn.close()

    if not row or not row["fichier_path"]:
        return jsonify({"error": "Aucun fichier vidéo pour cet événement"}), 404

    # Déduction du chemin absolu à partir du dossier evenements
    video_filename = os.path.basename(row["fichier_path"])
    video_abs = os.path.join(get_static_event_dir(), video_filename)

    if not os.path.exists(video_abs):
        return jsonify({"error": f"Fichier introuvable : {video_abs}"}), 404

    try:
        write_log(f"🎙 Génération sous-titres Whisper pour : {video_abs}")
        with open(video_abs, "rb") as f:
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="vtt",
                language="fr"
            )

        vtt_path = os.path.splitext(video_abs)[0] + ".vtt"
        with open(vtt_path, "w", encoding="utf-8") as out:
            out.write(transcription)  # ✅ transcription est déjà une chaîne

        write_log(f"✅ Sous-titres générés : {vtt_path}")
        return jsonify({"status": "ok", "path": vtt_path})
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        write_log(f"❌ Erreur génération sous-titres : {e}\n{err}")
        return jsonify({"error": str(e)})


# ==========================================================
# 🪶 Lecture et sauvegarde manuelle des sous-titres
# ==========================================================
@evenements_bp.route("/evenements/get_srt/<int:eid>")
@login_required
@require_access("evenements", "lecture")
def get_srt(eid):

    """Renvoie le contenu du fichier de sous-titres (.vtt ou .srt) d’un événement vidéo."""
    conn = get_db_connection()
    row = conn.execute("SELECT fichier_path FROM evenements WHERE id = ?", (eid,)).fetchone()
    conn.close()

    if not row or not row["fichier_path"]:
        return "Fichier non trouvé", 404

    base = os.path.splitext(os.path.basename(row["fichier_path"]))[0]
    upload_dir = get_upload_dir()
    for ext in (".vtt", ".srt"):
        srt_path = os.path.join(upload_dir, base + ext)
        if os.path.exists(srt_path):
            with open(srt_path, "r", encoding="utf-8") as f:
                return f.read()

    return "Sous-titres non trouvés", 404


@evenements_bp.route("/evenements/save_srt/<int:eid>", methods=["POST"])
@login_required
@require_access("evenements", "ecriture")
def save_srt(eid):

    """Sauvegarde les corrections manuelles du fichier de sous-titres."""
    from flask import request
    data = request.get_json()
    contenu = data.get("contenu", "")

    conn = get_db_connection()
    row = conn.execute("SELECT fichier_path FROM evenements WHERE id = ?", (eid,)).fetchone()
    conn.close()

    if not row or not row["fichier_path"]:
        return "Fichier non trouvé", 404

    base = os.path.splitext(os.path.basename(row["fichier_path"]))[0]
    srt_path = os.path.join(get_upload_dir(), base + ".vtt")

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(contenu)

    write_log(f"💾 Sous-titres corrigés enregistrés : {srt_path}")
    return "OK"




# ============================================================
# 🖥️ Page d’affichage public
# ============================================================

@evenements_bp.route("/affichage_evenement/<int:evenement_id>")
def affichage_evenement(evenement_id):

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    evenement = cur.execute(
        "SELECT * FROM evenements WHERE id = ?",
        (evenement_id,)
    ).fetchone()

    evenements_rows = cur.execute(
        "SELECT * FROM evenements WHERE actif = 1 ORDER BY date_debut"
    ).fetchall()

    conn.close()

    evenements = [dict(r) for r in evenements_rows]

    return render_template(
        "evenements/affichage_evenement.html",
        evenement=dict(evenement) if evenement else None,
        evenements=evenements
    )


def to_abs_path(web_path: str) -> str:
    """Convertit un chemin web /static/evenements/... en chemin absolu."""
    if not web_path:
        return ""
    upload_dir = get_static_event_dir()
    if web_path.startswith(WEB_PREFIX):
        rel = web_path[len(WEB_PREFIX):].lstrip("/")
        return os.path.join(upload_dir, rel)
    return os.path.join(upload_dir, os.path.basename(web_path))
