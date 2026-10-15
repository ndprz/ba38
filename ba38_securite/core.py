"""
============================================================================
🔐 SÉCURITÉ DES CONNEXIONS
============================================================================
Mesures "invisibles" pour l'utilisateur (aucune contrainte supplémentaire
tant que tout va bien) :

- verrouillage par compte : 5 échecs en 15 min sur un même identifiant,
  quelle que soit l'adresse IP → compte bloqué 15 min (déblocage auto,
  ou immédiat par un admin depuis la gestion des utilisateurs) ;
- filtre des pays : connexion refusée hors des pays autorisés (FR par
  défaut), sauf exception datée posée par un admin sur le compte ;
- alerte nouvel appareil : email "Est-ce bien vous ?" avec un lien
  "ce n'est pas moi" qui déconnecte tout le monde et force un nouveau
  mot de passe ;
- dernière connexion mémorisée (cf. comptes_inactifs.py).

Géolocalisation : base DB-IP "IP to Country Lite" (CC BY 4.0), fichier
partagé DEV/PROD hors des arborescences, mis à jour par maj_geoip.py.
En cas d'absence ou d'erreur de la base, on laisse passer (fail-open) :
un incident de géolocalisation ne doit jamais bloquer les bénévoles.
============================================================================
"""

import hashlib
import ipaddress
import os
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import after_this_request, current_app, request, session

from ba38_utilitaires.core import get_real_db_connection, write_log

# --------------------------------------------------------------------------
# ⚙️ Paramètres
# --------------------------------------------------------------------------
CONTACT_SECURITE = os.getenv("SECURITE_CONTACT_EMAIL", "ba380.informatique2@banquealimentaire.org")
PAYS_AUTORISES = {p.strip().upper() for p in os.getenv("SECURITE_PAYS_AUTORISES", "FR").split(",") if p.strip()}
GEOIP_DB = os.getenv("GEOIP_DB", "/srv/ba38/geoip/dbip-country-lite.mmdb")

VERROU_MAX_ECHECS = 5
VERROU_FENETRE = 15 * 60       # secondes
VERROU_DUREE = 15 * 60         # secondes

COOKIE_APPAREIL = "ba38_appareil"   # suffixé par l'id du compte : un PC partagé
                                     # par plusieurs comptes garde la confiance de chacun
COOKIE_APPAREIL_DUREE = 400 * 24 * 3600   # plafond navigateurs (~13 mois)

NOMS_PAYS = {
    "FR": "France", "BE": "Belgique", "CH": "Suisse", "IT": "Italie", "ES": "Espagne",
    "DE": "Allemagne", "GB": "Royaume-Uni", "NL": "Pays-Bas", "PT": "Portugal",
    "US": "États-Unis", "CA": "Canada", "MA": "Maroc", "DZ": "Algérie", "TN": "Tunisie",
    "RU": "Russie", "CN": "Chine", "UA": "Ukraine", "PL": "Pologne", "RO": "Roumanie",
    "IN": "Inde", "BR": "Brésil", "NG": "Nigeria", "VN": "Viêt Nam", "SG": "Singapour",
}


def nom_pays(code):
    if not code:
        return "pays inconnu"
    return NOMS_PAYS.get(code, code)


def maintenant():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# 🔒 Verrouillage par compte
# --------------------------------------------------------------------------
def secondes_verrouillage(login):
    """Secondes restantes de verrouillage du compte (0 si libre). Compte
    les échecs enregistrés dans login_attempts pour cet identifiant, toutes
    IP confondues. Fonctionne aussi pour un identifiant inexistant (même
    réponse qu'un vrai compte : pas d'énumération possible)."""
    if not login:
        return 0
    now = time.time()
    conn = get_real_db_connection()
    rows = conn.execute(
        "SELECT timestamp FROM login_attempts WHERE login=? AND timestamp > ? ORDER BY timestamp",
        (login, now - VERROU_FENETRE),
    ).fetchall()
    conn.close()
    if len(rows) < VERROU_MAX_ECHECS:
        return 0
    reste = int(rows[-1]["timestamp"] + VERROU_DUREE - now)
    return max(reste, 0)


def enregistrer_echec(ip, login):
    conn = get_real_db_connection()
    conn.execute(
        "INSERT INTO login_attempts (ip, login, timestamp) VALUES (?, ?, ?)",
        (ip, login, time.time()),
    )
    conn.commit()
    conn.close()


def effacer_echecs(login):
    conn = get_real_db_connection()
    conn.execute("DELETE FROM login_attempts WHERE login=?", (login,))
    conn.commit()
    conn.close()


def message_verrouillage(secondes):
    minutes = max(1, (secondes + 59) // 60)
    return (
        f"⛔ Trop de tentatives : ce compte est bloqué pendant {minutes} minute(s). "
        f"Vous pourrez réessayer ensuite, ou utiliser « Mot de passe oublié ». "
        f"En cas de besoin : {CONTACT_SECURITE}"
    )


def notifier_verrouillage(login, ip):
    """Prévient le titulaire du compte (une fois par heure maximum)."""
    user = _user_par_email(login)
    if not user or not _alerte_autorisee(f"verrou:{user['id']}", 3600):
        return
    pays = pays_ip(ip)
    texte = f"""Bonjour {user['username'] or ''},

Plusieurs tentatives de connexion ont échoué sur votre compte BA38 ({user['email']}).
Par sécurité, le compte est bloqué pendant 15 minutes.

Origine des tentatives : {nom_pays(pays)} (adresse {ip})

• Si c'est vous (mot de passe oublié) : attendez 15 minutes, ou utilisez le lien
  « Mot de passe oublié » sur la page de connexion.
• Si ce n'est pas vous : rien à faire, votre compte s'est protégé tout seul.
  Si ces messages se répètent, prévenez {CONTACT_SECURITE}.

L'équipe informatique BA38
"""
    _envoyer_en_arriere_plan("Tentatives de connexion sur votre compte", [user["email"]], texte)


# --------------------------------------------------------------------------
# 🌍 Filtre des pays
# --------------------------------------------------------------------------
_lecteur_geoip = None
_lecteur_geoip_mtime = None


def pays_ip(ip):
    """Code pays ISO (ex. "FR") de l'adresse, ou None si inconnu / adresse
    locale / base indisponible."""
    global _lecteur_geoip, _lecteur_geoip_mtime
    try:
        adresse = ipaddress.ip_address(ip)
        if adresse.is_private or adresse.is_loopback or adresse.is_link_local:
            return None
        mtime = os.path.getmtime(GEOIP_DB)
        if _lecteur_geoip is None or mtime != _lecteur_geoip_mtime:
            import maxminddb
            _lecteur_geoip = maxminddb.open_database(GEOIP_DB)
            _lecteur_geoip_mtime = mtime
        info = _lecteur_geoip.get(ip) or {}
        return (info.get("country") or {}).get("iso_code")
    except Exception as e:
        write_log(f"⚠️ Géolocalisation impossible pour {ip} : {e}")
        return None


def pays_refuse(ip, user=None):
    """Retourne le code pays si la connexion doit être refusée, sinon None.
    Pays inconnu → autorisé (fail-open). Exception datée sur le compte
    (users.etranger_autorise_jusqu, incluse) → autorisé."""
    pays = pays_ip(ip)
    if not pays or pays in PAYS_AUTORISES:
        return None
    if user is not None:
        jusqu = (user["etranger_autorise_jusqu"] or "").strip()
        if jusqu and jusqu >= datetime.now().strftime("%Y-%m-%d"):
            return None
    return pays


def message_pays_refuse():
    return (
        "🌍 La connexion n'est pas possible depuis l'étranger. "
        f"Si vous êtes en déplacement, demandez une autorisation temporaire à {CONTACT_SECURITE}."
    )


def notifier_pays_refuse_mot_de_passe_correct(user, ip, pays):
    """Le bon mot de passe a été saisi depuis un pays refusé : soit la
    personne voyage, soit son mot de passe est connu d'un tiers. On prévient
    le titulaire et l'administrateur (une fois par jour maximum)."""
    if not _alerte_autorisee(f"pays:{user['id']}", 24 * 3600):
        return
    quand = datetime.now().strftime("%d/%m/%Y à %Hh%M")
    texte_user = f"""Bonjour {user['username'] or ''},

Une connexion à votre compte BA38 ({user['email']}) a été tentée le {quand}
depuis : {nom_pays(pays)} (adresse {ip}), avec votre mot de passe.

Les connexions depuis l'étranger sont bloquées par sécurité.

• Si c'est vous (voyage) : demandez une autorisation temporaire à {CONTACT_SECURITE}.
• Si ce n'est pas vous : votre mot de passe est connu de quelqu'un d'autre.
  Changez-le dès que possible avec le lien « Mot de passe oublié » de la page
  de connexion, et prévenez {CONTACT_SECURITE}.

L'équipe informatique BA38
"""
    texte_admin = f"""Connexion refusée depuis l'étranger AVEC LE BON MOT DE PASSE.

Compte : {user['username'] or ''} <{user['email']}>
Date   : {quand}
Pays   : {nom_pays(pays)} ({pays})
IP     : {ip}

Le titulaire a été prévenu. Si c'est un voyage, posez une autorisation
temporaire (colonne Sécurité de la gestion des utilisateurs).
Sinon le mot de passe est compromis : faites-le changer.
"""
    _envoyer_en_arriere_plan("Connexion depuis l'étranger sur votre compte", [user["email"]], texte_user)
    _envoyer_en_arriere_plan(f"🌍 Connexion étrangère refusée — {user['email']}", [CONTACT_SECURITE], texte_admin)


# --------------------------------------------------------------------------
# 💻 Appareils connus + alerte nouvel appareil
# --------------------------------------------------------------------------
def _hash_jeton(jeton):
    return hashlib.sha256(jeton.encode()).hexdigest()


def description_appareil(ua):
    ua = ua or ""
    if "Edg/" in ua:
        nav = "Edge"
    elif "SamsungBrowser" in ua:
        nav = "Samsung Internet"
    elif "Firefox/" in ua:
        nav = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        nav = "Chrome"
    elif "Safari/" in ua:
        nav = "Safari"
    else:
        nav = "navigateur inconnu"

    if "iPhone" in ua:
        sys_ = "iPhone"
    elif "iPad" in ua:
        sys_ = "iPad"
    elif "Android" in ua:
        sys_ = "téléphone ou tablette Android"
    elif "Windows" in ua:
        sys_ = "ordinateur Windows"
    elif "Macintosh" in ua or "Mac OS" in ua:
        sys_ = "Mac"
    elif "CrOS" in ua:
        sys_ = "Chromebook"
    elif "Linux" in ua:
        sys_ = "ordinateur Linux"
    else:
        sys_ = "appareil inconnu"
    return f"{nav} sur {sys_}"


def _appareil_par_cookie(conn, user_id, jeton):
    """Appareil de ce compte identifié par son cookie (seule preuve forte)."""
    if not jeton:
        return None
    row = conn.execute(
        "SELECT id FROM users_appareils WHERE user_id=? AND jeton_hash=?",
        (user_id, _hash_jeton(jeton)),
    ).fetchone()
    return row["id"] if row else None


def _ressemble_appareil_connu(conn, user_id, ua, ip):
    """Même navigateur + même IP qu'un appareil déjà vu (cookies effacés,
    navigation privée) : sert UNIQUEMENT à ne pas envoyer d'alerte à
    répétition sur le PC habituel. Ne transmet jamais l'identité ni la
    confiance de l'appareil existant : deux PC d'un même bureau partagent
    souvent IP et navigateur."""
    return conn.execute(
        "SELECT 1 FROM users_appareils WHERE user_id=? AND user_agent=? AND ip=? LIMIT 1",
        (user_id, ua, ip),
    ).fetchone() is not None


def apres_connexion_reussie(user, ip, ua, confiance_jours=0):
    """À appeler juste après login_user() (connexion simple ou 2FA).
    - mémorise la dernière connexion (et annule un avertissement d'inactivité),
    - mémorise le jeton de session (invalidation possible à distance),
    - reconnaît l'appareil ou l'enregistre + alerte "nouvel appareil",
    - confiance_jours > 0 : plus de code 2FA demandé sur cet appareil
      pendant cette durée (case cochée après saisie d'un code).
    Ne doit jamais faire échouer une connexion."""
    try:
        session["jeton_session"] = user["jeton_session"]
        now = maintenant()
        jeton = request.cookies.get(f"{COOKIE_APPAREIL}_{user['id']}")
        pays = pays_ip(ip)

        conn = get_real_db_connection()
        conn.execute(
            "UPDATE users SET derniere_connexion=?, avertissement_inactivite_le=NULL WHERE id=?",
            (now, user["id"]),
        )
        nb_appareils = conn.execute(
            "SELECT COUNT(*) FROM users_appareils WHERE user_id=?", (user["id"],)
        ).fetchone()[0]
        appareil_id = _appareil_par_cookie(conn, user["id"], jeton)

        if appareil_id:
            conn.execute(
                "UPDATE users_appareils SET vu_le=?, ip=?, pays=?, user_agent=? WHERE id=?",
                (now, ip, pays, ua, appareil_id),
            )
        else:
            ressemble = _ressemble_appareil_connu(conn, user["id"], ua, ip)
            jeton = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO users_appareils (user_id, jeton_hash, user_agent, ip, pays, cree_le, vu_le) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user["id"], _hash_jeton(jeton), ua, ip, pays, now, now),
            )
            _poser_cookie_appareil(user["id"], jeton)
            # Premier appareil jamais vu pour ce compte (mise en service) :
            # on l'apprend sans alerter, sinon tout le monde recevrait une
            # alerte à sa prochaine connexion.
            if nb_appareils > 0 and not ressemble:
                _alerter_nouvel_appareil(user, ip, ua, pays)
            appareil_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if confiance_jours:
            jusqu = (datetime.now() + timedelta(days=confiance_jours)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE users_appareils SET confiance_jusqu=? WHERE id=?", (jusqu, appareil_id))
        conn.commit()
        conn.close()
    except Exception as e:
        write_log(f"❌ apres_connexion_reussie user_id={user['id']} : {e}")


def _poser_cookie_appareil(user_id, jeton):
    @after_this_request
    def _cookie(response):
        response.set_cookie(
            f"{COOKIE_APPAREIL}_{user_id}", jeton, max_age=COOKIE_APPAREIL_DUREE,
            httponly=True, secure=True, samesite="Lax",
        )
        return response


def _alerter_nouvel_appareil(user, ip, ua, pays):
    from ba38_securite.routes import lien_pas_moi
    quand = datetime.now().strftime("%d/%m/%Y à %Hh%M")
    lien = lien_pas_moi(user)
    texte = f"""Bonjour {user['username'] or ''},

Une connexion à votre compte BA38 vient d'avoir lieu depuis un appareil
que nous ne connaissions pas :

   • Quand    : le {quand}
   • Appareil : {description_appareil(ua)}
   • Lieu     : {nom_pays(pays)} (adresse {ip})

✅ C'est vous (nouvel ordinateur, nouveau téléphone, autre navigateur) ?
   Vous n'avez rien à faire.

❌ Ce n'est pas vous ? Cliquez sur ce lien pour bloquer l'accès :
   {lien}
   Toutes les connexions à votre compte seront coupées et vous recevrez
   un email pour choisir un nouveau mot de passe.

En cas de doute : {CONTACT_SECURITE}

L'équipe informatique BA38
"""
    _envoyer_en_arriere_plan("Nouvelle connexion à votre compte BA38", [user["email"]], texte)
    write_log(f"📱 Nouvel appareil {user['email']} | {description_appareil(ua)} | IP={ip} ({pays})")


def appareil_de_confiance(user):
    """True si le navigateur porte le cookie d'un appareil de ce compte
    marqué "de confiance" et pas encore expiré (cookie seul : le repli
    navigateur+IP de _ressemble_appareil_connu ne suffit pas pour sauter la 2FA)."""
    jeton = request.cookies.get(f"{COOKIE_APPAREIL}_{user['id']}")
    if not jeton:
        return False
    conn = get_real_db_connection()
    row = conn.execute(
        "SELECT 1 FROM users_appareils WHERE user_id=? AND jeton_hash=? AND confiance_jusqu > ?",
        (user["id"], _hash_jeton(jeton), maintenant()),
    ).fetchone()
    conn.close()
    return bool(row)


# --------------------------------------------------------------------------
# 📱 Code de connexion par SMS ou email (2FA sans application)
# --------------------------------------------------------------------------
CONFIANCE_JOURS = 6             # "ne plus demander de code sur cet appareil"
CODE_DUREE = 10 * 60            # secondes
CODE_MAX_ENVOIS = 3             # par compte...
CODE_FENETRE_ENVOIS = 15 * 60   # ...sur cette durée (coût SMS, fraude aux SMS)


def masquer_numero(numero):
    """'33612345678' → '06 •• •• •• 78'"""
    n = "0" + numero[2:] if numero and numero.startswith("33") else (numero or "")
    return f"{n[:2]} •• •• •• {n[-2:]}" if len(n) == 10 else "•• •• •• •• ••"


def masquer_email(email):
    nom, _, domaine = (email or "").partition("@")
    return f"{nom[:3]}•••@{domaine}" if domaine else "•••"


def telephones_du_compte(user_id):
    conn = get_real_db_connection()
    rows = conn.execute(
        "SELECT * FROM users_telephones WHERE user_id=? ORDER BY libelle COLLATE NOCASE", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def envoyer_code(user, telephone=None):
    """Génère un code à 6 chiffres, le mémorise (haché) dans la session
    serveur et l'envoie par SMS (telephone = ligne users_telephones) ou,
    à défaut, par email. Retourne (ok, message d'erreur)."""
    conn = get_real_db_connection()
    seuil = (datetime.now() - timedelta(seconds=CODE_FENETRE_ENVOIS)).strftime("%Y-%m-%d %H:%M:%S")
    nb = conn.execute(
        "SELECT COUNT(*) FROM securite_alertes WHERE cle=? AND envoye_le > ?", (f"code:{user['id']}", seuil)
    ).fetchone()[0]
    if nb >= CODE_MAX_ENVOIS:
        conn.close()
        return False, "Trop de codes envoyés. Patientez un quart d'heure avant d'en redemander un."
    conn.execute("INSERT INTO securite_alertes (cle, envoye_le) VALUES (?, ?)", (f"code:{user['id']}", maintenant()))
    conn.commit()
    conn.close()

    code = f"{secrets.randbelow(1_000_000):06d}"
    texte_sms = f"BA38 : votre code de connexion est {code}. Valable 10 minutes. Ne le communiquez à personne."
    try:
        if telephone is not None:
            _envoyer_sms(telephone["numero"], texte_sms)
            via = f"SMS {telephone['libelle']}"
        else:
            from ba38_utilitaires.core import envoyer_mail
            envoyer_mail(
                sujet=f"Votre code de connexion BA38 : {code}",
                destinataires=[user["email"]],
                sender_name="BA38 Sécurité",
                # ⚠️ pas de current_user_email : en DEV il sert de destinataire
                # de redirection, le code partirait à la vraie boîte du compte
                texte=f"""Bonjour,

Votre code de connexion à l'application BA38 est :

        {code}

Il est valable 10 minutes. Ne le communiquez à personne : l'équipe
informatique ne vous le demandera jamais.

Vous n'avez pas essayé de vous connecter ? Quelqu'un connaît votre mot de
passe : changez-le avec « Mot de passe oublié » et prévenez {CONTACT_SECURITE}.

L'équipe informatique BA38
""",
            )
            via = "email"
    except Exception as e:
        write_log(f"❌ Envoi code 2FA user_id={user['id']} : {e}")
        return False, "L'envoi du code a échoué. Réessayez, ou choisissez un autre moyen."

    session["code_2fa"] = {
        "user_id": user["id"],
        "hash": hashlib.sha256(f"{user['id']}:{code}".encode()).hexdigest(),
        "expire": time.time() + CODE_DUREE,
        "tel_id": telephone["id"] if telephone is not None else None,
        "via": via,
        "destination": masquer_numero(telephone["numero"]) if telephone is not None else masquer_email(user["email"]),
    }
    session.modified = True
    write_log(f"📨 Code 2FA envoyé user_id={user['id']} {user['email']} | {via}")
    return True, None


def verifier_code(user, code):
    """Retourne (ok, via). Un code juste n'est utilisable qu'une fois."""
    attendu = session.get("code_2fa") or {}
    if attendu.get("user_id") != user["id"] or time.time() > attendu.get("expire", 0):
        return False, None
    saisi = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secrets.compare_digest(
        hashlib.sha256(f"{user['id']}:{saisi}".encode()).hexdigest(), attendu.get("hash", "")
    ):
        return False, None
    session.pop("code_2fa", None)
    if attendu.get("tel_id"):
        conn = get_real_db_connection()
        conn.execute(
            "UPDATE users_telephones SET verifie_le=COALESCE(verifie_le, ?), utilise_le=? WHERE id=?",
            (maintenant(), maintenant(), attendu["tel_id"]),
        )
        conn.commit()
        conn.close()
    return True, attendu.get("via")


def _envoyer_sms(numero, texte):
    """SMS via SmsFactor ; en DEV, toujours vers le numéro de test (même
    garde-fou que le module SMS bénévoles)."""
    from ba38_sms.smsfactor_client import envoyer_sms_reel, get_dev_test_number, is_dev_environment
    if is_dev_environment():
        test = get_dev_test_number()
        if not test:
            raise RuntimeError("SMS_DEV_TEST_NUMBER absent : envoi SMS impossible en DEV")
        return envoyer_sms_reel(test, f"[TEST {masquer_numero(numero)}] {texte}")
    return envoyer_sms_reel(numero, texte)


# --------------------------------------------------------------------------
# 🧰 Outils internes
# --------------------------------------------------------------------------
def _user_par_email(email):
    conn = get_real_db_connection()
    user = conn.execute("SELECT * FROM users WHERE LOWER(email)=?", ((email or "").lower(),)).fetchone()
    conn.close()
    return user


def _alerte_autorisee(cle, delai_secondes):
    """Anti-spam des emails d'alerte : True (et mémorise l'envoi) si aucune
    alerte de cette clé n'est partie depuis delai_secondes."""
    seuil = (datetime.now() - timedelta(seconds=delai_secondes)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_real_db_connection()
    deja = conn.execute(
        "SELECT 1 FROM securite_alertes WHERE cle=? AND envoye_le > ?", (cle, seuil)
    ).fetchone()
    if not deja:
        conn.execute("INSERT INTO securite_alertes (cle, envoye_le) VALUES (?, ?)", (cle, maintenant()))
        conn.commit()
    conn.close()
    return not deja


def _envoyer_en_arriere_plan(sujet, destinataires, texte):
    """Envoi hors de la requête : la connexion ne doit pas attendre Mailjet."""
    from ba38_utilitaires.core import envoyer_mail
    app = current_app._get_current_object()

    def _envoi():
        with app.app_context():
            try:
                envoyer_mail(sujet=sujet, destinataires=destinataires, texte=texte, sender_name="BA38 Sécurité")
            except Exception as e:
                write_log(f"❌ Envoi alerte sécurité '{sujet}' à {destinataires} : {e}")

    threading.Thread(target=_envoi, daemon=True).start()
