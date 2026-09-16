#!/usr/bin/env python3
"""Géocode les associations actives (colonnes latitude/longitude vides) via
Nominatim (OpenStreetMap, gratuit, pas de clé API — même service déjà utilisé
ponctuellement dans ba38_collecte/moteur_tournees.py). Adresse utilisée :
adresse_association_1/2 + CP + COMMUNE (la mieux renseignée : 129/129
associations actives, contre 88/129 pour adresse_siege). Résultat persisté
en base pour ne plus jamais avoir à regéocoder à chaque affichage de la
carte « Localisation magasins et associations ».

Respecte la limite d'usage Nominatim (1 requête/seconde max) : bien plus
lent qu'un géocodage par lot classique, mais c'est un service gratuit sans
clé — à ne lancer qu'une fois puis pour les nouvelles associations
seulement (cf. --toutes pour tout regéocoder)."""
import sys
import time
import json
import argparse
import urllib.request
import urllib.parse
import sqlite3
from datetime import datetime
from ba38_utilitaires.core import get_db_path, write_log


def _nettoyer_adresse(adresse):
    """Retire le bruit fréquent dans adresse_association_1/2 (email de contact
    collé à l'adresse, doublons 'Mairie Mairie', boîte postale 'BP84' qui n'est
    pas une adresse de rue géocodable) qui fait échouer Nominatim."""
    import re
    adresse = re.sub(r'\S+@\S+', '', adresse)
    adresse = re.sub(r'\bBP\s*\d+\b', '', adresse, flags=re.IGNORECASE)
    adresse = re.sub(r'\bcedex\b\s*\d*', '', adresse, flags=re.IGNORECASE)
    adresse = re.sub(r'\b(\w+)\s+\1\b', r'\1', adresse, flags=re.IGNORECASE)
    return ' '.join(adresse.split())


_CACHE_CP_COMMUNE = {}


def _commune_via_cp(cp):
    """Résout le nom officiel de la commune à partir du code postal, via
    l'API gouvernementale geo.api.gouv.fr (gratuite, sans clé) — plus fiable
    que le champ libre COMMUNE, qui contient parfois 'Cedex'/'BP...' collé au
    nom (ex. 'MEYLAN CEDEX', 'GRENOBLE Cedex 2') et fait échouer Nominatim.
    Retourne None si le CP est absent/invalide/inconnu (ex. CP de Cedex,
    souvent différent du vrai code postal de la commune) — pas une erreur,
    juste un cas où le repli sur COMMUNE nettoyée prend le relais."""
    cp = (cp or '').strip()
    if not cp or not cp.isdigit():
        return None
    if cp in _CACHE_CP_COMMUNE:
        return _CACHE_CP_COMMUNE[cp]
    try:
        url = f'https://geo.api.gouv.fr/communes?codePostal={cp}&fields=nom'
        req = urllib.request.Request(url, headers={'User-Agent': 'BA38-localisation/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        nom = data[0]['nom'] if data else None
    except Exception:
        nom = None
    _CACHE_CP_COMMUNE[cp] = nom
    return nom


def _nettoyer_commune(commune):
    """Repli si le CP ne résout rien : retire 'Cedex'/'CEDEX'/'BP\\d+' du
    champ COMMUNE libre (ex. 'GRENOBLE Cedex 2' -> 'GRENOBLE')."""
    import re
    commune = re.sub(r'\bcedex\b\s*\d*', '', commune, flags=re.IGNORECASE)
    commune = re.sub(r'\bBP\s*\d+\b', '', commune, flags=re.IGNORECASE)
    return ' '.join(commune.split())


def _resoudre_commune(cp, commune_brute):
    return _commune_via_cp(cp) or _nettoyer_commune(commune_brute or '')


# Boîte englobante large autour de l'Isère (+ départements limitrophes) — même
# zone que celle utilisée à l'affichage de la carte (ba38_collecte/routes.py,
# _coords_plausibles) : un siège national (Paris, Montpellier...) hors de
# cette zone n'est pas géographiquement utile sur une carte de localisation
# BAI 38, même si l'adresse est exacte — dans ce cas on préfère l'adresse de
# distribution locale (adresse_distribution_1/2 + CP2 + COMMUNE2) si connue.
ZONE_LAT_MIN, ZONE_LAT_MAX = 44.0, 46.3
ZONE_LON_MIN, ZONE_LON_MAX = 4.2, 6.3


def _coords_plausibles(coords):
    if not coords:
        return False
    lat, lon = coords
    return ZONE_LAT_MIN <= lat <= ZONE_LAT_MAX and ZONE_LON_MIN <= lon <= ZONE_LON_MAX


def _appel_nominatim(params):
    url = f'https://nominatim.openstreetmap.org/search?{urllib.parse.urlencode(params)}&format=json&limit=1'
    req = urllib.request.Request(url, headers={'User-Agent': 'BA38-localisation/1.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if data:
        return float(data[0]['lat']), float(data[0]['lon'])
    return None


def geocoder_adresse(adresse, cp, commune_brute):
    """Essaie dans l'ordre : adresse complète nettoyée (requête libre), adresse
    complète en requête structurée (souvent plus tolérante au bruit), puis
    repli sur ville seule (position approximative — suffisant pour une carte
    de vue d'ensemble, mieux que pas de marqueur du tout). La commune utilisée
    est résolue depuis le CP (_resoudre_commune) plutôt que le champ COMMUNE
    brut, qui contient parfois 'Cedex'/'BP...' collé au nom."""
    adresse = _nettoyer_adresse(adresse or '')
    commune = _resoudre_commune(cp, commune_brute)
    cp = (cp or '').strip()

    if adresse:
        q = ', '.join(p for p in [adresse, cp, commune, 'France'] if p)
        coords = _appel_nominatim({'q': q})
        if coords:
            return coords, 'adresse'
        time.sleep(1.1)

        coords = _appel_nominatim({
            'street': adresse, 'postalcode': cp, 'city': commune, 'country': 'France',
        })
        if coords:
            return coords, 'adresse (structurée)'
        time.sleep(1.1)

        # Repli : rue + CP seuls, sans ville — utile quand COMMUNE est
        # invalide (ex. champ corrompu contenant autre chose qu'un nom de
        # ville) alors que le CP, lui, est correct ; un CP valide suffit à
        # Nominatim pour situer la rue correctement.
        if cp:
            coords = _appel_nominatim({'street': adresse, 'postalcode': cp, 'country': 'France'})
            if coords:
                return coords, 'adresse+CP (sans ville)'
            time.sleep(1.1)

    if commune:
        coords = _appel_nominatim({'city': commune, 'postalcode': cp, 'country': 'France'})
        if coords:
            return coords, 'ville (approximatif)'
        time.sleep(1.1)

    if cp:
        coords = _appel_nominatim({'postalcode': cp, 'country': 'France'})
        if coords:
            return coords, 'CP seul (approximatif)'

    return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toutes", action="store_true",
                        help="Regéocoder aussi les associations qui ont déjà des coordonnées")
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"➡ Connexion base : {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Par défaut, retraite aussi les lignes déjà géocodées mais hors zone
    # plausible (ex. adresse de siège national) — pas seulement les lat/lon
    # NULL — pour rattraper automatiquement ce cas sans reset manuel.
    clause = "" if args.toutes else f"""AND (
        latitude IS NULL OR longitude IS NULL
        OR latitude NOT BETWEEN {ZONE_LAT_MIN} AND {ZONE_LAT_MAX}
        OR longitude NOT BETWEEN {ZONE_LON_MIN} AND {ZONE_LON_MAX}
    )"""
    rows = cursor.execute(f"""
        SELECT Id, nom_association, adresse_association_1, adresse_association_2, CP, COMMUNE,
               adresse_distribution_1, adresse_distribution_2, CP2, COMMUNE2
        FROM associations
        WHERE LOWER(TRIM(COALESCE(validite,''))) = 'oui' {clause}
        ORDER BY nom_association
    """).fetchall()

    print(f"→ {len(rows)} association(s) à géocoder")
    ok, echecs = 0, []
    maintenant = datetime.now().strftime("%Y-%m-%d %H:%M")

    for i, r in enumerate(rows, 1):
        adresse = ' '.join(p for p in [r["adresse_association_1"], r["adresse_association_2"]] if p and p.strip())
        try:
            coords, methode = geocoder_adresse(adresse, r["CP"], r["COMMUNE"])
        except Exception as e:
            coords, methode = None, None
            print(f"  [{i}/{len(rows)}] ⚠️ {r['nom_association']} : erreur réseau ({e})")

        # Adresse de siège hors zone (probable siège national) : la
        # distribution locale est géographiquement plus pertinente ici.
        if not _coords_plausibles(coords):
            adresse_distrib = ' '.join(p for p in [r["adresse_distribution_1"], r["adresse_distribution_2"]] if p and p.strip())
            if adresse_distrib or r["CP2"] or r["COMMUNE2"]:
                try:
                    coords_d, methode_d = geocoder_adresse(adresse_distrib, r["CP2"], r["COMMUNE2"])
                except Exception:
                    coords_d, methode_d = None, None
                if _coords_plausibles(coords_d):
                    coords, methode = coords_d, f"distribution : {methode_d}"

        if coords:
            lat, lon = coords
            cursor.execute(
                "UPDATE associations SET latitude=?, longitude=?, geocode_le=? WHERE Id=?",
                (lat, lon, f"{maintenant} ({methode})", r["Id"])
            )
            conn.commit()
            ok += 1
            print(f"  [{i}/{len(rows)}] ✅ {r['nom_association']} → {lat:.5f}, {lon:.5f} [{methode}]")
        else:
            echecs.append(r["nom_association"])
            print(f"  [{i}/{len(rows)}] ❌ {r['nom_association']} : adresse non trouvée ({adresse}, {r['CP']} {r['COMMUNE']})")

        time.sleep(1.1)  # limite Nominatim : 1 requête/seconde max

    conn.close()

    print(f"\n✅ {ok}/{len(rows)} géocodée(s)")
    if echecs:
        print(f"⚠️ {len(echecs)} échec(s) à corriger manuellement (adresse imprécise/introuvable) :")
        for nom in echecs:
            print(f"   - {nom}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        write_log(f"❌ Erreur geocoder_associations : {e}")
        raise
