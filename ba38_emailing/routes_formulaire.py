# ============================================================
# 📝 Formulaire de réponse d'une campagne d'emailing
#   - questions simples (oui/non, texte court), éditées dans la campagne ;
#   - page publique par lien personnel (jeton) : une réponse par adresse,
#     modifiable ; - suivi (« répondu le »), export Excel, relance des
#     non-répondants.
# ============================================================

import io
import sqlite3
from datetime import datetime

from flask import (
    abort, current_app, flash, redirect, render_template, request, send_file, url_for,
)
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Font

from ba38_emailing import emailing_bp
from ba38_emailing.routes import (
    STATUTS_ECHEC, _connect, acces_emailing, assurer_tokens, rendre_message,
)
from ba38_utilitaires.core import (
    adresse_test_utilisateur, envoyer_mail, get_db_path, upload_database, write_log,
)
from ba38_utilitaires.taches_fond import lancer_tache_fond

TYPES_QUESTION = {"oui_non": "Oui / Non", "texte": "Texte court"}


def _maintenant():
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------
# Édition des questions (campagne)
# ------------------------------------------------------------
@emailing_bp.route("/<int:campagne_id>/formulaire", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def enregistrer_formulaire(campagne_id):
    retour = redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#formulaire")
    f = request.form
    actif = 1 if f.get("form_actif") == "1" else 0
    ids = f.getlist("q_id")
    libelles = f.getlist("q_libelle")
    types = f.getlist("q_type")
    obl = f.getlist("q_obl")
    questions = []
    for i, lib in enumerate(libelles):
        lib = (lib or "").strip()
        if not lib:
            continue
        t = types[i] if i < len(types) and types[i] in TYPES_QUESTION else "oui_non"
        questions.append((ids[i] if i < len(ids) and ids[i].isdigit() else None, lib[:300], t,
                          1 if i < len(obl) and obl[i] == "1" else 0))
    if actif and not questions:
        flash("⚠️ Ajoutez au moins une question pour activer le formulaire.", "warning")
        return retour
    with _connect() as conn:
        conn.execute(
            "UPDATE emailing_campagnes SET form_actif=?, form_titre=?, form_intro=?, form_merci=? WHERE id=?",
            (actif, (f.get("form_titre") or "").strip() or None, (f.get("form_intro") or "").strip() or None,
             (f.get("form_merci") or "").strip() or None, campagne_id))
        existants = {r["id"] for r in conn.execute(
            "SELECT id FROM emailing_questions WHERE campagne_id = ?", (campagne_id,))}
        gardes = set()
        for ordre, (qid, lib, t, o) in enumerate(questions):
            if qid and int(qid) in existants:
                conn.execute("UPDATE emailing_questions SET ordre=?, libelle=?, type=?, obligatoire=? WHERE id=?",
                             (ordre, lib, t, o, int(qid)))
                gardes.add(int(qid))
            else:
                conn.execute(
                    "INSERT INTO emailing_questions (campagne_id, ordre, libelle, type, obligatoire) VALUES (?,?,?,?,?)",
                    (campagne_id, ordre, lib, t, o))
        for qid in existants - gardes:
            conn.execute("DELETE FROM emailing_reponses WHERE question_id = ?", (qid,))
            conn.execute("DELETE FROM emailing_questions WHERE id = ?", (qid,))
        conn.commit()
    flash("✅ Formulaire enregistré. Écrivez <<lien_formulaire>> dans le texte du mail pour insérer le lien personnel.",
          "success")
    return retour


# ------------------------------------------------------------
# Page publique de réponse (lien personnel, sans connexion)
# ------------------------------------------------------------
def _charger_public(conn, token):
    if not token or len(token) > 64:
        abort(404)
    d = conn.execute("SELECT * FROM emailing_destinataires WHERE token = ?", (token,)).fetchone()
    if d is None:
        abort(404)
    c = conn.execute("SELECT * FROM emailing_campagnes WHERE id = ?", (d["campagne_id"],)).fetchone()
    if c is None or not c["form_actif"]:
        abort(404)
    questions = conn.execute(
        "SELECT * FROM emailing_questions WHERE campagne_id = ? ORDER BY ordre, id", (c["id"],)).fetchall()
    return d, c, questions


@emailing_bp.route("/repondre/<token>", methods=["GET", "POST"])
def repondre(token):
    with _connect() as conn:
        d, c, questions = _charger_public(conn, token)
        reponses = {r["question_id"]: r["valeur"] for r in conn.execute(
            "SELECT question_id, valeur FROM emailing_reponses WHERE destinataire_id = ?", (d["id"],))}
        erreurs = []
        if request.method == "POST":
            saisies = {}
            for q in questions:
                v = (request.form.get(f"q_{q['id']}") or "").strip()
                if q["type"] == "oui_non":
                    v = v if v in ("oui", "non") else ""
                else:
                    v = v[:300]
                saisies[q["id"]] = v
                if q["obligatoire"] and not v:
                    erreurs.append(q["libelle"])
            reponses = {**reponses, **saisies}
            if not erreurs:
                now = _maintenant()
                for qid, v in saisies.items():
                    conn.execute(
                        """INSERT INTO emailing_reponses (destinataire_id, question_id, valeur, maj_le)
                           VALUES (?,?,?,?)
                           ON CONFLICT(destinataire_id, question_id) DO UPDATE SET valeur=excluded.valeur, maj_le=excluded.maj_le""",
                        (d["id"], qid, v, now))
                conn.execute("UPDATE emailing_destinataires SET repondu_le = ? WHERE id = ?", (now, d["id"]))
                conn.commit()
                write_log(f"📝 Emailing campagne {c['id']} : réponse de {d['email']}")
                return redirect(url_for("emailing.repondre", token=token, ok=1))
    return render_template("emailing/repondre.html", d=d, c=c, questions=questions, reponses=reponses,
                           erreurs=erreurs, ok=request.args.get("ok") == "1")


@emailing_bp.errorhandler(404)
def _lien_invalide(e):
    if request.endpoint == "emailing.repondre":
        return render_template("emailing/repondre_invalide.html"), 404
    return e


# ------------------------------------------------------------
# Export des réponses
# ------------------------------------------------------------
@emailing_bp.route("/<int:campagne_id>/reponses.xlsx")
@login_required
@acces_emailing("lecture")
def export_reponses(campagne_id):
    with _connect() as conn:
        c = conn.execute("SELECT * FROM emailing_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        if not c:
            abort(404)
        questions = conn.execute(
            "SELECT * FROM emailing_questions WHERE campagne_id = ? ORDER BY ordre, id", (campagne_id,)).fetchall()
        dests = conn.execute(
            """SELECT * FROM emailing_destinataires WHERE campagne_id = ?
               AND (mail_envoye_le IS NOT NULL OR repondu_le IS NOT NULL)
               ORDER BY nom_association COLLATE NOCASE, email""", (campagne_id,)).fetchall()
        rep = {(r["destinataire_id"], r["question_id"]): r["valeur"] for r in conn.execute(
            "SELECT r.* FROM emailing_reponses r JOIN emailing_destinataires d ON d.id = r.destinataire_id "
            "WHERE d.campagne_id = ?", (campagne_id,))}
    wb = Workbook()
    ws = wb.active
    ws.title = "Réponses"
    ws.append(["Association", "Email", "Fonction(s)", "Envoyé le", "Répondu le"] + [q["libelle"] for q in questions])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for d in dests:
        ws.append([d["nom_association"], d["email"], d["fonctions"],
                   (d["mail_envoye_le"] or "")[:16].replace("T", " "),
                   (d["repondu_le"] or "")[:16].replace("T", " ")]
                  + [{"oui": "Oui", "non": "Non"}.get(rep.get((d["id"], q["id"])), rep.get((d["id"], q["id"])) or "")
                     for q in questions])
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(45, max(12, max(len(str(c.value or "")) for c in col) + 2))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"emailing_{campagne_id}_reponses.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------
# Relance des non-répondants
# ------------------------------------------------------------
def envoyer_relance_background(app, db_path, campagne_id, ids, mode_test, user_email):
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.execute("SELECT * FROM emailing_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        assurer_tokens(conn, campagne_id)
        ok = ko = nb_test = 0
        for did in ids:
            d = conn.execute("SELECT * FROM emailing_destinataires WHERE id = ?", (did,)).fetchone()
            if not d:
                continue
            if mode_test:
                if nb_test >= 2:
                    break
                nb_test += 1
            sujet, corps = rendre_message(c, d)
            try:
                envoyer_mail(
                    sujet=f"Relance : {sujet}", destinataires=[d["email"]], texte=corps,
                    sender_override=c["expediteur"], sender_name=c["expediteur_nom"],
                    reply_to=c["reply_to"], is_html=bool(c["is_html"]),
                    mode_test=bool(mode_test), current_user_email=user_email)
                if not mode_test:
                    conn.execute(
                        "UPDATE emailing_destinataires SET relance_le=?, nb_relances=COALESCE(nb_relances,0)+1, "
                        "relance_erreur=NULL WHERE id=?", (_maintenant(), did))
                ok += 1
            except Exception as e:
                write_log(f"❌ Emailing relance campagne {campagne_id} → {d['email']} : {e}")
                conn.execute("UPDATE emailing_destinataires SET relance_erreur=? WHERE id=?", (str(e)[:500], did))
                ko += 1
            conn.commit()
        conn.close()
        upload_database()
        write_log(f"📤 Emailing campagne {campagne_id} — relance : {ok} envoyé(s), {ko} erreur(s)")


@emailing_bp.route("/<int:campagne_id>/relancer", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def relancer(campagne_id):
    retour = redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#suivi")
    mode_test = request.form.get("mode_test") == "1"
    with _connect() as conn:
        c = conn.execute("SELECT * FROM emailing_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        if not c or not c["form_actif"]:
            flash("❌ Aucun formulaire actif sur cette campagne.", "danger")
            return retour
        ids = [r["id"] for r in conn.execute(
            """SELECT id, mail_statut_final FROM emailing_destinataires
               WHERE campagne_id = ? AND selected = 1 AND repondu_le IS NULL
                 AND mail_envoye_le IS NOT NULL AND mail_mode_test = 0 AND mail_erreur IS NULL
               ORDER BY nom_association COLLATE NOCASE, email""", (campagne_id,))
            if r["mail_statut_final"] not in STATUTS_ECHEC]
    if not ids:
        flash("ℹ️ Aucun non-répondant à relancer (envoyés avec succès, cochés, sans réponse).", "warning")
        return retour
    lancer_tache_fond(
        target=envoyer_relance_background,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, ids, mode_test, current_user.email),
        nom="Emailing associations — relance", utilisateur=current_user.email)
    flash("🧪 Relance TEST lancée (2 mails max vers vous)." if mode_test
          else f"🔔 Relance lancée en arrière-plan pour {len(ids)} non-répondant(s).",
          "warning" if mode_test else "info")
    return retour
