# ============================================================
# 🔔 Relances des factures cuisine impayées
#     Sur le modèle de ba38_tresorerie/cotisations_v2_relance.py : relance
#     de niveau N envoyée aux factures émises, réellement envoyées, non
#     payées et déjà relancées N-1 fois ; modèles de mail
#     "CUISINE Relance 1/2/3" (variables {xxx}) ; facture régénérée et
#     jointe ; mode TEST (2 mails max vers l'adresse de test).
# ============================================================

import os
import sqlite3
from datetime import datetime

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ba38_utilitaires.core import (
    envoyer_mail, get_db_path, mailjet_get_message_status, split_emails, upload_database, write_log,
)
from ba38_utilitaires.taches_fond import lancer_tache_fond
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.routes_facturation import (
    MAIL_SENDER, date_fr, euros, mail_mode_courant, pdf_facture_vers_fichier, periode_label,
    require_access_facturation,
)
from ba38_cuisine.utils import _connect

NIVEAUX_RELANCE = 3


def _impayees(conn, campagne_id):
    """Factures émises, réellement envoyées, non soldées (reste_du = montant
    moins les règlements partiels). Email de secours :
    l'adresse actuelle de l'association si celle figée à la génération est vide."""
    lignes = []
    for row in conn.execute(
        """SELECT f.*, a.courriel_resp_tresorerie AS _assoc_tresorerie, a.courriel_association AS _assoc_association,
                  ROUND(f.montant - (SELECT COALESCE(SUM(r.montant), 0) FROM cuisine_factures_reglements r
                                     WHERE r.facture_id = f.id), 2) AS reste_du
           FROM cuisine_factures f LEFT JOIN associations a ON a.id = f.association_id
           WHERE f.campagne_id = ? AND f.statut = 'emise' AND f.date_paiement IS NULL
             AND f.mail_envoye_le IS NOT NULL AND f.mail_mode_test = 0 AND f.mail_erreur IS NULL
           ORDER BY f.numero""",
        (campagne_id,),
    ).fetchall():
        d = dict(row)
        secours = d.pop("_assoc_tresorerie", None) or d.pop("_assoc_association", None)
        if not d.get("email"):
            d["email"] = secours
        lignes.append(d)
    return lignes


def envoyer_relances_cuisine_background(app, db_path, items, sujet_modele, corps_modele, numero_relance,
                                        periode, mail_mode, mail_test_to, current_user_email):
    with app.app_context():
        nb_mails, nb_erreurs = 0, 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for item in (items[:2] if mail_mode == "TEST" else items):
            pdf_path = f"/tmp/facture_cuisine_relance_{item['facture_id']}.pdf"
            try:
                valeurs = dict(numero_relance=numero_relance + 1, numero=item["numero"], periode=periode,
                               montant=euros(item["reste_du"]), montant_facture=euros(item["montant"]),
                               echeance=date_fr(item["date_echeance"]),
                               nom_association=item["nom_association"])
                sujet = sujet_modele.format(**valeurs)
                texte = corps_modele.format(**valeurs)
                if mail_mode == "TEST":
                    destinataires = [mail_test_to]
                    sujet = f"🧪 [TEST] {sujet}"
                else:
                    destinataires = split_emails(item["email"])
                    if not destinataires:
                        raise ValueError(f"Aucune adresse email valide pour {item['nom_association']}")
                pdf_facture_vers_fichier(conn, item["facture_id"], pdf_path)
                resultat = envoyer_mail(
                    sujet=sujet, destinataires=destinataires, texte=texte, sender_override=MAIL_SENDER,
                    attachment_path=pdf_path, bcc=[MAIL_SENDER], current_user_email=current_user_email,
                )
                mj_status, mj_ids = None, None
                if resultat and resultat.get("Messages"):
                    mj_message = resultat["Messages"][0]
                    mj_status = mj_message.get("Status")
                    mj_ids = ",".join(str(t["MessageID"]) for t in mj_message.get("To", []) if "MessageID" in t) or None
                # Le niveau n'augmente qu'en envoi réel : une relance TEST
                # peut être rejouée pour de vrai ensuite.
                conn.execute(
                    """UPDATE cuisine_factures
                       SET relance_niveau = COALESCE(relance_niveau, 0) + ?, date_derniere_relance = ?,
                           mode_test_relance = ?, relance_sujet = ?, relance_corps = ?, relance_mail_erreur = NULL,
                           relance_mailjet_status = ?, relance_mailjet_message_ids = ?,
                           relance_statut_final = NULL, relance_statut_verifie_le = NULL,
                           email = COALESCE(NULLIF(email, ''), ?)
                       WHERE id = ?""",
                    (0 if mail_mode == "TEST" else 1, datetime.now().isoformat(timespec="seconds"),
                     1 if mail_mode == "TEST" else 0, sujet, texte, mj_status, mj_ids, item["email"],
                     item["facture_id"]),
                )
                conn.commit()
                nb_mails += 1
            except Exception as e:
                write_log(f"❌ Erreur relance facture cuisine {item['numero']} : {e}")
                nb_erreurs += 1
                conn.execute("UPDATE cuisine_factures SET relance_mail_erreur = ? WHERE id = ?", (str(e), item["facture_id"]))
                conn.commit()
            finally:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
        conn.close()
        upload_database()
        write_log(f"📤 Relances factures cuisine {periode} terminées : {nb_mails} envoyée(s), {nb_erreurs} erreur(s)")


@production_cuisine_bp.route("/facturation/<int:campagne_id>/relance", methods=["GET", "POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_relance(campagne_id):
    try:
        numero_relance = int(request.values.get("numero_relance", 0))
    except ValueError:
        numero_relance = 0
    numero_relance = max(0, min(numero_relance, NIVEAUX_RELANCE - 1))
    mail_mode = mail_mode_courant()
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")

    with _connect() as conn:
        campagne = conn.execute("SELECT * FROM cuisine_factures_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        if not campagne:
            flash("❌ Mois de facturation introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        impayees = _impayees(conn, campagne_id)
        code_modele = f"CUISINE Relance {numero_relance + 1}"
        modele = conn.execute("SELECT sujet, corps FROM modeles_emails WHERE code_modele = ? LIMIT 1",
                              (code_modele,)).fetchone()
    periode = periode_label(campagne["annee"], campagne["mois"])
    a_relancer = [l for l in impayees if (l["relance_niveau"] or 0) == numero_relance]
    ctx = dict(campagne=campagne, periode=periode, impayees=impayees, a_relancer=a_relancer,
               numero_relance=numero_relance, niveaux=NIVEAUX_RELANCE, code_modele=code_modele,
               modele=modele, mail_mode=mail_mode, mail_test_to=mail_test_to, euros=euros, date_fr=date_fr,
               total=sum(l["reste_du"] or 0 for l in a_relancer),
               nb_par_niveau=[sum(1 for l in impayees if (l["relance_niveau"] or 0) == n) for n in range(NIVEAUX_RELANCE)])

    if request.method == "GET":
        return render_template("production_cuisine/facturation_relance.html", **ctx)

    retour = redirect(url_for("production_cuisine.facturation_relance", campagne_id=campagne_id,
                              numero_relance=numero_relance))
    if not modele:
        flash(f"❌ Modèle de mail « {code_modele} » introuvable.", "danger")
        return retour
    if mail_mode == "PROD" and not request.form.get("confirm_production"):
        flash("⚠️ Cochez la confirmation pour envoyer réellement en PRODUCTION.", "danger")
        return retour
    items = [
        {"facture_id": l["id"], "numero": l["numero"], "nom_association": l["nom_association"],
         "montant": l["montant"], "reste_du": l["reste_du"], "date_echeance": l["date_echeance"], "email": l["email"]}
        for l in a_relancer if l["email"]
    ]
    if not items:
        flash("ℹ️ Aucune facture à relancer à ce niveau (avec une adresse email).", "warning")
        return retour
    current_user_email = current_user.email
    lancer_tache_fond(
        target=envoyer_relances_cuisine_background,
        args=(current_app._get_current_object(), get_db_path(), items, modele["sujet"], modele["corps"],
              numero_relance, periode, mail_mode, mail_test_to, current_user_email),
        nom="Relances factures cuisine",
        utilisateur=current_user_email,
    )
    if mail_mode == "TEST":
        flash("🧪 Relances TEST lancées en arrière-plan (2 mails max vers l'adresse de test).", "warning")
    else:
        flash(f"🚀 Relance {numero_relance + 1} lancée en arrière-plan pour {len(items)} facture(s).", "info")
    return retour


@production_cuisine_bp.route("/facturation/<int:campagne_id>/relance/verifier_statut_mailjet", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_relance_verifier_statut_mailjet(campagne_id):
    counts, verifies = {}, 0
    with _connect() as conn:
        for ligne in conn.execute(
            """SELECT id, relance_mailjet_message_ids FROM cuisine_factures
               WHERE campagne_id = ? AND relance_mailjet_message_ids IS NOT NULL AND relance_mailjet_message_ids != ''""",
            (campagne_id,),
        ).fetchall():
            statut = mailjet_get_message_status(ligne["relance_mailjet_message_ids"].split(",")[0])
            if not statut:
                continue
            verifies += 1
            counts[statut] = counts.get(statut, 0) + 1
            conn.execute("UPDATE cuisine_factures SET relance_statut_final = ?, relance_statut_verifie_le = ? WHERE id = ?",
                         (statut, datetime.now().isoformat(timespec="seconds"), ligne["id"]))
        conn.commit()
    if verifies:
        upload_database()
        flash(f"🔄 Statut Mailjet des relances vérifié pour {verifies} mail(s) : "
              + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())), "info")
    else:
        flash("ℹ️ Aucune relance avec un identifiant Mailjet à vérifier pour ce mois.", "warning")
    return redirect(url_for("production_cuisine.facturation_relance", campagne_id=campagne_id,
                            numero_relance=request.form.get("numero_relance", 0)))


@production_cuisine_bp.route("/facturation/facture/<int:facture_id>/relance/renvoyer_gmail", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_relance_renvoyer_gmail(facture_id):
    from ba38_utilitaires.gmail_send import envoyer_mail_gmail, GmailSendError

    with _connect() as conn:
        f = conn.execute("SELECT * FROM cuisine_factures WHERE id = ?", (facture_id,)).fetchone()
        if not f:
            flash("❌ Facture introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        retour = redirect(url_for("production_cuisine.facturation_relance", campagne_id=f["campagne_id"],
                                  numero_relance=request.form.get("numero_relance", 0)))
        destinataires = split_emails(f["email"])
        if not destinataires or not f["relance_sujet"] or not f["relance_corps"]:
            flash(f"⛔ Pas de relance précédente (ou pas d'email) pour {f['nom_association']}.", "danger")
            return retour
        if f["mode_test_relance"]:
            flash(f"⛔ La dernière relance pour {f['nom_association']} était en Mode TEST — un renvoi Gmail "
                  "partirait, lui, pour de vrai. Refaites d'abord une relance réelle.", "danger")
            return retour
        pdf_path = f"/tmp/facture_cuisine_relance_gmail_{facture_id}.pdf"
        pdf_facture_vers_fichier(conn, facture_id, pdf_path)
    try:
        envoyer_mail_gmail(sujet=f["relance_sujet"], destinataires=destinataires, texte=f["relance_corps"],
                           attachment_path=pdf_path)
        with _connect() as conn:
            conn.execute("UPDATE cuisine_factures SET relance_renvoi_gmail_le = ? WHERE id = ?",
                         (datetime.now().isoformat(timespec="seconds"), facture_id))
            conn.commit()
        upload_database()
        flash(f"📧 Relance de la facture {f['numero']} renvoyée via Gmail à {f['email']}.", "success")
    except GmailSendError as e:
        write_log(f"❌ Erreur renvoi Gmail relance facture cuisine {f['numero']} : {e}")
        flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
    return retour
