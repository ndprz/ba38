"""Accès aux données du planning ESOPE et génération du planning réel."""

import calendar
from datetime import date, timedelta

from flask_login import current_user

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"]
JOURS_COURTS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

STATUTS = {
    "prevu": "Prévu",
    "confirme": "Confirmé",
    "annule": "Annulé",
}


# ─────────────────────────────────────────────────────────────
# Outils dates / heures
# ─────────────────────────────────────────────────────────────

def lundi_de(d):
    return d - timedelta(days=d.weekday())


def parse_date(valeur, defaut=None):
    try:
        return date.fromisoformat((valeur or "").strip())
    except ValueError:
        return defaut


def parse_mois(valeur):
    """'2026-11' → (2026, 11), ou None."""
    try:
        a, m = (valeur or "").split("-")
        a, m = int(a), int(m)
        return (a, m) if 1 <= m <= 12 else None
    except ValueError:
        return None


def jours_du_mois(annee, mois):
    nb = calendar.monthrange(annee, mois)[1]
    return [date(annee, mois, j) for j in range(1, nb + 1)]


def lundis_du_mois(annee, mois):
    """Lundis des semaines qui touchent le mois (lundi → samedi)."""
    premier = date(annee, mois, 1)
    dernier = date(annee, mois, calendar.monthrange(annee, mois)[1])
    lundi = lundi_de(premier)
    if (premier - lundi).days >= 6:  # le mois commence un dimanche
        lundi += timedelta(days=7)
    res = []
    while lundi <= dernier:
        res.append(lundi)
        lundi += timedelta(days=7)
    return res


def heure_ok(valeur):
    """'9:5' / '09:05' / '9h05' → '09:05', vide → None, invalide → False."""
    v = (valeur or "").strip().lower().replace("h", ":")
    if not v:
        return None
    if v.endswith(":"):
        v += "00"
    try:
        h, m = v.split(":")
        h, m = int(h), int(m or 0)
    except ValueError:
        return False
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return False
    return f"{h:02d}:{m:02d}"


def fmt_heure(h):
    """'08:45' → '8h45', '12:00' → '12h'."""
    if not h:
        return ""
    hh, mm = h.split(":")
    return f"{int(hh)}h{mm if mm != '00' else ''}"


def fmt_plage(debut, fin):
    return f"{fmt_heure(debut)}–{fmt_heure(fin)}"


def fmt_date_courte(d):
    return f"{JOURS[d.weekday()] if d.weekday() < 6 else 'dimanche'} {d.day} {MOIS[d.month - 1]}"


def utilisateur():
    return getattr(current_user, "email", None) or getattr(current_user, "username", None) or "?"


# ─────────────────────────────────────────────────────────────
# Historique
# ─────────────────────────────────────────────────────────────

def journaliser(conn, action, creneau_id=None, affectation_id=None, benevole_id=None,
                ancien=None, nouveau=None, commentaire=None, auteur=None):
    conn.execute(
        "INSERT INTO esope_historique (creneau_id, affectation_id, benevole_id, auteur, action, "
        "ancien, nouveau, commentaire) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (creneau_id, affectation_id, benevole_id, auteur or utilisateur(), action,
         ancien, nouveau, commentaire),
    )


# ─────────────────────────────────────────────────────────────
# Fonctions, lignes du planning, bénévoles
# ─────────────────────────────────────────────────────────────

def fonctions(conn, actives_seulement=True):
    sql = "SELECT * FROM esope_fonctions"
    if actives_seulement:
        sql += " WHERE actif = 1"
    return [dict(r) for r in conn.execute(sql + " ORDER BY ordre, nom")]


def lignes_planning(conn):
    """Une ligne par (fonction, poste) ayant au moins un créneau type actif."""
    rows = conn.execute("""
        SELECT f.id AS fonction_id, f.nom AS fonction, f.couleur, ct.poste,
               MIN(ct.nb_max) AS nb_max_min, MAX(ct.nb_max) AS nb_max
        FROM esope_creneaux_types ct
        JOIN esope_fonctions f ON f.id = ct.fonction_id
        WHERE ct.actif = 1 AND f.actif = 1
        GROUP BY f.id, ct.poste
        ORDER BY f.ordre, f.nom, ct.poste
    """).fetchall()
    lignes = []
    for r in rows:
        d = dict(r)
        d["cle"] = f"{d['fonction_id']}|{d['poste']}"
        lignes.append(d)
    # nombre de lignes par fonction (rowspan du bandeau fonction)
    nb = {}
    for l in lignes:
        nb[l["fonction_id"]] = nb.get(l["fonction_id"], 0) + 1
    vu = set()
    for l in lignes:
        l["premiere"] = l["fonction_id"] not in vu
        l["nb_postes"] = nb[l["fonction_id"]]
        vu.add(l["fonction_id"])
    return lignes


def creneaux_types(conn, actifs_seulement=True):
    sql = """
        SELECT ct.*, f.nom AS fonction, f.couleur, f.ordre AS fonction_ordre
        FROM esope_creneaux_types ct
        JOIN esope_fonctions f ON f.id = ct.fonction_id
    """
    if actifs_seulement:
        sql += " WHERE ct.actif = 1 AND f.actif = 1"
    sql += " ORDER BY f.ordre, f.nom, ct.poste, ct.jour"
    return [dict(r) for r in conn.execute(sql)]


def jours_dispo_liste(valeur):
    return [j.strip() for j in (valeur or "").split(",") if j.strip() in JOURS]


def benevoles_esope(conn):
    """Bénévoles marqués ESOPE (benevoles.esope = oui), avec leurs fonctions et jours."""
    rows = conn.execute("""
        SELECT id, nom, prenom, telephone_portable, email, esope_jour_dispo, esope_commentaire
        FROM benevoles
        WHERE LOWER(TRIM(COALESCE(esope, ''))) = 'oui'
        ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE
    """).fetchall()
    fct = {}
    for r in conn.execute("SELECT benevole_id, fonction_id FROM esope_benevoles_fonctions"):
        fct.setdefault(r["benevole_id"], []).append(r["fonction_id"])
    res = []
    for r in rows:
        d = dict(r)
        d["fonctions"] = fct.get(d["id"], [])
        d["jours"] = jours_dispo_liste(d["esope_jour_dispo"])
        d["jours_idx"] = [JOURS.index(j) for j in d["jours"]]
        d["libelle"] = f"{d['nom'] or ''} {d['prenom'] or ''}".strip()
        res.append(d)
    return res


def noms_benevoles(conn, ids):
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    return {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, nom, prenom, telephone_portable, email FROM benevoles WHERE id IN ({ph})", ids)}


def profil_benevole(conn, benevole_id):
    """Bloc ESOPE de la fiche bénévole."""
    b = conn.execute(
        "SELECT esope, esope_jour_dispo, esope_commentaire FROM benevoles WHERE id = ?",
        (benevole_id,)).fetchone()
    if not b:
        return None
    coches = {r[0] for r in conn.execute(
        "SELECT fonction_id FROM esope_benevoles_fonctions WHERE benevole_id = ?", (benevole_id,))}
    return {
        "est_esope": (b["esope"] or "").strip().lower() == "oui",
        "fonctions": fonctions(conn),
        "fonctions_cochees": coches,
        "jours": JOURS,
        "jours_coches": jours_dispo_liste(b["esope_jour_dispo"]),
        "commentaire": b["esope_commentaire"] or "",
    }


def enregistrer_profil(conn, benevole_id, fonction_ids, jours, commentaire):
    conn.execute("DELETE FROM esope_benevoles_fonctions WHERE benevole_id = ?", (benevole_id,))
    valides = {f["id"] for f in fonctions(conn, actives_seulement=False)}
    for fid in fonction_ids:
        if fid in valides:
            conn.execute("INSERT OR IGNORE INTO esope_benevoles_fonctions (benevole_id, fonction_id) "
                         "VALUES (?, ?)", (benevole_id, fid))
    jours_ok = [j for j in JOURS if j in jours]
    conn.execute("UPDATE benevoles SET esope_jour_dispo = ?, esope_commentaire = ? WHERE id = ?",
                 (",".join(jours_ok) or None, (commentaire or "").strip() or None, benevole_id))


# ─────────────────────────────────────────────────────────────
# Planning réel : lecture d'une semaine
# ─────────────────────────────────────────────────────────────

def absent_sql(aff="a", cre="c"):
    """Condition SQL : le bénévole de l'affectation a une absence (table commune `absences`, dates jj/mm/aaaa) ce jour-là."""
    iso = lambda col: (f"(substr(trim(ab.{col}),7,4) || '-' || substr(trim(ab.{col}),4,2) || '-' || substr(trim(ab.{col}),1,2))")
    return (f"EXISTS (SELECT 1 FROM absences ab WHERE ab.benevole_id = {aff}.benevole_id "
            f"AND length(trim(ab.date_debut)) = 10 AND length(trim(ab.date_fin)) = 10 "
            f"AND {iso('date_debut')} <= {cre}.date AND {iso('date_fin')} >= {cre}.date)")


def appliquer_absence(d):
    """Une absence saisie vaut « annulé » à l'affichage (sans toucher à la base : supprimer l'absence rend le créneau)."""
    if d.get("absent_saisie") and d["statut"] != "annule":
        d["statut"] = "annule"
        d["absence"] = 1
    return d


def _affectations_par_creneau(conn, creneau_ids):
    if not creneau_ids:
        return {}
    ph = ",".join("?" * len(creneau_ids))
    rows = conn.execute(f"""
        SELECT a.*, b.nom, b.prenom, b.telephone_portable, b.email, {absent_sql()} AS absent_saisie
        FROM esope_affectations a
        JOIN esope_creneaux c ON c.id = a.creneau_id
        LEFT JOIN benevoles b ON b.id = a.benevole_id
        WHERE a.creneau_id IN ({ph})
        ORDER BY b.nom COLLATE NOCASE, b.prenom COLLATE NOCASE
    """, list(creneau_ids)).fetchall()
    res = {}
    for r in sorted((appliquer_absence(dict(r)) for r in rows), key=lambda d: d["statut"] == "annule"):
        d = r
        d["libelle"] = f"{d['nom'] or '?'} {d['prenom'] or ''}".strip()
        d["partiel"] = fmt_plage(d["heure_debut"], d["heure_fin"]) if d["heure_debut"] and d["heure_fin"] else (
            f"dès {fmt_heure(d['heure_debut'])}" if d["heure_debut"] else (
                f"jusqu'à {fmt_heure(d['heure_fin'])}" if d["heure_fin"] else ""))
        res.setdefault(d["creneau_id"], []).append(d)
    return res


def _completer_creneau(c, affs):
    c["affectations"] = affs
    actifs = [a for a in affs if a["statut"] != "annule"]
    c["nb_actifs"] = len(actifs)
    c["plage"] = fmt_plage(c["heure_debut"], c["heure_fin"])
    c["places_vides"] = max(0, c["nb_max"] - len(actifs))
    if c["ferme"]:
        c["couverture"] = "ferme"
    elif len(actifs) < c["nb_min"]:
        c["couverture"] = "manque"
    elif len(actifs) > c["nb_max"]:
        c["couverture"] = "trop"
    else:
        c["couverture"] = "ok"
    return c


def semaine(conn, lundi):
    """Grille d'une semaine : {cle_ligne: {jour_idx: creneau}}, événements, fermetures."""
    samedi = lundi + timedelta(days=5)
    rows = [dict(r) for r in conn.execute("""
        SELECT c.*, f.nom AS fonction, f.couleur
        FROM esope_creneaux c
        LEFT JOIN esope_fonctions f ON f.id = c.fonction_id
        WHERE c.date BETWEEN ? AND ?
        ORDER BY c.date, c.heure_debut
    """, (lundi.isoformat(), samedi.isoformat()))]
    affs = _affectations_par_creneau(conn, [r["id"] for r in rows])
    grille, evenements = {}, []
    for c in rows:
        _completer_creneau(c, affs.get(c["id"], []))
        d = date.fromisoformat(c["date"])
        c["jour_idx"] = d.weekday()
        if c["creneau_type_id"] is None:
            c["date_txt"] = fmt_date_courte(d)
            evenements.append(c)
        else:
            grille.setdefault(f"{c['fonction_id']}|{c['poste'] or ''}", {})[d.weekday()] = c
    fermetures = {r["date"]: r["motif"] for r in conn.execute(
        "SELECT date, motif FROM esope_fermetures WHERE date BETWEEN ? AND ?",
        (lundi.isoformat(), samedi.isoformat()))}
    jours = []
    for i in range(6):
        d = lundi + timedelta(days=i)
        jours.append({"idx": i, "date": d, "iso": d.isoformat(), "nom": JOURS[i],
                      "court": JOURS_COURTS[i], "fermeture": fermetures.get(d.isoformat())})
    return {"grille": grille, "evenements": evenements, "jours": jours}


def evenements_a_venir(conn, depuis):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM esope_creneaux WHERE creneau_type_id IS NULL AND date >= ? ORDER BY date, heure_debut",
        (depuis.isoformat(),))]
    affs = _affectations_par_creneau(conn, [r["id"] for r in rows])
    for c in rows:
        _completer_creneau(c, affs.get(c["id"], []))
        c["date_txt"] = fmt_date_courte(date.fromisoformat(c["date"]))
    return rows


def etat_mois(conn, annee, mois):
    premier = date(annee, mois, 1).isoformat()
    dernier = date(annee, mois, calendar.monthrange(annee, mois)[1]).isoformat()
    r = conn.execute("""
        SELECT COUNT(DISTINCT c.id) AS nb_creneaux,
               COUNT(a.id) AS nb_affectations
        FROM esope_creneaux c
        LEFT JOIN esope_affectations a ON a.creneau_id = c.id AND a.statut != 'annule'
        WHERE c.creneau_type_id IS NOT NULL AND c.date BETWEEN ? AND ?
    """, (premier, dernier)).fetchone()
    return dict(r)


# ─────────────────────────────────────────────────────────────
# Planning modèle
# ─────────────────────────────────────────────────────────────

def modele(conn):
    """{creneau_type_id: [entrées du modèle]}"""
    rows = conn.execute("""
        SELECT m.*, b.nom, b.prenom
        FROM esope_modele m
        LEFT JOIN benevoles b ON b.id = m.benevole_id
        ORDER BY b.nom COLLATE NOCASE, b.prenom COLLATE NOCASE
    """).fetchall()
    res = {}
    for r in rows:
        d = dict(r)
        d["libelle"] = f"{d['nom'] or '?'} {d['prenom'] or ''}".strip()
        d["partiel"] = fmt_plage(d["heure_debut"], d["heure_fin"]) if d["heure_debut"] and d["heure_fin"] else ""
        d["recurrence"] = libelle_recurrence(d["frequence"], d["date_reference"])
        res.setdefault(d["creneau_type_id"], []).append(d)
    return res


def libelle_recurrence(frequence, date_reference):
    f = frequence or 1
    if f == 1:
        return "toutes les semaines"
    ref = parse_date(date_reference)
    depuis = f" dès le {ref.day:02d}/{ref.month:02d}/{ref.year}" if ref else ""
    return f"1 semaine sur {f}{depuis}"


def tombe_cette_semaine(frequence, date_reference, jour):
    """Vrai si une entrée du modèle (toutes les `frequence` semaines à partir de la
    semaine de `date_reference`) s'applique à la semaine de `jour`.

    Calcul par écart de semaines depuis la date de référence, et non par parité
    ISO : 2026 compte 53 semaines (S53 et S1 2027 toutes deux impaires)."""
    f = frequence or 1
    if f <= 1:
        return True
    ref = parse_date(date_reference)
    if not ref:
        return True
    ecart = (lundi_de(jour) - lundi_de(ref)).days // 7
    return ecart % f == 0


# ─────────────────────────────────────────────────────────────
# Génération du planning réel
# ─────────────────────────────────────────────────────────────

def generer_mois(conn, annee, mois):
    """Crée les créneaux manquants du mois à partir des créneaux types et y place
    les bénévoles du modèle. Un créneau qui existe déjà n'est jamais touché :
    les retouches manuelles sont conservées."""
    fermes = {r[0] for r in conn.execute("SELECT date FROM esope_fermetures")}
    types = creneaux_types(conn)
    mod = modele(conn)
    nb_creneaux = nb_affectations = 0
    for d in jours_du_mois(annee, mois):
        if d.weekday() > 5 or d.isoformat() in fermes:
            continue
        for ct in types:
            if ct["jour"] != d.weekday():
                continue
            existe = conn.execute(
                "SELECT 1 FROM esope_creneaux WHERE date = ? AND creneau_type_id = ?",
                (d.isoformat(), ct["id"])).fetchone()
            if existe:
                continue
            cur = conn.execute("""
                INSERT INTO esope_creneaux (date, creneau_type_id, fonction_id, poste, libelle,
                    heure_debut, heure_fin, nb_min, nb_max, user_creation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (d.isoformat(), ct["id"], ct["fonction_id"], ct["poste"] or "", ct["libelle"],
                  ct["heure_debut"], ct["heure_fin"], ct["nb_min"], ct["nb_max"], utilisateur()))
            creneau_id = cur.lastrowid
            nb_creneaux += 1
            for m in mod.get(ct["id"], []):
                if not tombe_cette_semaine(m["frequence"], m["date_reference"], d):
                    continue
                cur = conn.execute("""
                    INSERT OR IGNORE INTO esope_affectations (creneau_id, benevole_id, heure_debut,
                        heure_fin, commentaire, origine, user_modif)
                    VALUES (?, ?, ?, ?, ?, 'modele', ?)
                """, (creneau_id, m["benevole_id"], m["heure_debut"], m["heure_fin"],
                      m["commentaire"], utilisateur()))
                if cur.rowcount:
                    nb_affectations += 1
    if nb_creneaux:
        journaliser(conn, "generation", commentaire=f"{MOIS[mois - 1]} {annee} : {nb_creneaux} créneaux, "
                                                     f"{nb_affectations} affectations")
    return nb_creneaux, nb_affectations


def reinitialiser_mois(conn, annee, mois):
    """Supprime les créneaux réguliers du mois (pas les événements) puis régénère."""
    premier = date(annee, mois, 1).isoformat()
    dernier = date(annee, mois, calendar.monthrange(annee, mois)[1]).isoformat()
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM esope_creneaux WHERE creneau_type_id IS NOT NULL AND date BETWEEN ? AND ?",
        (premier, dernier))]
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM esope_affectations WHERE creneau_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM esope_creneaux WHERE id IN ({ph})", ids)
    journaliser(conn, "reinitialisation", commentaire=f"{MOIS[mois - 1]} {annee} : {len(ids)} créneaux supprimés")
    return generer_mois(conn, annee, mois)
