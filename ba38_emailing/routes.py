# ============================================================
# 📣 Emailing associations
#   1. campagne (sujet) → 2. message (modèle ou texte riche + images)
#   → 3. expéditeur → 4. champs email visés → 5. liste triée
#   association/email → 6. lignes décochables → 7. envoi Mailjet en
#   arrière-plan + statut → renvoi Gmail des échecs → tableau de suivi.
#   Écran unique par campagne (modèle Cotisations V2).
# ============================================================

import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime
from functools import wraps

import requests
from flask import (
    current_app, flash, jsonify, redirect, render_template, request, session, url_for,
)
from flask_login import current_user, login_required
from PIL import Image

from ba38_emailing import emailing_bp
from ba38_utilitaires.core import (
    adresse_test_utilisateur, envoyer_mail, get_db_path, mailjet_get_message_status,
    has_access, render_modele_email, split_emails, write_log, upload_database,
)
from ba38_utilitaires.gmail_send import GmailSendError, envoyer_mail_gmail
from ba38_utilitaires.organisation import get_organisation
from ba38_utilitaires.taches_fond import lancer_tache_fond

GMAIL_SENDER = "ba380@banquealimentaire.org"
# Statuts Mailjet finaux considérés comme un échec de remise
STATUTS_ECHEC = {"bounce", "hardbounced", "softbounced", "blocked", "spam", "deferred"}
# Repli si l'API Mailjet des expéditeurs est indisponible
EXPEDITEURS_REPLI = [
    "ba380@banquealimentaire.org",
    "ba380.informatique2@banquealimentaire.org",
    "ba380.partenariat@banquealimentaire.org",
]
# Champs courriel inutilisés (ne pas proposer / ne jamais écrire à ces adresses)
CHAMPS_IGNORES = {"courriel_resp_tresorerie2"}
_cache_senders = {"t": 0, "liste": None}


CIBLES = {"associations": "associations", "benevoles": "benevoles"}  # cible → droit applicatif
LIBELLES_CIBLE = {"associations": "Associations", "benevoles": "Bénévoles"}


def droit_cible(cible, niveau):
    return has_access(CIBLES.get(cible, "associations"), niveau)


def acces_emailing(niveau):
    """Contrôle d'accès selon la cible de la campagne : droit « associations »
    pour une campagne associations, droit « benevoles » pour une campagne bénévoles."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            cid = kwargs.get("campagne_id")
            if cid is not None:
                with _connect() as conn:
                    r = conn.execute("SELECT cible FROM emailing_campagnes WHERE id = ?", (cid,)).fetchone()
                ok = droit_cible(r["cible"] if r else "associations", niveau)
            elif request.method == "POST" and request.form.get("cible") in CIBLES:
                ok = droit_cible(request.form.get("cible"), niveau)
            else:
                ok = droit_cible("associations", niveau) or droit_cible("benevoles", niveau)
            if not ok:
                write_log(f"⛔ Accès refusé emailing : niveau={niveau}")
                flash("⛔ Accès refusé", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def _connect():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------
# Données de référence
# ------------------------------------------------------------
def expediteurs_disponibles():
    """Expéditeurs validés (actifs) côté Mailjet, mis en cache 10 min."""
    if _cache_senders["liste"] and time.time() - _cache_senders["t"] < 600:
        return _cache_senders["liste"]
    liste = None
    try:
        r = requests.get(
            "https://api.mailjet.com/v3/REST/sender?Limit=200",
            auth=(os.getenv("MAILJET_API_KEY"), os.getenv("MAILJET_API_SECRET")),
            timeout=8,
        )
        r.raise_for_status()
        liste = sorted(
            s["Email"] for s in r.json().get("Data", [])
            if s.get("Status") == "Active" and "*" not in s.get("Email", "")
        )
    except Exception as e:
        write_log(f"⚠️ Emailing : liste des expéditeurs Mailjet indisponible : {e}")
    liste = liste or EXPEDITEURS_REPLI
    _cache_senders.update(t=time.time(), liste=liste)
    return liste


def champs_email_associations(conn):
    """[(nom_colonne, libellé)] des champs email de la table associations."""
    # Toutes les colonnes courriel_* (certaines n'ont pas type_champ='email'
    # dans field_groups : resp_accueil, resp_collecte, resp_proxidon...)
    noms = [r["name"] for r in conn.execute("PRAGMA table_info(associations)")
            if r["name"].lower().startswith("courriel_") and r["name"] not in CHAMPS_IGNORES]
    if "courriel_association" in noms:
        noms.remove("courriel_association")
        noms.insert(0, "courriel_association")
    return [(n, n.replace("courriel_", "").replace("_", " ").capitalize()) for n in noms]


def construire_destinataires(conn, campagne_id, champs, valides_seulement=True):
    """Reconstruit la liste des destinataires : une ligne par adresse (dédoublonnée
    sur toute la campagne). Les lignes déjà envoyées en réel sont conservées telles
    quelles ; les autres sont recréées en gardant leur case cochée/décochée."""
    deja = {r["email"].lower() for r in conn.execute(
        "SELECT email FROM emailing_destinataires WHERE campagne_id = ? "
        "AND mail_envoye_le IS NOT NULL AND mail_mode_test = 0", (campagne_id,))}
    decoches = {r["email"].lower() for r in conn.execute(
        "SELECT email FROM emailing_destinataires WHERE campagne_id = ? AND selected = 0", (campagne_id,))}
    conn.execute(
        "DELETE FROM emailing_destinataires WHERE campagne_id = ? "
        "AND NOT (mail_envoye_le IS NOT NULL AND mail_mode_test = 0)", (campagne_id,))

    colonnes_ok = {c for c, _ in champs_email_associations(conn)}
    champs = [c for c in champs if c in colonnes_ok]
    libelles = dict(champs_email_associations(conn))
    where = "WHERE LOWER(TRIM(COALESCE(validite,''))) = 'oui'" if valides_seulement else ""
    vus, nb = {}, 0
    for a in conn.execute(f"SELECT * FROM associations {where} ORDER BY nom_association COLLATE NOCASE"):
        for champ in champs:
            for email in split_emails(a[champ]):
                cle = email.lower()
                if cle in deja:
                    continue
                if cle in vus:
                    vus[cle]["fonctions"].append(libelles[champ])
                    continue
                vus[cle] = {"assoc_id": a["Id"], "nom": a["nom_association"], "email": email,
                            "fonctions": [libelles[champ]]}
    for cle, v in vus.items():
        conn.execute(
            """INSERT INTO emailing_destinataires
               (campagne_id, association_id, nom_association, fonctions, email, selected)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (campagne_id, v["assoc_id"], v["nom"], ", ".join(dict.fromkeys(v["fonctions"])),
             v["email"], 0 if cle in decoches else 1))
        nb += 1
    return nb


def _libelle_fonction(nom):
    return nom.replace("_", " ").capitalize()


def fonctions_benevoles(conn):
    """[(colonne, libellé)] des fonctions bénévoles (champs oui/non du groupe « fonction »)."""
    colonnes = {r["name"] for r in conn.execute("PRAGMA table_info(benevoles)")}
    return [(r["field_name"], _libelle_fonction(r["field_name"])) for r in conn.execute(
        "SELECT field_name FROM field_groups WHERE appli = 'benevoles' AND group_name = 'fonction' "
        "AND type_champ = 'oui_non' ORDER BY display_order") if r["field_name"] in colonnes]


def types_benevole(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT type_benevole FROM benevoles WHERE TRIM(COALESCE(type_benevole,'')) != '' "
        "ORDER BY type_benevole COLLATE NOCASE")]


def construire_destinataires_benevoles(conn, campagne_id, codes, types, tous, aucune):
    """Liste des bénévoles : tous, ou ayant « oui » dans au moins une fonction cochée (OU),
    ou n'ayant aucune fonction ; restreinte aux types cochés (si aucun type coché : tous).
    Même règles de conservation que pour les associations."""
    deja = {r["email"].lower() for r in conn.execute(
        "SELECT email FROM emailing_destinataires WHERE campagne_id = ? "
        "AND mail_envoye_le IS NOT NULL AND mail_mode_test = 0", (campagne_id,))}
    decoches = {r["email"].lower() for r in conn.execute(
        "SELECT email FROM emailing_destinataires WHERE campagne_id = ? AND selected = 0", (campagne_id,))}
    conn.execute(
        "DELETE FROM emailing_destinataires WHERE campagne_id = ? "
        "AND NOT (mail_envoye_le IS NOT NULL AND mail_mode_test = 0)", (campagne_id,))
    toutes = fonctions_benevoles(conn)
    libelles = dict(toutes)
    vus, nb = set(), 0
    for b in conn.execute("SELECT * FROM benevoles ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE"):
        if types and (b["type_benevole"] or "") not in types:
            continue
        a_fonctions = [code for code, _ in toutes if (b[code] or "").strip().lower() == "oui"]
        if not (tous or (aucune and not a_fonctions) or any(c in a_fonctions for c in codes)):
            continue
        for email in split_emails(b["email"]):
            cle = email.lower()
            if cle in deja or cle in vus:
                continue
            vus.add(cle)
            conn.execute(
                """INSERT INTO emailing_destinataires
                   (campagne_id, benevole_id, nom_association, prenom, nom, fonctions, fonctions_codes, email, selected)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (campagne_id, b["id"], f"{(b['nom'] or '').strip()} {(b['prenom'] or '').strip()}".strip(),
                 (b["prenom"] or "").strip(), (b["nom"] or "").strip(),
                 ", ".join(libelles[c] for c in a_fonctions), ",".join(a_fonctions),
                 email, 0 if cle in decoches else 1))
            nb += 1
    return nb


LIBELLES_STATUT = {
    "sent": "Délivré (ouverture non détectée)",
    "queued": "En file d'attente",
    "unknown": "Statut inconnu",
    "unsub": "Désinscrit",
    "bounce": "Rebond", "hardbounced": "Rebond définitif", "softbounced": "Rebond temporaire",
    "blocked": "Bloqué", "spam": "Classé spam", "deferred": "Remise différée",
}


def statut_ligne(d):
    """(code, libellé, couleur bootstrap) du suivi d'une ligne."""
    if d["mail_renvoi_gmail_le"] and not d["mail_renvoi_gmail_erreur"]:
        return ("gmail", "Renvoyé par Gmail", "info")
    if d["mail_erreur"]:
        return ("erreur", "Erreur d'envoi", "danger")
    if d["mail_statut_final"] in STATUTS_ECHEC:
        return ("echec", "Échec remise : " + LIBELLES_STATUT.get(d["mail_statut_final"], d["mail_statut_final"]), "danger")
    if d["mail_envoye_le"] and d["mail_mode_test"]:
        return ("test", "Envoi test", "warning")
    if d["mail_statut_final"] in ("opened", "clicked"):
        return ("lu", "Cliqué" if d["mail_statut_final"] == "clicked" else "Lu", "primary")
    if d["mail_statut_final"]:
        return ("ok", LIBELLES_STATUT.get(d["mail_statut_final"], d["mail_statut_final"]), "success")
    if d["mail_envoye_le"]:
        return ("envoye", "Envoyé (" + (d["mail_mailjet_status"] or "?") + ")", "success")
    if not d["selected"]:
        return ("decoche", "Décoché", "secondary")
    return ("attente", "À envoyer", "light text-dark border")


def _contient_lien(corps):
    return "lien_formulaire" in (corps or "")


def _campagne_ou_redirect(conn, campagne_id):
    return conn.execute("SELECT * FROM emailing_campagnes WHERE id = ?", (campagne_id,)).fetchone()


# ------------------------------------------------------------
# Pages
# ------------------------------------------------------------
@emailing_bp.route("/")
@login_required
@acces_emailing("lecture")
def liste():
    with _connect() as conn:
        campagnes = conn.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM emailing_destinataires d WHERE d.campagne_id = c.id) AS nb_dest,
                      (SELECT COUNT(*) FROM emailing_destinataires d WHERE d.campagne_id = c.id
                         AND d.mail_envoye_le IS NOT NULL AND d.mail_mode_test = 0
                         AND d.mail_erreur IS NULL) AS nb_envoyes,
                      (SELECT COUNT(*) FROM emailing_destinataires d WHERE d.campagne_id = c.id
                         AND d.mail_erreur IS NOT NULL) AS nb_erreurs
               FROM emailing_campagnes c ORDER BY c.id DESC"""
        ).fetchall()
    peut = {k: droit_cible(k, "lecture") for k in CIBLES}
    peut_ecr = {k: droit_cible(k, "ecriture") for k in CIBLES}
    campagnes = [c for c in campagnes if peut.get(c["cible"] or "associations")]
    cible = request.args.get("cible")
    if cible not in CIBLES or not peut.get(cible):
        cible = "associations" if peut["associations"] else "benevoles"
        filtrer = False
    else:
        filtrer = True
    if filtrer:
        campagnes = [c for c in campagnes if (c["cible"] or "associations") == cible]
    return render_template("emailing/liste.html", campagnes=campagnes, cible=cible, filtrer=filtrer,
                           peut=peut, peut_ecr=peut_ecr, libelles_cible=LIBELLES_CIBLE)


@emailing_bp.route("/nouvelle", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def nouvelle():
    sujet = (request.form.get("sujet") or "").strip()
    cible = request.form.get("cible") if request.form.get("cible") in CIBLES else "associations"
    if not sujet:
        flash("❌ Le sujet de la campagne est obligatoire.", "danger")
        return redirect(url_for("emailing.liste", cible=cible))
    cfg = ({"fonctions": [], "tous": 1, "aucune": 0, "types": []} if cible == "benevoles"
           else {"champs": ["courriel_association"], "valides": 1})
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO emailing_campagnes (sujet, corps, is_html, champs_email, cree_le, cree_par, cible)
               VALUES (?, '', 1, ?, ?, ?, ?)""",
            (sujet, json.dumps(cfg), datetime.now().isoformat(timespec="seconds"), current_user.email, cible))
        conn.commit()
        cid = cur.lastrowid
    return redirect(url_for("emailing.campagne", campagne_id=cid))


@emailing_bp.route("/<int:campagne_id>")
@login_required
@acces_emailing("lecture")
def campagne(campagne_id):
    with _connect() as conn:
        c = _campagne_ou_redirect(conn, campagne_id)
        if not c:
            flash("❌ Campagne introuvable.", "danger")
            return redirect(url_for("emailing.liste"))
        modeles = conn.execute(
            "SELECT id, code_modele, sujet, corps, is_html FROM modeles_emails "
            "WHERE type_periode = 'emailing' ORDER BY code_modele COLLATE NOCASE").fetchall()
        cible = c["cible"] or "associations"
        champs = champs_email_associations(conn) if cible == "associations" else []
        fonctions = fonctions_benevoles(conn) if cible == "benevoles" else []
        types_benevoles = types_benevole(conn) if cible == "benevoles" else []
        dests = conn.execute(
            "SELECT * FROM emailing_destinataires WHERE campagne_id = ? "
            "ORDER BY nom_association COLLATE NOCASE, email COLLATE NOCASE", (campagne_id,)).fetchall()
        questions = conn.execute(
            "SELECT * FROM emailing_questions WHERE campagne_id = ? ORDER BY ordre, id", (campagne_id,)).fetchall()
        reponses = {(r["destinataire_id"], r["question_id"]): r["valeur"] for r in conn.execute(
            "SELECT r.* FROM emailing_reponses r JOIN emailing_destinataires d ON d.id = r.destinataire_id "
            "WHERE d.campagne_id = ?", (campagne_id,))}
    try:
        cfg = json.loads(c["champs_email"] or "{}")
    except ValueError:
        cfg = {}
    lignes = []
    for d in dests:
        code, libelle, couleur = statut_ligne(d)
        lignes.append({"d": d, "code": code, "libelle": libelle, "couleur": couleur})
    verifs = [l["d"]["mail_statut_verifie_le"] for l in lignes if l["d"]["mail_statut_verifie_le"]]
    derniere_verif = max(verifs) if verifs else None
    nb = {"total": len(lignes), "coches": sum(1 for l in lignes if l["d"]["selected"]),
          "repondus": sum(1 for l in lignes if l["d"]["repondu_le"]),
          "envoyes_reels": sum(1 for l in lignes if l["d"]["mail_envoye_le"] and not l["d"]["mail_mode_test"])}
    for k in ("lu", "ok", "envoye", "gmail", "erreur", "echec", "test", "attente", "decoche"):
        nb[k] = sum(1 for l in lignes if l["code"] == k)
    expediteurs = expediteurs_disponibles()
    try:
        nom_org = get_organisation().get("nom") or ""
    except Exception:
        nom_org = ""
    return render_template(
        "emailing/campagne.html", c=c, modeles=modeles, champs=champs, lignes=lignes, nb=nb,
        cfg_champs=cfg.get("champs", []), cfg_valides=cfg.get("valides", 1), cible=cible,
        lib_cible="Bénévole" if cible == "benevoles" else "Association", cfg=cfg,
        fonctions=fonctions, types_benevoles=types_benevoles,
        lien_manquant=bool(c["form_actif"]) and not _contient_lien(c["corps"]),
        expediteurs=expediteurs, nom_org=nom_org, gmail_sender=GMAIL_SENDER,
        peut_ecrire=droit_cible(cible, "ecriture"), questions=questions, reponses=reponses, derniere_verif=derniere_verif,
        deja_envoye_reel=any(l["d"]["mail_envoye_le"] and not l["d"]["mail_mode_test"] for l in lignes),
    )


# ------------------------------------------------------------
# Actions
# ------------------------------------------------------------
@emailing_bp.route("/<int:campagne_id>/message", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def enregistrer_message(campagne_id):
    sujet = (request.form.get("sujet") or "").strip()
    corps = request.form.get("corps") or ""
    is_html = 1 if request.form.get("is_html") == "1" else 0
    expediteur = (request.form.get("expediteur") or "").strip()
    expediteur_nom = (request.form.get("expediteur_nom") or "").strip()
    reply_to = (request.form.get("reply_to") or "").strip()
    modele_id = request.form.get("modele_id", type=int)
    if not sujet or not corps.strip():
        flash("❌ Sujet et message sont obligatoires.", "danger")
        return redirect(url_for("emailing.campagne", campagne_id=campagne_id))
    if expediteur not in expediteurs_disponibles():
        flash("❌ Expéditeur non valide (non validé côté Mailjet).", "danger")
        return redirect(url_for("emailing.campagne", campagne_id=campagne_id))
    with _connect() as conn:
        conn.execute(
            """UPDATE emailing_campagnes SET sujet=?, corps=?, is_html=?, expediteur=?, expediteur_nom=?,
                      reply_to=?, modele_id=? WHERE id=?""",
            (sujet, corps, is_html, expediteur, expediteur_nom or None, reply_to or None, modele_id, campagne_id))
        code = (request.form.get("enregistrer_modele") or "").strip()
        if code:
            conn.execute(
                """INSERT INTO modeles_emails (code_modele, sujet, corps, type_periode, is_html, date_modification)
                   VALUES (?, ?, ?, 'emailing', ?, ?)
                   ON CONFLICT(code_modele) DO UPDATE SET sujet=excluded.sujet, corps=excluded.corps,
                      type_periode='emailing', is_html=excluded.is_html, date_modification=excluded.date_modification""",
                (code, sujet, corps, is_html, datetime.now().isoformat(timespec="seconds")))
            flash(f"✅ Modèle « {code} » enregistré.", "success")
        conn.commit()
    flash("✅ Message enregistré.", "success")
    with _connect() as conn:
        form_actif = conn.execute("SELECT form_actif FROM emailing_campagnes WHERE id = ?", (campagne_id,)).fetchone()[0]
    if form_actif and not _contient_lien(corps):
        flash("⚠️ Le formulaire est activé mais le texte ne contient pas <<lien_formulaire>> : "
              "les destinataires ne recevront aucun lien. Utilisez le bouton « Insérer le lien du formulaire ».", "warning")
    return redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#destinataires")


@emailing_bp.route("/<int:campagne_id>/destinataires", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def definir_destinataires(campagne_id):
    retour = redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#liste")
    with _connect() as conn:
        c = _campagne_ou_redirect(conn, campagne_id)
        if not c:
            return redirect(url_for("emailing.liste"))
        if (c["cible"] or "associations") == "benevoles":
            tous = request.form.get("tous_benevoles") == "1"
            aucune = request.form.get("aucune_fonction") == "1"
            codes = [f for f in request.form.getlist("fonctions") if f in dict(fonctions_benevoles(conn))]
            types = request.form.getlist("types")
            if not (tous or aucune or codes):
                flash("⚠️ Choisissez « Tous les bénévoles », « Aucune fonction » ou au moins une fonction.", "warning")
                return redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#destinataires")
            conn.execute("UPDATE emailing_campagnes SET champs_email = ? WHERE id = ?",
                         (json.dumps({"fonctions": codes, "tous": int(tous), "aucune": int(aucune), "types": types}),
                          campagne_id))
            nb = construire_destinataires_benevoles(conn, campagne_id, codes, types, tous, aucune)
            conn.commit()
            flash(f"✅ Liste construite : {nb} adresse(s) à vérifier ci-dessous.", "success")
            return retour
        champs = request.form.getlist("champs")
        valides = 1 if request.form.get("valides") == "1" else 0
        if request.form.get("tous") == "1":
            champs = [x for x, _ in champs_email_associations(conn)]
        if not champs:
            flash("⚠️ Choisissez au moins un type d'adresse email.", "warning")
            return redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#destinataires")
        conn.execute("UPDATE emailing_campagnes SET champs_email = ? WHERE id = ?",
                     (json.dumps({"champs": champs, "valides": valides}), campagne_id))
        nb = construire_destinataires(conn, campagne_id, champs, bool(valides))
        conn.commit()
    flash(f"✅ Liste construite : {nb} adresse(s) à vérifier ci-dessous.", "success")
    return retour


@emailing_bp.route("/<int:campagne_id>/selection", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def selection(campagne_id):
    data = request.get_json(silent=True) or {}
    selected = 1 if data.get("selected") else 0
    ids = [int(i) for i in data.get("ids", []) if str(i).isdigit()]
    with _connect() as conn:
        if data.get("tous"):
            conn.execute("UPDATE emailing_destinataires SET selected = ? WHERE campagne_id = ?",
                         (selected, campagne_id))
        elif ids:
            conn.execute(
                f"UPDATE emailing_destinataires SET selected = ? WHERE campagne_id = ? "
                f"AND id IN ({','.join('?' * len(ids))})", [selected, campagne_id, *ids])
        total = conn.execute(
            "SELECT COUNT(*) FROM emailing_destinataires WHERE campagne_id = ? AND selected = 1",
            (campagne_id,)).fetchone()[0]
        conn.commit()
    return jsonify({"ok": True, "coches": total})


@emailing_bp.route("/<int:campagne_id>/supprimer", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def supprimer(campagne_id):
    with _connect() as conn:
        reel = conn.execute(
            "SELECT COUNT(*) FROM emailing_destinataires WHERE campagne_id = ? "
            "AND mail_envoye_le IS NOT NULL AND mail_mode_test = 0", (campagne_id,)).fetchone()[0]
        if reel:
            flash("⛔ Campagne déjà envoyée : suppression impossible (traçabilité).", "danger")
            return redirect(url_for("emailing.campagne", campagne_id=campagne_id))
        conn.execute("DELETE FROM emailing_destinataires WHERE campagne_id = ?", (campagne_id,))
        conn.execute("DELETE FROM emailing_campagnes WHERE id = ?", (campagne_id,))
        conn.commit()
    flash("🗑️ Campagne supprimée.", "warning")
    return redirect(url_for("emailing.liste"))


@emailing_bp.route("/upload_image", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def upload_image():
    """Image collée dans l'éditeur riche : redimensionnée et hébergée (même
    dossier que les images des modèles d'emails)."""
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"error": "Aucune image reçue"}), 400
    try:
        Image.open(f).verify()
        f.seek(0)
        image = Image.open(f).convert("RGB")
    except Exception:
        return jsonify({"error": "Fichier image invalide"}), 400
    image.thumbnail((900, 900))
    dossier = os.path.join(current_app.root_path, "static", "modeles_images")
    os.makedirs(dossier, exist_ok=True)
    nom = f"{uuid.uuid4().hex}.jpg"
    image.save(os.path.join(dossier, nom), format="JPEG", quality=85)
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    return jsonify({"url": f"{base}/static/modeles_images/{nom}"})


# ------------------------------------------------------------
# Envoi Mailjet (arrière-plan)
# ------------------------------------------------------------
def _contexte(d):
    return {"nom_association": d["nom_association"] or "", "fonction": d["fonctions"] or "",
            "prenom": d["prenom"] or "", "nom": d["nom"] or ""}


def assurer_tokens(conn, campagne_id):
    """Donne un jeton personnel à chaque destinataire qui n'en a pas encore."""
    for r in conn.execute("SELECT id FROM emailing_destinataires WHERE campagne_id = ? AND token IS NULL",
                          (campagne_id,)).fetchall():
        conn.execute("UPDATE emailing_destinataires SET token = ? WHERE id = ?",
                     (secrets.token_urlsafe(16), r["id"]))
    conn.commit()


def url_formulaire(token):
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    return f"{base}/emailing/repondre/{token}"


def rendre_message(c, d):
    """(sujet, corps) personnalisés pour un destinataire : variables
    <<nom_association>>, <<fonction>> et <<lien_formulaire>> (lien personnel,
    transformé en lien cliquable dans un mail HTML)."""
    ctx = _contexte(d)
    sujet = render_modele_email(c["sujet"], ctx).strip()
    # L'éditeur riche (Quill) enregistre « << >> » sous la forme &lt;&lt; &gt;&gt;
    corps_brut = re.sub(r"&lt;&lt;(\w+)&gt;&gt;", r"<<\1>>", c["corps"] or "")
    corps = render_modele_email(corps_brut, ctx)
    lien = url_formulaire(d["token"]) if (c["form_actif"] and d["token"]) else ""
    if "<<lien_formulaire>>" in corps:
        if c["is_html"] and lien:
            # déjà dans un href → URL brute ; sinon lien cliquable
            corps = re.sub(r'(?<!href=")(?<!href=\')<<lien_formulaire>>',
                           f'<a href="{lien}">{lien}</a>', corps)
        corps = corps.replace("<<lien_formulaire>>", lien)
    return sujet, corps


def envoyer_background(app, db_path, campagne_id, ids, mode_test, mail_test_to, user_email, prefixe=""):
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.execute("SELECT * FROM emailing_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        ok, erreurs, nb_test = 0, 0, 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        assurer_tokens(conn, campagne_id)
        for did in ids:
            d = conn.execute("SELECT * FROM emailing_destinataires WHERE id = ?", (did,)).fetchone()
            if not d:
                continue
            if mode_test:
                if nb_test >= 2:
                    break
                nb_test += 1
            sujet, corps = rendre_message(c, d)
            if prefixe:
                sujet = f"{prefixe}{sujet}"
            try:
                resultat = envoyer_mail(
                    sujet=sujet, destinataires=[d["email"]], texte=corps,
                    sender_override=c["expediteur"], sender_name=c["expediteur_nom"],
                    reply_to=c["reply_to"], is_html=bool(c["is_html"]),
                    mode_test=bool(mode_test), current_user_email=user_email,
                )
                mj_status, mj_ids = None, None
                if resultat and resultat.get("Messages"):
                    m = resultat["Messages"][0]
                    mj_status = m.get("Status")
                    mj_ids = ",".join(str(t["MessageID"]) for t in m.get("To", []) if "MessageID" in t) or None
                conn.execute(
                    """UPDATE emailing_destinataires SET mail_envoye_le=?, mail_mode_test=?, mail_erreur=NULL,
                         mail_mailjet_status=?, mail_mailjet_message_ids=?, mail_statut_final=NULL,
                         mail_statut_verifie_le=NULL, mail_renvoi_gmail_le=NULL, mail_renvoi_gmail_erreur=NULL,
                         nb_envois = CASE WHEN ? = 1 THEN COALESCE(nb_envois,0) ELSE COALESCE(nb_envois,0) + 1 END,
                         premier_envoi_le = CASE WHEN ? = 1 THEN premier_envoi_le ELSE COALESCE(premier_envoi_le, ?) END
                       WHERE id=?""",
                    (now_iso, 1 if mode_test else 0, mj_status, mj_ids,
                     1 if mode_test else 0, 1 if mode_test else 0, now_iso, did))
                ok += 1
            except Exception as e:
                write_log(f"❌ Emailing campagne {campagne_id} → {d['email']} : {e}")
                conn.execute(
                    "UPDATE emailing_destinataires SET mail_envoye_le=?, mail_mode_test=?, mail_erreur=? WHERE id=?",
                    (now_iso, 1 if mode_test else 0, str(e)[:500], did))
                erreurs += 1
            conn.commit()
        conn.execute(
            """UPDATE emailing_campagnes SET dernier_envoi_le=?, dernier_envoi_par=?, dernier_envoi_mode_test=?,
                      dernier_envoi_nb_ok=?, dernier_envoi_nb_erreur=? WHERE id=?""",
            (now_iso, user_email, 1 if mode_test else 0, ok, erreurs, campagne_id))
        conn.commit()
        conn.close()
        upload_database()
        write_log(f"📤 Emailing campagne {campagne_id} terminé : {ok} envoyé(s), {erreurs} erreur(s)")


@emailing_bp.route("/<int:campagne_id>/envoyer", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def envoyer(campagne_id):
    retour = redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#suivi")
    mode_test = request.form.get("mode_test") == "1"
    with _connect() as conn:
        c = _campagne_ou_redirect(conn, campagne_id)
        if not c or not c["expediteur"] or not (c["corps"] or "").strip():
            flash("❌ Enregistrez d'abord le message et l'expéditeur.", "danger")
            return retour
        # À envoyer : cochées, jamais envoyées en réel avec succès
        ids = [r["id"] for r in conn.execute(
            """SELECT id FROM emailing_destinataires WHERE campagne_id = ? AND selected = 1
                 AND (mail_envoye_le IS NULL OR mail_mode_test = 1 OR mail_erreur IS NOT NULL)
               ORDER BY nom_association COLLATE NOCASE, email""", (campagne_id,))]
    if not ids:
        flash("ℹ️ Rien à envoyer : aucune ligne cochée non encore envoyée.", "warning")
        return retour
    lancer_tache_fond(
        target=envoyer_background,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, ids, mode_test,
              adresse_test_utilisateur(), current_user.email),
        nom="Emailing associations", utilisateur=current_user.email,
    )
    if mode_test:
        flash("🧪 Envoi TEST lancé (2 mails maximum, vers votre adresse).", "warning")
    else:
        flash(f"🚀 Envoi réel lancé en arrière-plan pour {len(ids)} adresse(s) — actualisez la page dans quelques instants.", "info")
    return retour


PREFIXES_RENVOI = {"": "", "rappel": "Rappel : ", "maj": "Mise à jour : "}


@emailing_bp.route("/<int:campagne_id>/renvoyer_campagne", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def renvoyer_campagne(campagne_id):
    """Nouvel envoi à TOUS les cochés, y compris ceux déjà servis (ex. message
    corrigé, formulaire ajouté après coup). Le compteur nb_envois garde la trace."""
    retour = redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#suivi")
    mode_test = request.form.get("mode_test") == "1"
    prefixe = PREFIXES_RENVOI.get(request.form.get("prefixe", ""), "")
    with _connect() as conn:
        c = _campagne_ou_redirect(conn, campagne_id)
        if not c or not c["expediteur"] or not (c["corps"] or "").strip():
            flash("❌ Enregistrez d'abord le message et l'expéditeur.", "danger")
            return retour
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM emailing_destinataires WHERE campagne_id = ? AND selected = 1 "
            "ORDER BY nom_association COLLATE NOCASE, email", (campagne_id,))]
    if not ids:
        flash("ℹ️ Aucune ligne cochée à renvoyer.", "warning")
        return retour
    lancer_tache_fond(
        target=envoyer_background,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, ids, mode_test,
              adresse_test_utilisateur(), current_user.email, prefixe),
        nom="Emailing — renvoi de la campagne", utilisateur=current_user.email)
    flash("🧪 Renvoi TEST lancé (2 mails maximum, vers votre adresse)." if mode_test
          else f"🔁 Renvoi de la campagne lancé en arrière-plan pour {len(ids)} adresse(s) cochée(s).",
          "warning" if mode_test else "info")
    return retour


STATUTS_DEFINITIFS = {"clicked", "bounce", "hardbounced", "softbounced", "blocked", "spam", "unsub"}


def verifier_statut_background(app, db_path, campagne_id):
    """Interroge Mailjet adresse par adresse (long : ~1 s par mail) et
    enregistre chaque statut au fur et à mesure."""
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        lignes = conn.execute(
            """SELECT id, mail_mailjet_message_ids, mail_statut_final FROM emailing_destinataires
               WHERE campagne_id = ? AND mail_mailjet_message_ids IS NOT NULL AND mail_mailjet_message_ids != ''""",
            (campagne_id,)).fetchall()
        counts, verifies = {}, 0
        for l in lignes:
            if l["mail_statut_final"] in STATUTS_DEFINITIFS:
                continue
            statut = mailjet_get_message_status(l["mail_mailjet_message_ids"].split(",")[0])
            if not statut:
                continue
            verifies += 1
            counts[statut] = counts.get(statut, 0) + 1
            conn.execute(
                "UPDATE emailing_destinataires SET mail_statut_final = ?, mail_statut_verifie_le = ? WHERE id = ?",
                (statut, datetime.now().isoformat(timespec="seconds"), l["id"]))
            conn.commit()
        conn.close()
        upload_database()
        write_log(f"🔄 Emailing campagne {campagne_id} — statuts Mailjet vérifiés : {verifies} ({counts})")


@emailing_bp.route("/<int:campagne_id>/verifier_statut", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def verifier_statut(campagne_id):
    with _connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM emailing_destinataires WHERE campagne_id = ? "
            "AND mail_mailjet_message_ids IS NOT NULL AND mail_mailjet_message_ids != ''", (campagne_id,)).fetchone()[0]
    if not n:
        flash("ℹ️ Aucun mail envoyé à vérifier.", "warning")
    else:
        lancer_tache_fond(
            target=verifier_statut_background,
            args=(current_app._get_current_object(), get_db_path(), campagne_id),
            nom="Emailing associations — vérification statuts", utilisateur=current_user.email)
        flash(f"🔄 Vérification des statuts Mailjet lancée en arrière-plan ({n} mail(s)) : "
              "actualisez la page dans quelques instants.", "info")
    return redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#suivi")


# ------------------------------------------------------------
# Renvoi Gmail
# ------------------------------------------------------------
def _renvoyer_gmail_ligne(conn, c, d, user_email=None):
    assurer_tokens(conn, c["id"])
    d = conn.execute("SELECT * FROM emailing_destinataires WHERE id = ?", (d["id"],)).fetchone()
    sujet, corps = rendre_message(c, d)
    now_iso = datetime.now().isoformat(timespec="seconds")
    try:
        envoyer_mail_gmail(sujet, [d["email"]], corps, sender=GMAIL_SENDER,
                           reply_to=c["reply_to"] or (c["expediteur"] if c["expediteur"] != GMAIL_SENDER else None),
                           is_html=bool(c["is_html"]))
        conn.execute("UPDATE emailing_destinataires SET mail_renvoi_gmail_le=?, mail_renvoi_gmail_erreur=NULL WHERE id=?",
                     (now_iso, d["id"]))
        conn.commit()
        return True, None
    except GmailSendError as e:
        conn.execute("UPDATE emailing_destinataires SET mail_renvoi_gmail_erreur=? WHERE id=?",
                     (str(e)[:500], d["id"]))
        conn.commit()
        return False, str(e)


def renvoyer_gmail_background(app, db_path, campagne_id, ids, user_email):
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.execute("SELECT * FROM emailing_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        ok = ko = 0
        for did in ids:
            d = conn.execute("SELECT * FROM emailing_destinataires WHERE id = ?", (did,)).fetchone()
            if not d:
                continue
            reussi, _ = _renvoyer_gmail_ligne(conn, c, d, user_email)
            ok += reussi
            ko += (not reussi)
        conn.close()
        upload_database()
        write_log(f"📤 Emailing campagne {campagne_id} — renvoi Gmail : {ok} ok, {ko} erreur(s)")


def _en_echec(d):
    return bool(d["mail_erreur"] or d["mail_statut_final"] in STATUTS_ECHEC) and not d["mail_mode_test"]


@emailing_bp.route("/<int:campagne_id>/renvoyer_gmail", methods=["POST"])
@login_required
@acces_emailing("ecriture")
def renvoyer_gmail(campagne_id):
    retour = redirect(url_for("emailing.campagne", campagne_id=campagne_id) + "#suivi")
    dest_id = request.form.get("dest_id", type=int)
    with _connect() as conn:
        c = _campagne_ou_redirect(conn, campagne_id)
        if not c:
            return retour
        if dest_id:
            d = conn.execute("SELECT * FROM emailing_destinataires WHERE id = ? AND campagne_id = ?",
                             (dest_id, campagne_id)).fetchone()
            if not d:
                flash("❌ Ligne introuvable.", "danger")
                return retour
            ok, err = _renvoyer_gmail_ligne(conn, c, d, current_user.email)
            flash(f"✅ Renvoyé par Gmail à {d['email']}." if ok else f"❌ Échec Gmail : {err}",
                  "success" if ok else "danger")
            return retour
        ids = [d["id"] for d in conn.execute(
            "SELECT * FROM emailing_destinataires WHERE campagne_id = ? ORDER BY nom_association COLLATE NOCASE",
            (campagne_id,)) if _en_echec(d) and not (d["mail_renvoi_gmail_le"] and not d["mail_renvoi_gmail_erreur"])]
    if not ids:
        flash("ℹ️ Aucune ligne en échec à renvoyer par Gmail.", "warning")
        return retour
    lancer_tache_fond(
        target=renvoyer_gmail_background,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, ids, current_user.email),
        nom="Emailing associations — renvoi Gmail", utilisateur=current_user.email,
    )
    flash(f"🔁 Renvoi Gmail lancé en arrière-plan pour {len(ids)} adresse(s).", "info")
    return retour
