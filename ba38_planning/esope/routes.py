"""Planning ESOPE : planning réel (semaine), événements, fermetures, impression."""

from datetime import date, timedelta

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required

from ba38_utilitaires.core import get_db_connection, write_log, upload_database, require_access, has_access
from ba38_planning.esope import esope_bp
from ba38_planning.esope import donnees as D


def _retour(defaut_endpoint="esope.semaine", **kw):
    """Redirection vers la page d'origine (limitée au module)."""
    retour = request.form.get("retour") or ""
    if retour.startswith("/planning/esope"):
        return redirect(retour)
    return redirect(url_for(defaut_endpoint, **kw))


def _int(valeur, defaut=None):
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return defaut


def _heures_form(prefixe=""):
    """Lit heure_debut / heure_fin du formulaire. Retourne (debut, fin, erreur)."""
    debut = D.heure_ok(request.form.get(prefixe + "heure_debut"))
    fin = D.heure_ok(request.form.get(prefixe + "heure_fin"))
    if debut is False or fin is False:
        return None, None, "Heure invalide (format attendu : 14:45)."
    if debut and fin and fin <= debut:
        return None, None, "L'heure de fin doit être après l'heure de début."
    return debut, fin, None


# ─────────────────────────────────────────────────────────────
# Accueil du module
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/")
@login_required
@require_access("planning_esope", "lecture")
def index():
    conn = get_db_connection()
    aujourd_hui = date.today()
    mois = []
    a, m = aujourd_hui.year, aujourd_hui.month
    statuts = {"brouillon": "", "envoye": "envoyé", "verrouille": "🔒 verrouillé"}
    for _ in range(4):
        etat = D.etat_mois(conn, a, m)
        p = conn.execute("SELECT statut FROM esope_periodes WHERE mois = ?", (f"{a}-{m:02d}",)).fetchone()
        nb_alertes = conn.execute(
            "SELECT COUNT(*) FROM esope_affectations WHERE alerte = 1 AND creneau_id IN "
            "(SELECT id FROM esope_creneaux WHERE substr(date, 1, 7) = ?)", (f"{a}-{m:02d}",)).fetchone()[0]
        mois.append({"valeur": f"{a}-{m:02d}", "libelle": f"{D.MOIS[m - 1].capitalize()} {a}", **etat,
                     "statut_envoi": statuts.get(p["statut"], "") if p else "", "nb_alertes": nb_alertes})
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    nb_benevoles = len(D.benevoles_esope(conn))
    conn.close()
    return render_template("planning/esope/index.html", mois=mois, nb_benevoles=nb_benevoles,
                           peut_ecrire=has_access("planning_esope", "ecriture"))


# ─────────────────────────────────────────────────────────────
# Planning de la semaine
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/semaine")
@login_required
@require_access("planning_esope", "lecture")
def semaine():
    jour = D.parse_date(request.args.get("date"), date.today())
    lundi = D.lundi_de(jour)
    conn = get_db_connection()
    data = D.semaine(conn, lundi)
    lignes = D.lignes_planning(conn)
    benevoles = D.benevoles_esope(conn)
    conn.close()
    samedi = lundi + timedelta(days=5)
    return render_template(
        "planning/esope/semaine.html",
        lundi=lundi, samedi=samedi,
        precedente=(lundi - timedelta(days=7)).isoformat(),
        suivante=(lundi + timedelta(days=7)).isoformat(),
        mois_courant=f"{lundi.year}-{lundi.month:02d}",
        titre=f"Semaine du {lundi.day} {D.MOIS[lundi.month - 1]} au {samedi.day} {D.MOIS[samedi.month - 1]} {samedi.year}",
        lignes=lignes, benevoles=benevoles, statuts=D.STATUTS,
        peut_ecrire=has_access("planning_esope", "ecriture"),
        peut_sms=has_access("sms_benevoles", "lecture"),
        **data,
    )


@esope_bp.route("/generer", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def generer():
    am = D.parse_mois(request.form.get("mois"))
    if not am:
        flash("❌ Mois invalide.", "danger")
        return _retour("esope.index")
    annee, mois = am
    action = request.form.get("action", "generer")
    conn = get_db_connection()
    if action == "reinitialiser":
        nb_c, nb_a = D.reinitialiser_mois(conn, annee, mois)
        msg = f"🔄 {D.MOIS[mois - 1].capitalize()} {annee} réinitialisé : {nb_c} créneaux, {nb_a} bénévoles placés depuis le modèle."
    else:
        nb_c, nb_a = D.generer_mois(conn, annee, mois)
        if nb_c:
            msg = f"✅ {D.MOIS[mois - 1].capitalize()} {annee} : {nb_c} créneaux créés, {nb_a} bénévoles placés depuis le modèle."
        else:
            msg = f"ℹ️ {D.MOIS[mois - 1].capitalize()} {annee} était déjà généré : rien n'a été modifié."
    conn.commit()
    conn.close()
    upload_database()
    write_log(f"ESOPE {action} {annee}-{mois:02d} : {nb_c} créneaux, {nb_a} affectations")
    flash(msg, "success" if nb_c else "info")
    premier = date(annee, mois, 1)
    return redirect(url_for("esope.semaine", date=max(premier, D.lundi_de(premier)).isoformat()))


# ─────────────────────────────────────────────────────────────
# Affectations
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/affectation/ajouter", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def affectation_ajouter():
    creneau_id = _int(request.form.get("creneau_id"))
    benevole_id = _int(request.form.get("benevole_id"))
    debut, fin, err = _heures_form()
    if err or not creneau_id or not benevole_id:
        flash(f"❌ {err or 'Choisissez un bénévole.'}", "danger")
        return _retour()
    conn = get_db_connection()
    c = conn.execute("SELECT * FROM esope_creneaux WHERE id = ?", (creneau_id,)).fetchone()
    if not c:
        conn.close()
        flash("❌ Créneau introuvable.", "danger")
        return _retour()
    nb = conn.execute("SELECT COUNT(*) FROM esope_affectations WHERE creneau_id = ? AND statut != 'annule'",
                      (creneau_id,)).fetchone()[0]
    if nb >= c["nb_max"]:
        conn.close()
        flash(f"⚠️ Ce créneau est complet ({c['nb_max']} bénévole(s) maximum). "
              "Augmentez le maximum du créneau si besoin.", "warning")
        return _retour()
    existe = conn.execute("SELECT id, statut FROM esope_affectations WHERE creneau_id = ? AND benevole_id = ?",
                          (creneau_id, benevole_id)).fetchone()
    commentaire = (request.form.get("commentaire") or "").strip() or None
    if existe:
        # bénévole déjà présent mais annulé : on le remet
        conn.execute("UPDATE esope_affectations SET statut = 'prevu', heure_debut = ?, heure_fin = ?, "
                     "commentaire = ?, modifie_le = datetime('now','localtime'), user_modif = ? WHERE id = ?",
                     (debut, fin, commentaire, D.utilisateur(), existe["id"]))
        aff_id = existe["id"]
    else:
        aff_id = conn.execute(
            "INSERT INTO esope_affectations (creneau_id, benevole_id, heure_debut, heure_fin, commentaire, "
            "origine, user_modif) VALUES (?, ?, ?, ?, ?, 'manuel', ?)",
            (creneau_id, benevole_id, debut, fin, commentaire, D.utilisateur())).lastrowid
    D.journaliser(conn, "ajout", creneau_id=creneau_id, affectation_id=aff_id, benevole_id=benevole_id,
                  nouveau=D.fmt_plage(debut, fin) if debut and fin else None, commentaire=commentaire)
    conn.commit()
    conn.close()
    upload_database()
    return _retour()


@esope_bp.route("/affectation/<int:aff_id>/modifier", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def affectation_modifier(aff_id):
    debut, fin, err = _heures_form()
    if err:
        flash(f"❌ {err}", "danger")
        return _retour()
    statut = request.form.get("statut", "prevu")
    if statut not in D.STATUTS:
        statut = "prevu"
    commentaire = (request.form.get("commentaire") or "").strip() or None
    conn = get_db_connection()
    a = conn.execute("SELECT * FROM esope_affectations WHERE id = ?", (aff_id,)).fetchone()
    if not a:
        conn.close()
        flash("❌ Affectation introuvable.", "danger")
        return _retour()
    if a["statut"] == "annule" and statut != "annule":
        c = conn.execute("SELECT nb_max FROM esope_creneaux WHERE id = ?", (a["creneau_id"],)).fetchone()
        nb = conn.execute("SELECT COUNT(*) FROM esope_affectations WHERE creneau_id = ? AND statut != 'annule'",
                          (a["creneau_id"],)).fetchone()[0]
        if c and nb >= c["nb_max"]:
            conn.close()
            flash("⚠️ Le créneau est complet : impossible de remettre ce bénévole.", "warning")
            return _retour()
    # enregistrer depuis le planning vaut lecture de la réponse du bénévole
    conn.execute("UPDATE esope_affectations SET statut = ?, heure_debut = ?, heure_fin = ?, commentaire = ?, alerte = 0, "
                 "modifie_le = datetime('now','localtime'), user_modif = ? WHERE id = ?",
                 (statut, debut, fin, commentaire, D.utilisateur(), aff_id))
    ancien = f"{D.STATUTS.get(a['statut'])} {D.fmt_plage(a['heure_debut'], a['heure_fin']) if a['heure_debut'] and a['heure_fin'] else ''}".strip()
    nouveau = f"{D.STATUTS.get(statut)} {D.fmt_plage(debut, fin) if debut and fin else ''}".strip()
    D.journaliser(conn, "modification", creneau_id=a["creneau_id"], affectation_id=aff_id,
                  benevole_id=a["benevole_id"], ancien=ancien, nouveau=nouveau, commentaire=commentaire)
    conn.commit()
    conn.close()
    upload_database()
    return _retour()


@esope_bp.route("/affectation/<int:aff_id>/supprimer", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def affectation_supprimer(aff_id):
    conn = get_db_connection()
    a = conn.execute("SELECT * FROM esope_affectations WHERE id = ?", (aff_id,)).fetchone()
    if a:
        conn.execute("DELETE FROM esope_affectations WHERE id = ?", (aff_id,))
        D.journaliser(conn, "retrait", creneau_id=a["creneau_id"], affectation_id=aff_id,
                      benevole_id=a["benevole_id"], ancien=D.STATUTS.get(a["statut"]))
        conn.commit()
        upload_database()
    conn.close()
    return _retour()


# ─────────────────────────────────────────────────────────────
# Créneaux datés (retouche d'un jour) et événements
# ─────────────────────────────────────────────────────────────

def _min_max_form():
    nb_min = _int(request.form.get("nb_min"), 1)
    nb_max = _int(request.form.get("nb_max"), 1)
    if nb_min < 0 or nb_max < 1 or nb_min > nb_max:
        return None, None, "Nombre de bénévoles invalide (minimum ≤ maximum, maximum ≥ 1)."
    return nb_min, nb_max, None


@esope_bp.route("/creneau/<int:creneau_id>/modifier", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def creneau_modifier(creneau_id):
    debut, fin, err = _heures_form()
    nb_min, nb_max, err2 = _min_max_form()
    if err or err2 or not debut or not fin:
        flash(f"❌ {err or err2 or 'Horaires obligatoires.'}", "danger")
        return _retour()
    conn = get_db_connection()
    c = conn.execute("SELECT * FROM esope_creneaux WHERE id = ?", (creneau_id,)).fetchone()
    if not c:
        conn.close()
        flash("❌ Créneau introuvable.", "danger")
        return _retour()
    champs = {
        "heure_debut": debut, "heure_fin": fin, "nb_min": nb_min, "nb_max": nb_max,
        "ferme": 1 if request.form.get("ferme") else 0,
        "commentaire": (request.form.get("commentaire") or "").strip() or None,
    }
    if c["creneau_type_id"] is None:  # événement
        champs["libelle"] = (request.form.get("libelle") or "").strip() or c["libelle"]
        champs["lieu"] = (request.form.get("lieu") or "").strip() or None
        d = D.parse_date(request.form.get("date"))
        if d:
            champs["date"] = d.isoformat()
    sets = ", ".join(f"{k} = ?" for k in champs)
    conn.execute(f"UPDATE esope_creneaux SET {sets} WHERE id = ?", [*champs.values(), creneau_id])
    D.journaliser(conn, "creneau_modifie", creneau_id=creneau_id,
                  ancien=f"{D.fmt_plage(c['heure_debut'], c['heure_fin'])} {c['nb_min']}-{c['nb_max']}"
                         f"{' fermé' if c['ferme'] else ''}",
                  nouveau=f"{D.fmt_plage(debut, fin)} {nb_min}-{nb_max}{' fermé' if champs['ferme'] else ''}")
    conn.commit()
    conn.close()
    upload_database()
    return _retour()


@esope_bp.route("/creneau/<int:creneau_id>/supprimer", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def creneau_supprimer(creneau_id):
    conn = get_db_connection()
    c = conn.execute("SELECT * FROM esope_creneaux WHERE id = ?", (creneau_id,)).fetchone()
    if c:
        conn.execute("DELETE FROM esope_affectations WHERE creneau_id = ?", (creneau_id,))
        conn.execute("DELETE FROM esope_creneaux WHERE id = ?", (creneau_id,))
        D.journaliser(conn, "creneau_supprime", creneau_id=creneau_id,
                      ancien=f"{c['date']} {c['libelle'] or ''} {D.fmt_plage(c['heure_debut'], c['heure_fin'])}")
        conn.commit()
        upload_database()
        flash("🗑️ Créneau supprimé.", "success")
    conn.close()
    return _retour()


@esope_bp.route("/evenements", methods=["GET", "POST"])
@login_required
@require_access("planning_esope", "lecture")
def evenements():
    if request.method == "POST":
        if not has_access("planning_esope", "ecriture"):
            flash("⛔ Accès refusé", "danger")
            return redirect(url_for("esope.evenements"))
        d = D.parse_date(request.form.get("date"))
        libelle = (request.form.get("libelle") or "").strip()
        debut, fin, err = _heures_form()
        nb_min, nb_max, err2 = _min_max_form()
        if not d or not libelle or not debut or not fin or err or err2:
            flash(f"❌ {err or err2 or 'Date, libellé et horaires sont obligatoires.'}", "danger")
            return redirect(url_for("esope.evenements"))
        conn = get_db_connection()
        cid = conn.execute(
            "INSERT INTO esope_creneaux (date, libelle, lieu, heure_debut, heure_fin, nb_min, nb_max, "
            "commentaire, user_creation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d.isoformat(), libelle, (request.form.get("lieu") or "").strip() or None, debut, fin,
             nb_min, nb_max, (request.form.get("commentaire") or "").strip() or None, D.utilisateur())).lastrowid
        D.journaliser(conn, "evenement_cree", creneau_id=cid, nouveau=f"{d.isoformat()} {libelle}")
        conn.commit()
        conn.close()
        upload_database()
        flash(f"✅ Événement « {libelle} » créé. Ajoutez les bénévoles depuis le planning de la semaine.", "success")
        return redirect(url_for("esope.semaine", date=d.isoformat()))

    conn = get_db_connection()
    evts = D.evenements_a_venir(conn, date.today() - timedelta(days=30))
    conn.close()
    return render_template("planning/esope/evenements.html", evenements=evts,
                           peut_ecrire=has_access("planning_esope", "ecriture"))


# ─────────────────────────────────────────────────────────────
# Fermetures
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/fermetures", methods=["GET", "POST"])
@login_required
@require_access("planning_esope", "lecture")
def fermetures():
    conn = get_db_connection()
    if request.method == "POST":
        if not has_access("planning_esope", "ecriture"):
            conn.close()
            flash("⛔ Accès refusé", "danger")
            return redirect(url_for("esope.fermetures"))
        if request.form.get("action") == "supprimer":
            conn.execute("DELETE FROM esope_fermetures WHERE date = ?", (request.form.get("date"),))
            flash("🗑️ Jour de fermeture retiré.", "success")
        else:
            debut = D.parse_date(request.form.get("date_debut"))
            fin = D.parse_date(request.form.get("date_fin")) or debut
            motif = (request.form.get("motif") or "").strip() or None
            if not debut or fin < debut or (fin - debut).days > 92:
                conn.close()
                flash("❌ Dates invalides (période de 3 mois maximum).", "danger")
                return redirect(url_for("esope.fermetures"))
            nb, deja = 0, 0
            d = debut
            while d <= fin:
                if d.weekday() <= 5:
                    conn.execute("INSERT OR REPLACE INTO esope_fermetures (date, motif) VALUES (?, ?)",
                                 (d.isoformat(), motif))
                    nb += 1
                    deja += conn.execute("SELECT COUNT(*) FROM esope_creneaux WHERE date = ? "
                                         "AND creneau_type_id IS NOT NULL", (d.isoformat(),)).fetchone()[0]
                d += timedelta(days=1)
            flash(f"✅ {nb} jour(s) de fermeture enregistré(s).", "success")
            if deja:
                flash(f"⚠️ {deja} créneau(x) déjà générés ces jours-là restent dans le planning : "
                      "supprimez-les ou réinitialisez le mois.", "warning")
        conn.commit()
        conn.close()
        upload_database()
        return redirect(url_for("esope.fermetures"))

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM esope_fermetures WHERE date >= ? ORDER BY date",
        ((date.today() - timedelta(days=31)).isoformat(),))]
    conn.close()
    for r in rows:
        r["date_txt"] = D.fmt_date_courte(date.fromisoformat(r["date"]))
    return render_template("planning/esope/fermetures.html", fermetures=rows,
                           peut_ecrire=has_access("planning_esope", "ecriture"))


# ─────────────────────────────────────────────────────────────
# Historique
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/historique")
@login_required
@require_access("planning_esope", "lecture")
def historique():
    conn = get_db_connection()
    rows = [dict(r) for r in conn.execute("""
        SELECT h.*, b.nom, b.prenom, c.date AS creneau_date, c.libelle AS creneau_libelle,
               c.poste, f.nom AS fonction
        FROM esope_historique h
        LEFT JOIN benevoles b ON b.id = h.benevole_id
        LEFT JOIN esope_creneaux c ON c.id = h.creneau_id
        LEFT JOIN esope_fonctions f ON f.id = c.fonction_id
        ORDER BY h.id DESC LIMIT 300
    """)]
    conn.close()
    return render_template("planning/esope/historique.html", lignes=rows)


# ─────────────────────────────────────────────────────────────
# Impression : une semaine par page A4 paysage
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/imprimer")
@login_required
@require_access("planning_esope", "lecture")
def imprimer():
    am = D.parse_mois(request.args.get("mois"))
    if am:
        lundis = D.lundis_du_mois(*am)
        titre = f"{D.MOIS[am[1] - 1].capitalize()} {am[0]}"
    else:
        lundis = [D.lundi_de(D.parse_date(request.args.get("date"), date.today()))]
        titre = None
    conn = get_db_connection()
    lignes = D.lignes_planning(conn)
    semaines = []
    for lundi in lundis:
        s = D.semaine(conn, lundi)
        samedi = lundi + timedelta(days=5)
        s["titre"] = (f"Semaine du {lundi.day} {D.MOIS[lundi.month - 1]} "
                      f"au {samedi.day} {D.MOIS[samedi.month - 1]} {samedi.year}")
        if am:
            for j in s["jours"]:
                j["hors_mois"] = j["date"].month != am[1]
        semaines.append(s)
    conn.close()
    return render_template("planning/esope/imprimer.html", semaines=semaines, lignes=lignes,
                           titre=titre, imprime_le=date.today())
