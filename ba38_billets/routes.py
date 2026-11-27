# ============================================================
# 🎟️ Billets spectacle — écran de gestion (un écran par campagne)
#   1. fiche (donateur, lieu, spectacle, date/heure, retrait, nb billets,
#      date limite) → 2. message d'invitation (texte riche + image)
#   → 3. bénévoles à jour de cotisation (liste décochable) + envoi
#   → 4. inscriptions (formulaire par lien personnel) → 5. tirage au sort
#   après la date limite (inscrits, puis salariés pour les places restantes)
#   → 6. mails de résultat.
# ============================================================

import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

from flask import (
    current_app, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required
from PIL import Image

from ba38_billets import attribution, billets_bp
from ba38_billets import mails
from ba38_utilitaires.core import (
    adresse_test_utilisateur, get_db_path, has_access, split_emails, upload_database, write_log,
)
from ba38_utilitaires.taches_fond import lancer_tache_fond

TEXTE_PAR_DEFAUT = (
    "<p>Bonjour &lt;&lt;prenom&gt;&gt;,</p>"
    "<p>&lt;&lt;donateur&gt;&gt; offre à nos bénévoles <b>&lt;&lt;nb_billets&gt;&gt; place(s)</b> pour le spectacle "
    "<b>&lt;&lt;spectacle&gt;&gt;</b>, &lt;&lt;lieu&gt;&gt;, le &lt;&lt;date_spectacle&gt;&gt; à &lt;&lt;heure&gt;&gt;.</p>"
    "<p>Si vous souhaitez participer, inscrivez-vous en cliquant sur le bouton ci-dessous "
    "avant le <b>&lt;&lt;date_limite&gt;&gt;</b> :</p>"
    "<p>&lt;&lt;lien_inscription&gt;&gt;</p>"
    "<p>Si les inscrits sont plus nombreux que les places, un tirage au sort sera effectué après cette date "
    "et vous serez averti(e) du résultat par mail.</p>"
    "<p>Retrait des billets : &lt;&lt;mode_retrait&gt;&gt;</p>"
    "<p>Cordialement,<br>La Banque Alimentaire</p>")


def acces(niveau):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not has_access("billets", niveau):
                write_log(f"⛔ Accès refusé billets : niveau={niveau}")
                flash("⛔ Accès refusé", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


TEXTE_PREMIER_DEFAUT = (
    "<p>Bonjour &lt;&lt;prenom&gt;&gt;,</p>"
    "<p>&lt;&lt;donateur&gt;&gt; offre à ses bénévoles et salariés <b>&lt;&lt;nb_billets&gt;&gt; place(s)</b> pour le "
    "spectacle <b>&lt;&lt;spectacle&gt;&gt;</b>, &lt;&lt;lieu&gt;&gt;, le &lt;&lt;date_spectacle&gt;&gt; à &lt;&lt;heure&gt;&gt;.</p>"
    "<p>Les places sont attribuées <b>dans l'ordre des inscriptions (premier inscrit, premier servi)</b>. "
    "Pour réserver la vôtre, cliquez sur le bouton ci-dessous avant le <b>&lt;&lt;date_limite&gt;&gt;</b> :</p>"
    "<p>&lt;&lt;lien_inscription&gt;&gt;</p>"
    "<p>Quand toutes les places sont prises, les inscriptions suivantes sont placées en liste d'attente et "
    "servies en cas de désistement.</p>"
    "<p>Retrait des billets : &lt;&lt;mode_retrait&gt;&gt;</p>"
    "<p>Cordialement,<br>La Banque Alimentaire</p>")


def _connect():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    mails.ensure_schema(conn)  # auto-réparation : colonnes ajoutées après la 1re version
    return conn


def _campagne(conn, cid):
    return conn.execute("SELECT * FROM billets_campagnes WHERE id = ?", (cid,)).fetchone()


def _retour(cid, ancre=""):
    return redirect(url_for("billets.campagne", campagne_id=cid) + ancre)


EXPEDITEUR = mails.EXPEDITEUR_DEFAUT  # expéditeur imposé (validé chez Mailjet)


def utilisateurs_basilic():
    """Comptes Basilic actifs (hors comptes de test) pour la liste « adresse de réponse »."""
    with _connect() as conn:
        return conn.execute(
            "SELECT email, username FROM users WHERE actif = 1 AND COALESCE(test_only,0) = 0 "
            "AND TRIM(COALESCE(email,'')) != '' ORDER BY LOWER(email)").fetchall()


def _dt(valeur):
    """Valeur d'un champ datetime-local (« 2026-11-20T23:59 ») ou date seule."""
    v = (valeur or "").strip()
    if len(v) == 10:
        v += "T23:59"
    return v[:16] or None


# ------------------------------------------------------------
# Liste + création
# ------------------------------------------------------------
@billets_bp.route("/")
@login_required
@acces("lecture")
def liste():
    with _connect() as conn:
        campagnes = conn.execute(
            """SELECT c.*,
                 (SELECT COUNT(*) FROM billets_participants p WHERE p.campagne_id=c.id AND p.categorie='benevole' AND p.selected=1) AS nb_invites,
                 (SELECT COUNT(*) FROM billets_participants p WHERE p.campagne_id=c.id AND p.categorie='benevole' AND p.etat='inscrit') AS nb_inscrits,
                 (SELECT COUNT(*) FROM billets_participants p WHERE p.campagne_id=c.id AND p.resultat='gagnant') AS nb_gagnants
               FROM billets_campagnes c ORDER BY c.date_spectacle DESC, c.id DESC""").fetchall()
    return render_template("billets/liste.html", campagnes=campagnes, peut_ecrire=has_access("billets", "ecriture"),
                           limite_fr=mails.limite_fr, date_fr=mails.date_fr, limite_depassee=mails.limite_depassee)


def _date_iso(texte):
    """Accepte jj/mm/aaaa (champ .date-fr) ou aaaa-mm-jj ; renvoie aaaa-mm-jj, ou None."""
    texte = (texte or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texte, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _limite(f):
    """Date limite : date jj/mm/aaaa (champ .date-fr) + heure (défaut 23:59) → « aaaa-mm-jjTHH:MM », ou None."""
    d = _date_iso(f.get("date_limite"))
    if not d:
        return None
    h = (f.get("heure_limite") or "").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", h):
        h = "23:59"
    return f"{d}T{h.zfill(5)}"


def _champs_fiche(f):
    return (
        (f.get("donateur") or "").strip(), (f.get("lieu") or "").strip(), (f.get("spectacle") or "").strip(),
        _date_iso(f.get("date_spectacle")), (f.get("heure_spectacle") or "").strip() or None,
        (f.get("mode_retrait") or "").strip(), max(1, f.get("nb_billets", type=int) or 1),
        _limite(f))


@billets_bp.route("/nouvelle", methods=["POST"])
@login_required
@acces("ecriture")
def nouvelle():
    donateur, lieu, spectacle, d, h, retrait, nb, limite = _champs_fiche(request.form)
    if not (donateur and spectacle and d and limite):
        flash("❌ Donateur, spectacle, date du spectacle (jj/mm/aaaa) et date limite d'inscription sont obligatoires.", "danger")
        return redirect(url_for("billets.liste"))
    mode = "premier" if request.form.get("mode") == "premier" else "tirage"
    regle = 1 if request.form.get("regle_12_mois") == "1" else 0
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO billets_campagnes (donateur, lieu, spectacle, date_spectacle, heure_spectacle,
                 mode_retrait, nb_billets, date_limite, sujet, corps, statut, cree_le, cree_par, mode, regle_12_mois)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'ouverte', ?, ?, ?, ?)""",
            (donateur, lieu, spectacle, d, h, retrait, nb, limite,
             "Places offertes : <<spectacle>>", TEXTE_PREMIER_DEFAUT if mode == "premier" else TEXTE_PAR_DEFAUT,
             mails.maintenant(), current_user.email, mode, regle))
        conn.commit()
        cid = cur.lastrowid
    return redirect(url_for("billets.campagne", campagne_id=cid))


# ------------------------------------------------------------
# Écran de campagne
# ------------------------------------------------------------
@billets_bp.route("/<int:campagne_id>")
@login_required
@acces("lecture")
def campagne(campagne_id):
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c:
            flash("❌ Campagne introuvable.", "danger")
            return redirect(url_for("billets.liste"))
        parts = conn.execute(
            "SELECT * FROM billets_participants WHERE campagne_id = ? "
            "ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE", (campagne_id,)).fetchall()
        gains = mails.gains_recents(conn, campagne_id)
    premier = mails.est_premier(c)
    annulee = c["statut"] == "annulee"
    if premier:  # bénévoles ET salariés sont invités et s'inscrivent
        benevoles, salaries = list(parts), []
    else:
        benevoles = [p for p in parts if p["categorie"] == "benevole"]
        salaries = [p for p in parts if p["categorie"] == "salarie"]
    inscrits = [p for p in benevoles if p["etat"] == "inscrit"]
    nb = {
        "invites": sum(1 for p in benevoles if p["selected"]),
        "envoyes": sum(1 for p in benevoles if p["invite_le"] and not p["invite_mode_test"] and not p["invite_erreur"]),
        "erreurs": sum(1 for p in benevoles if p["invite_erreur"]),
        "inscrits": len(inscrits),
        "desistes": sum(1 for p in benevoles if p["etat"] == "desiste"),
        "gagnants": sum(1 for p in parts if p["resultat"] == "gagnant"),
        "gagnants_inscrits": sum(1 for p in parts if p["resultat"] == "gagnant" and p["origine_place"] == "inscrit"),
        "gagnants_salaries": sum(1 for p in parts if p["resultat"] == "gagnant" and p["origine_place"] == "salarie"),
        "salaries_coches": sum(1 for p in salaries if p["selected"]),
        "resultats_envoyes": sum(1 for p in parts if p["resultat_envoye_le"]),
        "annul_prevenus": sum(1 for p in parts if p["annulation_envoyee_le"]),
        "annul_inscrits": sum(1 for p in parts if p["etat"] == "inscrit" and not p["annulation_envoyee_le"]),
        "annul_invites": sum(1 for p in parts if p["etat"] == "invite" and p["selected"] and p["invite_le"]
                             and not p["invite_mode_test"] and not p["invite_erreur"] and not p["annulation_envoyee_le"]),
        "resultats_a_envoyer": sum(1 for p in parts if p["resultat"] and p["resultat"] != "desiste"
                                  and not p["resultat_envoye_le"]),
    }
    return render_template(
        "billets/campagne.html", c=c, benevoles=benevoles, salaries=salaries, inscrits=inscrits, nb=nb,
        gagnants=sorted([p for p in parts if p["resultat"] == "gagnant"], key=lambda p: p["ordre_tirage"] or 0),
        non_retenus=[p for p in parts if p["resultat"] == "non_retenu"],
        non_eligibles=[p for p in parts if p["resultat"] == "non_eligible"],
        desistes=[p for p in parts if p["resultat"] == "desiste"],
        premier=premier, regle12=mails.regle_12_mois(c),
        attente=sorted([p for p in parts if p["resultat"] == "attente"], key=lambda p: (p["ordre_tirage"] or 0, p["id"])),
        cles_textes=mails.cles_textes(c),
        gains=gains,
        depassee=mails.limite_depassee(c), annulee=annulee,
        tiree=(c["statut"] == "tiree" or (annulee and bool(c["tirage_le"]))),
        fige=(c["statut"] in ("tiree", "annulee")),
        peut_ecrire=has_access("billets", "ecriture"), utilisateurs=utilisateurs_basilic(),
        variables=mails.VARIABLES_AIDE, limite_fr=mails.limite_fr, date_fr=mails.date_fr,
        modele_defaut=TEXTE_PAR_DEFAUT, textes_mails=mails.textes_campagne(c),
        textes_defaut={k: {"sujet": v["sujet"], "corps": v["corps"]} for k, v in mails.TEXTES_MAILS.items()})


@billets_bp.route("/<int:campagne_id>/fiche", methods=["POST"])
@login_required
@acces("ecriture")
def enregistrer_fiche(campagne_id):
    donateur, lieu, spectacle, d, h, retrait, nb, limite = _champs_fiche(request.form)
    if not (donateur and spectacle and d and limite):
        flash("❌ Donateur, spectacle, date du spectacle (jj/mm/aaaa) et date limite sont obligatoires.", "danger")
        return _retour(campagne_id)
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if c and c["statut"] == "tiree" and nb != c["nb_billets"]:
            flash("❌ Le tirage est fait : le nombre de billets ne peut plus changer.", "danger")
            return _retour(campagne_id)
        if c and mails.est_premier(c) and nb < attribution.nb_gagnants(conn, campagne_id):
            flash(f"❌ {attribution.nb_gagnants(conn, campagne_id)} places sont déjà attribuées : "
                  "désinscrivez d'abord des personnes pour réduire le nombre de billets.", "danger")
            return _retour(campagne_id)
        regle = 1 if request.form.get("regle_12_mois") == "1" else 0
        conn.execute(
            """UPDATE billets_campagnes SET donateur=?, lieu=?, spectacle=?, date_spectacle=?, heure_spectacle=?,
                 mode_retrait=?, nb_billets=?, date_limite=?, regle_12_mois=? WHERE id=?""",
            (donateur, lieu, spectacle, d, h, retrait, nb, limite, regle, campagne_id))
        if c and mails.regle_12_mois(c) != bool(regle):
            flash("ℹ️ Règle des 12 mois " + ("activée : cliquez sur « Actualiser la liste » (étape 3) pour décocher les personnes concernées."
                  if regle else "désactivée : recochez, si besoin, les personnes précédemment décochées (étape 3)."), "info")
        promus = []
        if c and mails.est_premier(c):  # plus de billets : la liste d'attente avance
            promus = attribution.promouvoir(conn, _campagne(conn, campagne_id))
        conn.commit()
        if promus:
            attribution.prevenir_promus(conn, _campagne(conn, campagne_id), promus)
            flash(f"ℹ️ {len(promus)} personne(s) de la liste d'attente ont obtenu une place et sont prévenues par mail.", "info")
    flash("✅ Fiche enregistrée.", "success")
    return _retour(campagne_id)


@billets_bp.route("/<int:campagne_id>/message", methods=["POST"])
@login_required
@acces("ecriture")
def enregistrer_message(campagne_id):
    sujet = (request.form.get("sujet") or "").strip()
    corps = request.form.get("corps") or ""
    reply_to = (request.form.get("reply_to") or "").strip()
    if not sujet or not corps.strip():
        flash("❌ Sujet et message sont obligatoires.", "danger")
        return _retour(campagne_id, "#message")
    if reply_to:
        with _connect() as conn:
            c0 = _campagne(conn, campagne_id)
        autorises = {u["email"].lower() for u in utilisateurs_basilic()}
        if reply_to.lower() not in autorises and reply_to != (c0["reply_to"] if c0 else None):
            flash("❌ L'adresse de réponse doit être celle d'un utilisateur Basilic.", "danger")
            return _retour(campagne_id, "#message")
    if "lien_inscription" not in corps:
        flash("⚠️ Le texte ne contient pas <<lien_inscription>> : les bénévoles n'auront aucun lien pour s'inscrire.",
              "warning")
    with _connect() as conn:
        conn.execute(
            "UPDATE billets_campagnes SET sujet=?, corps=?, expediteur=?, expediteur_nom=?, reply_to=? WHERE id=?",
            (sujet, corps, EXPEDITEUR, (request.form.get("expediteur_nom") or "").strip() or None,
             reply_to or None, campagne_id))
        conn.commit()
    flash("✅ Message enregistré.", "success")
    return _retour(campagne_id, "#destinataires")


@billets_bp.route("/<int:campagne_id>/textes_mails", methods=["POST"])
@login_required
@acces("ecriture")
def enregistrer_textes_mails(campagne_id):
    """Enregistre les textes des mails de confirmation, désistement et résultat."""
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
    try:
        textes = json.loads(c["textes_mails"] or "{}") if c else {}
    except ValueError:
        textes = {}
    for cle in mails.cles_textes(c):  # seuls les textes du mode de la campagne sont modifiés
        d = mails.TEXTES_MAILS[cle]
        sujet = (request.form.get(f"sujet_{cle}") or "").strip()
        corps = request.form.get(f"corps_{cle}") or ""
        if not sujet or not re.sub(r"<[^>]+>|&nbsp;|\s", "", corps):
            flash(f"❌ Sujet et texte obligatoires : « {d['libelle']} ».", "danger")
            return _retour(campagne_id, "#textes")
        textes[cle] = {"sujet": sujet[:200], "corps": corps}
    with _connect() as conn:
        conn.execute("UPDATE billets_campagnes SET textes_mails=? WHERE id=?",
                     (json.dumps(textes, ensure_ascii=False), campagne_id))
        conn.commit()
    flash("✅ Textes des mails enregistrés.", "success")
    return _retour(campagne_id, "#textes")


@billets_bp.route("/upload_image", methods=["POST"])
@login_required
@acces("ecriture")
def upload_image():
    """Image collée dans l'éditeur riche (même dossier que l'emailing)."""
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
# Destinataires
# ------------------------------------------------------------
def construire_participants(conn, campagne_id):
    """Synchronise la liste avec la base bénévoles : bénévoles (non salariés) à jour de
    cotisation 2026 avec une adresse mail → invités ; salariés avec mail → réserve du
    tirage complémentaire. Conserve les lignes déjà engagées (inscription, envoi, résultat)."""
    existants = {r["benevole_id"]: r for r in conn.execute(
        "SELECT * FROM billets_participants WHERE campagne_id = ?", (campagne_id,))}
    voulus, ajoutes = set(), 0
    gains = mails.gains_recents(conn, campagne_id)
    ids_a_jour = {r[0] for r in conn.execute(
        "SELECT id_benevole FROM benevoles_cotisations WHERE annee = 2026")}
    for b in conn.execute("SELECT * FROM benevoles ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE"):
        salarie = (b["type_benevole"] or "").strip().lower() == "salarie"
        a_jour = b["id"] in ids_a_jour
        if not salarie and not a_jour:
            continue
        emails = split_emails(b["email"])
        if not emails:
            continue
        voulus.add(b["id"])
        if b["id"] in existants:
            continue
        conn.execute(
            """INSERT INTO billets_participants (campagne_id, benevole_id, prenom, nom, email, categorie, token)
               VALUES (?,?,?,?,?,?,?)""",
            (campagne_id, b["id"], (b["prenom"] or "").strip(), (b["nom"] or "").strip(), emails[0],
             "salarie" if salarie else "benevole", secrets.token_urlsafe(16)))
        if b["id"] in gains:  # a déjà gagné dans les 12 derniers mois : décoché d'office
            conn.execute("UPDATE billets_participants SET selected = 0 WHERE campagne_id = ? AND benevole_id = ?",
                         (campagne_id, b["id"]))
        ajoutes += 1
    for bid, r in existants.items():  # lignes pas encore engagées d'un bénévole devenu non éligible
        if bid in gains and not r["invite_le"] and r["etat"] == "invite":
            conn.execute("UPDATE billets_participants SET selected = 0 WHERE id = ?", (r["id"],))
    retires = 0
    for bid, r in existants.items():
        engage = r["invite_le"] or r["etat"] != "invite" or r["resultat"]
        if bid not in voulus and not engage and r["benevole_id"] is not None:
            conn.execute("DELETE FROM billets_participants WHERE id = ?", (r["id"],))
            retires += 1
    return ajoutes, retires


@billets_bp.route("/<int:campagne_id>/destinataires", methods=["POST"])
@login_required
@acces("ecriture")
def definir_destinataires(campagne_id):
    with _connect() as conn:
        ajoutes, retires = construire_participants(conn, campagne_id)
        conn.commit()
    flash(f"✅ Liste mise à jour : {ajoutes} ajouté(s), {retires} retiré(s).", "success")
    return _retour(campagne_id, "#destinataires")


@billets_bp.route("/<int:campagne_id>/selection", methods=["POST"])
@login_required
@acces("ecriture")
def selection(campagne_id):
    d = request.get_json(silent=True) or {}
    ids = [int(i) for i in d.get("ids", []) if str(i).isdigit()][:1000]
    if not ids:
        return jsonify({"ok": True})
    with _connect() as conn:
        conn.executemany("UPDATE billets_participants SET selected=? WHERE id=? AND campagne_id=?",
                         [(1 if d.get("selected") else 0, i, campagne_id) for i in ids])
        conn.commit()
    return jsonify({"ok": True})


@billets_bp.route("/<int:campagne_id>/inscription_manuelle", methods=["POST"])
@login_required
@acces("ecriture")
def inscription_manuelle(campagne_id):
    """Le gestionnaire inscrit / désinscrit un bénévole à sa place (ex. demande par téléphone)."""
    pid = request.form.get("pid", type=int)
    action = request.form.get("action")
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c or c["statut"] in ("tiree", "annulee"):
            flash("❌ Les inscriptions sont closes (tirage fait ou spectacle annulé).", "danger")
            return _retour(campagne_id, "#inscriptions")
        premier = mails.est_premier(c)
        if action == "desinscrire" and premier:
            promus = attribution.desinscrire(conn, c, pid)
            conn.commit()
            attribution.prevenir_promus(conn, c, promus)
            if promus:
                flash(f"ℹ️ {len(promus)} personne(s) de la liste d'attente ont obtenu une place et sont prévenues par mail.", "info")
            return _retour(campagne_id, "#inscriptions")
        if action == "inscrire":
            p0 = conn.execute("SELECT * FROM billets_participants WHERE id=? AND campagne_id=?",
                              (pid, campagne_id)).fetchone()
            gain = mails.gains_recents(conn, campagne_id).get(p0["benevole_id"]) if p0 else None
            if gain:
                flash(f"❌ Ce bénévole a déjà gagné une place le {mails.date_fr(gain, False)} : un seul gain par "
                      f"période de 12 mois.", "danger")
                return _retour(campagne_id, "#inscriptions")
            if premier:
                if p0:
                    res = attribution.inscrire(conn, c, p0, par=current_user.email)
                    flash("✅ Inscrit : place attribuée." if res == "gagnant" else
                          "⏳ Inscrit en liste d'attente (toutes les places sont prises).", "info")
            else:
                conn.execute("UPDATE billets_participants SET etat='inscrit', inscrit_le=?, inscrit_par=? "
                             "WHERE id=? AND campagne_id=? AND categorie='benevole'",
                             (mails.maintenant(), current_user.email, pid, campagne_id))
        elif action == "desinscrire":
            conn.execute("UPDATE billets_participants SET etat='desiste' WHERE id=? AND campagne_id=?",
                         (pid, campagne_id))
        conn.commit()
    return _retour(campagne_id, "#inscriptions")


# ------------------------------------------------------------
# Envois (arrière-plan) : invitations, relance, résultats
# ------------------------------------------------------------
def envoyer_invitations_bg(app, db_path, campagne_id, ids, mode_test, user_email, prefixe=""):
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.execute("SELECT * FROM billets_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        ok = erreurs = nb_test = 0
        for pid in ids:
            p = conn.execute("SELECT * FROM billets_participants WHERE id = ?", (pid,)).fetchone()
            if not p:
                continue
            if mode_test:
                if nb_test >= 2:
                    break
                nb_test += 1
            sujet, corps = mails.rendre_invitation(c, p, prefixe)
            now = mails.maintenant()
            try:
                mails.envoyer(c, p["email"], sujet, corps, mode_test=mode_test, user_email=user_email)
                conn.execute(
                    """UPDATE billets_participants SET invite_le=?, invite_mode_test=?, invite_erreur=NULL,
                         nb_invitations = COALESCE(nb_invitations,0) + CASE WHEN ?=1 THEN 0 ELSE 1 END WHERE id=?""",
                    (now, 1 if mode_test else 0, 1 if mode_test else 0, pid))
                ok += 1
            except Exception as e:
                write_log(f"❌ Billets campagne {campagne_id} → {p['email']} : {e}")
                conn.execute("UPDATE billets_participants SET invite_le=?, invite_mode_test=?, invite_erreur=? WHERE id=?",
                             (now, 1 if mode_test else 0, str(e)[:400], pid))
                erreurs += 1
            conn.commit()
        conn.close()
        upload_database()
        write_log(f"📤 Billets campagne {campagne_id} : {ok} invitation(s), {erreurs} erreur(s)")


@billets_bp.route("/<int:campagne_id>/envoyer", methods=["POST"])
@login_required
@acces("ecriture")
def envoyer(campagne_id):
    """mode = nouveaux (jamais invités avec succès) | relance (invités n'ayant pas répondu)."""
    mode_test = request.form.get("mode_test") == "1"
    mode = request.form.get("mode", "nouveaux")
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c or not (c["corps"] or "").strip():
            flash("❌ Enregistrez d'abord le message.", "danger")
            return _retour(campagne_id, "#message")
        if not c["date_spectacle"] or not c["date_limite"]:
            flash("❌ Renseignez d'abord la date du spectacle et la date limite (bloc Fiche).", "danger")
            return _retour(campagne_id)
        if c["statut"] == "annulee":
            flash("❌ Le spectacle est annulé : plus d'invitation possible.", "danger")
            return _retour(campagne_id, "#destinataires")
        if c["statut"] == "tiree" or mails.limite_depassee(c):
            flash("❌ La date limite est dépassée : plus d'invitation possible.", "danger")
            return _retour(campagne_id, "#destinataires")
        cat = "" if mails.est_premier(c) else "AND categorie='benevole' "
        if mode == "relance":
            sql = ("SELECT id, benevole_id FROM billets_participants WHERE campagne_id=? " + cat + "AND selected=1 "
                   "AND etat='invite' AND invite_le IS NOT NULL AND invite_mode_test=0 AND invite_erreur IS NULL")
        else:
            sql = ("SELECT id, benevole_id FROM billets_participants WHERE campagne_id=? " + cat + "AND selected=1 "
                   "AND (invite_le IS NULL OR invite_mode_test=1 OR invite_erreur IS NOT NULL) "
                   "AND etat='invite'")
        gains = mails.gains_recents(conn, campagne_id)
        ids = [r["id"] for r in conn.execute(sql + " ORDER BY nom COLLATE NOCASE", (campagne_id,))
               if r["benevole_id"] not in gains]
    if not ids:
        flash("ℹ️ Personne à contacter.", "warning")
        return _retour(campagne_id, "#destinataires")
    lancer_tache_fond(
        target=envoyer_invitations_bg,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, ids, mode_test, current_user.email,
              "Rappel : " if mode == "relance" else ""),
        nom="Billets spectacle — invitations", utilisateur=current_user.email)
    flash("🧪 Envoi TEST lancé (2 mails maximum, vers votre adresse)." if mode_test else
          f"🚀 Envoi lancé en arrière-plan pour {len(ids)} bénévole(s) — actualisez dans quelques instants.",
          "warning" if mode_test else "info")
    return _retour(campagne_id, "#destinataires")


def envoyer_resultats_bg(app, db_path, campagne_id, ids, mode_test, user_email):
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.execute("SELECT * FROM billets_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        ok = erreurs = nb_test = 0
        for pid in ids:
            p = conn.execute("SELECT * FROM billets_participants WHERE id = ?", (pid,)).fetchone()
            if not p:
                continue
            if mode_test:
                if nb_test >= 2:
                    break
                nb_test += 1
            sujet, corps = mails.mail_resultat(c, p)
            try:
                mails.envoyer(c, p["email"], sujet, corps, mode_test=mode_test, user_email=user_email)
                if not mode_test:
                    conn.execute("UPDATE billets_participants SET resultat_envoye_le=?, resultat_erreur=NULL WHERE id=?",
                                 (mails.maintenant(), pid))
                ok += 1
            except Exception as e:
                write_log(f"❌ Billets résultat campagne {campagne_id} → {p['email']} : {e}")
                conn.execute("UPDATE billets_participants SET resultat_erreur=? WHERE id=?", (str(e)[:400], pid))
                erreurs += 1
            conn.commit()
        if not mode_test and ok and not erreurs:
            conn.execute("UPDATE billets_campagnes SET resultats_envoyes_le=? WHERE id=?",
                         (mails.maintenant(), campagne_id))
            conn.commit()
        conn.close()
        upload_database()
        write_log(f"📤 Billets résultats campagne {campagne_id} : {ok} mail(s), {erreurs} erreur(s)")


@billets_bp.route("/<int:campagne_id>/envoyer_resultats", methods=["POST"])
@login_required
@acces("ecriture")
def envoyer_resultats(campagne_id):
    mode_test = request.form.get("mode_test") == "1"
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c or c["statut"] != "tiree":
            flash("❌ Le tirage n'a pas encore été fait.", "danger")
            return _retour(campagne_id, "#tirage")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM billets_participants WHERE campagne_id=? AND resultat IS NOT NULL "
            "AND resultat != 'desiste' AND resultat_envoye_le IS NULL ORDER BY ordre_tirage", (campagne_id,))]
    if not ids:
        flash("ℹ️ Tous les résultats ont déjà été envoyés.", "warning")
        return _retour(campagne_id, "#tirage")
    lancer_tache_fond(
        target=envoyer_resultats_bg,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, ids, mode_test, current_user.email),
        nom="Billets spectacle — résultats", utilisateur=current_user.email)
    flash("🧪 Envoi TEST lancé (2 mails maximum, vers votre adresse)." if mode_test else
          f"🚀 Envoi des résultats lancé pour {len(ids)} personne(s).", "warning" if mode_test else "info")
    return _retour(campagne_id, "#tirage")


# ------------------------------------------------------------
# Tirage au sort
# ------------------------------------------------------------
def effectuer_tirage(conn, c, par):
    """Tire au sort (SystemRandom) : d'abord parmi les inscrits, puis, s'il reste des places,
    parmi les salariés cochés. Un bénévole ayant déjà gagné une place dans une autre campagne
    au cours des 12 derniers mois n'est pas éligible (résultat « non_eligible » pour un inscrit,
    simplement écarté pour un salarié). Enregistre résultat + ordre. Retourne un dict de synthèse."""
    rng = secrets.SystemRandom()
    places = c["nb_billets"]
    gains = mails.gains_recents(conn, c["id"])
    inscrits = list(conn.execute(
        "SELECT * FROM billets_participants WHERE campagne_id=? AND categorie='benevole' AND etat='inscrit'",
        (c["id"],)))
    non_eligibles = [p for p in inscrits if p["benevole_id"] in gains]
    eligibles = [p for p in inscrits if p["benevole_id"] not in gains]
    for p in non_eligibles:
        conn.execute("UPDATE billets_participants SET resultat='non_eligible', ordre_tirage=NULL WHERE id=?", (p["id"],))
    gagnants = rng.sample(eligibles, min(places, len(eligibles)))
    ids_g = {p["id"] for p in gagnants}
    ordre = 0
    for p in gagnants:
        ordre += 1
        conn.execute("UPDATE billets_participants SET resultat='gagnant', origine_place='inscrit', ordre_tirage=? WHERE id=?",
                     (ordre, p["id"]))
    for p in eligibles:
        if p["id"] not in ids_g:
            conn.execute("UPDATE billets_participants SET resultat='non_retenu', ordre_tirage=NULL WHERE id=?", (p["id"],))
    restantes = places - len(gagnants)
    salaries = [p for p in conn.execute(
        "SELECT * FROM billets_participants WHERE campagne_id=? AND categorie='salarie' AND selected=1",
        (c["id"],)) if p["benevole_id"] not in gains]
    gagnants_sal = rng.sample(salaries, min(restantes, len(salaries))) if restantes > 0 else []
    for p in gagnants_sal:
        ordre += 1
        conn.execute("UPDATE billets_participants SET resultat='gagnant', origine_place='salarie', ordre_tirage=? WHERE id=?",
                     (ordre, p["id"]))
    conn.execute("UPDATE billets_campagnes SET statut='tiree', tirage_le=?, tirage_par=? WHERE id=?",
                 (mails.maintenant(), par, c["id"]))
    return {"inscrits": len(inscrits), "non_eligibles": len(non_eligibles), "gagnants_inscrits": len(gagnants),
            "gagnants_salaries": len(gagnants_sal),
            "places_non_pourvues": places - len(gagnants) - len(gagnants_sal)}


@billets_bp.route("/<int:campagne_id>/tirage", methods=["POST"])
@login_required
@acces("ecriture")
def tirage(campagne_id):
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c:
            return redirect(url_for("billets.liste"))
        if c["statut"] == "annulee":
            flash("❌ Le spectacle est annulé : pas de tirage.", "danger")
        elif mails.est_premier(c):
            flash("❌ Cette campagne est en « premier inscrit, premier servi » : il n'y a pas de tirage.", "danger")
        elif c["statut"] == "tiree":
            flash("❌ Le tirage a déjà été effectué (il ne peut pas être refait).", "danger")
        elif not mails.limite_depassee(c):
            flash(f"❌ La date limite d'inscription ({mails.limite_fr(c['date_limite'])}) n'est pas encore dépassée.",
                  "danger")
        elif request.form.get("confirmer") != "1":
            flash("❌ Cochez la case de confirmation.", "danger")
        else:
            s = effectuer_tirage(conn, c, current_user.email)
            conn.commit()
            write_log(f"🎲 Billets campagne {campagne_id} : tirage {s}")
            msg = (f"🎲 Tirage effectué : {s['gagnants_inscrits']} gagnant(s) parmi {s['inscrits']} inscrit(s)"
                   f"{', ' + str(s['gagnants_salaries']) + ' parmi les salariés' if s['gagnants_salaries'] else ''}.")
            flash(msg, "success")
            if s["non_eligibles"]:
                flash(f"ℹ️ {s['non_eligibles']} inscrit(s) écarté(s) du tirage : déjà gagnant(s) dans les 12 derniers mois.", "info")
            if s["places_non_pourvues"]:
                flash(f"⚠️ {s['places_non_pourvues']} place(s) restent non pourvues (plus de salarié disponible).", "warning")
    return _retour(campagne_id, "#tirage")


@billets_bp.route("/<int:campagne_id>/desister_gagnant", methods=["POST"])
@login_required
@acces("ecriture")
def desister_gagnant(campagne_id):
    """Désistement d'un gagnant reçu hors lien (mail, téléphone) : la place est libérée."""
    pid = request.form.get("pid", type=int)
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        p = conn.execute("SELECT * FROM billets_participants WHERE id=? AND campagne_id=?",
                         (pid, campagne_id)).fetchone()
        if not c or c["statut"] != "tiree" or mails.est_premier(c) or not p or p["resultat"] != "gagnant":
            flash("❌ Désistement impossible (gagnant introuvable ou campagne non tirée).", "danger")
        else:
            conn.execute("UPDATE billets_participants SET resultat='desiste', etat='desiste' WHERE id=?", (pid,))
            conn.commit()
            write_log(f"🚫 Billets campagne {campagne_id} : désistement du gagnant {p['nom']} {p['prenom']} "
                      f"enregistré par {current_user.email}")
            flash(f"✅ Désistement de {p['prenom']} {p['nom']} enregistré : une place est libérée. "
                  "Vous pouvez tirer un remplaçant.", "success")
    return _retour(campagne_id, "#tirage")


@billets_bp.route("/<int:campagne_id>/remplacer", methods=["POST"])
@login_required
@acces("ecriture")
def remplacer(campagne_id):
    """Tire au sort un remplaçant pour une place libérée : parmi les inscrits non retenus,
    à défaut parmi les salariés cochés n'ayant pas encore de résultat (hors gains des 12 mois)."""
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c or c["statut"] != "tiree" or mails.est_premier(c):
            flash("❌ Aucun tirage effectué sur cette campagne.", "danger")
            return _retour(campagne_id, "#tirage")
        if attribution.nb_gagnants(conn, campagne_id) >= c["nb_billets"]:
            flash("ℹ️ Toutes les places sont déjà attribuées.", "warning")
            return _retour(campagne_id, "#tirage")
        gains = mails.gains_recents(conn, campagne_id)
        rng = secrets.SystemRandom()
        pool = [p for p in conn.execute(
            "SELECT * FROM billets_participants WHERE campagne_id=? AND resultat='non_retenu' AND etat='inscrit'",
            (campagne_id,)) if p["benevole_id"] not in gains]
        origine = "inscrit"
        if not pool:
            pool = [p for p in conn.execute(
                "SELECT * FROM billets_participants WHERE campagne_id=? AND categorie='salarie' AND selected=1 "
                "AND resultat IS NULL AND etat != 'desiste'", (campagne_id,)) if p["benevole_id"] not in gains]
            origine = "salarie"
        if not pool:
            flash("⚠️ Plus personne à tirer (ni inscrit non retenu, ni salarié disponible).", "warning")
            return _retour(campagne_id, "#tirage")
        g = rng.choice(pool)
        ordre = (conn.execute("SELECT COALESCE(MAX(ordre_tirage),0) FROM billets_participants WHERE campagne_id=?",
                              (campagne_id,)).fetchone()[0] or 0) + 1
        conn.execute("UPDATE billets_participants SET resultat='gagnant', origine_place=?, ordre_tirage=?, "
                     "resultat_envoye_le=NULL, resultat_erreur=NULL WHERE id=?", (origine, ordre, g["id"]))
        conn.commit()
        write_log(f"🎲 Billets campagne {campagne_id} : remplaçant tiré {g['nom']} {g['prenom']} ({origine}) "
                  f"par {current_user.email}")
        flash(f"🎲 Remplaçant tiré au sort : {g['prenom']} {g['nom']} "
              f"({'salarié' if origine == 'salarie' else 'inscrit non retenu'}). "
              "Cliquez sur « Envoyer les résultats » pour le prévenir.", "success")
    return _retour(campagne_id, "#tirage")


def envoyer_annulation_bg(app, db_path, campagne_id, ids, mode_test, user_email):
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.execute("SELECT * FROM billets_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        ok = erreurs = nb_test = 0
        for pid in ids:
            p = conn.execute("SELECT * FROM billets_participants WHERE id = ?", (pid,)).fetchone()
            if not p or not p["email"]:
                continue
            if mode_test:
                if nb_test >= 2:
                    break
                nb_test += 1
            sujet, corps = mails.mail_annulation(c, p)
            try:
                mails.envoyer(c, p["email"], sujet, corps, mode_test=mode_test, user_email=user_email)
                if not mode_test:
                    conn.execute("UPDATE billets_participants SET annulation_envoyee_le=?, resultat_erreur=NULL WHERE id=?",
                                 (mails.maintenant(), pid))
                ok += 1
            except Exception as e:
                write_log(f"❌ Billets annulation campagne {campagne_id} → {p['email']} : {e}")
                conn.execute("UPDATE billets_participants SET resultat_erreur=? WHERE id=?", (str(e)[:400], pid))
                erreurs += 1
            conn.commit()
        conn.close()
        upload_database()
        write_log(f"📤 Billets annulation campagne {campagne_id} : {ok} mail(s), {erreurs} erreur(s)")


@billets_bp.route("/<int:campagne_id>/annuler_spectacle", methods=["POST"])
@login_required
@acces("ecriture")
def annuler_spectacle(campagne_id):
    """Le spectacle est annulé : la campagne est close (plus d'invitation, d'inscription ni de tirage) et
    ses places ne comptent plus pour la règle des 12 mois. L'avis aux personnes se fait ensuite."""
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c or c["statut"] == "annulee":
            flash("❌ Campagne introuvable ou déjà annulée.", "danger")
        elif request.form.get("confirmer") != "1":
            flash("❌ Cochez la case de confirmation.", "danger")
        else:
            conn.execute("UPDATE billets_campagnes SET statut='annulee', annulee_le=?, annulee_par=? WHERE id=?",
                         (mails.maintenant(), current_user.email, campagne_id))
            conn.commit()
            write_log(f"🚫 Billets campagne {campagne_id} : spectacle annulé par {current_user.email}")
            flash("🚫 Spectacle marqué comme annulé : plus d'invitation, d'inscription ni de tirage. "
                  "Envoyez maintenant l'avis d'annulation aux personnes concernées (chapitre « Spectacle annulé »).",
                  "warning")
    return _retour(campagne_id)


@billets_bp.route("/<int:campagne_id>/envoyer_annulation", methods=["POST"])
@login_required
@acces("ecriture")
def envoyer_annulation(campagne_id):
    """Avis d'annulation aux inscrits (et, si coché, aux invités n'ayant pas répondu) pas encore prévenus."""
    mode_test = request.form.get("mode_test") == "1"
    tous = request.form.get("tous") == "1"
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c or c["statut"] != "annulee":
            flash("❌ Le spectacle n'est pas marqué comme annulé.", "danger")
            return _retour(campagne_id)
        sql = ("SELECT id FROM billets_participants WHERE campagne_id=? AND annulation_envoyee_le IS NULL AND "
               "(etat='inscrit'" + (" OR (etat='invite' AND selected=1 AND invite_le IS NOT NULL "
                                    "AND invite_mode_test=0 AND invite_erreur IS NULL)" if tous else "") + ") "
               "ORDER BY nom COLLATE NOCASE")
        ids = [r["id"] for r in conn.execute(sql, (campagne_id,))]
    if not ids:
        flash("ℹ️ Personne à prévenir.", "warning")
        return _retour(campagne_id)
    lancer_tache_fond(
        target=envoyer_annulation_bg,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, ids, mode_test, current_user.email),
        nom="Billets spectacle — annulation", utilisateur=current_user.email)
    flash("🧪 Envoi TEST lancé (2 mails maximum, vers votre adresse)." if mode_test else
          f"🚀 Avis d'annulation en cours d'envoi à {len(ids)} personne(s).", "warning" if mode_test else "info")
    return _retour(campagne_id)


@billets_bp.route("/<int:campagne_id>/retablir", methods=["POST"])
@login_required
@acces("ecriture")
def retablir(campagne_id):
    """Annule l'annulation (erreur de manipulation) tant qu'aucun avis n'est réellement parti."""
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c or c["statut"] != "annulee":
            flash("❌ Le spectacle n'est pas marqué comme annulé.", "danger")
        elif conn.execute("SELECT 1 FROM billets_participants WHERE campagne_id=? AND annulation_envoyee_le IS NOT NULL",
                          (campagne_id,)).fetchone():
            flash("❌ Des avis d'annulation ont déjà été envoyés : la campagne ne peut plus être rétablie.", "danger")
        else:
            conn.execute("UPDATE billets_campagnes SET statut=?, annulee_le=NULL, annulee_par=NULL WHERE id=?",
                         ("tiree" if c["tirage_le"] else "ouverte", campagne_id))
            conn.commit()
            write_log(f"↩️ Billets campagne {campagne_id} : annulation du spectacle retirée par {current_user.email}")
            flash("↩️ Campagne rétablie.", "success")
    return _retour(campagne_id)


@billets_bp.route("/<int:campagne_id>/annuler_tirage", methods=["POST"])
@login_required
@acces("ecriture")
def annuler_tirage(campagne_id):
    """Annule un tirage tant qu'AUCUN mail de résultat n'a été réellement envoyé."""
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c or c["statut"] != "tiree":
            flash("❌ Aucun tirage à annuler.", "danger")
        elif c["resultats_envoyes_le"] or conn.execute(
                "SELECT 1 FROM billets_participants WHERE campagne_id=? AND resultat_envoye_le IS NOT NULL",
                (campagne_id,)).fetchone():
            flash("❌ Des résultats ont déjà été envoyés : le tirage ne peut plus être annulé.", "danger")
        else:
            conn.execute("UPDATE billets_participants SET resultat=NULL, origine_place=NULL, ordre_tirage=NULL, "
                         "resultat_erreur=NULL WHERE campagne_id=?", (campagne_id,))
            conn.execute("UPDATE billets_campagnes SET statut='ouverte', tirage_le=NULL, tirage_par=NULL WHERE id=?",
                         (campagne_id,))
            conn.commit()
            write_log(f"↩️ Billets campagne {campagne_id} : tirage annulé par {current_user.email}")
            flash("↩️ Tirage annulé : vous pouvez modifier la campagne et relancer le tirage.", "warning")
    return _retour(campagne_id, "#tirage")


@billets_bp.route("/<int:campagne_id>/dupliquer", methods=["POST"])
@login_required
@acces("ecriture")
def dupliquer(campagne_id):
    """Copie la fiche, le message d'invitation et les textes des mails ; dates à ressaisir, aucun destinataire."""
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c:
            return redirect(url_for("billets.liste"))
        cur = conn.execute(
            """INSERT INTO billets_campagnes (donateur, lieu, spectacle, date_spectacle, heure_spectacle,
                 mode_retrait, nb_billets, date_limite, sujet, corps, expediteur, expediteur_nom, reply_to,
                 textes_mails, statut, cree_le, cree_par, mode, regle_12_mois)
               VALUES (?,?,?,NULL,?,?,?,NULL,?,?,?,?,?,?, 'ouverte', ?, ?, ?, ?)""",
            (c["donateur"], c["lieu"], c["spectacle"], c["heure_spectacle"], c["mode_retrait"], c["nb_billets"],
             c["sujet"], c["corps"], c["expediteur"], c["expediteur_nom"], c["reply_to"], c["textes_mails"],
             mails.maintenant(), current_user.email, c["mode"] or "tirage",
             1 if mails.regle_12_mois(c) else 0))
        conn.commit()
        cid = cur.lastrowid
    flash("📑 Campagne dupliquée : renseignez la date du spectacle et la date limite d'inscription, "
          "puis constituez la liste des destinataires.", "info")
    return redirect(url_for("billets.campagne", campagne_id=cid))


@billets_bp.route("/<int:campagne_id>/supprimer", methods=["POST"])
@login_required
@acces("ecriture")
def supprimer(campagne_id):
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if c and c["statut"] == "tiree" and not has_access("billets", "admin"):
            flash("❌ Une campagne tirée ne peut être supprimée que par un administrateur.", "danger")
            return _retour(campagne_id)
        conn.execute("DELETE FROM billets_participants WHERE campagne_id = ?", (campagne_id,))
        conn.execute("DELETE FROM billets_campagnes WHERE id = ?", (campagne_id,))
        conn.commit()
    flash("🗑️ Campagne supprimée.", "warning")
    return redirect(url_for("billets.liste"))
