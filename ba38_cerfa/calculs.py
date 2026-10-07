"""Calculs du module CERFA abandon de frais : barème kilométrique, totaux,
somme en toutes lettres, géocodage et distance routière domicile → lieu."""

import json
import math
from decimal import Decimal, ROUND_HALF_UP

import requests

from ba38_utilitaires.core import write_log

from ba38_cerfa.constants import BAREME_DEFAUT


# ============================================================================
# 🔢 Lecture des JSON stockés en base
# ============================================================================
def charger_json(texte, defaut=None):
    if not texte:
        return {} if defaut is None else defaut
    try:
        return json.loads(texte)
    except (TypeError, ValueError):
        return {} if defaut is None else defaut


def bareme_campagne(campagne):
    bareme = charger_json(campagne["bareme_json"] if campagne else None)
    return bareme or BAREME_DEFAUT


def arrondi_euro(valeur):
    """Arrondi commercial à l'euro (0,5 → supérieur), pas l'arrondi bancaire."""
    return int(Decimal(str(valeur or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def arrondi_centimes(valeur):
    return float(Decimal(str(valeur or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ============================================================================
# 🚗 Barème kilométrique
# ============================================================================
def ligne_bareme(bareme, vehicule_type, cv):
    categorie = bareme.get(vehicule_type or "voiture")
    if not categorie or not categorie.get("lignes"):
        return None, None
    lignes = categorie["lignes"]
    try:
        cv = int(cv) if cv not in (None, "") else None
    except (TypeError, ValueError):
        cv = None
    if cv is None and vehicule_type != "cyclomoteur":
        return categorie, None
    for ligne in lignes:
        if ligne.get("cv_max") is None or (cv is not None and cv <= ligne["cv_max"]):
            return categorie, ligne
    return categorie, lignes[-1]


def montant_bareme(bareme, vehicule_type, cv, distance, electrique=False, majoration_electrique=0.20):
    """Frais kilométriques selon le barème fiscal pour une distance annuelle."""
    categorie, ligne = ligne_bareme(bareme, vehicule_type, cv)
    if not ligne or not distance:
        return 0.0
    seuil1, seuil2 = categorie["seuils"]
    d = float(distance)
    if d <= seuil1:
        montant = d * float(ligne["a1"])
    elif d <= seuil2:
        montant = d * float(ligne["a2"]) + float(ligne["b2"])
    else:
        montant = d * float(ligne["a3"])
    if electrique:
        montant *= 1 + float(majoration_electrique or 0)
    return montant


# ============================================================================
# 📊 Totaux d'une déclaration
# ============================================================================
def totaux_declaration(declaration, campagne, lieux):
    """Recalcule journées, distance et montant à partir des réponses.

    journees_json = {lieu_id: {mois(1..12): nb}} ; km_json = {lieu_id: km A/R}
    """
    journees = charger_json(declaration["journees_json"])
    km = charger_json(declaration["km_json"])

    par_lieu = []
    total_journees = 0
    distance_totale = 0.0
    for lieu in lieux:
        cle = str(lieu["id"])
        nb = sum(int(v or 0) for v in (journees.get(cle) or {}).values())
        km_ar = _float(km.get(cle))
        distance = nb * km_ar
        total_journees += nb
        distance_totale += distance
        par_lieu.append({"lieu": lieu, "journees": nb, "km_ar": km_ar, "distance": distance})

    resultat = {
        "par_lieu": par_lieu,
        "total_journees": total_journees,
        "distance_totale": round(distance_totale, 1),
        "montant": 0.0,
        "montant_arrondi": 0,
    }

    if declaration["imposable"] == 1:
        montant = montant_bareme(
            bareme_campagne(campagne),
            declaration["vehicule_type"],
            declaration["vehicule_cv"],
            distance_totale,
            electrique=bool(declaration["vehicule_electrique"]),
            majoration_electrique=campagne["majoration_electrique"],
        )
        resultat["montant"] = round(montant, 2)
        resultat["montant_arrondi"] = arrondi_euro(montant)
    elif declaration["imposable"] == 0:
        montant = total_journees * int(campagne["nb_tickets_par_journee"] or 0) * float(campagne["prix_ticket_tag"] or 0)
        resultat["montant"] = arrondi_centimes(montant)
        resultat["montant_arrondi"] = arrondi_euro(montant)

    return resultat


def _float(valeur):
    try:
        return float(str(valeur).replace(",", ".")) if valeur not in (None, "") else 0.0
    except ValueError:
        return 0.0


def champs_manquants(declaration, campagne, lieux, pieces):
    """Liste des éléments manquants empêchant de transmettre la déclaration."""
    manquants = []
    if declaration["imposable"] not in (0, 1):
        manquants.append("situation fiscale (imposable ou non)")
        return manquants
    for champ, libelle in (("nom", "nom"), ("prenom", "prénom"), ("rue", "adresse"),
                           ("code_postal", "code postal"), ("ville", "ville")):
        if not (declaration[champ] or "").strip():
            manquants.append(libelle)

    totaux = totaux_declaration(declaration, campagne, lieux)
    if totaux["total_journees"] <= 0:
        manquants.append("au moins une journée d'activité")

    autres = [p for p in totaux["par_lieu"] if p["lieu"]["est_autre"] and p["journees"] > 0]
    if autres and not (declaration["commentaire_autres"] or "").strip():
        manquants.append("le commentaire précisant les journées « Autres »")

    types_pieces = {p["type_piece"] for p in pieces}
    if declaration["imposable"] == 1:
        if not declaration["vehicule_type"]:
            manquants.append("type de véhicule")
        if declaration["vehicule_type"] != "cyclomoteur" and not declaration["vehicule_cv"]:
            manquants.append("puissance fiscale du véhicule (CV)")
        if not (declaration["vehicule_marque"] or "").strip():
            manquants.append("marque du véhicule")
        if not (declaration["vehicule_immatriculation"] or "").strip():
            manquants.append("immatriculation du véhicule")
        sans_km = [p["lieu"]["nom"] for p in totaux["par_lieu"] if p["journees"] > 0 and p["km_ar"] <= 0]
        if sans_km:
            manquants.append("km aller-retour pour : " + ", ".join(sans_km))
        if "carte_grise" not in types_pieces:
            manquants.append("copie de la carte grise")
    else:
        if "avis_non_imposition" not in types_pieces:
            manquants.append("copie de l'avis de non-imposition")
    return manquants


# ============================================================================
# 🔤 Somme en toutes lettres (orthographe traditionnelle)
# ============================================================================
_UNITES = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf",
           "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
           "dix-sept", "dix-huit", "dix-neuf"]
_DIZAINES = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}


def _moins_de_cent(n):
    if n < 20:
        return _UNITES[n]
    dizaine, unite = divmod(n, 10)
    if dizaine in (7, 9):
        base = "soixante" if dizaine == 7 else "quatre-vingt"
        reste = 10 + unite
        if dizaine == 7 and unite == 1:
            return "soixante et onze"
        return f"{base}-{_UNITES[reste]}"
    if dizaine == 8:
        return "quatre-vingts" if unite == 0 else f"quatre-vingt-{_UNITES[unite]}"
    base = _DIZAINES[dizaine]
    if unite == 0:
        return base
    if unite == 1:
        return f"{base} et un"
    return f"{base}-{_UNITES[unite]}"


def _moins_de_mille(n, final=True):
    centaines, reste = divmod(n, 100)
    parties = []
    if centaines:
        if centaines == 1:
            parties.append("cent")
        else:
            # « cents » seulement s'il termine le nombre (deux cents, mais deux cent mille)
            pluriel = "s" if reste == 0 and final else ""
            parties.append(f"{_UNITES[centaines]} cent{pluriel}")
    if reste:
        texte = _moins_de_cent(reste)
        if not final and texte == "quatre-vingts":
            texte = "quatre-vingt"
        parties.append(texte)
    return " ".join(parties)


def nombre_en_lettres(n):
    n = int(n)
    if n == 0:
        return "zéro"
    if n < 0:
        return "moins " + nombre_en_lettres(-n)
    parties = []
    millions, reste = divmod(n, 1_000_000)
    milliers, unites = divmod(reste, 1000)
    if millions:
        mot = "million" if millions == 1 else "millions"
        parties.append(f"{_moins_de_mille(millions)} {mot}")
    if milliers:
        parties.append("mille" if milliers == 1 else f"{_moins_de_mille(milliers, final=False)} mille")
    if unites:
        parties.append(_moins_de_mille(unites))
    return " ".join(parties)


def montant_en_lettres(montant_euros):
    n = int(montant_euros)
    return f"{nombre_en_lettres(n)} euro{'s' if n > 1 else ''}"


# ============================================================================
# 📍 Géocodage (Base Adresse Nationale) et distance routière (OSRM)
# ============================================================================
GEOCODEUR_URLS = [
    "https://data.geopf.fr/geocodage/search",
    "https://api-adresse.data.gouv.fr/search/",
]
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"


def geocoder(adresse):
    """Retourne (lat, lon, label, score) ou None. Adresses françaises (BAN)."""
    adresse = (adresse or "").replace("\r", " ").replace("\n", " ").strip()
    if not adresse:
        return None
    for url in GEOCODEUR_URLS:
        try:
            r = requests.get(url, params={"q": adresse, "limit": 1}, timeout=8)
            r.raise_for_status()
            features = r.json().get("features") or []
            if not features:
                return None
            f = features[0]
            lon, lat = f["geometry"]["coordinates"]
            props = f.get("properties", {})
            return lat, lon, props.get("label"), props.get("score")
        except Exception as e:
            write_log(f"⚠️ CERFA géocodage via {url} échoué pour « {adresse} » : {e}")
    return None


def distance_route_km(lat1, lon1, lat2, lon2):
    """Distance routière en km (OSRM), repli sur distance à vol d'oiseau × 1,3."""
    try:
        r = requests.get(OSRM_URL.format(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2), timeout=8)
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            return data["routes"][0]["distance"] / 1000.0, "route"
    except Exception as e:
        write_log(f"⚠️ CERFA OSRM indisponible : {e}")
    return _haversine_km(lat1, lon1, lat2, lon2) * 1.3, "estimation"


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def km_auto_par_lieu(adresse_benevole, lieux):
    """Calcule les km aller-retour domicile → chaque lieu géocodé.

    Retourne (km_par_lieu {lieu_id: km}, geocodage_benevole | None).
    """
    geo = geocoder(adresse_benevole)
    if not geo:
        return {}, None
    lat, lon, _, _ = geo
    resultats = {}
    for lieu in lieux:
        if lieu["est_autre"] or lieu["latitude"] is None or lieu["longitude"] is None:
            continue
        km, _ = distance_route_km(lat, lon, lieu["latitude"], lieu["longitude"])
        resultats[str(lieu["id"])] = int(round(km * 2))
    return resultats, geo


def adresse_complete(rue, complement, code_postal, ville):
    return " ".join(filter(None, [(rue or "").strip(), (complement or "").strip(),
                                  (code_postal or "").strip(), (ville or "").strip()]))
