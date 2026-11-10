# ============================================================
# 🎟️ Billets spectacle — écran de gestion (un écran par campagne)
#   1. fiche (donateur, lieu, spectacle, date/heure, retrait, nb billets,
#      date limite) → 2. message d'invitation (texte riche + image)
#   → 3. bénévoles à jour de cotisation (liste décochable) + envoi
#   → 4. inscriptions (formulaire par lien personnel) → 5. tirage au sort
#   après la date limite (inscrits, puis salariés pour les places restantes)
#   → 6. mails de résultat.
# ============================================================

import os
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

from ba38_billets import billets_bp
from ba38_billets import mails
from ba38_utilitaires.core import (
    adresse_test_utilisateur, get_db_path, has_access, split_emails, upload_database, write_log,
)
from ba38_utilitaires.taches_fond import lancer_tache_fond

TEXTE_PAR_DEFAUT = (
    "<p>Bonjour &lt;&lt;prenom&gt;&gt;,</p>"
    "<p>&lt;&lt;donateur&gt;&gt; offre à nos bénévoles <b>&lt;&lt;nb_billets&gt;&gt; place(s)</b> pour le spectacle "
    "<b>&lt;&lt;spectacle&gt;&gt;</b>, &lt;&lt;lieu&gt;&gt;, le &lt;&lt;date_spectacle&gt;&gt; à &lt;&lt;heure&gt;&gt;.</p>"
    "<p>Si vous souhaitez participer, inscrivez-vous en cliquant sur votre lien personnel "
    "avant le <b>&lt;&lt;date_limite&gt;&gt;</b> :<br>&lt;&lt;lien_inscription&gt;&gt;</p>"
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


def _connect():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _campagne(conn, cid):
    return conn.execute("SELECT * FROM billets_campagnes WHERE id = ?", (cid,)).fetchone()


def _retour(cid, ancre=""):
    return redirect(url_for("billets.campagne", campagne_id=cid) + ancre)


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


def _champs_fiche(f):
    return (
        (f.get("donateur") or "").strip(), (f.get("lieu") or "").strip(), (f.get("spectacle") or "").strip(),
        (f.get("date_spectacle") or "").strip() or None, (f.get("heure_spectacle") or "").strip() or None,
        (f.get("mode_retrait") or "").strip(), max(1, f.get("nb_billets", type=int) or 1),
        _dt(f.get("date_limite")))


@billets_bp.route("/nouvelle", methods=["POST"])
@login_required
@acces("ecriture")
def nouvelle():
    donateur, lieu, spectacle, d, h, retrait, nb, limite = _champs_fiche(request.form)
    if not (donateur and spectacle and d and limite):
        flash("❌ Donateur, spectacle, date du spectacle et date limite d'inscription sont obligatoires.", "danger")
        return redirect(url_for("billets.liste"))
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO billets_campagnes (donateur, lieu, spectacle, date_spectacle, heure_spectacle,
                 mode_retrait, nb_billets, date_limite, sujet, corps, statut, cree_le, cree_par)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'ouverte', ?, ?)""",
            (donateur, lieu, spectacle, d, h, retrait, nb, limite,
             "Places offertes : <<spectacle>>", TEXTE_PAR_DEFAUT, mails.maintenant(), current_user.email))
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
    from ba38_emailing.routes import expediteurs_disponibles
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c:
            flash("❌ Campagne introuvable.", "danger")
            return redirect(url_for("billets.liste"))
        parts = conn.execute(
            "SELECT * FROM billets_participants WHERE campagne_id = ? "
            "ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE", (campagne_id,)).fetchall()
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
        "resultats_a_envoyer": sum(1 for p in parts if p["resultat"] and not p["resultat_envoye_le"]),
    }
    return render_template(
        "billets/campagne.html", c=c, benevoles=benevoles, salaries=salaries, inscrits=inscrits, nb=nb,
        gagnants=sorted([p for p in parts if p["resultat"] == "gagnant"], key=lambda p: p["ordre_tirage"] or 0),
        non_retenus=[p for p in parts if p["resultat"] == "non_retenu"],
        depassee=mails.limite_depassee(c), tiree=(c["statut"] == "tiree"),
        peut_ecrire=has_access("billets", "ecriture"), expediteurs=expediteurs_disponibles(),
        variables=mails.VARIABLES_AIDE, limite_fr=mails.limite_fr, date_fr=mails.date_fr,
        modele_defaut=TEXTE_PAR_DEFAUT)


@billets_bp.route("/<int:campagne_id>/fiche", methods=["POST"])
@login_required
@acces("ecriture")
def enregistrer_fiche(campagne_id):
    donateur, lieu, spectacle, d, h, retrait, nb, limite = _champs_fiche(request.form)
    if not (donateur and spectacle and d and limite):
        flash("❌ Donateur, spectacle, date du spectacle et date limite sont obligatoires.", "danger")
        return _retour(campagne_id)
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if c and c["statut"] == "tiree" and nb != c["nb_billets"]:
            flash("❌ Le tirage est fait : le nombre de billets ne peut plus changer.", "danger")
            return _retour(campagne_id)
        conn.execute(
            """UPDATE billets_campagnes SET donateur=?, lieu=?, spectacle=?, date_spectacle=?, heure_spectacle=?,
                 mode_retrait=?, nb_billets=?, date_limite=? WHERE id=?""",
            (donateur, lieu, spectacle, d, h, retrait, nb, limite, campagne_id))
        conn.commit()
    flash("✅ Fiche enregistrée.", "success")
    return _retour(campagne_id)


@billets_bp.route("/<int:campagne_id>/message", methods=["POST"])
@login_required
@acces("ecriture")
def enregistrer_message(campagne_id):
    from ba38_emailing.routes import expediteurs_disponibles
    sujet = (request.form.get("sujet") or "").strip()
    corps = request.form.get("corps") or ""
    expediteur = (request.form.get("expediteur") or "").strip()
    if not sujet or not corps.strip():
        flash("❌ Sujet et message sont obligatoires.", "danger")
        return _retour(campagne_id, "#message")
    if expediteur not in expediteurs_disponibles():
        flash("❌ Expéditeur non valide (non validé côté Mailjet).", "danger")
        return _retour(campagne_id, "#message")
    if "lien_inscription" not in corps:
        flash("⚠️ Le texte ne contient pas <<lien_inscription>> : les bénévoles n'auront aucun lien pour s'inscrire.",
              "warning")
    with _connect() as conn:
        conn.execute(
            "UPDATE billets_campagnes SET sujet=?, corps=?, expediteur=?, expediteur_nom=?, reply_to=? WHERE id=?",
            (sujet, corps, expediteur, (request.form.get("expediteur_nom") or "").strip() or None,
             (request.form.get("reply_to") or "").strip() or None, campagne_id))
        conn.commit()
    flash("✅ Message enregistré.", "success")
    return _retour(campagne_id, "#destinataires")


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
    for b in conn.execute("SELECT * FROM benevoles ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE"):
        salarie = (b["type_benevole"] or "").strip().lower() == "salarie"
        a_jour = (b["cotisation_2026"] or "").strip().lower() == "oui"
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
        ajoutes += 1
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
        if not c or c["statut"] == "tiree":
            flash("❌ Le tirage est fait : les inscriptions sont closes.", "danger")
            return _retour(campagne_id, "#inscriptions")
        if action == "inscrire":
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
        if not c or not c["expediteur"] or not (c["corps"] or "").strip():
            flash("❌ Enregistrez d'abord le message et l'expéditeur.", "danger")
            return _retour(campagne_id, "#message")
        if c["statut"] == "tiree" or mails.limite_depassee(c):
            flash("❌ La date limite est dépassée : plus d'invitation possible.", "danger")
            return _retour(campagne_id, "#destinataires")
        if mode == "relance":
            sql = ("SELECT id FROM billets_participants WHERE campagne_id=? AND categorie='benevole' AND selected=1 "
                   "AND etat='invite' AND invite_le IS NOT NULL AND invite_mode_test=0 AND invite_erreur IS NULL")
        else:
            sql = ("SELECT id FROM billets_participants WHERE campagne_id=? AND categorie='benevole' AND selected=1 "
                   "AND (invite_le IS NULL OR invite_mode_test=1 OR invite_erreur IS NOT NULL) "
                   "AND etat='invite'")
        ids = [r["id"] for r in conn.execute(sql + " ORDER BY nom COLLATE NOCASE", (campagne_id,))]
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
            "AND resultat_envoye_le IS NULL ORDER BY ordre_tirage", (campagne_id,))]
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
    parmi les salariés cochés. Enregistre résultat + ordre. Retourne un dict de synthèse."""
    rng = secrets.SystemRandom()
    places = c["nb_billets"]
    inscrits = list(conn.execute(
        "SELECT * FROM billets_participants WHERE campagne_id=? AND categorie='benevole' AND etat='inscrit'",
        (c["id"],)))
    gagnants = rng.sample(inscrits, min(places, len(inscrits)))
    ids_g = {p["id"] for p in gagnants}
    ordre = 0
    for p in gagnants:
        ordre += 1
        conn.execute("UPDATE billets_participants SET resultat='gagnant', origine_place='inscrit', ordre_tirage=? WHERE id=?",
                     (ordre, p["id"]))
    for p in inscrits:
        if p["id"] not in ids_g:
            conn.execute("UPDATE billets_participants SET resultat='non_retenu', ordre_tirage=NULL WHERE id=?", (p["id"],))
    restantes = places - len(gagnants)
    salaries = list(conn.execute(
        "SELECT * FROM billets_participants WHERE campagne_id=? AND categorie='salarie' AND selected=1",
        (c["id"],)))
    gagnants_sal = rng.sample(salaries, min(restantes, len(salaries))) if restantes > 0 else []
    for p in gagnants_sal:
        ordre += 1
        conn.execute("UPDATE billets_participants SET resultat='gagnant', origine_place='salarie', ordre_tirage=? WHERE id=?",
                     (ordre, p["id"]))
    conn.execute("UPDATE billets_campagnes SET statut='tiree', tirage_le=?, tirage_par=? WHERE id=?",
                 (mails.maintenant(), par, c["id"]))
    return {"inscrits": len(inscrits), "gagnants_inscrits": len(gagnants), "gagnants_salaries": len(gagnants_sal),
            "places_non_pourvues": places - len(gagnants) - len(gagnants_sal)}


@billets_bp.route("/<int:campagne_id>/tirage", methods=["POST"])
@login_required
@acces("ecriture")
def tirage(campagne_id):
    with _connect() as conn:
        c = _campagne(conn, campagne_id)
        if not c:
            return redirect(url_for("billets.liste"))
        if c["statut"] == "tiree":
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
            if s["places_non_pourvues"]:
                flash(f"⚠️ {s['places_non_pourvues']} place(s) restent non pourvues (plus de salarié disponible).", "warning")
    return _retour(campagne_id, "#tirage")


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
