"""
🔐 Routes sécurité : lien "ce n'est pas moi" (public) + déblocage admin.
"""

import secrets
from datetime import datetime, timedelta

import jwt
from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import generate_password_hash

from ba38_securite import securite_bp
from ba38_securite.core import (
    CONTACT_SECURITE,
    _envoyer_en_arriere_plan,
    _envoyer_sms,
    effacer_echecs,
    maintenant,
    masquer_numero,
    telephones_du_compte,
)
from ba38_utilitaires.core import (
    get_real_db_connection,
    get_real_ip,
    require_admin_global,
    send_reset_email,
    synchroniser_utilisateur_vers_base_test,
    write_log,
)

DUREE_LIEN_PAS_MOI = 14 * 24 * 3600


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="ba38-securite-pas-moi")


def lien_pas_moi(user):
    """Lien à usage unique : il embarque le jeton de session courant du
    compte, qui change dès que le lien a servi."""
    jeton = _serializer().dumps({"u": user["id"], "j": user["jeton_session"] or ""})
    return url_for("securite.pas_moi", jeton=jeton, _external=True)


@securite_bp.route("/pas-moi/<jeton>", methods=["GET", "POST"])
def pas_moi(jeton):
    """GET = page de confirmation (les antivirus de messagerie ouvrent les
    liens des emails : l'action ne doit se déclencher que sur le bouton)."""
    try:
        data = _serializer().loads(jeton, max_age=DUREE_LIEN_PAS_MOI)
    except SignatureExpired:
        return render_template("securite/pas_moi.html", etat="expire", contact=CONTACT_SECURITE)
    except BadSignature:
        return render_template("securite/pas_moi.html", etat="invalide", contact=CONTACT_SECURITE)

    conn = get_real_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id=?", (data["u"],)).fetchone()
    if not user:
        conn.close()
        return render_template("securite/pas_moi.html", etat="invalide", contact=CONTACT_SECURITE)

    if (user["jeton_session"] or "") != data["j"]:
        conn.close()
        return render_template("securite/pas_moi.html", etat="deja_fait", contact=CONTACT_SECURITE)

    if request.method == "GET":
        conn.close()
        return render_template("securite/pas_moi.html", etat="confirmer", contact=CONTACT_SECURITE)

    # 🔒 Mise en sécurité : nouveau jeton de session (toutes les sessions
    # ouvertes tombent, cf. load_user), mot de passe inutilisable, appareils
    # oubliés, 2FA conservée.
    conn.execute(
        "UPDATE users SET jeton_session=?, password_hash=? WHERE id=?",
        (secrets.token_hex(16), generate_password_hash(secrets.token_urlsafe(32)), user["id"]),
    )
    conn.execute("DELETE FROM users_appareils WHERE user_id=?", (user["id"],))
    conn.commit()
    conn.close()
    synchroniser_utilisateur_vers_base_test(user["email"])

    ip = get_real_ip()
    write_log(f"🚨 'Ce n'est pas moi' : compte {user['email']} sécurisé | IP={ip}")

    token = jwt.encode(
        {"email": user["email"], "exp": datetime.utcnow() + timedelta(hours=24)},
        current_app.secret_key, algorithm="HS256",
    )
    send_reset_email(user["email"], token)

    _envoyer_en_arriere_plan(
        f"🚨 Piratage signalé — {user['email']}",
        [CONTACT_SECURITE],
        f"""{user['username'] or ''} <{user['email']}> a cliqué sur « Ce n'est pas moi »
après une alerte de connexion depuis un nouvel appareil.

Fait automatiquement :
- toutes les sessions ouvertes sur ce compte ont été coupées ;
- l'ancien mot de passe ne fonctionne plus ;
- un email de choix de nouveau mot de passe lui a été envoyé (valable 24 h).

À faire : vérifier avec la personne, et regarder dans le journal des
connexions ce qui a été consulté depuis l'appareil inconnu.
""",
    )
    return render_template("securite/pas_moi.html", etat="fait", contact=CONTACT_SECURITE)


@securite_bp.route("/debloquer/<int:user_id>", methods=["POST"])
@login_required
@require_admin_global
def debloquer(user_id):
    conn = get_real_db_connection()
    user = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if user:
        effacer_echecs(user["email"].strip().lower())
        write_log(f"🔓 Compte débloqué par admin : {user['email']}")
        flash(f"🔓 {user['email']} débloqué.", "success")
    return redirect(url_for("admin.gestion_utilisateurs"))


# --------------------------------------------------------------------------
# 📱 Téléphones de connexion d'un compte (admin uniquement : un pirate qui
# aurait le mot de passe ne doit pas pouvoir ajouter son propre numéro)
# --------------------------------------------------------------------------
def format_numero(numero):
    n = "0" + numero[2:] if numero.startswith("33") else numero
    return " ".join(n[i:i + 2] for i in range(0, len(n), 2))


@securite_bp.route("/telephones/<int:user_id>", methods=["GET", "POST"])
@login_required
@require_admin_global
def telephones(user_id):
    from ba38_sms.smsfactor_client import normalize_phone_fr

    conn = get_real_db_connection()
    user = conn.execute("SELECT id, email, username, force_2fa, totp_enabled FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if not user:
        flash("Utilisateur introuvable.", "danger")
        return redirect(url_for("admin.gestion_utilisateurs"))

    if request.method == "POST":
        libelle = (request.form.get("libelle") or "").strip()
        numero = normalize_phone_fr(request.form.get("numero"))
        if not libelle:
            flash("Indiquez le prénom (ou la fonction) de la personne.", "warning")
        elif not numero or not numero.startswith(("336", "337")):
            flash("Numéro de portable français attendu (06… ou 07…).", "warning")
        else:
            conn = get_real_db_connection()
            try:
                conn.execute(
                    "INSERT INTO users_telephones (user_id, libelle, numero, cree_le, cree_par) VALUES (?, ?, ?, ?, ?)",
                    (user_id, libelle[:40], numero, maintenant(), current_user.email),
                )
                conn.commit()
                write_log(f"📱 Téléphone ajouté à {user['email']} : {libelle} {masquer_numero(numero)} par {current_user.email}")
                flash(f"📱 {libelle} ajouté(e).", "success")
            except Exception:
                flash("Ce numéro est déjà enregistré pour ce compte.", "warning")
            finally:
                conn.close()
        return redirect(url_for("securite.telephones", user_id=user_id))

    tels = [dict(t, affiche=format_numero(t["numero"])) for t in telephones_du_compte(user_id)]
    return render_template("securite/telephones.html", user=user, telephones=tels)


@securite_bp.route("/telephones/<int:user_id>/<int:tel_id>/<action>", methods=["POST"])
@login_required
@require_admin_global
def telephone_action(user_id, tel_id, action):
    conn = get_real_db_connection()
    tel = conn.execute(
        "SELECT t.*, u.email FROM users_telephones t JOIN users u ON u.id=t.user_id WHERE t.id=? AND t.user_id=?",
        (tel_id, user_id),
    ).fetchone()
    if tel and action == "supprimer":
        conn.execute("DELETE FROM users_telephones WHERE id=?", (tel_id,))
        conn.commit()
        write_log(f"📱 Téléphone retiré de {tel['email']} : {tel['libelle']} par {current_user.email}")
        flash(f"🗑️ {tel['libelle']} retiré(e).", "success")
    elif tel and action == "tester":
        try:
            _envoyer_sms(tel["numero"], f"BA38 : ce numéro est enregistré pour recevoir les codes de connexion du compte {tel['email']}.")
            flash(f"📨 SMS de test envoyé à {tel['libelle']}.", "success")
        except Exception as e:
            write_log(f"❌ SMS de test {tel['email']} / {tel['libelle']} : {e}")
            flash("L'envoi du SMS de test a échoué.", "danger")
    conn.close()
    return redirect(url_for("securite.telephones", user_id=user_id))


# --------------------------------------------------------------------------
# 🆘 "Problème de connexion ? Écrire au support" (page publique)
# --------------------------------------------------------------------------
SUPPORT_MAX_PAR_IP = 3            # messages...
SUPPORT_FENETRE = 3600            # ...par heure et par adresse IP (anti-spam)


@securite_bp.route("/support", methods=["GET", "POST"])
def support():
    """Formulaire public : un bénévole qui n'arrive pas à se connecter écrit
    au support sans avoir besoin d'un logiciel de messagerie configuré.
    Le mail arrive sur CONTACT_SECURITE, avec "Répondre à" = la personne."""
    from ba38_utilitaires.core import envoyer_mail

    champs = {k: (request.form.get(k) or "").strip() for k in ("nom", "email", "telephone", "identifiant", "message")}
    if request.method == "POST":
        ip = get_real_ip()
        # Champ piège invisible : rempli uniquement par les robots
        if request.form.get("site_web"):
            write_log(f"🤖 Support : robot ignoré | IP={ip}")
            return render_template("securite/support.html", envoye=True, contact=CONTACT_SECURITE, champs={})

        if not champs["nom"] or not champs["message"] or not (champs["email"] or champs["telephone"]):
            flash("Indiquez votre nom, votre message, et un email ou un téléphone pour qu'on puisse vous répondre.", "warning")
            return render_template("securite/support.html", envoye=False, contact=CONTACT_SECURITE, champs=champs)

        seuil = (datetime.now() - timedelta(seconds=SUPPORT_FENETRE)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_real_db_connection()
        nb = conn.execute(
            "SELECT COUNT(*) FROM securite_alertes WHERE cle=? AND envoye_le > ?", (f"support:{ip}", seuil)
        ).fetchone()[0]
        if nb >= SUPPORT_MAX_PAR_IP:
            conn.close()
            flash(f"Vous avez déjà envoyé plusieurs messages. Si c'est urgent, écrivez directement à {CONTACT_SECURITE}.", "warning")
            return render_template("securite/support.html", envoye=False, contact=CONTACT_SECURITE, champs=champs)
        conn.execute("INSERT INTO securite_alertes (cle, envoye_le) VALUES (?, ?)", (f"support:{ip}", maintenant()))
        conn.commit()
        conn.close()

        from ba38_securite.core import description_appareil, nom_pays, pays_ip
        texte = f"""Demande d'aide envoyée depuis la page de connexion.

Nom         : {champs['nom']}
Email       : {champs['email'] or '—'}
Téléphone   : {champs['telephone'] or '—'}
Identifiant : {champs['identifiant'] or '—'}

Message :
{champs['message'][:3000]}

---
Appareil : {description_appareil(request.headers.get('User-Agent', ''))}
Origine  : {nom_pays(pays_ip(ip))} (adresse {ip})
"""
        try:
            envoyer_mail(
                sujet=f"🆘 Problème de connexion — {champs['nom'][:60]}",
                destinataires=[CONTACT_SECURITE],
                texte=texte,
                sender_name="BA38 Support",
                reply_to=champs["email"] if "@" in champs["email"] else None,
            )
        except Exception as e:
            write_log(f"❌ Support : envoi impossible ({e}) | IP={ip}")
            flash(f"L'envoi a échoué. Écrivez directement à {CONTACT_SECURITE}.", "danger")
            return render_template("securite/support.html", envoye=False, contact=CONTACT_SECURITE, champs=champs)
        write_log(f"🆘 Support : message de {champs['nom']} ({champs['email'] or champs['telephone']}) | IP={ip}")
        return render_template("securite/support.html", envoye=True, contact=CONTACT_SECURITE, champs={})

    return render_template("securite/support.html", envoye=False, contact=CONTACT_SECURITE, champs=champs)
