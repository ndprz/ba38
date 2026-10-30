"""Planning ESOPE : paramétrage, planning modèle, bénévoles ESOPE."""

from datetime import date, datetime

from flask import render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required

from ba38_utilitaires.core import get_db_connection, upload_database, require_access, has_access
from ba38_planning.esope import esope_bp
from ba38_planning.esope import donnees as D
from ba38_planning.esope.routes import _int, _heures_form, _min_max_form


# ─────────────────────────────────────────────────────────────
# Paramétrage : fonctions et créneaux types
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/parametrage")
@login_required
@require_access("planning_esope", "ecriture")
def parametrage():
    conn = get_db_connection()
    fcts = D.fonctions(conn, actives_seulement=False)
    types = D.creneaux_types(conn, actifs_seulement=False)
    utilises = {r[0] for r in conn.execute(
        "SELECT creneau_type_id FROM esope_modele UNION SELECT creneau_type_id FROM esope_creneaux "
        "WHERE creneau_type_id IS NOT NULL")}
    conn.close()
    for t in types:
        t["utilise"] = t["id"] in utilises
        t["jour_nom"] = D.JOURS[t["jour"]] if t["jour"] < 6 else "dimanche"
    return render_template("planning/esope/parametrage.html", fonctions=fcts, types=types, jours=D.JOURS)


@esope_bp.route("/parametrage/fonction", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def fonction_enregistrer():
    fid = _int(request.form.get("id"))
    nom = (request.form.get("nom") or "").strip()
    if not nom:
        flash("❌ Le nom de la fonction est obligatoire.", "danger")
        return redirect(url_for("esope.parametrage"))
    ordre = _int(request.form.get("ordre"), 0)
    couleur = (request.form.get("couleur") or "#6c757d").strip()[:7]
    actif = 1 if request.form.get("actif") else 0
    conn = get_db_connection()
    doublon = conn.execute("SELECT id FROM esope_fonctions WHERE nom = ? AND id != ?",
                           (nom, fid or 0)).fetchone()
    if doublon:
        conn.close()
        flash(f"❌ Une fonction « {nom} » existe déjà.", "danger")
        return redirect(url_for("esope.parametrage"))
    if fid:
        conn.execute("UPDATE esope_fonctions SET nom = ?, ordre = ?, couleur = ?, actif = ? WHERE id = ?",
                     (nom, ordre, couleur, actif, fid))
    else:
        conn.execute("INSERT INTO esope_fonctions (nom, ordre, couleur, actif) VALUES (?, ?, ?, 1)",
                     (nom, ordre, couleur))
    conn.commit()
    conn.close()
    upload_database()
    flash(f"✅ Fonction « {nom} » enregistrée.", "success")
    return redirect(url_for("esope.parametrage"))


@esope_bp.route("/parametrage/creneau_type", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def creneau_type_enregistrer():
    ctid = _int(request.form.get("id"))
    fonction_id = _int(request.form.get("fonction_id"))
    jour = _int(request.form.get("jour"))
    debut, fin, err = _heures_form()
    nb_min, nb_max, err2 = _min_max_form()
    if err or err2 or not debut or not fin or fonction_id is None or jour is None or not 0 <= jour <= 5:
        flash(f"❌ {err or err2 or 'Fonction, jour et horaires sont obligatoires.'}", "danger")
        return redirect(url_for("esope.parametrage"))
    poste = (request.form.get("poste") or "").strip()
    libelle = (request.form.get("libelle") or "").strip() or None
    actif = 1 if request.form.get("actif") or not ctid else 0
    conn = get_db_connection()
    if ctid:
        conn.execute("""UPDATE esope_creneaux_types SET fonction_id = ?, poste = ?, jour = ?, heure_debut = ?,
                        heure_fin = ?, nb_min = ?, nb_max = ?, libelle = ?, actif = ? WHERE id = ?""",
                     (fonction_id, poste, jour, debut, fin, nb_min, nb_max, libelle, actif, ctid))
    else:
        conn.execute("""INSERT INTO esope_creneaux_types (fonction_id, poste, jour, heure_debut, heure_fin,
                        nb_min, nb_max, libelle) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (fonction_id, poste, jour, debut, fin, nb_min, nb_max, libelle))
    conn.commit()
    conn.close()
    upload_database()
    flash("✅ Créneau enregistré. Les mois déjà générés ne changent pas : "
          "seuls les prochains mois générés utiliseront ces horaires.", "success")
    return redirect(url_for("esope.parametrage"))


@esope_bp.route("/parametrage/creneau_type/<int:ctid>/supprimer", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def creneau_type_supprimer(ctid):
    conn = get_db_connection()
    utilise = conn.execute(
        "SELECT (SELECT COUNT(*) FROM esope_modele WHERE creneau_type_id = ?) + "
        "(SELECT COUNT(*) FROM esope_creneaux WHERE creneau_type_id = ?)", (ctid, ctid)).fetchone()[0]
    if utilise:
        conn.execute("UPDATE esope_creneaux_types SET actif = 0 WHERE id = ?", (ctid,))
        flash("ℹ️ Créneau utilisé dans le modèle ou le planning : il est désactivé au lieu d'être supprimé.", "info")
    else:
        conn.execute("DELETE FROM esope_creneaux_types WHERE id = ?", (ctid,))
        flash("🗑️ Créneau supprimé.", "success")
    conn.commit()
    conn.close()
    upload_database()
    return redirect(url_for("esope.parametrage"))


# ─────────────────────────────────────────────────────────────
# Planning modèle
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/modele")
@login_required
@require_access("planning_esope", "lecture")
def modele():
    conn = get_db_connection()
    types = D.creneaux_types(conn)
    grille = {}
    for t in types:
        t["plage"] = D.fmt_plage(t["heure_debut"], t["heure_fin"])
        grille.setdefault(f"{t['fonction_id']}|{t['poste'] or ''}", {})[t["jour"]] = t
    data = {
        "lignes": D.lignes_planning(conn),
        "grille": grille,
        "modele": D.modele(conn),
        "benevoles": D.benevoles_esope(conn),
    }
    conn.close()
    jours = [{"idx": i, "nom": n, "court": D.JOURS_COURTS[i]} for i, n in enumerate(D.JOURS)]
    return render_template("planning/esope/modele.html", jours=jours,
                           peut_ecrire=has_access("planning_esope", "ecriture"), **data)


@esope_bp.route("/modele/enregistrer", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def modele_enregistrer():
    mid = _int(request.form.get("id"))
    ctid = _int(request.form.get("creneau_type_id"))
    benevole_id = _int(request.form.get("benevole_id"))
    frequence = max(1, min(8, _int(request.form.get("frequence"), 1)))
    ref = D.parse_date(request.form.get("date_reference"))
    debut, fin, err = _heures_form()
    if err or not ctid or not benevole_id:
        flash(f"❌ {err or 'Choisissez un bénévole.'}", "danger")
        return redirect(url_for("esope.modele"))
    if frequence > 1 and not ref:
        flash("❌ Pour « 1 semaine sur N », indiquez une date de la première semaine de venue.", "danger")
        return redirect(url_for("esope.modele"))
    commentaire = (request.form.get("commentaire") or "").strip() or None
    ref_txt = D.lundi_de(ref).isoformat() if ref and frequence > 1 else None
    conn = get_db_connection()
    if mid:
        conn.execute("""UPDATE esope_modele SET benevole_id = ?, frequence = ?, date_reference = ?,
                        heure_debut = ?, heure_fin = ?, commentaire = ? WHERE id = ?""",
                     (benevole_id, frequence, ref_txt, debut, fin, commentaire, mid))
    else:
        doublon = conn.execute("SELECT 1 FROM esope_modele WHERE creneau_type_id = ? AND benevole_id = ? "
                               "AND frequence = ? AND COALESCE(date_reference,'') = ?",
                               (ctid, benevole_id, frequence, ref_txt or "")).fetchone()
        if doublon:
            conn.close()
            flash("ℹ️ Ce bénévole est déjà dans le modèle sur ce créneau.", "info")
            return redirect(url_for("esope.modele"))
        conn.execute("""INSERT INTO esope_modele (creneau_type_id, benevole_id, frequence, date_reference,
                        heure_debut, heure_fin, commentaire, user_creation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (ctid, benevole_id, frequence, ref_txt, debut, fin, commentaire, D.utilisateur()))
    conn.commit()
    conn.close()
    upload_database()
    return redirect(url_for("esope.modele") + f"#ct-{ctid}")


@esope_bp.route("/modele/<int:mid>/supprimer", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def modele_supprimer(mid):
    conn = get_db_connection()
    conn.execute("DELETE FROM esope_modele WHERE id = ?", (mid,))
    conn.commit()
    conn.close()
    upload_database()
    return redirect(url_for("esope.modele"))


# ─────────────────────────────────────────────────────────────
# Bénévoles ESOPE : fonctions possibles, jours disponibles, commentaire
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/benevoles")
@login_required
@require_access("planning_esope", "lecture")
def benevoles():
    conn = get_db_connection()
    data = {"benevoles": D.benevoles_esope(conn), "fonctions": D.fonctions(conn)}
    conn.close()
    return render_template("planning/esope/benevoles.html", jours=D.JOURS, jours_courts=D.JOURS_COURTS,
                           peut_ecrire=has_access("planning_esope", "ecriture"), **data)


@esope_bp.route("/benevoles/maj", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture", redirect_if_denied=False)
def benevoles_maj():
    """Enregistrement automatique d'une case du tableau Bénévoles ESOPE."""
    p = request.get_json(silent=True) or {}
    bid = _int(p.get("benevole_id"))
    champ = p.get("champ")
    conn = get_db_connection()
    if not bid or not conn.execute("SELECT 1 FROM benevoles WHERE id = ?", (bid,)).fetchone():
        conn.close()
        return jsonify(ok=False, erreur="Bénévole introuvable"), 404
    if champ == "fonction":
        fid = _int(p.get("fonction_id"))
        if p.get("valeur"):
            conn.execute("INSERT OR IGNORE INTO esope_benevoles_fonctions (benevole_id, fonction_id) VALUES (?, ?)",
                         (bid, fid))
        else:
            conn.execute("DELETE FROM esope_benevoles_fonctions WHERE benevole_id = ? AND fonction_id = ?",
                         (bid, fid))
    elif champ == "jour":
        jour = p.get("jour")
        if jour not in D.JOURS:
            conn.close()
            return jsonify(ok=False, erreur="Jour invalide"), 400
        actuels = set(D.jours_dispo_liste(conn.execute(
            "SELECT esope_jour_dispo FROM benevoles WHERE id = ?", (bid,)).fetchone()[0]))
        if p.get("valeur"):
            actuels.add(jour)
        else:
            actuels.discard(jour)
        conn.execute("UPDATE benevoles SET esope_jour_dispo = ? WHERE id = ?",
                     (",".join(j for j in D.JOURS if j in actuels) or None, bid))
    elif champ == "commentaire":
        conn.execute("UPDATE benevoles SET esope_commentaire = ? WHERE id = ?",
                     ((p.get("valeur") or "").strip() or None, bid))
    else:
        conn.close()
        return jsonify(ok=False, erreur="Champ inconnu"), 400
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@esope_bp.route("/benevole/<int:benevole_id>/profil", methods=["POST"])
@login_required
def benevole_profil(benevole_id):
    """Bloc ESOPE de la fiche bénévole."""
    if not (has_access("planning_esope", "ecriture") or has_access("benevoles", "ecriture")):
        abort(403)
    fonction_ids = [i for i in (_int(v) for v in request.form.getlist("fonctions")) if i]
    conn = get_db_connection()
    D.enregistrer_profil(conn, benevole_id, fonction_ids, request.form.getlist("jours"),
                         request.form.get("commentaire"))
    conn.commit()
    conn.close()
    upload_database()
    flash("✅ Profil ESOPE enregistré.", "success")
    return redirect(url_for("benevoles.update_benevole", benevole_id=benevole_id))


# ─────────────────────────────────────────────────────────────
# Absences des bénévoles ESOPE (table `absences` commune à tous les plannings)
# ─────────────────────────────────────────────────────────────

def _fr_vers_iso(txt):
    try:
        return datetime.strptime((txt or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _absence_form():
    """(date_debut, date_fin) du formulaire (champs <input type=date>) → textes jj/mm/aaaa, ou None."""
    debut = D.parse_date(request.form.get("date_debut"))
    fin = D.parse_date(request.form.get("date_fin")) or debut
    if not debut or not fin or fin < debut:
        return None
    return debut.strftime("%d/%m/%Y"), fin.strftime("%d/%m/%Y")


def _nb_creneaux_touches(conn, benevole_id, debut_txt, fin_txt):
    d, f = _fr_vers_iso(debut_txt), _fr_vers_iso(fin_txt)
    if not d or not f:
        return 0
    return conn.execute("""SELECT COUNT(*) FROM esope_affectations a JOIN esope_creneaux c ON c.id = a.creneau_id
                           WHERE a.benevole_id = ? AND a.statut != 'annule' AND c.ferme = 0 AND c.date BETWEEN ? AND ?""",
                        (benevole_id, d.isoformat(), f.isoformat())).fetchone()[0]


@esope_bp.route("/absences")
@login_required
@require_access("planning_esope", "lecture")
def absences():
    voir_toutes = request.args.get("voir_toutes") == "1"
    conn = get_db_connection()
    aujourdhui = date.today()
    lignes = []
    for r in conn.execute("""
            SELECT ab.id, ab.benevole_id, ab.date_debut, ab.date_fin, b.nom, b.prenom
            FROM absences ab JOIN benevoles b ON b.id = ab.benevole_id
            WHERE LOWER(TRIM(COALESCE(b.esope,''))) = 'oui'
            ORDER BY b.nom COLLATE NOCASE, b.prenom COLLATE NOCASE"""):
        d, f = _fr_vers_iso(r["date_debut"]), _fr_vers_iso(r["date_fin"])
        if not voir_toutes and f and f < aujourdhui:
            continue
        x = dict(r)
        x["debut_iso"] = d.isoformat() if d else ""
        x["fin_iso"] = f.isoformat() if f else ""
        x["nb_creneaux"] = _nb_creneaux_touches(conn, r["benevole_id"], r["date_debut"], r["date_fin"])
        lignes.append(x)
    lignes.sort(key=lambda x: (x["debut_iso"], x["nom"] or ""))
    benevoles = conn.execute("""SELECT id, nom, prenom FROM benevoles
                                WHERE LOWER(TRIM(COALESCE(esope,''))) = 'oui'
                                ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE""").fetchall()
    conn.close()
    return render_template("planning/esope/absences.html", absences=lignes, benevoles=benevoles,
                           voir_toutes=voir_toutes, peut_ecrire=has_access("planning_esope", "ecriture"))


@esope_bp.route("/absences/ajouter", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def absence_ajouter():
    bid = _int(request.form.get("benevole_id"))
    dates = _absence_form()
    conn = get_db_connection()
    ok = bid and conn.execute("SELECT 1 FROM benevoles WHERE id = ?", (bid,)).fetchone()
    if not ok or not dates:
        conn.close()
        flash("❌ Choisissez un bénévole et des dates valides (la fin ne peut pas précéder le début).", "danger")
        return redirect(url_for("esope.absences"))
    conn.execute("INSERT INTO absences (benevole_id, date_debut, date_fin) VALUES (?, ?, ?)", (bid, *dates))
    nb = _nb_creneaux_touches(conn, bid, *dates)
    D.journaliser(conn, "absence", benevole_id=bid, commentaire=f"du {dates[0]} au {dates[1]} ({nb} créneau(x) ESOPE)")
    conn.commit()
    conn.close()
    upload_database()
    flash(f"✅ Absence enregistrée : {nb} créneau(x) ESOPE passent en absent." if nb else "✅ Absence enregistrée (aucun créneau ESOPE concerné).", "success")
    return redirect(url_for("esope.absences"))


@esope_bp.route("/absences/<int:absence_id>/modifier", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def absence_modifier(absence_id):
    dates = _absence_form()
    if not dates:
        flash("❌ Dates invalides (la fin ne peut pas précéder le début).", "danger")
        return redirect(url_for("esope.absences"))
    conn = get_db_connection()
    a = conn.execute("SELECT benevole_id FROM absences WHERE id = ?", (absence_id,)).fetchone()
    if a:
        conn.execute("UPDATE absences SET date_debut = ?, date_fin = ? WHERE id = ?", (*dates, absence_id))
        D.journaliser(conn, "absence", benevole_id=a["benevole_id"], commentaire=f"modifiée : du {dates[0]} au {dates[1]}")
        conn.commit()
        upload_database()
        flash("✅ Absence modifiée.", "success")
    conn.close()
    return redirect(url_for("esope.absences"))


@esope_bp.route("/absences/<int:absence_id>/supprimer", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def absence_supprimer(absence_id):
    conn = get_db_connection()
    a = conn.execute("SELECT * FROM absences WHERE id = ?", (absence_id,)).fetchone()
    if a:
        conn.execute("DELETE FROM absences WHERE id = ?", (absence_id,))
        D.journaliser(conn, "absence", benevole_id=a["benevole_id"],
                      commentaire=f"supprimée : du {a['date_debut']} au {a['date_fin']}")
        conn.commit()
        upload_database()
        flash("🗑️ Absence supprimée : les créneaux concernés redeviennent actifs.", "warning")
    conn.close()
    return redirect(url_for("esope.absences"))
