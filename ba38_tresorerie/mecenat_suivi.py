# ============================================================
# 📋 Mécénat — suivi des échanges (CRM léger)
#   - journal d'échanges par mécène / prospect (rendez-vous, appels, mails,
#     événements) avec compte rendu, prochaine action, date de relance
#   - pièces jointes (stockées hors de l'arbre de code, par environnement)
#   - tableau de bord : relances à faire, rendez-vous à venir, mécènes sans contact
#   Tous les utilisateurs ayant le droit « mecenat » voient les actions de tous.
# ============================================================

import os
import uuid
from datetime import date, datetime, timedelta

from flask import abort, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ba38_tresorerie import tresorerie_bp
from ba38_tresorerie.mecenat import (
    STATUTS_MECENE, TYPES_INTERACTION, _benevoles, _connect, _maintenant, _txt, acces,
)
from ba38_utilitaires.core import get_db_path, has_access, upload_database, write_log

# Hors de dev/ et prod/ (le rsync de déploiement ne les touche pas), un dossier par base :
# /srv/ba38/data/mecenat/<ba380dev | ba380dev_test | ba380 | ba380_test>/
RACINE_PIECES = "/srv/ba38/data/mecenat"
EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "odt", "ods", "ppt", "pptx", "png", "jpg", "jpeg", "txt", "eml", "msg"}
TAILLE_MAX = 15 * 1024 * 1024
MOIS_SANS_CONTACT = 6


def _dossier_pieces():
    base = os.path.splitext(os.path.basename(get_db_path()))[0]
    d = os.path.join(RACINE_PIECES, base)
    os.makedirs(d, exist_ok=True)
    return d


def _dt(valeur):
    v = (valeur or "").strip()
    if len(v) == 10:
        v += "T09:00"
    return v[:16] or None


def _date(valeur):
    v = (valeur or "").strip()[:10]
    return v or None


def _champs_interaction(f):
    type_i = _txt(f, "type_interaction")
    return (
        type_i if type_i in TYPES_INTERACTION else "autre",
        _dt(f.get("date_interaction")) or datetime.now().strftime("%Y-%m-%dT%H:%M"),
        f.get("dossier_id", type=int), f.get("benevole_id", type=int),
        _txt(f, "compte_rendu"), _txt(f, "prochaine_action"), _date(f.get("date_relance")))


def _enregistrer_pieces(conn, interaction_id):
    """Enregistre les fichiers du formulaire ; renvoie (nb_ok, [refus])."""
    ok, refus = 0, []
    for f in request.files.getlist("pieces"):
        if not f or not f.filename:
            continue
        nom = secure_filename(f.filename) or "piece"
        ext = nom.rsplit(".", 1)[-1].lower() if "." in nom else ""
        if ext not in EXTENSIONS:
            refus.append(f"{f.filename} (type non autorisé)")
            continue
        f.stream.seek(0, os.SEEK_END)
        taille = f.stream.tell()
        f.stream.seek(0)
        if taille > TAILLE_MAX:
            refus.append(f"{f.filename} (plus de 15 Mo)")
            continue
        stocke = f"{uuid.uuid4().hex}.{ext}"
        f.save(os.path.join(_dossier_pieces(), stocke))
        conn.execute(
            "INSERT INTO mecenat_pieces (interaction_id, nom_original, fichier, taille, date_ajout, user_ajout) "
            "VALUES (?,?,?,?,?,?)", (interaction_id, f.filename, stocke, taille, _maintenant(), current_user.email))
        ok += 1
    return ok, refus


def _supprimer_fichier(fichier):
    try:
        os.remove(os.path.join(_dossier_pieces(), fichier))
    except OSError:
        pass


def _flash_pieces(ok, refus):
    if ok:
        flash(f"📎 {ok} pièce(s) jointe(s) ajoutée(s).", "success")
    for r in refus:
        flash(f"❌ Pièce refusée : {r}. Types admis : {', '.join(sorted(EXTENSIONS))} ; 15 Mo maximum.", "danger")


# ------------------------------------------------------------
# Création / modification d'un échange
# ------------------------------------------------------------
@tresorerie_bp.route("/tresorerie/mecenat/mecene/<int:mecene_id>/interaction", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_interaction_ajouter(mecene_id):
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM mecenat WHERE id=?", (mecene_id,)).fetchone():
            abort(404)
        type_i, dt, dossier_id, benevole_id, cr, action, relance = _champs_interaction(request.form)
        cur = conn.execute(
            "INSERT INTO mecenat_interactions (mecene_id, dossier_id, type_interaction, date_interaction, benevole_id,"
            " compte_rendu, prochaine_action, date_relance, relance_faite, date_creation, user_creation, date_modif,"
            " user_modif) VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?)",
            (mecene_id, dossier_id, type_i, dt, benevole_id, cr, action, relance, _maintenant(), current_user.email,
             _maintenant(), current_user.email))
        ok, refus = _enregistrer_pieces(conn, cur.lastrowid)
        # un prospect qu'on contacte pour la première fois passe en « Contacté »
        conn.execute("UPDATE mecenat SET statut='contacte' WHERE id=? AND statut='prospect'", (mecene_id,))
        conn.commit()
    upload_database()
    write_log(f"📋 Mécénat : échange {cur.lastrowid} ajouté (mécène {mecene_id}) par {current_user.email}")
    flash("✅ Échange enregistré.", "success")
    _flash_pieces(ok, refus)
    return redirect(url_for("tresorerie.mecenat_mecene", mecene_id=mecene_id) + "#historique")


@tresorerie_bp.route("/tresorerie/mecenat/interaction/<int:interaction_id>", methods=["GET", "POST"])
@login_required
@acces("lecture")
def mecenat_interaction(interaction_id):
    with _connect() as conn:
        i = conn.execute(
            "SELECT i.*, m.structure FROM mecenat_interactions i JOIN mecenat m ON m.id=i.mecene_id WHERE i.id=?",
            (interaction_id,)).fetchone()
        if not i:
            abort(404)
        if request.method == "POST":
            if not has_access("mecenat", "ecriture"):
                abort(403)
            type_i, dt, dossier_id, benevole_id, cr, action, relance = _champs_interaction(request.form)
            conn.execute(
                "UPDATE mecenat_interactions SET type_interaction=?, date_interaction=?, dossier_id=?, benevole_id=?,"
                " compte_rendu=?, prochaine_action=?, date_relance=?, relance_faite=?, date_modif=?, user_modif=? "
                "WHERE id=?",
                (type_i, dt, dossier_id, benevole_id, cr, action, relance,
                 1 if request.form.get("relance_faite") else 0, _maintenant(), current_user.email, interaction_id))
            ok, refus = _enregistrer_pieces(conn, interaction_id)
            conn.commit()
            upload_database()
            flash("✅ Échange enregistré.", "success")
            _flash_pieces(ok, refus)
            return redirect(url_for("tresorerie.mecenat_mecene", mecene_id=i["mecene_id"]) + "#historique")
        pieces = conn.execute("SELECT * FROM mecenat_pieces WHERE interaction_id=? ORDER BY id", (interaction_id,)).fetchall()
        dossiers = conn.execute("SELECT id, objet FROM mecenat_dossiers ORDER BY objet COLLATE NOCASE").fetchall()
        benevoles = _benevoles(conn)
    return render_template("tresorerie/mecenat/interaction.html", i=i, pieces=pieces, tous_dossiers=dossiers,
                           benevoles=benevoles, types_interaction=TYPES_INTERACTION,
                           peut_ecrire=has_access("mecenat", "ecriture"))


@tresorerie_bp.route("/tresorerie/mecenat/interaction/<int:interaction_id>/relance", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_interaction_relance(interaction_id):
    """Coche / décoche « relance faite »."""
    with _connect() as conn:
        i = conn.execute("SELECT mecene_id, relance_faite FROM mecenat_interactions WHERE id=?",
                         (interaction_id,)).fetchone()
        if not i:
            abort(404)
        conn.execute("UPDATE mecenat_interactions SET relance_faite=?, date_modif=?, user_modif=? WHERE id=?",
                     (0 if i["relance_faite"] else 1, _maintenant(), current_user.email, interaction_id))
        conn.commit()
    upload_database()
    suite = request.form.get("retour") or ""
    if suite == "suivi":
        return redirect(url_for("tresorerie.mecenat_suivi"))
    return redirect(url_for("tresorerie.mecenat_mecene", mecene_id=i["mecene_id"]) + "#historique")


@tresorerie_bp.route("/tresorerie/mecenat/interaction/<int:interaction_id>/supprimer", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_interaction_supprimer(interaction_id):
    with _connect() as conn:
        i = conn.execute("SELECT mecene_id FROM mecenat_interactions WHERE id=?", (interaction_id,)).fetchone()
        if not i:
            abort(404)
        fichiers = [r["fichier"] for r in conn.execute("SELECT fichier FROM mecenat_pieces WHERE interaction_id=?",
                                                       (interaction_id,))]
        conn.execute("DELETE FROM mecenat_pieces WHERE interaction_id=?", (interaction_id,))
        conn.execute("DELETE FROM mecenat_interactions WHERE id=?", (interaction_id,))
        conn.commit()
    for f in fichiers:
        _supprimer_fichier(f)
    upload_database()
    write_log(f"🗑️ Mécénat : échange {interaction_id} supprimé par {current_user.email}")
    flash("🗑️ Échange supprimé.", "success")
    return redirect(url_for("tresorerie.mecenat_mecene", mecene_id=i["mecene_id"]) + "#historique")


# ------------------------------------------------------------
# Pièces jointes
# ------------------------------------------------------------
@tresorerie_bp.route("/tresorerie/mecenat/piece/<int:piece_id>")
@login_required
@acces("lecture")
def mecenat_piece(piece_id):
    with _connect() as conn:
        p = conn.execute("SELECT * FROM mecenat_pieces WHERE id=?", (piece_id,)).fetchone()
    if not p:
        abort(404)
    if not os.path.exists(os.path.join(_dossier_pieces(), p["fichier"])):
        flash("❌ Fichier introuvable sur le serveur (base copiée depuis un autre environnement ?).", "danger")
        return redirect(request.referrer or url_for("tresorerie.mecenat_suivi"))
    return send_from_directory(_dossier_pieces(), p["fichier"], as_attachment=True, download_name=p["nom_original"])


@tresorerie_bp.route("/tresorerie/mecenat/piece/<int:piece_id>/supprimer", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_piece_supprimer(piece_id):
    with _connect() as conn:
        p = conn.execute("SELECT * FROM mecenat_pieces WHERE id=?", (piece_id,)).fetchone()
        if not p:
            abort(404)
        conn.execute("DELETE FROM mecenat_pieces WHERE id=?", (piece_id,))
        conn.commit()
    _supprimer_fichier(p["fichier"])
    flash("🗑️ Pièce jointe supprimée.", "success")
    return redirect(url_for("tresorerie.mecenat_interaction", interaction_id=p["interaction_id"]))


# ------------------------------------------------------------
# Tableau de bord « Suivi / relances »
# ------------------------------------------------------------
@tresorerie_bp.route("/tresorerie/mecenat/suivi")
@login_required
@acces("lecture")
def mecenat_suivi():
    resp = request.args.get("resp", type=int)
    aujourdhui = date.today()
    j0 = aujourdhui.isoformat()
    j7 = (aujourdhui + timedelta(days=7)).isoformat()
    limite_contact = (aujourdhui - timedelta(days=30 * MOIS_SANS_CONTACT)).isoformat()
    filtre_resp = " AND i.benevole_id = ?" if resp else ""
    args_resp = (resp,) if resp else ()

    with _connect() as conn:
        relances = conn.execute(
            f"""SELECT i.*, m.structure, TRIM(COALESCE(b.prenom,'') || ' ' || COALESCE(b.nom,'')) AS benevole
                FROM mecenat_interactions i JOIN mecenat m ON m.id = i.mecene_id
                LEFT JOIN benevoles b ON b.id = i.benevole_id
                WHERE i.relance_faite = 0 AND COALESCE(i.date_relance,'') != ''{filtre_resp}
                ORDER BY i.date_relance, m.structure COLLATE NOCASE""", args_resp).fetchall()
        rdv = conn.execute(
            f"""SELECT i.*, m.structure, TRIM(COALESCE(b.prenom,'') || ' ' || COALESCE(b.nom,'')) AS benevole
                FROM mecenat_interactions i JOIN mecenat m ON m.id = i.mecene_id
                LEFT JOIN benevoles b ON b.id = i.benevole_id
                WHERE i.type_interaction IN ('rendez_vous','evenement') AND substr(i.date_interaction,1,10) >= ?{filtre_resp}
                ORDER BY i.date_interaction""", (j0,) + args_resp).fetchall()
        sans_contact = conn.execute(
            """SELECT m.id, m.structure, m.statut,
                      (SELECT MAX(substr(date_interaction,1,10)) FROM mecenat_interactions
                         WHERE mecene_id = m.id AND substr(date_interaction,1,10) <= ?) AS dernier_contact
               FROM mecenat m
               WHERE m.statut != 'inactif'
                 AND NOT EXISTS (SELECT 1 FROM mecenat_interactions WHERE mecene_id = m.id
                                 AND substr(date_interaction,1,10) > ?)
                 AND NOT EXISTS (SELECT 1 FROM mecenat_interactions WHERE mecene_id = m.id
                                 AND relance_faite = 0 AND COALESCE(date_relance,'') != '')
               ORDER BY dernier_contact IS NOT NULL, dernier_contact, m.structure COLLATE NOCASE""",
            (j0, limite_contact)).fetchall()
        benevoles = conn.execute(
            "SELECT DISTINCT b.id, b.nom, b.prenom FROM benevoles b JOIN mecenat_interactions i ON i.benevole_id=b.id "
            "ORDER BY b.nom COLLATE NOCASE").fetchall()

    return render_template(
        "tresorerie/mecenat/suivi.html",
        en_retard=[r for r in relances if r["date_relance"] < j0],
        cette_semaine=[r for r in relances if j0 <= r["date_relance"] <= j7],
        plus_tard=[r for r in relances if r["date_relance"] > j7],
        rdv=rdv, sans_contact=sans_contact, benevoles=benevoles, resp=resp, mois=MOIS_SANS_CONTACT,
        types_interaction=TYPES_INTERACTION, statuts=STATUTS_MECENE, peut_ecrire=has_access("mecenat", "ecriture"))
