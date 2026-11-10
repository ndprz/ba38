# Page publique d'inscription (lien personnel, sans connexion)
import sqlite3

from flask import abort, render_template, request

from ba38_billets import billets_bp, mails
from ba38_utilitaires.core import get_db_path, upload_database, write_log


def _connect():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


@billets_bp.route("/inscription/<token>", methods=["GET", "POST"])
def inscription(token):
    if not token or len(token) > 64:
        abort(404)
    message = None
    with _connect() as conn:
        p = conn.execute("SELECT * FROM billets_participants WHERE token = ?", (token,)).fetchone()
        if p is None:
            abort(404)
        c = conn.execute("SELECT * FROM billets_campagnes WHERE id = ?", (p["campagne_id"],)).fetchone()
        clos = c["statut"] == "tiree" or mails.limite_depassee(c)
        mail_a_envoyer = None
        if request.method == "POST" and p["categorie"] == "benevole":
            action = request.form.get("action")
            if clos:
                message = ("danger", "Les inscriptions sont closes.")
            elif action == "inscrire" and p["etat"] != "inscrit":
                conn.execute("UPDATE billets_participants SET etat='inscrit', inscrit_le=?, inscrit_par=NULL WHERE id=?",
                             (mails.maintenant(), p["id"]))
                conn.commit()
                message = ("success", "Votre inscription est enregistrée. Un mail de confirmation vous est envoyé.")
                mail_a_envoyer = mails.mail_confirmation_inscription
            elif action == "desister" and p["etat"] == "inscrit":
                conn.execute("UPDATE billets_participants SET etat='desiste' WHERE id=?", (p["id"],))
                conn.commit()
                message = ("info", "Votre désistement est enregistré.")
                mail_a_envoyer = mails.mail_desistement
            p = conn.execute("SELECT * FROM billets_participants WHERE id = ?", (p["id"],)).fetchone()
        if mail_a_envoyer:
            try:
                sujet, corps = mail_a_envoyer(c, p)
                mails.envoyer(c, p["email"], sujet, corps)
                if mail_a_envoyer is mails.mail_confirmation_inscription:
                    conn.execute("UPDATE billets_participants SET confirmation_le=? WHERE id=?",
                                 (mails.maintenant(), p["id"]))
                    conn.commit()
            except Exception as e:
                write_log(f"❌ Billets confirmation → {p['email']} : {e}")
        if mail_a_envoyer:
            upload_database()
    return render_template("billets/inscription.html", c=c, p=p, clos=clos, message=message,
                           date_fr=mails.date_fr, limite_fr=mails.limite_fr)
