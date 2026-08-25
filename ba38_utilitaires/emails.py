from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session
from flask_login import login_required, current_user
import sqlite3
import os
from datetime import datetime
from functools import wraps
from threading import Thread
from ba38_utilitaires.core import get_db_path, require_access, has_access, write_log, envoyer_mail, render_modele_email, get_templates_pdf_dir, copier_modele_email_vers_prod
from ba38_utilitaires.pdf_form import remplir_pdf_indicateurs

emails_bp = Blueprint("emails", __name__)


def require_access_modeles(niveau):
    """
    Les modèles de mail (modeles_emails) sont partagés par plusieurs modules
    (indicateurs, factures participation) — autorise l'accès si l'utilisateur
    a le droit soit sur "indicateurs" soit sur "tresorerie", pour ne pas
    bloquer les utilisateurs trésorerie qui n'ont pas accès aux indicateurs.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not (has_access("indicateurs", niveau) or has_access("tresorerie", niveau)):
                write_log(f"⛔ Accès refusé (modeles_emails) : user={session.get('user_email')}")
                flash("⛔ Accès refusé", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ============================================================================
# 📧 ENVOI DES MAILS INDICATEURS — TRAITEMENT ARRIÈRE-PLAN
# ============================================================================
# Tourne dans un Thread séparé (même pattern que ba38_tresorerie.py pour les
# factures) : une campagne complète (100+ associations x 2 mails Mailjet)
# dépasse largement le délai nginx/gunicorn si elle reste dans le cycle
# requête/réponse HTTP. Voir mémoire "Envoi indicateurs traçabilité".
def envoyer_indicateurs_background(app, db_path, campagne_id, campagne, lignes,
                                    modele, sender, mode_test_local, type_periode,
                                    periode, current_user_email):
    with app.app_context():
        envoyes = 0
        nb_erreurs = 0
        now_iso = datetime.now().isoformat(timespec="seconds")

        conn_suivi = sqlite3.connect(db_path)

        for i, l in enumerate(lignes):

            association = dict(l)

            # 📄 génération PDF
            pdf_path = f"/tmp/indicateurs_{association['association_id']}.pdf"

            if type_periode == "annuel":
                template = os.path.join(get_templates_pdf_dir(), "indicateurs_annuels.pdf")
            else:
                template = os.path.join(get_templates_pdf_dir(), "indicateurs_trimestriels.pdf")

            remplir_pdf_indicateurs(template, pdf_path, association, campagne)

            # ============================================================================
            # 📧 CONTEXTE MAIL
            # ============================================================================
            date_limite = campagne["date_limite"]

            if date_limite:
                try:
                    date_limite = datetime.strptime(date_limite, "%Y-%m-%d").strftime("%d/%m/%Y")
                except Exception:
                    pass

            contexte = {
                "nom_association": association["nom_association"],
                "periode": periode,
                "trimestre": periode.split()[0],
                "annee": periode.split()[1],
                "date_limite": date_limite,
            }

            write_log(f"CONTEXTE: {contexte}")

            sujet = render_modele_email(modele["sujet"], contexte).strip()
            corps = render_modele_email(modele["corps"], contexte)

            # ============================================================================
            # 📬 DESTINATAIRES
            # ============================================================================
            emails = []

            if association["courriel_resp_IE1"]:
                emails.append(association["courriel_resp_IE1"])

            if association["courriel_resp_IE2"]:
                emails.append(association["courriel_resp_IE2"])

            emails = list(set(emails))

            # ============================================================================
            # 🧪 MODE TEST
            # ============================================================================
            if mode_test_local:
                emails = ["ba380.informatique2@banquealimentaire.org"]
                sujet = f"🧪 [TEST] {sujet}"

                if i >= 2:
                    break

            # ============================================================================
            # 📤 ENVOI
            # ============================================================================
            try:
                write_log(f"📧 Envoi à {emails} | {association['nom_association']}")

                resultat = envoyer_mail(
                    sujet=sujet,
                    destinataires=emails,
                    texte=corps,
                    sender_override=sender,
                    attachment_path=pdf_path
                )

                mj_status, mj_ids = None, None
                if resultat and resultat.get("Messages"):
                    mj_message = resultat["Messages"][0]
                    mj_status = mj_message.get("Status")
                    mj_ids = ",".join(
                        str(t["MessageID"]) for t in mj_message.get("To", []) if "MessageID" in t
                    ) or None

                conn_suivi.execute("""
                    UPDATE indicateurs_suivi
                    SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = NULL,
                        mail_mailjet_status = ?, mail_mailjet_message_ids = ?, mail_modele_id = ?
                    WHERE id = ?
                """, (now_iso, 1 if mode_test_local else 0, mj_status, mj_ids, modele["id"], l["id"]))
                conn_suivi.commit()

                envoyes += 1

                # ============================================================================
                # 🗄️ COPIE D'ARCHIVE (mail séparé, jamais vu par l'association,
                # marqueur dédié pour filtrage côté boîte mail sans polluer l'Inbox)
                # ============================================================================
                try:
                    envoyer_mail(
                        sujet=f"[ARCHIVE BASILIC] Indicateurs état – {association['nom_association']} – {periode}",
                        destinataires=[sender],
                        texte=f"Copie d'archive automatique — destinataire(s) réel(s) : {', '.join(emails)}\n\n---\n\n{corps}",
                        sender_override=sender,
                        attachment_path=pdf_path
                    )
                except Exception as e_archive:
                    write_log(f"⚠️ Erreur envoi copie archive pour {association['nom_association']} : {e_archive}")

                # 🔥 nettoyage fichier
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)

            except Exception as e:
                write_log(f"❌ Erreur envoi mail pour {association['nom_association']} : {e}")
                nb_erreurs += 1

                conn_suivi.execute("""
                    UPDATE indicateurs_suivi
                    SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = ?,
                        mail_mailjet_status = NULL, mail_mailjet_message_ids = NULL, mail_modele_id = ?
                    WHERE id = ?
                """, (now_iso, 1 if mode_test_local else 0, str(e), modele["id"], l["id"]))
                conn_suivi.commit()

        conn_suivi.execute("""
            UPDATE indicateurs_campagnes
            SET dernier_envoi_le = ?, dernier_envoi_par = ?, dernier_envoi_mode_test = ?,
                dernier_envoi_nb_ok = ?, dernier_envoi_nb_erreur = ?
            WHERE id = ?
        """, (now_iso, current_user_email, 1 if mode_test_local else 0, envoyes, nb_erreurs, campagne_id))
        conn_suivi.commit()
        conn_suivi.close()

        write_log(f"📤 Envoi indicateurs (background) terminé : {envoyes} envoyés, {nb_erreurs} erreur(s), mode_test={mode_test_local}")

# ==========================================
# 📄 Liste des modèles
# ==========================================
@emails_bp.route("/emails")
@login_required
@require_access_modeles("lecture")
def liste_modeles():

    with sqlite3.connect(get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        modeles = conn.execute("""
            SELECT * FROM modeles_emails
            ORDER BY TRIM(code_modele) COLLATE NOCASE
        """).fetchall()

    return render_template(
        "emails/liste_modeles.html",
        modeles=modeles
    )



# ==========================================
# ➕ Ajouter / Modifier
# ==========================================
@emails_bp.route("/emails/edit/<int:id>", methods=["GET", "POST"])
@emails_bp.route("/emails/new", methods=["GET", "POST"])
@login_required
@require_access_modeles("ecriture")
def edit_modele(id=None):

    with sqlite3.connect(get_db_path()) as conn:
        conn.row_factory = sqlite3.Row

        if id:
            modele = conn.execute("SELECT * FROM modeles_emails WHERE id = ?", (id,)).fetchone()
        else:
            modele = None

        if request.method == "POST":
            code = request.form.get("code_modele")
            sujet = request.form.get("sujet")
            corps = request.form.get("corps")
            type_periode = request.form.get("type_periode") or None
            action = request.form.get("action", "save")

            if id:
                conn.execute("""
                    UPDATE modeles_emails
                    SET code_modele=?, sujet=?, corps=?, type_periode=?, date_modification=datetime('now')
                    WHERE id=?
                """, (code, sujet, corps, type_periode, id))
                flash("Modèle mis à jour", "success")

            else:
                conn.execute("""
                    INSERT INTO modeles_emails (code_modele, sujet, corps, type_periode, date_modification)
                    VALUES (?, ?, ?, ?, datetime('now'))
                """, (code, sujet, corps, type_periode))
                flash("Modèle créé", "success")

            conn.commit()

            if action == "save_both" and os.getenv("ENVIRONMENT", "DEV").upper() == "DEV":
                ok, err = copier_modele_email_vers_prod(code, sujet, corps, type_periode)
                if ok:
                    flash("Modèle également enregistré en PROD", "success")
                else:
                    flash(f"⚠️ Échec de la copie vers PROD : {err}", "danger")

            return redirect(url_for("emails.liste_modeles"))

    return render_template("emails/edit_modele.html", modele=modele)


# ==========================================
# 🗑️ Suppression
# ==========================================
@emails_bp.route("/emails/delete/<int:id>")
@login_required
@require_access_modeles("ecriture")
def delete_modele(id):
    with sqlite3.connect(get_db_path()) as conn:
        conn.execute("DELETE FROM modeles_emails WHERE id = ?", (id,))
        conn.commit()

    flash("Modèle supprimé", "warning")
    return redirect(url_for("emails.liste_modeles"))


# ============================================================================
# 📧 ENVOI DES MAILS INDICATEURS
# ============================================================================

@emails_bp.route("/emails/envoi/<int:campagne_id>", methods=["GET", "POST"])
@login_required
@require_access("indicateurs", "ecriture")
def envoyer_mails(campagne_id):

    db_path = get_db_path()
    

    # ============================================================================
    # 📥 RÉCUPÉRATION DES DONNÉES
    # ============================================================================
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # 1️⃣ récupérer campagne AVANT
        campagne = conn.execute("""
            SELECT * FROM indicateurs_campagnes
            WHERE id = ?
        """, (campagne_id,)).fetchone()

        if not campagne:
            raise ValueError(f"❌ Campagne {campagne_id} introuvable")

        # 2️⃣ ensuite seulement utiliser periode
        periode = campagne["periode"]

        if periode.lower().startswith("année"):
            type_periode = "annuel"
        else:
            type_periode = "trimestriel"

        # 3️⃣ filtrer les modèles
        modeles = conn.execute("""
            SELECT * FROM modeles_emails
            WHERE type_periode = ?
            ORDER BY TRIM(code_modele) COLLATE NOCASE
        """, (type_periode,)).fetchall()
        write_log(f"📧 campagne_id utilisé = {campagne_id}")

        if not campagne:
            raise ValueError(f"❌ Campagne {campagne_id} introuvable")

        lignes = conn.execute("""
            SELECT
                i.*,
                a.nom_association,
                a.code_VIF AS code_vif,
                a.courriel_resp_IE1,
                a.courriel_resp_IE2,
                a.responsable_IE,
                a.tel_resp_IE,
                a.CAR
            FROM indicateurs_suivi i
            JOIN associations a ON i.association_id = a.id
            WHERE i.campagne_id = ?
            AND LOWER(TRIM(i.statut_csv)) LIKE 'non%'
            AND (i.exclure_envoi_mail IS NULL OR i.exclure_envoi_mail = 0)
        """, (campagne_id,)).fetchall()

    # ============================================================================
    # 🚀 TRAITEMENT ENVOI
    # ============================================================================
    if request.method == "POST":

        modele_id = request.form.get("modele_id")
        sender = request.form.get("sender") or "ba380@banquealimentaire.org"
        mode_test_local = request.form.get("mode_test") == "on"

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            modele = conn.execute("""
                SELECT * FROM modeles_emails
                WHERE id = ?
            """, (modele_id,)).fetchone()
            periode = campagne["periode"]

            if periode.lower().startswith("année"):
                type_periode = "annuel"
            else:
                type_periode = "trimestriel"

            if not modele:
                flash("❌ Modèle introuvable", "danger")
                return redirect(url_for("emails.envoyer_mails", campagne_id=campagne_id))


            if modele["type_periode"] != type_periode:
                flash("❌ Modèle incompatible avec la période", "danger")
                return redirect(url_for("emails.envoyer_mails", campagne_id=campagne_id))


        # ============================================================================
        # ❌ VALIDATION EMAILS
        # ============================================================================
        erreurs = []

        for l in lignes:
            if not (l["courriel_resp_IE1"] or l["courriel_resp_IE2"]):
                erreurs.append(l["nom_association"])

        if erreurs:
            msg = "❌ Aucune adresse email pour :<br>" + "<br>".join(erreurs)
            flash(msg, "danger")
            return redirect(url_for("emails.envoyer_mails", campagne_id=campagne_id))

        # ============================================================================
        # 🚀 LANCEMENT EN ARRIÈRE-PLAN
        # ============================================================================
        periode = campagne["periode"]

        if periode.lower().startswith("année"):
            type_periode = "annuel"
        else:
            type_periode = "trimestriel"

        app_reel = current_app._get_current_object()
        current_user_email = current_user.email
        campagne_dict = dict(campagne)
        modele_dict = dict(modele)
        lignes_dicts = [dict(l) for l in lignes]

        Thread(
            target=envoyer_indicateurs_background,
            args=(app_reel, db_path, campagne_id, campagne_dict, lignes_dicts,
                  modele_dict, sender, mode_test_local, type_periode, periode,
                  current_user_email)
        ).start()

        # ============================================================================
        # 📢 MESSAGE IMMÉDIAT (l'envoi continue en arrière-plan)
        # ============================================================================
        if mode_test_local:
            flash("🧪 Envoi TEST lancé en arrière-plan (2 mails max vers l'adresse de test) — actualisez la page dans quelques secondes.", "warning")
        else:
            flash(f"🚀 Envoi réel lancé en arrière-plan pour {len(lignes)} association(s) — actualisez la page dans quelques instants pour voir le résultat.", "info")

        return redirect(url_for("indicateurs.resultats", campagne_id=campagne_id))

    # ============================================================================
    # 📄 AFFICHAGE PAGE
    # ============================================================================
    return render_template(
        "indicateurs/indicateurs_envoi_mails.html",
        modeles=modeles,
        campagne=campagne,
        nb=len(lignes)
    )
