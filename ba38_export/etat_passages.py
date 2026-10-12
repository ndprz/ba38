"""
============================================================
ÉTAT PDF : PASSAGES DES ASSOCIATIONS AU QUAI (mois en cours + suivant)
============================================================

Même base que le planning des passages de l'écran Événements
(ba38_evenements.generer_planning_du_jour) : associations non désactivées,
parking P1/P6, jour_de_passage_a_la_BAI / heure_de_passage normalisés.

En plus, comme l'état est daté, le champ libre `frequence` est appliqué
quand il est lisible :
- hebdomadaire, 2x/semaine, vide…        → chaque semaine
- « 14j pair / impair », « quinzaine paire », « 15 j pair »
                                          → semaines ISO paires / impaires
                                            (même règle que la comparaison VIF)
- « 1er … 3ème … dernier <jour> du mois » → seulement ces occurrences du mois
Une fréquence non reconnue n'exclut rien : l'association figure chaque
semaine et la fréquence est imprimée à côté du nom pour vérification.
"""

import calendar
import re
from datetime import date, timedelta

from ba38_partenaires.passage import JOURS_PASSAGE, PARKINGS_QUAI, _sans_accents

MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]

_RE_HEURE = re.compile(r"^(\d{2}):(\d{2})$")
_ORDINAUX = {"1er": 1, "1e": 1, "1ere": 1, "premier": 1,
             "2e": 2, "2eme": 2, "deuxieme": 2,
             "3e": 3, "3eme": 3, "troisieme": 3,
             "4e": 4, "4eme": 4, "quatrieme": 4}


# ------------------------------------------------------------
# Fréquence
# ------------------------------------------------------------
def regle_frequence(frequence: str):
    """Traduit le champ libre `frequence` en règle exploitable.
    Renvoie (type, valeur) avec type parmi :
    'hebdo', 'parite' (0 = paire, 1 = impaire), 'mois' (set d'ordinaux,
    -1 = dernier), 'inconnue'."""
    f = _sans_accents(frequence or "")
    if not f or "hebdo" in f or re.search(r"\d\s*x\s*/?\s*semaine", f):
        return ("hebdo", None)

    if re.search(r"\b(14|15)\s*j|quinzaine", f):
        if re.search(r"\bimpaire?s?\b", f):
            return ("parite", 1)
        if re.search(r"\bpaire?s?\b", f):
            return ("parite", 0)

    if "du mois" in f:
        ordinaux = set()
        for mot in re.findall(r"[a-z0-9]+", f):
            if mot in _ORDINAUX:
                ordinaux.add(_ORDINAUX[mot])
            elif mot == "dernier":
                ordinaux.add(-1)
        if ordinaux:
            return ("mois", ordinaux)

    return ("inconnue", None)


def passe_ce_jour(regle, d: date) -> bool:
    type_, val = regle
    if type_ == "parite":
        return d.isocalendar()[1] % 2 == val
    if type_ == "mois":
        rang = (d.day - 1) // 7 + 1
        dernier = d.day + 7 > calendar.monthrange(d.year, d.month)[1]
        return rang in val or (-1 in val and dernier)
    return True  # hebdo / inconnue


# ------------------------------------------------------------
# Jours fériés (la BA est fermée : la journée est signalée)
# ------------------------------------------------------------
def _paques(annee: int) -> date:
    a, b, c = annee % 19, annee // 100, annee % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mois = (h + l - 7 * m + 114) // 31
    jour = (h + l - 7 * m + 114) % 31 + 1
    return date(annee, mois, jour)


def jours_feries(annee: int) -> set:
    p = _paques(annee)
    return {
        date(annee, 1, 1), date(annee, 5, 1), date(annee, 5, 8),
        date(annee, 7, 14), date(annee, 8, 15), date(annee, 11, 1),
        date(annee, 11, 11), date(annee, 12, 25),
        p + timedelta(days=1), p + timedelta(days=39), p + timedelta(days=50),
    }


# ------------------------------------------------------------
# Construction de l'état
# ------------------------------------------------------------
def _demi_journee(heure: str):
    """('matin' | 'apres_midi' | 'non_precise', clé de tri)."""
    h = (heure or "").strip()
    m = _RE_HEURE.match(h)
    if m:
        minutes = int(m.group(1)) * 60 + int(m.group(2))
        return ("matin" if minutes < 12 * 60 else "apres_midi", minutes)
    brut = _sans_accents(h)
    if "matin" in brut:
        return ("matin", -1)
    if "midi" in brut:
        return ("apres_midi", -1)
    return ("non_precise", 0)


def _mois_suivant(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def construire_etat(conn, aujourdhui: date) -> dict:
    """Données de l'état pour le mois de `aujourdhui` et le suivant."""
    from ba38_partenaires.passage import heure_affichage

    rows = conn.execute("""
        SELECT nom_association, jour_de_passage_a_la_BAI, heure_de_passage,
               Emplacement, frequence
        FROM associations
        WHERE jour_de_passage_a_la_BAI IS NOT NULL
          AND (validite IS NULL OR LOWER(TRIM(validite)) != 'non')
    """).fetchall()

    index_jours = {_sans_accents(j): i for i, j in enumerate(JOURS_PASSAGE)}
    assos = []
    for r in rows:
        nom = (r["nom_association"] or "").strip()
        parking = re.sub(r"\s+", "", r["Emplacement"] or "").upper()
        if not nom or parking not in PARKINGS_QUAI:
            continue
        jours = {index_jours[j] for j in
                 (_sans_accents(x) for x in re.split(r"[,;/]", r["jour_de_passage_a_la_BAI"]))
                 if j in index_jours}
        if not jours:
            continue
        regle = regle_frequence(r["frequence"])
        dj, tri = _demi_journee(r["heure_de_passage"])
        assos.append({
            "nom": nom,
            "jours": jours,
            "parking": parking,
            "heure": heure_affichage(r["heure_de_passage"]) if tri >= 0 else "",
            "demi_journee": dj,
            "tri": tri,
            "regle": regle,
            # fréquence imprimée seulement quand elle n'est pas hebdomadaire
            "frequence": (r["frequence"] or "").strip() if regle[0] != "hebdo" else "",
            "frequence_inconnue": regle[0] == "inconnue",
        })

    debut = date(aujourdhui.year, aujourdhui.month, 1)
    mois = []
    for premier in (debut, _mois_suivant(debut)):
        feries = jours_feries(premier.year)
        jours = []
        d = premier
        while d.month == premier.month:
            if d.weekday() < 5:
                presents = [a for a in assos
                            if d.weekday() in a["jours"] and passe_ce_jour(a["regle"], d)]
                demi = []
                for cle, libelle in (("matin", "Matin"), ("apres_midi", "Après-midi"),
                                     ("non_precise", "Horaire non précisé")):
                    lignes = sorted((a for a in presents if a["demi_journee"] == cle),
                                    key=lambda a: (a["tri"], a["parking"], a["nom"].lower()))
                    if lignes:
                        demi.append({"libelle": libelle, "lignes": lignes})
                jours.append({
                    "date": d,
                    "libelle": f"{JOURS_PASSAGE[d.weekday()]} {d.day} {MOIS_FR[d.month - 1]} {d.year}",
                    "ferie": d in feries,
                    "nb": len(presents),
                    "demi_journees": demi,
                })
            d += timedelta(days=1)
        mois.append({
            "libelle": f"{MOIS_FR[premier.month - 1].capitalize()} {premier.year}",
            "jours": jours,
        })

    a_verifier = sorted({a["nom"] + " — " + a["frequence"] for a in assos if a["frequence_inconnue"]},
                        key=str.lower)
    return {"mois": mois, "a_verifier": a_verifier}
