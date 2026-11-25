# ============================================================
# 📅 Calendrier prévisionnel des visites aux associations (CAR)
#   - consultation + export PDF : tout utilisateur connecté
#   - modification : CAR (ses associations), responsable du pôle Partenariat
#     et suppléants + admins (toutes), autres utilisateurs avec droit
#     « écriture » (visites à leur propre nom, n'importe quelle association)
# ============================================================

import calendar
import io
import sqlite3
from datetime import date, datetime

from flask import flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from weasyprint import CSS, HTML

from ba38_calendrier_visites import calendrier_visites_bp as bp
from ba38_utilitaires.core import get_db_path, has_access, write_log
from ba38_utilitaires.organisation import get_organisation

MOIS = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]
JOURS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
STATUTS = {"prevue": "Prévue", "realisee": "Réalisée", "annulee": "Annulée"}
PALETTE = ["#0d6efd", "#198754", "#d63384", "#fd7e14", "#6f42c1", "#20c997",
           "#dc3545", "#0aa2c0", "#6610f2", "#795548", "#5c7080", "#b8860b", "#ff6f91"]


def _connect():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------
# Droits
# ------------------------------------------------------------
def _contexte_droits(conn):
    row = conn.execute(
        "SELECT responsable_id, suppleant1_id, suppleant2_id FROM engagement_poles "
        "WHERE code='partenariat' AND actif=1").fetchone()
    ids_pole = {i for i in tuple(row) if i} if row else set()
    role = (current_user.role or "").lower()
    return {
        "admin": role == "admin",
        "pole": current_user.id in ids_pole,   # responsable ou suppléant du pôle Partenariat
        "car": role == "car",
        "ecriture": has_access("calendrier_visites", "ecriture"),
        "username": (current_user.username or "").strip(),
    }


def _gestionnaire_total(d):
    return d["admin"] or d["pole"]


def _meme(a, b):
    return bool((a or "").strip()) and (a or "").strip().lower() == (b or "").strip().lower()


def _peut_creer(d, asso_car):
    if _gestionnaire_total(d):
        return True
    if d["car"]:
        return _meme(asso_car, d["username"])
    return d["ecriture"]


def _peut_modifier(d, visite):
    """visite : ligne visites_prevues + car_actuel (CAR actuel de l'association)."""
    if _gestionnaire_total(d):
        return True
    if _meme(visite["cree_par"], d["username"]):
        return True
    return d["car"] and _meme(visite["car_actuel"], d["username"])


MAX_INTERVENANTS = 8


def _resoudre_visiteur(conn, d, choix, existants=()):
    """Transforme le choix de la liste fermée en (nom, type, id_benevole), ou None si invalide.
    Valeurs : « moi » | « bene:<id> » | « car:<nom> » (admin / pôle seulement) | « garde » (modification :
    on conserve l'intervenant n°N déjà enregistré sur la visite). Aucun texte libre n'est jamais enregistré."""
    choix = (choix or "").strip()
    if choix.startswith("garde:"):
        try:
            e = existants[int(choix[6:])]
        except (ValueError, IndexError):
            return None
        return e["nom"], e["type"] or "texte", e["benevole_id"]
    if choix.startswith("bene:"):
        try:
            bid = int(choix[5:])
        except ValueError:
            return None
        b = conn.execute("SELECT nom, prenom FROM benevoles WHERE id=?", (bid,)).fetchone()
        if b is None:
            return None
        return f"{b['nom'] or ''} {b['prenom'] or ''}".strip(), "benevole", bid
    if choix.startswith("car:"):
        nom = choix[4:]
        if _gestionnaire_total(d) and nom in _cars(conn):
            return nom, "car", None
        return None
    if choix == "moi" and d["username"]:
        return d["username"], "user", None
    return None


def _intervenants_de(conn, visite_id):
    return [dict(r) for r in conn.execute(
        "SELECT nom, type, benevole_id FROM visites_prevues_visiteurs WHERE visite_id=? ORDER BY ordre, id",
        (visite_id,))]


def _enregistrer_intervenants(conn, visite_id, liste):
    conn.execute("DELETE FROM visites_prevues_visiteurs WHERE visite_id=?", (visite_id,))
    for i, (nom, typ, bid) in enumerate(liste):
        conn.execute("INSERT INTO visites_prevues_visiteurs (visite_id, ordre, nom, type, benevole_id) "
                     "VALUES (?,?,?,?,?)", (visite_id, i, nom, typ, bid))


def _benevoles(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, TRIM(COALESCE(nom,'') || ' ' || COALESCE(prenom,'')) AS nom FROM benevoles "
        "WHERE TRIM(COALESCE(nom,'') || COALESCE(prenom,'')) <> '' ORDER BY nom COLLATE NOCASE")]


# ------------------------------------------------------------
# Données
# ------------------------------------------------------------
def _cars(conn):
    rows = conn.execute(
        "SELECT param_value FROM parametres WHERE param_name='car' ORDER BY param_value").fetchall()
    return [r[0] for r in rows if r[0] and r[0] != "Non affecté"]


def _associations(conn):
    return conn.execute(
        "SELECT Id AS id, nom_association, COALESCE(CAR,'') AS car, COALESCE(COMMUNE,'') AS commune "
        "FROM associations WHERE LOWER(TRIM(COALESCE(validite,'')))='oui' "
        "ORDER BY nom_association COLLATE NOCASE").fetchall()


def _visites(conn, debut, fin, car_filtre="", asso_id=None):
    sql = """SELECT v.*, a.nom_association, COALESCE(a.COMMUNE,'') AS commune,
                    COALESCE(a.CAR,'') AS car_actuel
             FROM visites_prevues v JOIN associations a ON a.Id = v.association_id
             WHERE v.date_visite BETWEEN ? AND ?"""
    args = [debut, fin]
    if car_filtre:
        sql += (" AND (LOWER(v.visiteur)=LOWER(?) OR LOWER(COALESCE(a.CAR,''))=LOWER(?) OR EXISTS ("
                "SELECT 1 FROM visites_prevues_visiteurs x WHERE x.visite_id=v.id AND LOWER(x.nom)=LOWER(?)))")
        args += [car_filtre, car_filtre, car_filtre]
    if asso_id:
        sql += " AND v.association_id=?"
        args.append(asso_id)
    sql += " ORDER BY v.date_visite, COALESCE(v.heure_debut,''), a.nom_association"
    return conn.execute(sql, args).fetchall()


def _nom_association(conn, asso_id):
    if not asso_id:
        return ""
    r = conn.execute("SELECT nom_association FROM associations WHERE Id=?", (asso_id,)).fetchone()
    return r[0] if r else ""


def _periode():
    today = date.today()
    annee = request.args.get("annee", type=int) or today.year
    if not 2000 <= annee <= 2100:
        annee = today.year
    mois = request.args.get("mois", type=int) or today.month
    if not 1 <= mois <= 12:
        mois = today.month
    asso = request.args.get("asso", type=int)
    # fiche d'une association : l'année entière est plus parlante qu'un mois
    vue = request.args.get("vue", "annee" if asso else "mois")
    if vue not in ("mois", "annee"):
        vue = "mois"
    return annee, mois, vue, (request.args.get("car") or "").strip(), asso


def _construire(conn, annee, mois, vue, car_filtre, asso_id=None):
    d = _contexte_droits(conn)
    if vue == "annee":
        debut, fin = f"{annee}-01-01", f"{annee}-12-31"
    else:
        dernier = calendar.monthrange(annee, mois)[1]
        debut, fin = f"{annee}-{mois:02d}-01", f"{annee}-{mois:02d}-{dernier:02d}"
    visites = [dict(v) for v in _visites(conn, debut, fin, car_filtre, asso_id)]
    cars = _cars(conn)
    derniere = {r[0]: r[1] for r in conn.execute(
        "SELECT partenaire_id, MAX(date_visite) FROM fiches_visite GROUP BY partenaire_id")}
    interv = {}
    for r in conn.execute("SELECT visite_id, nom, type, benevole_id FROM visites_prevues_visiteurs ORDER BY ordre, id"):
        interv.setdefault(r["visite_id"], []).append({"nom": r["nom"], "type": r["type"], "benevole_id": r["benevole_id"]})
    for v in visites:
        v["intervenants"] = interv.get(v["id"]) or [{"nom": v["visiteur"], "type": v.get("visiteur_type") or "texte",
                                                      "benevole_id": v.get("visiteur_benevole_id")}]
        v["visiteurs_txt"] = ", ".join(i["nom"] for i in v["intervenants"])
        v["modifiable"] = bool(_peut_modifier(d, v))
        v["derniere_fiche"] = derniere.get(v["association_id"])
    # une couleur stable par personne (CAR + tous les intervenants), légende = personnes présentes sur la période
    presents = {i["nom"] for v in visites for i in v["intervenants"]}
    if car_filtre:
        presents.add(car_filtre)
    palette = {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(sorted(set(cars) | presents))}
    for v in visites:
        v["couleur"] = palette.get(v["visiteur"], "#6c757d")
    couleurs = {n: palette[n] for n in sorted(presents)}
    return d, visites, cars, couleurs


def _semaines(annee, mois, visites):
    par_jour = {}
    for v in visites:
        par_jour.setdefault(v["date_visite"], []).append(v)
    return [[{"date": j, "iso": j.isoformat(), "dans_mois": j.month == mois,
              "visites": par_jour.get(j.isoformat(), [])} for j in sem]
            for sem in calendar.Calendar(0).monthdatescalendar(annee, mois)]


def _mois_voisin(annee, mois, delta):
    m = mois - 1 + delta
    return annee + m // 12, m % 12 + 1


def date_fr(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return iso or ""


# ------------------------------------------------------------
# Écrans
# ------------------------------------------------------------
@bp.route("/")
@login_required
def calendrier():
    annee, mois, vue, car_filtre, asso_filtre = _periode()
    with _connect() as conn:
        d, visites, cars, couleurs = _construire(conn, annee, mois, vue, car_filtre, asso_filtre)
        assos = [dict(a) for a in _associations(conn)]
        asso_nom = _nom_association(conn, asso_filtre)
        benevoles = _benevoles(conn) if (_gestionnaire_total(d) or d["car"] or d["ecriture"]) else []
    peut_saisir = bool(_gestionnaire_total(d) or d["car"] or d["ecriture"])
    if d["car"] and not _gestionnaire_total(d):
        assos = [a for a in assos if _meme(a["car"], d["username"])]
    return render_template(
        "calendrier_visites/calendrier.html",
        annee=annee, mois=mois, vue=vue, car_filtre=car_filtre, asso_filtre=asso_filtre, asso_nom=asso_nom, MOIS=MOIS, JOURS=JOURS,
        semaines=_semaines(annee, mois, visites) if vue == "mois" else None,
        visites=visites, cars=cars, couleurs=couleurs, statuts=STATUTS,
        assos_saisie=assos, peut_saisir=peut_saisir, droits=d,
        benevoles=benevoles, peut_choisir_car=bool(_gestionnaire_total(d)), prec=_mois_voisin(annee, mois, -1),
        suiv=_mois_voisin(annee, mois, 1), aujourdhui=date.today().isoformat(), date_fr=date_fr)


def _date_iso(texte):
    """Accepte jj/mm/aaaa (champ .date-fr) ou aaaa-mm-jj ; renvoie aaaa-mm-jj, ou None."""
    texte = (texte or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texte, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _lire_formulaire(f):
    dv = _date_iso(f.get("date_visite"))
    if not dv:
        return None
    statut = f.get("statut") or "prevue"
    return {
        "association_id": f.get("association_id", type=int), "date_visite": dv,
        "heure_debut": (f.get("heure_debut") or "").strip() or None,
        "heure_fin": (f.get("heure_fin") or "").strip() or None,
        "statut": statut if statut in STATUTS else "prevue",
        "commentaire": (f.get("commentaire") or "").strip(),
    }


def _retour():
    cible = request.form.get("retour") or ""
    # redirection interne uniquement
    if not cible.startswith("/calendrier-visites"):
        cible = url_for("calendrier_visites.calendrier")
    return redirect(cible)


@bp.route("/enregistrer", methods=["POST"])
@login_required
def enregistrer():
    champs = _lire_formulaire(request.form)
    if not champs or not champs["association_id"]:
        flash("⚠️ Association et date valides obligatoires.", "warning")
        return _retour()
    visite_id = request.form.get("visite_id", type=int)
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        d = _contexte_droits(conn)
        asso = conn.execute("SELECT COALESCE(CAR,'') AS car FROM associations WHERE Id=?",
                            (champs["association_id"],)).fetchone()
        if asso is None:
            flash("Association introuvable.", "danger")
            return _retour()
        ancienne = None
        if visite_id:
            ancienne = conn.execute(
                "SELECT v.*, COALESCE(a.CAR,'') AS car_actuel FROM visites_prevues v "
                "JOIN associations a ON a.Id=v.association_id WHERE v.id=?", (visite_id,)).fetchone()
        existants = _intervenants_de(conn, visite_id) if visite_id else []
        if visite_id and not existants and ancienne is not None:
            existants = [{"nom": ancienne["visiteur"], "type": ancienne["visiteur_type"],
                          "benevole_id": ancienne["visiteur_benevole_id"]}]
        liste, vus = [], set()
        for choix in request.form.getlist("visiteur_choix")[:MAX_INTERVENANTS]:
            if not choix.strip():
                continue
            r = _resoudre_visiteur(conn, d, choix, existants)
            if r is None:
                liste = []
                break
            cle = (r[1], r[2], r[0].lower())
            if cle not in vus:
                vus.add(cle)
                liste.append(r)
        if not liste:
            flash("⚠️ Choisissez au moins un intervenant dans la liste (vous-même, un CAR ou un bénévole existant).",
                  "warning")
            return _retour()
        champs["visiteur"], champs["visiteur_type"], champs["visiteur_bene"] = liste[0]

        if visite_id:
            # droit sur la visite existante ET sur l'association cible (changement d'association)
            ok = (ancienne is not None and _peut_modifier(d, ancienne)
                  and (_peut_creer(d, asso["car"]) or _meme(ancienne["cree_par"], d["username"])))
            if not ok:
                write_log(f"⛔ calendrier_visites : modification refusée visite={visite_id} user={d['username']}")
                flash("⛔ Vous ne pouvez pas modifier cette visite.", "danger")
                return _retour()
            conn.execute(
                """UPDATE visites_prevues SET association_id=?, date_visite=?, heure_debut=?, heure_fin=?,
                   visiteur=?, visiteur_type=?, visiteur_benevole_id=?,
                   car_association=?, statut=?, commentaire=?, modifie_par=?, modifie_le=?
                   WHERE id=?""",
                (champs["association_id"], champs["date_visite"], champs["heure_debut"], champs["heure_fin"],
                 champs["visiteur"], champs["visiteur_type"], champs["visiteur_bene"], asso["car"], champs["statut"], champs["commentaire"],
                 d["username"], now, visite_id))
            _enregistrer_intervenants(conn, visite_id, liste)
            flash("✅ Visite modifiée.", "success")
        else:
            if not _peut_creer(d, asso["car"]):
                write_log(f"⛔ calendrier_visites : création refusée user={d['username']}")
                flash("⛔ Vous ne pouvez pas saisir de visite pour cette association.", "danger")
                return _retour()
            noms = sorted(r[0].lower() for r in liste)
            doublon = any(
                sorted(i["nom"].lower() for i in _intervenants_de(conn, c["id"])) == noms
                for c in conn.execute(
                    "SELECT id FROM visites_prevues WHERE association_id=? AND date_visite=? "
                    "AND COALESCE(heure_debut,'')=?",
                    (champs["association_id"], champs["date_visite"], champs["heure_debut"] or "")))
            if doublon:
                flash("ℹ️ Cette visite existe déjà au calendrier (même association, date, heure et intervenants).",
                      "info")
                return _retour()
            cur = conn.execute(
                """INSERT INTO visites_prevues (association_id, date_visite, heure_debut, heure_fin, visiteur,
                   visiteur_type, visiteur_benevole_id, car_association, statut, commentaire, cree_par, cree_le)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (champs["association_id"], champs["date_visite"], champs["heure_debut"], champs["heure_fin"],
                 champs["visiteur"], champs["visiteur_type"], champs["visiteur_bene"], asso["car"], champs["statut"], champs["commentaire"], d["username"], now))
            _enregistrer_intervenants(conn, cur.lastrowid, liste)
            flash("✅ Visite ajoutée au calendrier.", "success")
        conn.commit()
    return _retour()


@bp.route("/<int:visite_id>/supprimer", methods=["POST"])
@login_required
def supprimer(visite_id):
    with _connect() as conn:
        d = _contexte_droits(conn)
        v = conn.execute(
            "SELECT v.*, COALESCE(a.CAR,'') AS car_actuel FROM visites_prevues v "
            "JOIN associations a ON a.Id=v.association_id WHERE v.id=?", (visite_id,)).fetchone()
        if v is None or not _peut_modifier(d, v):
            flash("⛔ Vous ne pouvez pas supprimer cette visite.", "danger")
            return _retour()
        conn.execute("DELETE FROM visites_prevues_visiteurs WHERE visite_id=?", (visite_id,))
        conn.execute("DELETE FROM visites_prevues WHERE id=?", (visite_id,))
        conn.commit()
    flash("🗑️ Visite supprimée.", "success")
    return _retour()


@bp.route("/pdf")
@login_required
def pdf():
    annee, mois, vue, car_filtre, asso_filtre = _periode()
    with _connect() as conn:
        d, visites, cars, couleurs = _construire(conn, annee, mois, vue, car_filtre, asso_filtre)
        asso_nom = _nom_association(conn, asso_filtre)
    titre = (f"Visites prévisionnelles aux associations — {MOIS[mois]} {annee}" if vue == "mois"
             else f"Visites prévisionnelles aux associations — année {annee}")
    if asso_nom:
        titre += f" — {asso_nom}"
    html = render_template(
        "calendrier_visites/pdf.html", titre=titre, vue=vue, car_filtre=car_filtre, JOURS=JOURS,
        semaines=_semaines(annee, mois, visites) if vue == "mois" else None,
        visites=visites, couleurs=couleurs, statuts=STATUTS, date_fr=date_fr,
        organisation=get_organisation(), genere_le=datetime.now().strftime("%d/%m/%Y"))
    out = io.BytesIO()
    HTML(string=html, base_url=request.url_root).write_pdf(
        out, stylesheets=[CSS(string="@page { size: A4 landscape; margin: 1cm; }")])
    out.seek(0)
    nom = f"calendrier_visites_{'asso' + str(asso_filtre) + '_' if asso_filtre else ''}{annee}" + (f"_{mois:02d}" if vue == "mois" else "") + ".pdf"
    return send_file(out, mimetype="application/pdf", as_attachment=False, download_name=nom)
