# ============================================================
# 🧰 Utilitaires internes au module Production Cuisine / Hygiène Cuisine
# ============================================================

import os
from datetime import date, datetime, timedelta
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


# Une production pas terminée (cuisson de nuit, validation oubliée la veille)
# reste affichée dans les productions du jour pendant ce nombre de jours —
# 3 pour couvrir un week-end, sans traîner indéfiniment les oubliées.
JOURS_REPORT_PRODUCTIONS = 3


def date_debut_report(date_jour: str) -> str:
    """Date (AAAA-MM-JJ) à partir de laquelle une production encore en cours
    est reportée sur `date_jour`."""
    return (date.fromisoformat(date_jour) - timedelta(days=JOURS_REPORT_PRODUCTIONS)).isoformat()


def now_paris_str() -> str:
    return datetime.now(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_poids(valeur):
    """Poids saisi (virgule tolérée) → float > 0, sinon None."""
    valeur = (valeur or "").strip().replace(",", ".")
    try:
        poids = float(valeur)
    except ValueError:
        return None
    return poids if poids > 0 else None


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


def point_reference_refroidissement(lignes):
    """Point de départ du refroidissement (« DR ») : parmi les relevés des
    points chauds (fin de cuisson, tranchage à chaud début/fin,
    refroidissement à l'eau, conditionnement début/fin), la plus petite
    température supérieure ou égale à 63°C et l'heure de ce relevé.

    `lignes` : {etape_code: ligne cuisine_production_etapes} (dernière ligne
    par étape). Retourne (temperature, heure 'YYYY-MM-DD HH:MM:SS') ou None
    si aucun relevé n'atteint 63°C. Sert à la conformité HACCP
    (calculer_conformite_production) et à l'écran kiosk cuisine."""
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

    candidats = [(temp, heure) for temp, heure in points_chauds if temp >= 63]
    if not candidats:
        return None
    return min(candidats, key=lambda x: x[0])


def heure_dr(conn, production_id, seulement_cellule_en_cours=False):
    """Heure 'HH:MM' du DR (point_reference_refroidissement) d'une
    production, ou None si aucun relevé n'atteint 63°C. Avec
    seulement_cellule_en_cours=True, None aussi tant que la mise en cellule
    n'est pas démarrée ou une fois terminée (liste des productions)."""
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
    if seulement_cellule_en_cours:
        cellule = lignes.get("refroidissement_cellule")
        if not cellule or cellule["heure_fin"] is not None:
            return None
    reference = point_reference_refroidissement(lignes)
    return reference[1][11:16] if reference else None


def calculer_conformite_production(conn, production_id):
    """Calcule la conformité HACCP de la production à partir des relevés de
    température des points "chauds" (fin de cuisson, tranchage à chaud
    début/fin, refroidissement à l'eau, conditionnement début/fin) et de la
    fin de mise en cellule. Règle confirmée avec le responsable cuisine :

      1. Parmi les températures des points chauds, on retient la plus
         petite qui reste supérieure ou égale à 63°C (= le point le
         plus faible de la chaîne chaude) et l'heure à laquelle elle a été
         relevée.
      2. Conforme si (heure fin mise en cellule − heure retenue) < 120 min
         ET température fin mise en cellule < 10°C.
      3. Si aucune température n'atteint 63°, conformité impossible à
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

    reference = point_reference_refroidissement(lignes)
    if reference is None:
        return "non_conforme", "Aucune température relevée supérieure ou égale à 63°C : conformité impossible à établir."
    temp_reference, heure_reference = reference

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
    if not conforme:
        # Préciser le(s) critère(s) en échec en tête du motif.
        echecs = []
        if delta_minutes >= 120:
            echecs.append(f"refroidissement trop long ({delta_minutes:.0f} min, max 120 min)")
        if temperature_fin_cellule >= 10:
            echecs.append(f"température en fin de mise en cellule trop élevée ({temperature_fin_cellule}°C, doit être < 10°C)")
        texte = " et ".join(echecs)
        motif = texte[0].upper() + texte[1:] + ". Détail : " + motif
    return statut, motif


def motifs_non_conformite(conn, production_id):
    """Motifs lisibles des non-conformités d'une production (liste vide si
    aucune) : étapes marquées non conformes et réceptions rattachées dont
    un contrôle est non conforme. Le motif de la mise en cellule n'est pas
    stocké : il est recalculé à partir des relevés, comme au moment où la
    conformité a été établie."""
    motifs = []
    etapes = conn.execute(
        """SELECT e.etape_code, e.commentaire, r.libelle
           FROM cuisine_production_etapes e
           JOIN cuisine_etapes_ref r ON r.code = e.etape_code
           WHERE e.production_id = ? AND e.conforme = 'non_conforme'
             AND COALESCE(e.non_applicable, 0) = 0
           ORDER BY r.ordre""",
        (production_id,),
    ).fetchall()
    for etape_code, commentaire, libelle in etapes:
        if etape_code == "refroidissement_cellule":
            statut, motif = calculer_conformite_production(conn, production_id)
            if statut != "non_conforme":
                motif = "marquée non conforme (relevés corrigés depuis : à vérifier)."
        else:
            motif = "marquée non conforme."
        if commentaire:
            motif += f" Commentaire : {commentaire}"
        motifs.append(f"{libelle} — {motif}")

    controles = (("aspect_conforme", "aspect"), ("emballage_conforme", "emballage"),
                 ("etiquetage_conforme", "étiquetage"))
    receptions = conn.execute(
        """SELECT r.libelle_produit, r.aspect_conforme, r.emballage_conforme,
                  r.etiquetage_conforme, f.nom
           FROM cuisine_reception_utilisations ru
           JOIN cuisine_receptions r ON r.id = ru.reception_id
           LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
           WHERE ru.production_id = ? AND r.actif = 1""",
        (production_id,),
    ).fetchall()
    for rec in receptions:
        valeurs = dict(zip(("aspect_conforme", "emballage_conforme", "etiquetage_conforme"), rec[1:4]))
        ko = [nom for champ, nom in controles if valeurs[champ] == "non_conforme"]
        if ko:
            motifs.append(
                f"Réception {rec[0] or '—'} ({rec[4] or '—'}) — contrôle non conforme : {', '.join(ko)}."
            )
    return motifs


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
            continue
        reduire_photo(abs_path)
    return chemins


# Taille max (plus grand côté, en pixels) et qualité JPEG des photos
# Cuisine. Les tablettes photographient en 8 Mpx (~2 Mo) : 1600 px en
# qualité 80 reste largement lisible pour une étiquette / un n° de lot
# pour ~180 Ko. La page tablette réduit déjà avant envoi
# (_camera_capture_js.html), ceci est le filet de sécurité serveur.
PHOTO_MAX_PX = 1600
PHOTO_QUALITE_JPEG = 80


def reduire_photo(abs_path):
    """Réduit sur place une photo trop grande (même chemin, même format →
    aucun changement en base). Retourne True si le fichier a été réécrit.
    Toute erreur (fichier non image, Pillow absent...) laisse l'original
    intact : une photo trop lourde vaut mieux qu'une photo perdue."""
    try:
        from PIL import Image, ImageOps

        with Image.open(abs_path) as im:
            fmt = im.format
            if fmt not in ("JPEG", "MPO", "PNG", "WEBP") or max(im.size) <= PHOTO_MAX_PX:
                return False
            exif = im.getexif()
            im = ImageOps.exif_transpose(im)
            im.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX), Image.LANCZOS)
            # Orientation déjà appliquée aux pixels : on la neutralise pour
            # qu'un visualiseur ne la réapplique pas une seconde fois.
            exif[0x0112] = 1
            tmp_path = abs_path + ".tmp"
            if fmt in ("JPEG", "MPO"):
                im.convert("RGB").save(tmp_path, "JPEG", quality=PHOTO_QUALITE_JPEG,
                                       optimize=True, exif=exif.tobytes())
            else:
                im.save(tmp_path, fmt, optimize=True)
        os.replace(tmp_path, abs_path)
        return True
    except Exception as e:
        write_log(f"⚠️ Réduction photo production_cuisine impossible ({abs_path}) : {e}")
        try:
            if os.path.exists(abs_path + ".tmp"):
                os.remove(abs_path + ".tmp")
        except OSError:
            pass
        return False


# Barquettes sorties du stock par des bons de livraison non annulés, par
# (production_id, article_id) — clé stable d'une ligne de
# cuisine_stock_barquettes (recréée à chaque "Ajouter au stock", donc jamais
# décrémentée elle-même).
SQL_QUANTITES_LIVREES = """
    SELECT l.production_id, l.article_id, SUM(l.quantite) AS quantite_livree
    FROM cuisine_bons_livraison_lignes l
    JOIN cuisine_bons_livraison b ON b.id = l.bon_id
    WHERE b.statut != 'annule'
    GROUP BY l.production_id, l.article_id
"""


def stock_lignes_disponibles(conn):
    """Lignes de stock actives avec quantite_livree et disponible
    (= quantite − livré), plus taille / nb_portions / libellé de l'article."""
    return conn.execute(
        f"""
        SELECT s.*, a.taille, a.nb_portions, a.libelle AS article_libelle,
               COALESCE(lv.quantite_livree, 0) AS quantite_livree,
               s.quantite - COALESCE(lv.quantite_livree, 0) AS disponible
        FROM cuisine_stock_barquettes s
        JOIN cuisine_articles_barquettes a ON a.id = s.article_id
        LEFT JOIN ({SQL_QUANTITES_LIVREES}) lv
               ON lv.production_id = s.production_id AND lv.article_id = s.article_id
        WHERE s.actif = 1 AND a.actif = 1
        """
    ).fetchall()


def quantites_livrees_production(conn, production_id):
    """{article_id: barquettes livrées (BL non annulés)} pour une production."""
    return {
        r["article_id"]: r["quantite_livree"]
        for r in conn.execute(
            f"SELECT * FROM ({SQL_QUANTITES_LIVREES}) WHERE production_id = ?", (production_id,)
        ).fetchall()
    }


# DLC des barquettes : date de production + N jours, N dans le paramètre
# `cuisine_dlc_jours` (table parametres, catégorie 'config' ; J+3 au départ,
# J+5 après l'agrément DDETS). Figée sur la ligne de stock à son entrée :
# changer le paramètre ne modifie que les productions mises en stock ensuite.
PARAM_DLC_JOURS = "cuisine_dlc_jours"
DLC_JOURS_DEFAUT = 3


def dlc_jours(conn):
    row = conn.execute(
        "SELECT param_value FROM parametres WHERE param_name = ?", (PARAM_DLC_JOURS,)
    ).fetchone()
    try:
        return int(row[0]) if row else DLC_JOURS_DEFAUT
    except (TypeError, ValueError):
        return DLC_JOURS_DEFAUT


def calculer_dlc(date_production, jours):
    """'2026-09-24' + 3 → '2026-09-27' (None si date illisible)."""
    try:
        return (date.fromisoformat((date_production or "")[:10]) + timedelta(days=jours)).isoformat()
    except ValueError:
        return None



# ------------------------------------------------------------
# 📦 Stock des réceptions : solde disponible et DLC
# ------------------------------------------------------------
# Une réception (ligne produit) peut alimenter plusieurs productions, chacune
# pour une partie du poids (cuisine_reception_utilisations). Solde = poids
# reçu − poids utilisés − poids purgé. La réception reste proposée tant qu'il
# reste un solde, qu'elle n'est pas purgée et que sa DLC le permet.
SOLDE_EPSILON = 0.0005

SQL_RECEPTIONS_SOLDE = """
    SELECT r.*, f.nom AS fournisseur_nom,
           COALESCE(u.poids_utilise, 0) AS poids_utilise,
           COALESCE(u.nb_utilisations, 0) AS nb_utilisations,
           ROUND(COALESCE(r.poids_kg, 0) - COALESCE(u.poids_utilise, 0)
                 - COALESCE(r.poids_purge, 0), 3) AS solde_kg
    FROM cuisine_receptions r
    LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
    LEFT JOIN (
        SELECT ru.reception_id, SUM(ru.poids_kg) AS poids_utilise, COUNT(*) AS nb_utilisations
        FROM cuisine_reception_utilisations ru
        JOIN cuisine_productions p ON p.id = ru.production_id AND p.actif = 1
        GROUP BY ru.reception_id
    ) u ON u.reception_id = r.id
"""


GROUPE_SURGELE = "surgelé"


def _date_fr(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}"


def est_surgele(reception):
    """Groupe d'ingrédient « Surgelé » : DLC / DDM non contrôlées."""
    return (reception["ingredient_groupe"] or "").strip().lower() == GROUPE_SURGELE


def controle_dates(reception, date_production):
    """(blocage, avertissement) pour cuisiner cette réception dans une
    production du `date_production` (AAAA-MM-JJ).
    - blocage : impossible, même en forçant (réceptionnée après la date).
    - avertissement : DLC ou DDM dépassée (ou aucune des deux renseignée,
      réceptions historiques d'un autre jour) — utilisation possible en
      forçant, le forçage est tracé sur l'utilisation.
    Groupe Surgelé : aucune date contrôlée."""
    if reception["date_reception"] > date_production:
        return "réceptionnée après la date de production", None
    if est_surgele(reception):
        return None, None
    dlc, ddm = reception["dlc_ddm"], reception["ddm"]
    motifs = []
    if dlc and dlc < date_production:
        motifs.append(f"DLC dépassée ({_date_fr(dlc)})")
    if ddm and ddm < date_production:
        motifs.append(f"DDM dépassée ({_date_fr(ddm)})")
    if not dlc and not ddm and reception["date_reception"] != date_production:
        motifs.append("DLC / DDM non renseignée (réception d'un autre jour)")
    return None, (" · ".join(motifs) or None)


def receptions_en_stock(conn, date_ref):
    """Réceptions actives, non purgées, avec un solde > 0, reçues au plus
    tard le `date_ref` — tous jours confondus (un arrivage peut servir
    plusieurs jours). Chaque ligne (dict) porte `blocage` et `avertissement`
    (controle_dates pour `date_ref`). Tri : DLC / DDM la plus proche d'abord."""
    rows = conn.execute(
        f"""SELECT * FROM ({SQL_RECEPTIONS_SOLDE})
            WHERE actif = 1 AND date_purge IS NULL AND solde_kg > ? AND date_reception <= ?
            ORDER BY COALESCE(dlc_ddm, ddm) IS NULL,
                     MIN(COALESCE(dlc_ddm, ddm), COALESCE(ddm, dlc_ddm)),
                     date_reception, heure_arrivee, id""",
        (SOLDE_EPSILON, date_ref),
    ).fetchall()
    stock = []
    for r in rows:
        d = dict(r)
        d["blocage"], d["avertissement"] = controle_dates(d, date_ref)
        stock.append(d)
    return stock


def reception_avec_solde(conn, reception_id):
    return conn.execute(
        f"SELECT * FROM ({SQL_RECEPTIONS_SOLDE}) WHERE id = ?", (reception_id,)
    ).fetchone()


def ajouter_utilisation(conn, reception_id, production, poids, user, forcer=False):
    """Enregistre l'utilisation de `poids` kg de la réception dans la
    production (ligne cuisine_productions). Une DLC / DDM dépassée n'est
    acceptée qu'avec `forcer` (avertissement gardé dans `forcage`).
    Retourne un message d'erreur, ou None si enregistrée (commit à la charge
    de l'appelant)."""
    reception = reception_avec_solde(conn, reception_id)
    if not reception or not reception["actif"]:
        return "réception introuvable."
    libelle = reception["libelle_produit"] or f"réception #{reception_id}"
    if reception["date_purge"]:
        return f"{libelle} : réception purgée, plus utilisable."
    blocage, avertissement = controle_dates(reception, production["date_production"])
    if blocage:
        return f"{libelle} : impossible de cuisiner — {blocage}."
    if avertissement and not forcer:
        return f"{libelle} : {avertissement} — cocher « Forcer l'utilisation » pour la cuisiner quand même."
    if poids is None or poids <= 0:
        return f"{libelle} : merci d'indiquer le poids utilisé (en kg, supérieur à 0)."
    if poids > reception["solde_kg"] + SOLDE_EPSILON:
        return f"{libelle} : {format_kg(poids)} kg demandés, il n'en reste que {format_kg(reception['solde_kg'])} kg."
    conn.execute(
        """INSERT INTO cuisine_reception_utilisations (reception_id, production_id, poids_kg, user_creation, forcage)
           VALUES (?, ?, ?, ?, ?)""",
        (reception_id, production["id"], round(poids, 3), user or None, avertissement),
    )
    return None


def format_kg(valeur):
    """12.5 → '12,5' ; 12.0 → '12' (affichage des poids en kg)."""
    if valeur is None:
        return "—"
    texte = f"{valeur:.3f}".rstrip("0").rstrip(".")
    return texte.replace(".", ",")


# ------------------------------------------------------------
# 👤 Utilisateurs cuisine (prénoms proposés dans « Nom du réceptionnaire »)
#     Liste dans la table `parametres` (param_name cuisine_utilisateur),
#     gérée depuis Paramètres cuisine ou complétée à la saisie (option
#     « ➕ Nouveau… » du champ, cf. _champ_utilisateur.html).
# ------------------------------------------------------------
PARAM_UTILISATEURS = "cuisine_utilisateur"
NOUVEL_UTILISATEUR = "__nouveau__"


def utilisateurs_cuisine(conn=None):
    """Prénoms de la liste, triés."""
    def _lire(c):
        return [r[0] for r in c.execute(
            "SELECT param_value FROM parametres WHERE param_name = ? ORDER BY param_value COLLATE NOCASE",
            (PARAM_UTILISATEURS,),
        ).fetchall()]
    if conn is not None:
        return _lire(conn)
    with _connect() as c:
        return _lire(c)


def normaliser_prenom(valeur):
    """Espaces superflus retirés, 1re lettre en majuscule ("thomas" → "Thomas")."""
    valeur = " ".join((valeur or "").split())
    return valeur[:1].upper() + valeur[1:]


def utilisateur_saisi(form, champ="benevole"):
    """Nom choisi dans la liste, ou saisi via « ➕ Nouveau… » (champ
    `<champ>_nouveau`). Ne l'ajoute pas à la liste : voir
    enregistrer_utilisateur_cuisine, appelé une fois l'action enregistrée."""
    valeur = (form.get(champ) or "").strip()
    if valeur == NOUVEL_UTILISATEUR:
        valeur = form.get(f"{champ}_nouveau")
    return normaliser_prenom(valeur)


def enregistrer_utilisateur_cuisine(conn, nom):
    """Ajoute `nom` à la liste s'il n'y est pas (casse ignorée : "thomas"
    ne crée pas de doublon de "Thomas"). Retourne le nom tel qu'il figure
    dans la liste. Commit à la charge de l'appelant."""
    nom = normaliser_prenom(nom)
    if not nom:
        return nom
    for existant in utilisateurs_cuisine(conn):
        if existant.casefold() == nom.casefold():
            return existant
    conn.execute(
        "INSERT INTO parametres (param_name, param_value, categorie) VALUES (?, ?, 'liste')",
        (PARAM_UTILISATEURS, nom),
    )
    return nom
