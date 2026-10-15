"""Planning ESOPE, phase 1 : page « Mes créneaux » ouverte par le lien personnel
du bénévole (sans compte Basilic).

Le bénévole peut confirmer ou annuler chacun de ses créneaux du mois et
indiquer ses disponibilités. Une fois le mois verrouillé par le gestionnaire,
il ne peut plus qu'annuler (signaler une absence). Chaque réponse alerte la
responsable ESOPE par mail et marque l'affectation « à voir » dans le planning.
"""

from datetime import date, datetime, timedelta

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ba38_utilitaires.core import write_log
from ba38_planning.esope import esope_bp
from ba38_planning.esope import donnees as D
from ba38_planning.esope import liens as L


def _maintenant():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _charger(token):
    """(conn, lien, benevole) ou 404 — même réponse pour un jeton inconnu ou mal formé."""
    conn = L.connexion_token(token)
    if conn is None:
        abort(404)
    lien = conn.execute("SELECT * FROM esope_liens WHERE token = ?", (token,)).fetchone()
    if lien is None:
        conn.close()
        abort(404)
    b = conn.execute("SELECT id, nom, prenom, email, telephone_portable FROM benevoles WHERE id = ?",
                     (lien["benevole_id"],)).fetchone()
    if b is None:
        conn.close()
        abort(404)
    return conn, lien, b


@esope_bp.errorhandler(404)
def _lien_invalide(e):
    if request.endpoint and request.endpoint.startswith("esope.public_"):
        return render_template("planning/esope/public_lien_invalide.html"), 404
    return e


def _auteur():
    if current_user.is_authenticated:
        return f"{current_user.email} (pour le bénévole)"
    return "bénévole (lien personnel)"


@esope_bp.route("/c/<token>")
def public_creneaux(token):
    conn, lien, b = _charger(token)
    try:
        if not lien["premiere_ouverture_le"] and not current_user.is_authenticated:
            conn.execute("UPDATE esope_liens SET premiere_ouverture_le = ? WHERE id = ?", (_maintenant(), lien["id"]))
            conn.commit()
        p = L.periode(conn, lien["mois"])
        creneaux = L.creneaux_benevole(conn, b["id"], lien["mois"])
        return render_template(
            "planning/esope/public_creneaux.html",
            token=token, lien=lien, benevole=b, creneaux=creneaux, periode=p,
            verrouille=p["statut"] == "verrouille", mois_txt=L.libelle_mois(lien["mois"]),
            gestionnaire=current_user.is_authenticated,
        )
    finally:
        conn.close()


@esope_bp.route("/c/<token>/planning")
def public_planning(token):
    """Planning complet du mois, en lecture (même page que l'impression)."""
    conn, lien, b = _charger(token)
    try:
        a, m = D.parse_mois(lien["mois"])
        lignes = D.lignes_planning(conn)
        semaines = []
        for lundi in D.lundis_du_mois(a, m):
            s = D.semaine(conn, lundi)
            samedi = lundi + timedelta(days=5)
            s["titre"] = (f"Semaine du {lundi.day} {D.MOIS[lundi.month - 1]} "
                          f"au {samedi.day} {D.MOIS[samedi.month - 1]} {samedi.year}")
            for j in s["jours"]:
                j["hors_mois"] = j["date"].month != m
            semaines.append(s)
        return render_template("planning/esope/imprimer.html", semaines=semaines, lignes=lignes,
                               titre=L.libelle_mois(lien["mois"]).capitalize(), imprime_le=date.today())
    finally:
        conn.close()


def _affectation_du_lien(conn, lien, aff_id):
    a, m = D.parse_mois(lien["mois"])
    return conn.execute("""
        SELECT a.*, c.date, c.ferme, c.nb_max, c.heure_debut AS c_debut, c.heure_fin AS c_fin
        FROM esope_affectations a JOIN esope_creneaux c ON c.id = a.creneau_id
        WHERE a.id = ? AND a.benevole_id = ? AND substr(c.date, 1, 7) = ?
    """, (aff_id, lien["benevole_id"], f"{a}-{m:02d}")).fetchone()


def _intitule(conn, aff_id):
    for c in L.creneaux_benevole(conn, *conn.execute(
            "SELECT a.benevole_id, substr(c.date,1,7) FROM esope_affectations a JOIN esope_creneaux c "
            "ON c.id = a.creneau_id WHERE a.id = ?", (aff_id,)).fetchone()):
        if c["id"] == aff_id:
            return f"{c['date_txt']} – {c['intitule']} ({c['plage']})"
    return f"créneau n° {aff_id}"


@esope_bp.route("/c/<token>/reponse", methods=["POST"])
def public_reponse(token):
    conn, lien, b = _charger(token)
    retour = redirect(url_for("esope.public_creneaux", token=token))
    try:
        p = L.periode(conn, lien["mois"])
        verrouille = p["statut"] == "verrouille"
        action = request.form.get("action")
        commentaire = (request.form.get("commentaire") or "").strip()[:500] or None
        ids = [request.form.get("affectation_id", type=int)] if action != "tout_confirmer" else [
            c["id"] for c in L.creneaux_benevole(conn, b["id"], lien["mois"])
            if c["statut"] == "prevu" and not c["passe"] and not c["ferme"]]
        if action not in ("confirmer", "annuler", "commenter", "tout_confirmer") or not ids or not ids[0]:
            flash("❌ Action inconnue.", "danger")
            return retour
        if verrouille and action in ("confirmer", "tout_confirmer"):
            flash("🔒 Le planning de ce mois est verrouillé : vous pouvez seulement signaler une absence.", "warning")
            return retour

        changements = []
        for aff_id in ids:
            a = _affectation_du_lien(conn, lien, aff_id)
            if not a or a["ferme"] or date.fromisoformat(a["date"]) < date.today():
                continue
            nouveau = {"confirmer": "confirme", "tout_confirmer": "confirme", "annuler": "annule"}.get(action, a["statut"])
            if nouveau == "confirme" and a["statut"] == "annule":
                nb = conn.execute("SELECT COUNT(*) FROM esope_affectations WHERE creneau_id = ? AND statut != 'annule'",
                                  (a["creneau_id"],)).fetchone()[0]
                if nb >= a["nb_max"]:
                    flash("⚠️ Ce créneau a été complété entre-temps : contactez la responsable ESOPE.", "warning")
                    continue
            if nouveau == a["statut"] and not commentaire:
                continue
            conn.execute("""UPDATE esope_affectations SET statut = ?, reponse_le = ?, reponse_commentaire = COALESCE(?, reponse_commentaire),
                            alerte = 1, modifie_le = ?, user_modif = ? WHERE id = ?""",
                         (nouveau, _maintenant(), commentaire, _maintenant(), _auteur(), aff_id))
            D.journaliser(conn, "reponse_benevole", creneau_id=a["creneau_id"], affectation_id=aff_id,
                          benevole_id=b["id"], ancien=D.STATUTS.get(a["statut"]), nouveau=D.STATUTS.get(nouveau),
                          commentaire=commentaire, auteur=_auteur())
            texte = _intitule(conn, aff_id)
            if nouveau != a["statut"]:
                quoi = "❌ <b>absence</b>" + (" (planning verrouillé)" if verrouille else "") \
                    if nouveau == "annule" else "✔ <b>confirmé</b>"
                changements.append(f"{quoi} : {texte}")
            else:
                changements.append(f"💬 commentaire : {texte}")

        if not changements:
            flash("ℹ️ Rien n'a changé.", "info")
            return retour
        conn.execute("UPDATE esope_liens SET derniere_reponse_le = ? WHERE id = ?", (_maintenant(), lien["id"]))
        conn.commit()
        L.alerter_responsable(conn, token, lien, b, changements, commentaire)
        write_log(f"ESOPE réponse bénévole {b['id']} ({lien['mois']}) : {len(changements)} changement(s)")
        flash("✅ Merci, votre réponse est enregistrée. La responsable ESOPE est prévenue.", "success")
        return retour
    finally:
        conn.close()


@esope_bp.route("/c/<token>/disponibilites", methods=["POST"])
def public_disponibilites(token):
    conn, lien, b = _charger(token)
    try:
        texte = (request.form.get("disponibilites") or "").strip()[:1000]
        if texte == (lien["disponibilites"] or ""):
            flash("ℹ️ Rien n'a changé.", "info")
        else:
            conn.execute("UPDATE esope_liens SET disponibilites = ?, disponibilites_le = ?, disponibilites_vu = 0, "
                         "derniere_reponse_le = ? WHERE id = ?",
                         (texte or None, _maintenant(), _maintenant(), lien["id"]))
            D.journaliser(conn, "disponibilites", benevole_id=b["id"], nouveau=texte or "(effacé)",
                          commentaire=L.libelle_mois(lien["mois"]), auteur=_auteur())
            conn.commit()
            if texte:
                L.alerter_responsable(conn, token, lien, b, [f"📝 disponibilités : <i>{texte}</i>"])
            flash("✅ Merci, vos disponibilités sont enregistrées.", "success")
        return redirect(url_for("esope.public_creneaux", token=token))
    finally:
        conn.close()
