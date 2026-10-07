"""CERFA abandon de frais bénévoles — envois en arrière-plan
(invitations, relances, documents) et vérification du statut Mailjet.
Même dispositif que Cotisations V2 : mode TEST = 2 mails max vers
l'adresse de test, traçabilité par déclaration."""

import os
import sqlite3
import tempfile
from datetime import datetime

from flask import current_app, flash, redirect, request, session, url_for
from flask_login import current_user, login_required

from ba38_utilitaires.core import (
    adresse_test_utilisateur, envoyer_mail, get_db_connection, get_db_path, mailjet_get_message_status,
    require_access, write_log,
)
from ba38_utilitaires.taches_fond import lancer_tache_fond

from ba38_cerfa import cerfa_bp
from ba38_cerfa.constants import MAX_ENVOIS_TEST
from ba38_cerfa.frais_commun import (
    construire_document, contexte_variables, lien_questionnaire, nom_fichier_document, rendre,
)

TYPES_ENVOI = {
    "invitations": "Invitation",
    "relances": "Relance",
    "documents": "Document (CERFA / remboursement)",
}

SENDER_NAME = "Banque Alimentaire de l'Isère"


def _ids_mailjet(resultat):
    if resultat and resultat.get("Messages"):
        message = resultat["Messages"][0]
        ids = ",".join(str(t["MessageID"]) for t in message.get("To", []) if "MessageID" in t) or None
        return message.get("Status"), ids
    return None, None


def envoyer_cerfa_frais_background(app, db_path, campagne_id, type_envoi, items, mail_mode, mail_test_to,
                                   current_user_email):
    with app.app_context():
        envoyes = erreurs = 0
        maintenant = datetime.now().isoformat(timespec="seconds")
        test = 1 if mail_mode == "TEST" else 0

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        campagne = conn.execute("SELECT * FROM cerfa_frais_campagnes WHERE id = ?", (campagne_id,)).fetchone()

        for item in items[:MAX_ENVOIS_TEST] if test else items:
            sujet = f"🧪 [TEST] {item['sujet']}" if test else item["sujet"]
            destinataires = [mail_test_to] if test else [item["email"]]
            pdf_path = None
            try:
                pieces_jointes = {}
                if type_envoi == "documents":
                    d = conn.execute("SELECT * FROM cerfa_frais_declarations WHERE id = ?",
                                     (item["declaration_id"],)).fetchone()
                    contenu = construire_document(conn, d, campagne)
                    fd, pdf_path = tempfile.mkstemp(suffix=".pdf", prefix="cerfa_frais_")
                    with os.fdopen(fd, "wb") as f:
                        f.write(contenu)
                    pieces_jointes = {"attachment_path": pdf_path, "attachment_filename": nom_fichier_document(d)}

                write_log(f"📧 CERFA frais {type_envoi} → {destinataires} | declaration={item['declaration_id']}")
                resultat = envoyer_mail(
                    sujet=sujet,
                    destinataires=destinataires,
                    texte=item["corps"],
                    sender_override=campagne["expediteur_email"] or None,
                    sender_name=SENDER_NAME,
                    reply_to=campagne["reply_to_email"] or None,
                    current_user_email=current_user_email,
                    **pieces_jointes,
                )
                statut, ids = _ids_mailjet(resultat)
                erreur = None
                envoyes += 1
            except Exception as e:
                write_log(f"❌ CERFA frais erreur envoi {type_envoi} declaration={item['declaration_id']} : {e}")
                statut, ids, erreur = None, None, str(e)
                erreurs += 1
            finally:
                if pdf_path and os.path.exists(pdf_path):
                    os.remove(pdf_path)

            if type_envoi == "invitations":
                conn.execute("""
                    UPDATE cerfa_frais_declarations SET invitation_envoyee_le = ?, invitation_mode_test = ?,
                        invitation_erreur = ?, invitation_mailjet_status = ?, invitation_mailjet_ids = ?,
                        invitation_statut_final = NULL,
                        statut = CASE WHEN statut = 'a_inviter' AND ? = 0 AND ? IS NULL THEN 'invite' ELSE statut END
                    WHERE id = ?
                """, (maintenant, test, erreur, statut, ids, test, erreur, item["declaration_id"]))
            elif type_envoi == "relances":
                conn.execute("""
                    UPDATE cerfa_frais_declarations SET derniere_relance_le = ?, relance_mode_test = ?,
                        relance_erreur = ?,
                        nb_relances = COALESCE(nb_relances, 0) + CASE WHEN ? = 0 AND ? IS NULL THEN 1 ELSE 0 END
                    WHERE id = ?
                """, (maintenant, test, erreur, test, erreur, item["declaration_id"]))
            else:
                # Envoi réel réussi : dossier terminé (CERFA) ou en attente de remboursement (courrier)
                conn.execute("""
                    UPDATE cerfa_frais_declarations SET document_envoye_le = ?, document_mode_test = ?,
                        document_erreur = ?, document_mailjet_status = ?, document_mailjet_ids = ?,
                        document_statut_final = NULL,
                        statut = CASE WHEN statut = 'valide' AND ? = 0 AND ? IS NULL
                                      THEN CASE WHEN imposable = 1 THEN 'termine' ELSE 'courrier_envoye' END
                                      ELSE statut END
                    WHERE id = ?
                """, (maintenant, test, erreur, statut, ids, test, erreur, item["declaration_id"]))
            conn.commit()

        conn.execute("""
            UPDATE cerfa_frais_campagnes SET dernier_envoi_type = ?, dernier_envoi_le = ?, dernier_envoi_par = ?,
                dernier_envoi_mode_test = ?, dernier_envoi_nb_ok = ?, dernier_envoi_nb_erreur = ?
            WHERE id = ?
        """, (type_envoi, maintenant, current_user_email, test, envoyes, erreurs, campagne_id))
        conn.commit()
        conn.close()
        write_log(f"📤 CERFA frais {type_envoi} (background) terminé : {envoyes} envoyé(s), {erreurs} erreur(s)")


def _selection(conn, campagne_id, type_envoi, declaration_id=None, mail_mode="PROD"):
    filtre_id = " AND id = ?" if declaration_id else ""
    params = [campagne_id] + ([declaration_id] if declaration_id else [])
    if type_envoi == "invitations":
        # Jamais envoyées réellement (ou dernier envoi en test / en erreur)
        sql = """SELECT * FROM cerfa_frais_declarations WHERE campagne_id = ? AND email IS NOT NULL
                 AND statut IN ('a_inviter', 'invite')
                 AND (invitation_envoyee_le IS NULL OR invitation_mode_test = 1 OR invitation_erreur IS NOT NULL)"""
        if declaration_id:  # renvoi individuel : toujours possible tant que non transmis
            sql = """SELECT * FROM cerfa_frais_declarations WHERE campagne_id = ? AND email IS NOT NULL
                     AND statut IN ('a_inviter', 'invite', 'en_cours')"""
    elif type_envoi == "relances":
        # En mode TEST, les « à inviter » sont inclus pour pouvoir essayer le mail
        # de relance avant toute invitation réelle (le test ne change aucun statut).
        statuts = "'a_inviter', 'invite', 'en_cours'" if mail_mode == "TEST" else "'invite', 'en_cours'"
        sql = f"""SELECT * FROM cerfa_frais_declarations WHERE campagne_id = ? AND email IS NOT NULL
                  AND statut IN ({statuts})"""
    elif declaration_id:
        # Renvoi individuel : possible à tout moment après validation
        sql = """SELECT * FROM cerfa_frais_declarations WHERE campagne_id = ? AND email IS NOT NULL
                 AND statut IN ('valide', 'termine', 'courrier_envoye', 'rembourse')"""
    else:
        # Envoi groupé : validés pas encore remis (un envoi TEST ou en erreur laisse le statut à « valide »)
        sql = """SELECT * FROM cerfa_frais_declarations WHERE campagne_id = ? AND email IS NOT NULL
                 AND statut = 'valide'"""
    return conn.execute(sql + filtre_id + " ORDER BY nom, prenom", params).fetchall()


@cerfa_bp.route("/frais/<int:campagne_id>/envoyer/<type_envoi>", methods=["POST"])
@login_required
@require_access("cerfa", "ecriture")
def frais_envoyer(campagne_id, type_envoi):
    if type_envoi not in TYPES_ENVOI:
        flash("❌ Type d'envoi inconnu", "danger")
        return redirect(url_for("cerfa.frais_suivi", campagne_id=campagne_id))

    declaration_id = request.form.get("declaration_id", type=int)
    retour = url_for("cerfa.frais_detail", declaration_id=declaration_id) if declaration_id \
        else url_for("cerfa.frais_suivi", campagne_id=campagne_id)

    with get_db_connection() as conn:
        campagne = conn.execute("SELECT * FROM cerfa_frais_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        if not campagne:
            flash("❌ Campagne introuvable", "danger")
            return redirect(url_for("cerfa.frais_campagnes"))
        mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
        declarations = _selection(conn, campagne_id, type_envoi, declaration_id, mail_mode)

    if not declarations:
        flash("ℹ️ Aucun destinataire concerné par cet envoi.", "info")
        return redirect(retour)

    items = []
    for d in declarations:
        contexte = contexte_variables(d, campagne, lien_questionnaire(d))
        if type_envoi == "invitations":
            sujet, corps = campagne["mail_invitation_sujet"], campagne["mail_invitation_corps"]
        elif type_envoi == "relances":
            sujet, corps = campagne["mail_relance_sujet"], campagne["mail_relance_corps"]
        elif d["imposable"] == 1:
            sujet, corps = campagne["mail_cerfa_sujet"], campagne["mail_cerfa_corps"]
        else:
            sujet, corps = campagne["mail_remboursement_sujet"], campagne["mail_remboursement_corps"]
        items.append({
            "declaration_id": d["id"],
            "email": d["email"],
            "sujet": rendre(sujet, contexte).strip(),
            "corps": rendre(corps, contexte),
        })

    lancer_tache_fond(
        target=envoyer_cerfa_frais_background,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, type_envoi, items, mail_mode,
              adresse_test_utilisateur(), current_user.email),
        nom=f"Envoi CERFA frais ({TYPES_ENVOI[type_envoi]})",
        utilisateur=current_user.email,
    )
    write_log(f"📤 CERFA frais : envoi {type_envoi} lancé ({len(items)} item(s), mode {mail_mode}) "
              f"par {current_user.email}")

    if mail_mode == "TEST":
        flash(f"🧪 Envoi TEST lancé en arrière-plan ({MAX_ENVOIS_TEST} mails max vers {adresse_test_utilisateur()}).",
              "warning")
    else:
        flash(f"🚀 Envoi réel lancé en arrière-plan : {len(items)} mail(s) ({TYPES_ENVOI[type_envoi]}). "
              "Actualisez la page dans quelques instants.", "info")
    return redirect(retour)


@cerfa_bp.route("/frais/<int:campagne_id>/verifier_mailjet", methods=["POST"])
@login_required
@require_access("cerfa", "ecriture")
def frais_verifier_mailjet(campagne_id):
    nb = 0
    with get_db_connection() as conn:
        lignes = conn.execute("""
            SELECT id, invitation_mailjet_ids, invitation_mode_test, document_mailjet_ids, document_mode_test
            FROM cerfa_frais_declarations WHERE campagne_id = ?
              AND (invitation_mailjet_ids IS NOT NULL OR document_mailjet_ids IS NOT NULL)
        """, (campagne_id,)).fetchall()
        maintenant = datetime.now().isoformat(timespec="seconds")
        for l in lignes:
            for prefixe in ("invitation", "document"):
                ids = l[f"{prefixe}_mailjet_ids"]
                if not ids or l[f"{prefixe}_mode_test"]:
                    continue
                statut = mailjet_get_message_status(ids.split(",")[0])
                if statut:
                    conn.execute(f"UPDATE cerfa_frais_declarations SET {prefixe}_statut_final = ? WHERE id = ?",
                                 (statut, l["id"]))
                    nb += 1
        conn.commit()
    write_log(f"🔄 CERFA frais : {nb} statut(s) Mailjet actualisé(s) le {maintenant}")
    flash(f"🔄 {nb} statut(s) Mailjet actualisé(s)", "success")
    return redirect(url_for("cerfa.frais_suivi", campagne_id=campagne_id))


@cerfa_bp.route("/frais/toggle_mode", methods=["POST"])
@login_required
@require_access("cerfa", "ecriture")
def frais_toggle_mode():
    """Bascule TEST/PROD de la session (même clé MAIL_MODE que les autres envois)."""
    courant = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
    session["MAIL_MODE"] = "PROD" if courant == "TEST" else "TEST"
    write_log(f"CERFA frais - changement mode → {session['MAIL_MODE']} par {current_user.email}")
    if session["MAIL_MODE"] == "TEST":
        flash("🧪 Mode TEST activé (mails redirigés vers vous, 2 max)", "warning")
    else:
        flash("🚀 Mode PROD activé : les mails partiront réellement aux bénévoles", "danger")
    return redirect(request.referrer or url_for("cerfa.frais_campagnes"))
