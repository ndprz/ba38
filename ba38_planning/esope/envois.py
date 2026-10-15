"""Planning ESOPE, phase 1 : envoi du pré-planning, relances, suivi des
réponses, verrouillage du mois et textes des mails (côté gestionnaire).

Mode TEST (bascule de session MAIL_MODE, commune aux autres envois) :
2 mails au maximum, vers l'utilisateur connecté. En DEV, le garde-fou
d'envoyer_mail() redirige de toute façon tout vers l'utilisateur connecté.
"""

import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from ba38_utilitaires.core import (
    adresse_test_utilisateur, envoyer_mail, get_db_connection, get_db_path, has_access, require_access,
    upload_database, write_log,
)
from ba38_utilitaires.taches_fond import lancer_tache_fond
from ba38_planning.esope import esope_bp
from ba38_planning.esope import donnees as D
from ba38_planning.esope import liens as L

CODES_MAILS = {
    "esope_preplanning": "Pré-planning du mois",
    "esope_relance": "Relance des bénévoles sans réponse",
    "esope_rappel": "Rappel la veille du créneau",
}


def _mois_demande():
    mois = request.values.get("mois") or ""
    if not D.parse_mois(mois):
        t = date.today()
        a, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
        mois = f"{a}-{m:02d}"
    return mois


def _mode():
    return session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())


def _suivi(conn, mois):
    """Une ligne par bénévole ESOPE (ou affecté ce mois-ci)."""
    a, m = D.parse_mois(mois)
    rows = conn.execute("""
        SELECT b.id, b.nom, b.prenom, b.email, b.telephone_portable, LOWER(TRIM(COALESCE(b.esope,''))) = 'oui' AS esope,
               COUNT(CASE WHEN aff.statut != 'annule' THEN 1 END) AS nb_actifs,
               COUNT(CASE WHEN aff.statut = 'confirme' THEN 1 END) AS nb_confirmes,
               COUNT(CASE WHEN aff.statut = 'annule' THEN 1 END) AS nb_annules,
               COUNT(CASE WHEN aff.statut = 'prevu' THEN 1 END) AS nb_prevus,
               COUNT(CASE WHEN aff.alerte = 1 THEN 1 END) AS nb_alertes
        FROM benevoles b
        LEFT JOIN (SELECT a.* FROM esope_affectations a JOIN esope_creneaux c ON c.id = a.creneau_id
                   WHERE substr(c.date, 1, 7) = ?) aff ON aff.benevole_id = b.id
        WHERE LOWER(TRIM(COALESCE(b.esope,''))) = 'oui' OR aff.id IS NOT NULL
        GROUP BY b.id
        ORDER BY b.nom COLLATE NOCASE, b.prenom COLLATE NOCASE
    """, (f"{a}-{m:02d}",)).fetchall()
    liens = {r["benevole_id"]: dict(r) for r in conn.execute("SELECT * FROM esope_liens WHERE mois = ?", (mois,))}
    res = []
    for r in rows:
        d = dict(r)
        d["libelle"] = f"{d['nom'] or ''} {d['prenom'] or ''}".strip()
        d["lien"] = liens.get(d["id"])
        d["email_ok"] = bool(d["email"] and "@" in d["email"])
        l = d["lien"]
        if not d["email_ok"]:
            d["etat"] = "sans_mail"
        elif not l or not l["envoye_le"]:
            d["etat"] = "a_envoyer"
        elif l["envoi_erreur"]:
            d["etat"] = "erreur"
        elif l["derniere_reponse_le"]:
            d["etat"] = "repondu"
        elif l["premiere_ouverture_le"]:
            d["etat"] = "ouvert"
        else:
            d["etat"] = "envoye"
        res.append(d)
    return res


def _alertes(conn, mois, toutes=False):
    """Réponses des bénévoles du mois : à voir seulement, ou toutes (déjà vues comprises)."""
    a, m = D.parse_mois(mois)
    filtre = "a.reponse_le IS NOT NULL" if toutes else "a.alerte = 1"
    rows = conn.execute(f"""
        SELECT a.id, a.statut, a.reponse_le, a.reponse_commentaire, a.benevole_id, a.alerte, b.nom, b.prenom
        FROM esope_affectations a JOIN esope_creneaux c ON c.id = a.creneau_id
        LEFT JOIN benevoles b ON b.id = a.benevole_id
        WHERE {filtre} AND a.reponse_le IS NOT NULL AND substr(c.date, 1, 7) = ?
        ORDER BY a.reponse_le DESC
    """, (f"{a}-{m:02d}",)).fetchall()
    res = []
    for r in rows:
        d = dict(r)
        d["libelle"] = f"{d['nom'] or ''} {d['prenom'] or ''}".strip()
        d["creneau"] = next((c for c in L.creneaux_benevole(conn, d["benevole_id"], mois) if c["id"] == d["id"]), None)
        res.append(d)
    return res


# ─────────────────────────────────────────────────────────────
# Page de suivi
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/envoi")
@login_required
@require_access("planning_esope", "lecture")
def envoi():
    mois = _mois_demande()
    conn = get_db_connection()
    toutes = request.args.get("toutes") == "1"
    lignes = _suivi(conn, mois)
    alertes = _alertes(conn, mois, toutes)
    dispos = [l for l in lignes if l["lien"] and l["lien"]["disponibilites"]
              and (toutes or not l["lien"]["disponibilites_vu"])]
    nb_a_voir = sum(1 for x in alertes if x["alerte"]) + sum(1 for l in dispos if not l["lien"]["disponibilites_vu"])
    p = L.periode(conn, mois)
    etat = D.etat_mois(conn, *D.parse_mois(mois))
    conn.close()
    compte = {k: sum(1 for l in lignes if l["etat"] == k) for k in
              ("a_envoyer", "envoye", "ouvert", "repondu", "erreur", "sans_mail")}
    a, m = D.parse_mois(mois)
    precedent = f"{a - 1}-12" if m == 1 else f"{a}-{m - 1:02d}"
    suivant = f"{a + 1}-01" if m == 12 else f"{a}-{m + 1:02d}"
    return render_template(
        "planning/esope/envoi.html", mois=mois, mois_txt=L.libelle_mois(mois).capitalize(),
        precedent=precedent, suivant=suivant, lignes=lignes, alertes=alertes, dispos=dispos, periode=p,
        toutes=toutes, nb_a_voir=nb_a_voir,
        statuts_periode=L.STATUTS_PERIODE, etat=etat, compte=compte, mail_mode=_mode(),
        adresse_test=adresse_test_utilisateur(), max_test=L.MAX_ENVOIS_TEST,
        peut_ecrire=has_access("planning_esope", "ecriture"), url_lien=L.url_lien,
        premier_lundi=max(date(a, m, 1), D.lundi_de(date(a, m, 1))).isoformat(),
    )


@esope_bp.route("/envoi/toggle_mode", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def envoi_toggle_mode():
    session["MAIL_MODE"] = "PROD" if _mode() == "TEST" else "TEST"
    write_log(f"ESOPE - changement mode mails → {session['MAIL_MODE']} par {current_user.email}")
    if session["MAIL_MODE"] == "TEST":
        flash(f"🧪 Mode TEST : {L.MAX_ENVOIS_TEST} mails au plus, envoyés à {adresse_test_utilisateur()}.", "warning")
    else:
        flash("🚀 Mode RÉEL : les mails partiront aux bénévoles.", "danger")
    return redirect(url_for("esope.envoi", mois=request.form.get("mois")))


# ─────────────────────────────────────────────────────────────
# Envoi / relance
# ─────────────────────────────────────────────────────────────

def _pdf_planning(mois):
    """PDF du planning complet du mois (même rendu que l'impression)."""
    from weasyprint import HTML
    conn = get_db_connection()
    a, m = D.parse_mois(mois)
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
    conn.close()
    html = render_template("planning/esope/imprimer.html", semaines=semaines, lignes=lignes,
                           titre=L.libelle_mois(mois).capitalize(), imprime_le=date.today(), pour_pdf=True)
    fd, chemin = tempfile.mkstemp(prefix=f"planning_esope_{mois}_", suffix=".pdf")
    os.close(fd)
    HTML(string=html).write_pdf(chemin)
    return chemin


def envoyer_lot_background(app, db_path, mois, type_envoi, items, mail_mode, adresse_test, user_email, pdf_path):
    with app.app_context():
        test = mail_mode == "TEST"
        ok = ko = 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            for item in (items[:L.MAX_ENVOIS_TEST] if test else items):
                maintenant = datetime.now().isoformat(sep=" ", timespec="seconds")
                erreur = ids = None
                try:
                    res = envoyer_mail(
                        f"🧪 [TEST] {item['sujet']}" if test else item["sujet"],
                        [adresse_test] if test else [item["email"]], item["corps"],
                        sender_override=L.EXPEDITEUR, sender_name=L.NOM_EXPEDITEUR, reply_to=L.RESPONSABLE,
                        is_html=True, attachment_path=pdf_path,
                        attachment_filename=f"Planning_ESOPE_{mois}.pdf" if pdf_path else None,
                        current_user_email=user_email)
                    if res and res.get("Messages"):
                        msg = res["Messages"][0]
                        ids = ",".join(str(t["MessageID"]) for t in msg.get("To", []) if "MessageID" in t) or None
                        if msg.get("Status") != "success":
                            erreur = f"Mailjet : {msg.get('Status')}"
                    elif res is None:
                        erreur = "Aucun destinataire valide"
                except Exception as e:
                    erreur = str(e)[:300]
                if type_envoi == "relance":
                    conn.execute("UPDATE esope_liens SET relance_le = ?, nb_relances = COALESCE(nb_relances,0) + 1, "
                                 "envoi_erreur = ?, mailjet_ids = COALESCE(?, mailjet_ids) WHERE id = ?",
                                 (maintenant, erreur, ids, item["lien_id"]))
                else:
                    conn.execute("UPDATE esope_liens SET envoye_le = ?, envoye_par = ?, envoi_mode_test = ?, "
                                 "envoi_erreur = ?, mailjet_ids = ? WHERE id = ?",
                                 (maintenant, user_email, 1 if test else 0, erreur, ids, item["lien_id"]))
                conn.commit()
                ok, ko = (ok + 1, ko) if not erreur else (ok, ko + 1)
        finally:
            conn.close()
            if pdf_path and os.path.exists(pdf_path):
                os.remove(pdf_path)
        write_log(f"📤 ESOPE {type_envoi} {mois} ({mail_mode}) : {ok} envoyé(s), {ko} erreur(s)")


@esope_bp.route("/envoi/envoyer", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def envoi_envoyer():
    mois = _mois_demande()
    type_envoi = request.form.get("type", "preplanning")  # preplanning | relance
    cible = request.form.get("benevole_id", type=int)
    sans_creneau = bool(request.form.get("sans_creneau"))
    mail_mode = _mode()
    conn = get_db_connection()
    lignes = _suivi(conn, mois)
    if cible:
        selection = [l for l in lignes if l["id"] == cible]
    elif type_envoi == "relance":
        selection = [l for l in lignes if l["etat"] in ("envoye", "ouvert") and l["nb_prevus"] > 0]
    else:
        selection = [l for l in lignes if l["etat"] in ("a_envoyer", "erreur")
                     or (l["lien"] and l["lien"]["envoi_mode_test"] and mail_mode != "TEST")]
        if not sans_creneau:
            selection = [l for l in selection if l["nb_actifs"] > 0]
    selection = [l for l in selection if l["email_ok"]]
    if mail_mode == "TEST":
        # les 2 mails de test montrent de préférence des bénévoles qui ont des créneaux
        selection.sort(key=lambda l: l["nb_actifs"] == 0)
    if not selection:
        conn.close()
        flash("ℹ️ Personne à qui envoyer ce mail.", "info")
        return redirect(url_for("esope.envoi", mois=mois))

    code = "esope_relance" if type_envoi == "relance" else "esope_preplanning"
    sujet_m, corps_m = L.modele_mail(conn, code)
    items = []
    for l in selection:
        lien = L.lien_benevole(conn, l["id"], mois)
        creneaux = L.creneaux_benevole(conn, l["id"], mois)
        ctx = L.contexte_mail(l, mois, creneaux, lien["token"])
        items.append({"lien_id": lien["id"], "email": l["email"],
                      "sujet": L.rendre(sujet_m, ctx).strip(), "corps": L.rendre(corps_m, ctx)})
    if mail_mode != "TEST":
        L.changer_statut_periode(conn, mois, "envoye", current_user.email)
    else:
        conn.execute("INSERT OR IGNORE INTO esope_periodes (mois) VALUES (?)", (mois,))
        conn.execute("UPDATE esope_periodes SET envoye_par = COALESCE(envoye_par, ?) WHERE mois = ?",
                     (current_user.email, mois))
    conn.commit()
    conn.close()
    upload_database()

    pdf = None
    if type_envoi != "relance":
        try:
            pdf = _pdf_planning(mois)
        except Exception as e:
            write_log(f"❌ ESOPE PDF planning {mois} : {e}")
            flash("⚠️ Le PDF du planning n'a pas pu être créé : les mails partent sans pièce jointe.", "warning")

    lancer_tache_fond(
        target=envoyer_lot_background,
        args=(current_app._get_current_object(), get_db_path(), mois, type_envoi, items, mail_mode,
              adresse_test_utilisateur(), current_user.email, pdf),
        nom=f"Envoi ESOPE {type_envoi} {mois}", utilisateur=current_user.email)
    write_log(f"📤 ESOPE {type_envoi} {mois} lancé : {len(items)} mail(s), mode {mail_mode}, par {current_user.email}")
    if mail_mode == "TEST":
        flash(f"🧪 Envoi TEST lancé : {min(len(items), L.MAX_ENVOIS_TEST)} mail(s) vers {adresse_test_utilisateur()} "
              f"(sur {len(items)} prévus en réel).", "warning")
    else:
        flash(f"🚀 Envoi lancé en arrière-plan : {len(items)} mail(s). Actualisez la page dans quelques instants.",
              "success")
    return redirect(url_for("esope.envoi", mois=mois))


# ─────────────────────────────────────────────────────────────
# Verrouillage, alertes vues, lien d'un bénévole
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/envoi/statut", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def envoi_statut():
    mois = _mois_demande()
    action = request.form.get("action")
    if action not in ("verrouille", "deverrouille"):
        flash("❌ Action inconnue.", "danger")
        return redirect(url_for("esope.envoi", mois=mois))
    conn = get_db_connection()
    L.changer_statut_periode(conn, mois, action, current_user.email)
    D.journaliser(conn, "verrouillage" if action == "verrouille" else "deverrouillage", commentaire=L.libelle_mois(mois))
    conn.commit()
    conn.close()
    upload_database()
    flash("🔒 Planning verrouillé : les bénévoles ne peuvent plus que signaler une absence." if action == "verrouille"
          else "🔓 Planning déverrouillé : les bénévoles peuvent de nouveau confirmer.", "success")
    return redirect(url_for("esope.envoi", mois=mois))


@esope_bp.route("/alertes/vu", methods=["POST"])
@login_required
@require_access("planning_esope", "ecriture")
def alertes_vu():
    """Marque comme vues une réponse (affectation_id), des disponibilités (lien_id) ou tout le mois."""
    mois = _mois_demande()
    conn = get_db_connection()
    aff_id = request.form.get("affectation_id", type=int)
    lien_id = request.form.get("lien_id", type=int)
    if aff_id:
        conn.execute("UPDATE esope_affectations SET alerte = 0 WHERE id = ?", (aff_id,))
    elif lien_id:
        conn.execute("UPDATE esope_liens SET disponibilites_vu = 1 WHERE id = ?", (lien_id,))
    else:
        a, m = D.parse_mois(mois)
        conn.execute("UPDATE esope_affectations SET alerte = 0 WHERE alerte = 1 AND creneau_id IN "
                     "(SELECT id FROM esope_creneaux WHERE substr(date, 1, 7) = ?)", (f"{a}-{m:02d}",))
        conn.execute("UPDATE esope_liens SET disponibilites_vu = 1 WHERE mois = ?", (mois,))
    conn.commit()
    conn.close()
    retour = request.form.get("retour") or ""
    return redirect(retour if retour.startswith("/planning/esope") else url_for("esope.envoi", mois=mois))


@esope_bp.route("/envoi/ouvrir/<int:benevole_id>")
@login_required
@require_access("planning_esope", "ecriture")
def envoi_ouvrir_lien(benevole_id):
    """Ouvre la page d'un bénévole (pour saisir ses réponses à sa place, ou copier son lien)."""
    mois = _mois_demande()
    conn = get_db_connection()
    lien = L.lien_benevole(conn, benevole_id, mois)
    conn.commit()
    conn.close()
    return redirect(url_for("esope.public_creneaux", token=lien["token"]))


# ─────────────────────────────────────────────────────────────
# Textes des mails
# ─────────────────────────────────────────────────────────────

@esope_bp.route("/textes", methods=["GET", "POST"])
@login_required
@require_access("planning_esope", "ecriture")
def textes():
    conn = get_db_connection()
    if request.method == "POST":
        code = request.form.get("code")
        sujet = (request.form.get("sujet") or "").strip()
        corps = request.form.get("corps") or ""
        if code not in CODES_MAILS or not sujet or not corps.strip():
            flash("❌ Objet et texte obligatoires.", "danger")
        else:
            conn.execute("UPDATE modeles_emails SET sujet = ?, corps = ?, date_modification = datetime('now','localtime') "
                         "WHERE code_modele = ?", (sujet, corps, code))
            conn.commit()
            upload_database()
            flash(f"✅ Texte « {CODES_MAILS[code]} » enregistré.", "success")
        conn.close()
        return redirect(url_for("esope.textes") + f"#{code}")
    modeles = {r["code_modele"]: dict(r) for r in conn.execute(
        "SELECT * FROM modeles_emails WHERE code_modele IN (%s)" % ",".join("?" * len(CODES_MAILS)), list(CODES_MAILS))}
    conn.close()
    return render_template("planning/esope/textes.html", modeles=modeles, codes=CODES_MAILS)
