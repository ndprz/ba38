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


def etape_bloquante(conn, production_id, etape_code):
    """Retourne le libellé de la première étape précédente (ordre inférieur,
    non optionnelle) pas encore résolue — ni terminée, ni marquée non
    applicable — pour cette production, ou None si la voie est libre pour
    `etape_code`. Les étapes marquées `optionnelle` ne bloquent jamais la
    suite (ex. décongélation)."""
    cible = conn.execute(
        "SELECT ordre FROM cuisine_etapes_ref WHERE code = ?", (etape_code,)
    ).fetchone()
    if not cible:
        return None

    precedentes = conn.execute(
        """SELECT code, libelle FROM cuisine_etapes_ref
           WHERE actif = 1 AND ordre < ? AND optionnelle = 0
           ORDER BY ordre""",
        (cible["ordre"],),
    ).fetchall()
    if not precedentes:
        return None

    codes_resolus = {
        row["etape_code"]
        for row in conn.execute(
            """SELECT DISTINCT etape_code FROM cuisine_production_etapes
               WHERE production_id = ? AND heure_fin IS NOT NULL""",
            (production_id,),
        ).fetchall()
    }
    for p in precedentes:
        if p["code"] not in codes_resolus:
            return p["libelle"]
    return None


def heure_fin_max_precedentes(conn, production_id, etape_code):
    """Heure de fin la plus tardive parmi les étapes précédentes (ordre
    inférieur, non optionnelles) déjà résolues pour cette production — sert
    de repère pour détecter une saisie d'heure incohérente (étape terminée
    "avant" une étape censée la précéder)."""
    cible = conn.execute(
        "SELECT ordre FROM cuisine_etapes_ref WHERE code = ?", (etape_code,)
    ).fetchone()
    if not cible:
        return None
    row = conn.execute(
        """SELECT MAX(e.heure_fin) AS m
           FROM cuisine_production_etapes e
           JOIN cuisine_etapes_ref r ON r.code = e.etape_code
           WHERE e.production_id = ? AND e.heure_fin IS NOT NULL
             AND r.actif = 1 AND r.optionnelle = 0 AND r.ordre < ?""",
        (production_id, cible["ordre"]),
    ).fetchone()
    return row["m"] if row else None


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
