# ============================================================
# 🧰 Utilitaires internes au module Production Cuisine / Hygiène Cuisine
# ============================================================

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from werkzeug.utils import secure_filename

from ba38_utilitaires.core import get_db_connection, write_log

PARIS_TZ = ZoneInfo("Europe/Paris")


def today_paris() -> str:
    """Date du jour (AAAA-MM-JJ) en heure de Paris — PAS UTC brut (cf. bug de
    fuseau horaire déjà rencontré sur api_evenements_actifs)."""
    return datetime.now(PARIS_TZ).strftime("%Y-%m-%d")


def now_paris_str() -> str:
    return datetime.now(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _connect():
    conn = get_db_connection()
    return conn


def upload_dir_reception(reception_id):
    """uploads/production_cuisine/receptions/<reception_id>/"""
    base_dir = os.getenv("BA38_BASE_DIR", ".")
    dossier = os.path.join(base_dir, "uploads", "production_cuisine", "receptions", str(reception_id))
    os.makedirs(dossier, exist_ok=True)
    return dossier


def upload_dir_traca_lot(production_id, lot_id):
    """uploads/production_cuisine/tracabilite/<production_id>/<lot_id>/"""
    base_dir = os.getenv("BA38_BASE_DIR", ".")
    dossier = os.path.join(
        base_dir, "uploads", "production_cuisine", "tracabilite", str(production_id), str(lot_id)
    )
    os.makedirs(dossier, exist_ok=True)
    return dossier


def save_uploaded_files(files, dossier, prefix=""):
    """Enregistre une liste de fichiers uploadés (request.files.getlist(...))
    dans `dossier` et retourne la liste des chemins absolus sauvegardés.
    Ignore silencieusement les entrées sans nom de fichier (input vide)."""
    chemins = []
    for i, f in enumerate(files):
        if not f or not f.filename:
            continue
        filename = secure_filename(f.filename)
        if prefix:
            filename = f"{prefix}_{i}_{filename}"
        abs_path = os.path.join(dossier, filename)
        try:
            f.save(abs_path)
            chemins.append(abs_path)
        except Exception as e:
            write_log(f"❌ Erreur sauvegarde fichier production_cuisine ({filename}) : {e}")
    return chemins
