# Page publique d'inscription (lien personnel, sans connexion)
import sqlite3

from flask import abort, render_template, request

from ba38_billets import attribution, billets_bp, mails
from ba38_utilitaires.core import get_db_path, upload_database, write_log


def _connect():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    mails.ensure_schema(conn)
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
        premier = mails.est_premier(c)
        annulee = c["statut"] == "annulee"
        clos = c["statut"] in ("tiree", "annulee") or mails.limite_depassee(c)
        peut_inscrire = premier or p["categorie"] == "benevole"
        gain = mails.gains_recents(conn, c["id"]).get(p["benevole_id"]) if peut_inscrire else None
        mail_a_envoyer = None
        promus = []
        if request.method == "POST" and peut_inscrire and annulee:
            message = ("danger", "Le spectacle est annulé : plus aucune action n'est possible.")
        elif request.method == "POST" and peut_inscrire:
            action = request.form.get("action")
            if clos and not (premier and action == "desister"):
                message = ("danger", "Les inscriptions sont closes.")
            elif gain and action == "inscrire":
                message = ("danger", "Vous avez déjà gagné une place récemment : une personne ne peut gagner "
                                     "qu'une place par période de 12 mois.")
            elif action == "inscrire" and p["etat"] != "inscrit":
                if premier:
                    res = attribution.inscrire(conn, c, p)
                    conn.commit()
                    message = (("success", "Votre inscription est enregistrée : une place vous est réservée ! "
                                           "Un mail de confirmation vous est envoyé.") if res == "gagnant" else
                               ("warning", "Toutes les places sont déjà prises : vous êtes en liste d'attente. "
                                           "Si une place se libère, elle vous sera attribuée et vous serez prévenu(e) par mail."))
                else:
                    conn.execute("UPDATE billets_participants SET etat='inscrit', inscrit_le=?, inscrit_par=NULL WHERE id=?",
                                 (mails.maintenant(), p["id"]))
                    conn.commit()
                    message = ("success", "Votre inscription est enregistrée. Un mail de confirmation vous est envoyé.")
                mail_a_envoyer = mails.mail_confirmation_inscription
            elif action == "desister" and p["etat"] == "inscrit":
                if premier:
                    promus = attribution.desinscrire(conn, c, p["id"])
                else:
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
        if promus:
            attribution.prevenir_promus(conn, c, promus)
        rang = attribution.rang_attente(conn, p) if premier and p["resultat"] == "attente" else None
        if mail_a_envoyer or promus:
            upload_database()
    return render_template("billets/inscription.html", c=c, p=p, clos=clos, gain=gain, message=message,
                           premier=premier, annulee=annulee, rang=rang, peut_inscrire=peut_inscrire,
                           date_fr=mails.date_fr, limite_fr=mails.limite_fr)
