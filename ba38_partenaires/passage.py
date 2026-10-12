"""
============================================================
PASSAGE À LA BAI : JOURS / HEURE / PARKING
============================================================

Saisie normalisée des 3 champs de planification des associations,
pilotée par field_groups.type_champ :

- jours_semaine  → "Lundi, Mercredi"  (cases à cocher, lundi → vendredi)
- heure_passage  → "13:30", "Matin" ou "Après-midi"
- parking        → "P1", "P6", "Livraison" ou "À la demande"

Jours et heure sont obligatoires quand le parking est P1 ou P6 (passage
au quai). Seules ces associations apparaissent dans le planning des
passages de l'écran Événements.

Le même contrôle sert à la fiche (création / modification), à l'édition
en tableau et à la mise à jour depuis la comparaison VIF.
"""

import calendar
import re
import unicodedata

JOURS_PASSAGE = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi"]
HEURES_LIBELLES = ["Matin", "Après-midi"]
PARKINGS = ["P1", "P6", "Livraison", "À la demande"]
PARKINGS_QUAI = ("P1", "P6")

TYPES_PASSAGE = ("jours_semaine", "heure_passage", "parking")
# champ texte libre (pas de type_champ dédié), contrôlé par nom de colonne
CHAMP_FREQUENCE = "frequence"
EXEMPLES_FREQUENCE = ("Hebdomadaire, 2x/semaine, 14j pair, 14j impair, "
                      "« 1er mercredi du mois », « 1er et 3e jeudi du mois », "
                      "« dernier mardi du mois »")

_RE_HEURE = re.compile(
    r"^(\d{1,2})\s*[hH:]\s*(\d{2})?"
    # plage éventuelle : seule l'heure de début est conservée
    r"(?:\s*(?:-|–|à|a)\s*\d{1,2}\s*[hH:]\s*(?:\d{2})?)?$"
)


def _sans_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def normaliser_jours(val) -> str | None:
    """'lundi;JEUDI' ou ['Jeudi', 'Lundi'] → 'Lundi, Jeudi'. ValueError si un
    morceau n'est pas un jour de lundi à vendredi."""
    if isinstance(val, (list, tuple)):
        morceaux = [str(v) for v in val]
    else:
        morceaux = re.split(r"\s*(?:,|;|/|\bet\b)\s*", str(val or ""))
    morceaux = [m.strip() for m in morceaux if m and m.strip()]
    if not morceaux:
        return None

    index = {_sans_accents(j): j for j in JOURS_PASSAGE}
    trouves = set()
    for m in morceaux:
        jour = index.get(_sans_accents(m))
        if not jour:
            raise ValueError(f"« {m} » n'est pas un jour de passage (lundi à vendredi)")
        trouves.add(jour)
    return ", ".join(j for j in JOURS_PASSAGE if j in trouves)


def normaliser_heure(val) -> str | None:
    """'13h30', '13H', '13:30', '14h40-15h' → 'HH:MM' ; 'le matin' → 'Matin' ;
    'apres midi' → 'Après-midi'. ValueError sinon."""
    s = str(val or "").strip()
    if not s:
        return None
    brut = _sans_accents(s)
    if brut in ("matin", "le matin"):
        return "Matin"
    if brut.replace("-", " ") in ("apres midi", "l'apres midi"):
        return "Après-midi"
    m = _RE_HEURE.match(s)
    if m:
        h, mn = int(m.group(1)), int(m.group(2) or 0)
        if h <= 23 and mn <= 59:
            return f"{h:02d}:{mn:02d}"
    raise ValueError(f"« {s} » n'est pas une heure valide (HH:MM, Matin ou Après-midi)")


def normaliser_parking(val) -> str | None:
    """'p 6' → 'P6', 'a la demande' → 'À la demande'. ValueError sinon."""
    s = str(val or "").strip()
    if not s:
        return None
    compact = re.sub(r"\s+", "", s).upper()
    if compact in PARKINGS_QUAI:
        return compact
    brut = _sans_accents(s)
    for p in PARKINGS:
        if brut == _sans_accents(p):
            return p
    raise ValueError(f"« {s} » n'est pas un parking valide ({', '.join(PARKINGS)})")


_NORMALISEURS = {
    "jours_semaine": normaliser_jours,
    "heure_passage": normaliser_heure,
    "parking": normaliser_parking,
}


def normaliser_valeur(type_champ: str, val):
    """Normalise une valeur selon son type_champ de passage (ValueError si invalide).
    Les autres types sont renvoyés tels quels."""
    fn = _NORMALISEURS.get(type_champ or "")
    return fn(val) if fn else val


def controler_passage(field_types: dict, valeurs: dict, champs=None,
                      anciennes=None) -> list[tuple[str, str]]:
    """Normalise en place les champs de passage de `valeurs` (ligne complète
    d'une association) et renvoie les erreurs [(champ, message)].

    field_types : {field_name: type_champ}
    champs      : limite la normalisation à ces champs (ceux réellement
                  soumis) ; la règle « obligatoire si P1/P6 » porte toujours
                  sur la ligne complète.

    anciennes   : valeurs en base avant modification (fiche, tableau) ; une
                  fréquence inchangée n'est pas recontrôlée.

    Jours et heure ne sont exigés (format + présence) que pour un parking
    P1/P6 : hors quai, l'association n'est pas au planning et une ancienne
    saisie libre est conservée telle quelle. Pour P1/P6, la fréquence saisie
    doit aussi être exploitable par le planning (regle_frequence).
    """
    par_type = {
        t: fname for fname, t in field_types.items()
        if t in TYPES_PASSAGE and fname in valeurs
    }
    a_traiter = lambda fname: champs is None or fname in champs  # noqa: E731

    erreurs = []

    # parking d'abord : il décide de la rigueur sur jours / heure
    f_parking = par_type.get("parking")
    parking = None
    if f_parking:
        try:
            parking = normaliser_parking(valeurs.get(f_parking))
            if a_traiter(f_parking):
                valeurs[f_parking] = parking
        except ValueError as e:
            if a_traiter(f_parking):
                erreurs.append((f_parking, str(e)))
    # sans champ parking configuré, on reste strict
    au_quai = parking in PARKINGS_QUAI if f_parking else True

    for t in ("jours_semaine", "heure_passage"):
        fname = par_type.get(t)
        if not fname or not a_traiter(fname):
            continue
        try:
            valeurs[fname] = normaliser_valeur(t, valeurs[fname])
        except ValueError as e:
            if au_quai:
                erreurs.append((fname, str(e)))

    if parking in PARKINGS_QUAI and not erreurs:
        for t, message in (("jours_semaine", "les jours de passage sont obligatoires"),
                           ("heure_passage", "l'heure de passage est obligatoire")):
            fname = par_type.get(t)
            if fname and not valeurs.get(fname):
                erreurs.append((fname, f"Parking {parking} : {message}."))

    f_freq = CHAMP_FREQUENCE
    if au_quai and f_freq in valeurs and a_traiter(f_freq):
        freq = (valeurs.get(f_freq) or "").strip()
        inchangee = anciennes is not None and freq == (anciennes.get(f_freq) or "").strip()
        if freq and not inchangee and regle_frequence(freq)[0] == "inconnue":
            erreurs.append((f_freq,
                f"Fréquence « {freq} » non exploitable par le planning des passages. "
                f"Formats reconnus : {EXEMPLES_FREQUENCE}."))
    return erreurs


# ------------------------------------------------------------
# Fréquence (champ libre `frequence`, sans type_champ dédié)
# ------------------------------------------------------------
_ORDINAUX = {"1er": 1, "1e": 1, "1ere": 1, "premier": 1,
             "2e": 2, "2eme": 2, "deuxieme": 2,
             "3e": 3, "3eme": 3, "troisieme": 3,
             "4e": 4, "4eme": 4, "quatrieme": 4}


# ------------------------------------------------------------
# Fréquence
# ------------------------------------------------------------
def regle_frequence(frequence: str):
    """Traduit le champ libre `frequence` en règle exploitable par le
    planning des passages (écran Événements, état PDF) :
    - vide, hebdomadaire, 2x/semaine…              → chaque semaine
    - « 14j pair / impair », « quinzaine paire »  → semaines ISO paires /
      impaires (même règle que la comparaison VIF)
    - « 1er / 2e / 3e / 4e / dernier <jour> du mois » (ou « tous les mois »,
      « chaque mois ») → seulement ces occurrences du mois
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

    if re.search(r"\bmois\b", f):
        ordinaux = set()
        for mot in re.findall(r"[a-z0-9]+", f):
            if mot in _ORDINAUX:
                ordinaux.add(_ORDINAUX[mot])
            elif mot == "dernier":
                ordinaux.add(-1)
        if ordinaux:
            return ("mois", ordinaux)

    return ("inconnue", None)


def passe_ce_jour(regle, d) -> bool:
    type_, val = regle
    if type_ == "parite":
        return d.isocalendar()[1] % 2 == val
    if type_ == "mois":
        rang = (d.day - 1) // 7 + 1
        dernier = d.day + 7 > calendar.monthrange(d.year, d.month)[1]
        return rang in val or (-1 in val and dernier)
    return True  # hebdo / inconnue



def heure_affichage(val: str) -> str:
    """'13:30' → '13h30' pour l'affichage."""
    s = (val or "").strip()
    m = re.fullmatch(r"(\d{2}):(\d{2})", s)
    if m:
        return f"{int(m.group(1))}h{m.group(2)}"
    return s
