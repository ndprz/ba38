# ============================================================
# 🧰 Utilitaires internes au module Production Cuisine / Hygiène Cuisine
# ============================================================

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from werkzeug.utils import secure_filename

from ba38_utilitaires.core import get_db_connection, write_log

PARIS_TZ = ZoneInfo("Europe/Paris")

# Familles du référentiel recettes (cuisine_recettes_referentiel.famille)
# classées "carné" — tout le reste (Légumes, Légumineuses, Féculents,
# Œufs...) est "légumes", règle confirmée par le responsable cuisine.
FAMILLES_CARNEES = {"Viande", "Poisson", "Gibier"}


def categorie_depuis_famille(famille):
    """'carne' si la famille du référentiel recettes est Viande/Poisson/
    Gibier, sinon 'legumes' (y compris famille inconnue/absente — la
    catégorie reste de toute façon à confirmer par l'utilisateur, ce n'est
    qu'une suggestion de pré-remplissage)."""
    return "carne" if (famille or "").strip() in FAMILLES_CARNEES else "legumes"


def today_paris() -> str:
    """Date du jour (AAAA-MM-JJ) en heure de Paris — PAS UTC brut (cf. bug de
    fuseau horaire déjà rencontré sur api_evenements_actifs)."""
    return datetime.now(PARIS_TZ).strftime("%Y-%m-%d")


def now_paris_str() -> str:
    return datetime.now(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_temperature(valeur):
    """Normalise une température saisie au clavier tablette : le champ est
    en `type="text"` (pas `type="number"`) pour que le signe "-" reste
    disponible sur les claviers virtuels qui le masquent sinon — donc plus
    de normalisation automatique du séparateur décimal par le navigateur.
    Remplace la virgule (clavier français) par un point pour rester un
    nombre valide en base (sinon stocké tel quel comme texte, faussant
    silencieusement les totaux/comparaisons)."""
    valeur = (valeur or "").strip().replace(",", ".")
    return valeur or None


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


def decongelation_en_cours(conn, production_id):
    """True si une décongélation a été démarrée pour cette production et
    n'est pas encore terminée (heure_debut renseignée, heure_fin non)."""
    row = conn.execute(
        """SELECT 1 FROM cuisine_production_etapes
           WHERE production_id = ? AND etape_code = 'decongelation'
             AND heure_debut IS NOT NULL AND heure_fin IS NULL
           LIMIT 1""",
        (production_id,),
    ).fetchone()
    return row is not None


def etape_bloquante(conn, production_id, etape_code):
    """Retourne le libellé de la première étape précédente (ordre inférieur,
    non optionnelle) pas encore résolue — ni terminée, ni marquée non
    applicable — pour cette production, ou None si la voie est libre pour
    `etape_code`. Les étapes marquées `optionnelle` ne bloquent jamais la
    suite (ex. décongélation) — SAUF la décongélation elle-même : tant
    qu'elle est en cours, elle bloque prioritairement TOUTES les autres
    étapes (on ne fait rien d'autre pendant qu'un produit décongèle)."""
    if etape_code != "decongelation" and decongelation_en_cours(conn, production_id):
        return "Décongélation"

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


def etape_actuelle_libelle(conn, production_id):
    """Libellé court de l'étape où en est une production (en cours, ou
    prochaine à faire) — pour affichage dans la liste des productions du
    jour, à côté du statut, sans avoir à ouvrir la fiche."""
    if decongelation_en_cours(conn, production_id):
        return "🧊 Décongélation en cours"

    etapes_ref = conn.execute(
        "SELECT code, libelle, ordre, optionnelle FROM cuisine_etapes_ref WHERE actif = 1 ORDER BY ordre"
    ).fetchall()
    etapes_saisies = conn.execute(
        "SELECT etape_code, heure_fin FROM cuisine_production_etapes WHERE production_id = ? ORDER BY id",
        (production_id,),
    ).fetchall()
    etapes_par_code = {e["etape_code"]: e for e in etapes_saisies}
    codes_resolus = {e["etape_code"] for e in etapes_saisies if e["heure_fin"] is not None}

    for ref in etapes_ref:
        if ref["code"] in codes_resolus:
            continue
        precedentes = [r for r in etapes_ref if r["ordre"] < ref["ordre"] and not r["optionnelle"]]
        bloquante = next((p for p in precedentes if p["code"] not in codes_resolus), None)
        if bloquante:
            continue
        ligne = etapes_par_code.get(ref["code"])
        if ligne and ligne["heure_fin"] is None:
            return f"⏳ {ref['libelle']} en cours"
        return f"▶️ {ref['libelle']}"

    return "✅ Étapes terminées"


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


def calculer_conformite_production(conn, production_id):
    """Calcule la conformité HACCP de la production à partir des relevés de
    température des points "chauds" (fin de cuisson, tranchage à chaud
    début/fin, refroidissement à l'eau, conditionnement début/fin) et de la
    fin de mise en cellule. Règle confirmée avec le responsable cuisine :

      1. Parmi les températures des points chauds, on retient la plus
         petite qui reste strictement supérieure à 63°C (= le point le
         plus faible de la chaîne chaude) et l'heure à laquelle elle a été
         relevée.
      2. Conforme si (heure fin mise en cellule − heure retenue) < 120 min
         ET température fin mise en cellule < 10°C.
      3. Si aucune température ne dépasse 63°, conformité impossible à
         établir → non conforme par défaut (principe de précaution).

    N'écrit rien : retourne (statut, motif) où statut vaut 'conforme',
    'non_conforme', ou None si la mise en cellule n'est pas encore
    terminée (rien à calculer). L'appelant se charge d'enregistrer le
    résultat dans cuisine_production_etapes.conforme (ligne
    'refroidissement_cellule' — pas de colonne dédiée)."""
    lignes = {
        row["etape_code"]: row
        for row in conn.execute(
            """SELECT etape_code, heure_debut, heure_fin, temperature, temperature_debut
               FROM cuisine_production_etapes
               WHERE production_id = ?
               ORDER BY id""",
            (production_id,),
        ).fetchall()
    }

    mise_en_cellule = lignes.get("refroidissement_cellule")
    if not mise_en_cellule or mise_en_cellule["heure_fin"] is None or mise_en_cellule["temperature"] is None:
        return None, "Mise en cellule non terminée."

    points_chauds = []
    cuisson = lignes.get("cuisson")
    if cuisson and cuisson["temperature"] is not None and cuisson["heure_fin"]:
        points_chauds.append((cuisson["temperature"], cuisson["heure_fin"]))
    tranchage_chaud = lignes.get("tranchage_chaud")
    if tranchage_chaud:
        if tranchage_chaud["temperature_debut"] is not None and tranchage_chaud["heure_debut"]:
            points_chauds.append((tranchage_chaud["temperature_debut"], tranchage_chaud["heure_debut"]))
        if tranchage_chaud["temperature"] is not None and tranchage_chaud["heure_fin"]:
            points_chauds.append((tranchage_chaud["temperature"], tranchage_chaud["heure_fin"]))
    refroidissement_eau = lignes.get("refroidissement_eau")
    if refroidissement_eau and refroidissement_eau["temperature"] is not None and refroidissement_eau["heure_fin"]:
        points_chauds.append((refroidissement_eau["temperature"], refroidissement_eau["heure_fin"]))
    conditionnement = lignes.get("conditionnement")
    if conditionnement:
        if conditionnement["temperature_debut"] is not None and conditionnement["heure_debut"]:
            points_chauds.append((conditionnement["temperature_debut"], conditionnement["heure_debut"]))
        if conditionnement["temperature"] is not None and conditionnement["heure_fin"]:
            points_chauds.append((conditionnement["temperature"], conditionnement["heure_fin"]))

    candidats = [(temp, heure) for temp, heure in points_chauds if temp > 63]
    if not candidats:
        return "non_conforme", "Aucune température relevée supérieure à 63°C : conformité impossible à établir."

    temp_reference, heure_reference = min(candidats, key=lambda x: x[0])

    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        dt_reference = datetime.strptime(heure_reference, fmt)
        dt_fin_cellule = datetime.strptime(mise_en_cellule["heure_fin"], fmt)
    except ValueError:
        return "non_conforme", "Heure illisible, conformité impossible à établir."

    delta_minutes = (dt_fin_cellule - dt_reference).total_seconds() / 60
    temperature_fin_cellule = mise_en_cellule["temperature"]

    conforme = delta_minutes < 120 and temperature_fin_cellule < 10
    statut = "conforme" if conforme else "non_conforme"
    motif = (
        f"Écart {delta_minutes:.0f} min depuis {temp_reference}°C relevée à {heure_reference} "
        f"(seuil 120 min) · température fin mise en cellule {temperature_fin_cellule}°C (seuil < 10°C)."
    )
    return statut, motif


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
