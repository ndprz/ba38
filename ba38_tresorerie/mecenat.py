# ============================================================
# 🤝 Mécénat — suivi des dossiers à financer et des mécènes sollicités
#   - mécènes (table `mecenat`) : structure + contact
#   - dossiers à financer (ex. un camion l'an prochain) : famille, objet,
#     date prévue, montant TTC, pilotes (bénévoles), total des subventions
#   - demandes : un dossier peut concerner plusieurs mécènes, avec montant
#     demandé / montant obtenu / date de versement
# ============================================================

import sqlite3
from datetime import datetime
from functools import wraps

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ba38_tresorerie import tresorerie_bp
from ba38_tresorerie.mecenat_schema import ensure_schema
from ba38_utilitaires.core import get_db_path, has_access, upload_database, write_log

TYPES_DOSSIER = {"investissement": "Investissement", "achat_denrees": "Achat de denrées"}

STATUTS_MECENE = {
    "prospect": "Prospect", "contacte": "Contacté", "en_discussion": "En discussion",
    "engage": "Engagé", "donateur": "Donateur", "inactif": "Inactif / refus",
}
STATUT_COULEURS = {
    "prospect": "secondary", "contacte": "info", "en_discussion": "primary",
    "engage": "success", "donateur": "success", "inactif": "dark",
}

FAMILLES_SUGGEREES = [
    "Flotte automobile", "Travaux", "Matériel / équipement", "Informatique", "Autre",
]


def acces(niveau):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not has_access("mecenat", niveau):
                write_log(f"⛔ Accès refusé mécénat : niveau={niveau}")
                flash("⛔ Accès refusé", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def _connect():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)  # auto-réparation si la migration n'a pas été lancée
    return conn


def _montant(valeur):
    """« 1 234,50 » / « 1234.5 » → 1234.5 ; vide ou invalide → 0.0 (jamais négatif)."""
    v = (valeur or "").replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return max(0.0, round(float(v), 2))
    except ValueError:
        return 0.0


def _txt(f, nom):
    return (f.get(nom) or "").strip()


def _maintenant():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def eur(v):
    """Format français : 12 345,60 €"""
    try:
        return f"{float(v or 0):,.2f}".replace(",", " ").replace(".", ",") + " €"
    except (TypeError, ValueError):
        return "0,00 €"


def date_fr(v):
    try:
        return datetime.strptime((v or "")[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return ""


def _benevoles(conn):
    return conn.execute(
        "SELECT id, nom, prenom FROM benevoles ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE").fetchall()


def _recalcul_total(conn, dossier_id):
    conn.execute(
        "UPDATE mecenat_dossiers SET total_subvention_obtenues = "
        "(SELECT COALESCE(SUM(montant_obtenu), 0) FROM mecenat_demandes WHERE dossier_id = ?), "
        "date_modif = ?, user_modif = ? WHERE id = ?",
        (dossier_id, _maintenant(), current_user.email, dossier_id))


TYPES_INTERACTION = {
    "rendez_vous": "📅 Rendez-vous", "appel": "📞 Appel", "mail": "✉️ Mail / courrier",
    "evenement": "🎉 Événement", "autre": "📝 Autre",
}


def _historique(conn, mecene_id):
    """Échanges d'un mécène (du plus récent au plus ancien) avec pièces jointes, tous auteurs confondus."""
    lignes = conn.execute(
        """SELECT i.*, d.objet AS dossier_objet,
                  TRIM(COALESCE(b.prenom,'') || ' ' || COALESCE(b.nom,'')) AS benevole
           FROM mecenat_interactions i
           LEFT JOIN mecenat_dossiers d ON d.id = i.dossier_id
           LEFT JOIN benevoles b ON b.id = i.benevole_id
           WHERE i.mecene_id = ? ORDER BY i.date_interaction DESC, i.id DESC""", (mecene_id,)).fetchall()
    pieces = {}
    for p in conn.execute("SELECT * FROM mecenat_pieces WHERE interaction_id IN "
                          "(SELECT id FROM mecenat_interactions WHERE mecene_id = ?) ORDER BY id", (mecene_id,)):
        pieces.setdefault(p["interaction_id"], []).append(p)
    return [(i, pieces.get(i["id"], [])) for i in lignes]


def _poles(conn):
    """Pôles actifs (même table que les engagements de dépenses)."""
    try:
        return conn.execute("SELECT id, nom_affiche FROM engagement_poles WHERE COALESCE(actif,1)=1 "
                            "ORDER BY nom_affiche COLLATE NOCASE").fetchall()
    except sqlite3.OperationalError:
        return []


def _familles(conn):
    existantes = [r[0] for r in conn.execute(
        "SELECT DISTINCT famille FROM mecenat_dossiers WHERE TRIM(COALESCE(famille,'')) != '' ORDER BY famille")]
    return existantes + [f for f in FAMILLES_SUGGEREES if f not in existantes]


@tresorerie_bp.app_context_processor
def _mecenat_filtres():
    return {"mecenat_eur": eur, "mecenat_date_fr": date_fr, "statuts_mecene": STATUTS_MECENE,
            "statut_couleurs": STATUT_COULEURS,
            "mecenat_today": datetime.now().strftime("%Y-%m-%d"), "mecenat_now": datetime.now().strftime("%Y-%m-%dT%H:%M")}


# ------------------------------------------------------------
# Dossiers à financer
# ------------------------------------------------------------
@tresorerie_bp.route("/tresorerie/mecenat")
@login_required
@acces("lecture")
def mecenat_dossiers():
    filtre = request.args.get("type") if request.args.get("type") in TYPES_DOSSIER else ""
    filtre_pole = request.args.get("pole", type=int) or 0
    with _connect() as conn:
        poles = _poles(conn)
        dossiers = conn.execute(
            """SELECT d.*,
                 (SELECT COALESCE(SUM(montant_demande),0) FROM mecenat_demandes WHERE dossier_id=d.id) AS total_demande,
                 (SELECT COUNT(*) FROM mecenat_demandes WHERE dossier_id=d.id) AS nb_demandes,
                 TRIM(COALESCE(p1.prenom,'') || ' ' || COALESCE(p1.nom,'')) AS pilote_projet,
                 TRIM(COALESCE(p2.prenom,'') || ' ' || COALESCE(p2.nom,'')) AS pilote_mecenat,
                 (SELECT nom_affiche FROM engagement_poles WHERE id = d.pole_id) AS pole
               FROM mecenat_dossiers d
               LEFT JOIN benevoles p1 ON p1.id = d.pilote_projet_id
               LEFT JOIN benevoles p2 ON p2.id = d.pilote_mecenat_id
               WHERE (? = '' OR d.type_dossier = ?) AND (? = 0 OR d.pole_id = ?)
               ORDER BY COALESCE(NULLIF(d.date_prevue,''), '9999') , d.id""", (filtre, filtre, filtre_pole, filtre_pole)).fetchall()
    tot_ttc = sum(d["montant_ttc"] or 0 for d in dossiers)
    tot_obt = sum(d["total_subvention_obtenues"] or 0 for d in dossiers)
    return render_template("tresorerie/mecenat/dossiers.html", dossiers=dossiers, annee_courante=datetime.now().year, filtre=filtre, filtre_pole=filtre_pole, poles=poles, types=TYPES_DOSSIER, tot_ttc=tot_ttc, tot_obt=tot_obt,
                           peut_ecrire=has_access("mecenat", "ecriture"))


def _type(f):
    t = _txt(f, "type_dossier")
    return t if t in TYPES_DOSSIER else "investissement"


def _lire_dossier(f):
    return (
        _type(f), _txt(f, "famille"), _txt(f, "objet"), _txt(f, "date_prevue") or None, _montant(f.get("montant_ttc")),
        f.get("pilote_projet_id", type=int), f.get("pilote_mecenat_id", type=int), f.get("pole_id", type=int))


@tresorerie_bp.route("/tresorerie/mecenat/dossier/nouveau", methods=["GET", "POST"])
@login_required
@acces("ecriture")
def mecenat_dossier_nouveau():
    with _connect() as conn:
        if request.method == "POST":
            type_d, famille, objet, date_prevue, ttc, p1, p2, pole = _lire_dossier(request.form)
            if not objet:
                flash("❌ L'objet du dossier est obligatoire.", "danger")
            else:
                cur = conn.execute(
                    "INSERT INTO mecenat_dossiers (type_dossier, famille, objet, date_prevue, montant_ttc, "
                    "pilote_projet_id, pilote_mecenat_id, pole_id, date_modif, user_modif) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (type_d, famille, objet, date_prevue, ttc, p1, p2, pole, _maintenant(), current_user.email))
                conn.commit()
                upload_database()
                write_log(f"🤝 Mécénat : dossier {cur.lastrowid} créé ({objet})")
                flash("✅ Dossier créé. Ajoutez maintenant les mécènes sollicités.", "success")
                return redirect(url_for("tresorerie.mecenat_dossier", dossier_id=cur.lastrowid))
        return render_template("tresorerie/mecenat/dossier_form.html", dossier=None, benevoles=_benevoles(conn),
                               familles=_familles(conn), form=request.form, types=TYPES_DOSSIER, poles=_poles(conn))


@tresorerie_bp.route("/tresorerie/mecenat/dossier/<int:dossier_id>")
@login_required
@acces("lecture")
def mecenat_dossier(dossier_id):
    with _connect() as conn:
        dossier = conn.execute("SELECT * FROM mecenat_dossiers WHERE id = ?", (dossier_id,)).fetchone()
        if not dossier:
            abort(404)
        demandes = conn.execute(
            """SELECT dm.*, m.structure, m.nom, m.prenom FROM mecenat_demandes dm
               JOIN mecenat m ON m.id = dm.mecene_id WHERE dm.dossier_id = ?
               ORDER BY m.structure COLLATE NOCASE""", (dossier_id,)).fetchall()
        mecenes = conn.execute("SELECT id, structure, nom, prenom FROM mecenat ORDER BY structure COLLATE NOCASE").fetchall()
        deja = {d["mecene_id"] for d in demandes}
        return render_template(
            "tresorerie/mecenat/dossier.html", dossier=dossier, demandes=demandes,
            mecenes_dispo=[m for m in mecenes if m["id"] not in deja], benevoles=_benevoles(conn),
            familles=_familles(conn), types=TYPES_DOSSIER, poles=_poles(conn), total_demande=sum(d["montant_demande"] or 0 for d in demandes),
            peut_ecrire=has_access("mecenat", "ecriture"))


@tresorerie_bp.route("/tresorerie/mecenat/dossier/<int:dossier_id>/modifier", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_dossier_modifier(dossier_id):
    type_d, famille, objet, date_prevue, ttc, p1, p2, pole = _lire_dossier(request.form)
    if not objet:
        flash("❌ L'objet du dossier est obligatoire.", "danger")
        return redirect(url_for("tresorerie.mecenat_dossier", dossier_id=dossier_id))
    with _connect() as conn:
        conn.execute(
            "UPDATE mecenat_dossiers SET type_dossier=?, famille=?, objet=?, date_prevue=?, montant_ttc=?, pilote_projet_id=?, "
            "pilote_mecenat_id=?, pole_id=?, date_modif=?, user_modif=? WHERE id=?",
            (type_d, famille, objet, date_prevue, ttc, p1, p2, pole, _maintenant(), current_user.email, dossier_id))
        conn.commit()
    upload_database()
    flash("✅ Dossier enregistré.", "success")
    return redirect(url_for("tresorerie.mecenat_dossier", dossier_id=dossier_id))


@tresorerie_bp.route("/tresorerie/mecenat/dossier/<int:dossier_id>/supprimer", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_dossier_supprimer(dossier_id):
    with _connect() as conn:
        conn.execute("DELETE FROM mecenat_demandes WHERE dossier_id = ?", (dossier_id,))
        conn.execute("DELETE FROM mecenat_dossiers WHERE id = ?", (dossier_id,))
        conn.commit()
    upload_database()
    write_log(f"🗑️ Mécénat : dossier {dossier_id} supprimé par {current_user.email}")
    flash("🗑️ Dossier supprimé.", "success")
    return redirect(url_for("tresorerie.mecenat_dossiers"))


# ------------------------------------------------------------
# Demandes (mécène × dossier)
# ------------------------------------------------------------
@tresorerie_bp.route("/tresorerie/mecenat/dossier/<int:dossier_id>/demande", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_demande_ajouter(dossier_id):
    mecene_id = request.form.get("mecene_id", type=int)
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM mecenat_dossiers WHERE id=?", (dossier_id,)).fetchone():
            abort(404)
        if not mecene_id or not conn.execute("SELECT 1 FROM mecenat WHERE id=?", (mecene_id,)).fetchone():
            flash("❌ Choisissez un mécène.", "danger")
        elif conn.execute("SELECT 1 FROM mecenat_demandes WHERE dossier_id=? AND mecene_id=?",
                          (dossier_id, mecene_id)).fetchone():
            flash("ℹ️ Ce mécène est déjà associé à ce dossier.", "warning")
        else:
            conn.execute(
                "INSERT INTO mecenat_demandes (dossier_id, mecene_id, montant_demande, montant_obtenu, date_versement,"
                " notes, date_modif, user_modif) VALUES (?,?,?,?,?,?,?,?)",
                (dossier_id, mecene_id, _montant(request.form.get("montant_demande")),
                 _montant(request.form.get("montant_obtenu")), _txt(request.form, "date_versement") or None,
                 _txt(request.form, "notes"), _maintenant(), current_user.email))
            _recalcul_total(conn, dossier_id)
            conn.commit()
            upload_database()
            flash("✅ Mécène ajouté au dossier.", "success")
    return redirect(url_for("tresorerie.mecenat_dossier", dossier_id=dossier_id))


@tresorerie_bp.route("/tresorerie/mecenat/demande/<int:demande_id>/modifier", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_demande_modifier(demande_id):
    with _connect() as conn:
        d = conn.execute("SELECT dossier_id FROM mecenat_demandes WHERE id=?", (demande_id,)).fetchone()
        if not d:
            abort(404)
        conn.execute(
            "UPDATE mecenat_demandes SET montant_demande=?, montant_obtenu=?, date_versement=?, notes=?, "
            "date_modif=?, user_modif=? WHERE id=?",
            (_montant(request.form.get("montant_demande")), _montant(request.form.get("montant_obtenu")),
             _txt(request.form, "date_versement") or None, _txt(request.form, "notes"), _maintenant(),
             current_user.email, demande_id))
        _recalcul_total(conn, d["dossier_id"])
        conn.commit()
    upload_database()
    flash("✅ Demande enregistrée.", "success")
    return redirect(url_for("tresorerie.mecenat_dossier", dossier_id=d["dossier_id"]))


@tresorerie_bp.route("/tresorerie/mecenat/demande/<int:demande_id>/supprimer", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_demande_supprimer(demande_id):
    with _connect() as conn:
        d = conn.execute("SELECT dossier_id FROM mecenat_demandes WHERE id=?", (demande_id,)).fetchone()
        if not d:
            abort(404)
        conn.execute("DELETE FROM mecenat_demandes WHERE id=?", (demande_id,))
        _recalcul_total(conn, d["dossier_id"])
        conn.commit()
    upload_database()
    flash("🗑️ Mécène retiré du dossier.", "success")
    return redirect(url_for("tresorerie.mecenat_dossier", dossier_id=d["dossier_id"]))


# ------------------------------------------------------------
# Mécènes
# ------------------------------------------------------------
CHAMPS_MECENE = ("structure", "nom", "prenom", "telephone_fixe", "telephone_portable", "email", "adresse", "cp",
                 "ville", "commentaire", "statut")


@tresorerie_bp.route("/tresorerie/mecenat/mecenes")
@login_required
@acces("lecture")
def mecenat_mecenes():
    filtre = request.args.get("statut") if request.args.get("statut") in STATUTS_MECENE else ""
    with _connect() as conn:
        mecenes = conn.execute(
            """SELECT m.*,
                 (SELECT MAX(substr(date_interaction,1,10)) FROM mecenat_interactions
                    WHERE mecene_id=m.id AND substr(date_interaction,1,10) <= date('now','localtime')) AS dernier_contact,
                 (SELECT MIN(date_relance) FROM mecenat_interactions
                    WHERE mecene_id=m.id AND relance_faite=0 AND COALESCE(date_relance,'') != '') AS prochaine_relance,
                 (SELECT COUNT(*) FROM mecenat_demandes WHERE mecene_id=m.id) AS nb_dossiers,
                 (SELECT COALESCE(SUM(montant_obtenu),0) FROM mecenat_demandes WHERE mecene_id=m.id) AS total_obtenu
               FROM mecenat m WHERE (? = '' OR m.statut = ?)
               ORDER BY m.structure COLLATE NOCASE""", (filtre, filtre)).fetchall()
    return render_template("tresorerie/mecenat/mecenes.html", mecenes=mecenes, filtre=filtre,
                           peut_ecrire=has_access("mecenat", "ecriture"))


@tresorerie_bp.route("/tresorerie/mecenat/mecene/nouveau", methods=["GET", "POST"])
@login_required
@acces("ecriture")
def mecenat_mecene_nouveau():
    if request.method == "POST":
        valeurs = {c: _txt(request.form, c) for c in CHAMPS_MECENE}
        if valeurs["statut"] not in STATUTS_MECENE:
            valeurs["statut"] = "prospect"
        if not valeurs["structure"]:
            flash("❌ La structure (entité du mécène) est obligatoire.", "danger")
        else:
            with _connect() as conn:
                cur = conn.execute(
                    f"INSERT INTO mecenat ({', '.join(CHAMPS_MECENE)}, date_modif, user_modif) "
                    f"VALUES ({', '.join('?' * len(CHAMPS_MECENE))}, ?, ?)",
                    [valeurs[c] for c in CHAMPS_MECENE] + [_maintenant(), current_user.email])
                conn.commit()
            upload_database()
            flash("✅ Mécène créé.", "success")
            retour = request.form.get("retour_dossier", type=int)
            if retour:
                return redirect(url_for("tresorerie.mecenat_dossier", dossier_id=retour))
            return redirect(url_for("tresorerie.mecenat_mecenes"))
    return render_template("tresorerie/mecenat/mecene_form.html", mecene=None, form=request.form,
                           retour_dossier=request.values.get("retour_dossier", type=int))


@tresorerie_bp.route("/tresorerie/mecenat/mecene/<int:mecene_id>", methods=["GET", "POST"])
@login_required
@acces("lecture")
def mecenat_mecene(mecene_id):
    with _connect() as conn:
        mecene = conn.execute("SELECT * FROM mecenat WHERE id = ?", (mecene_id,)).fetchone()
        if not mecene:
            abort(404)
        if request.method == "POST":
            if not has_access("mecenat", "ecriture"):
                abort(403)
            valeurs = {c: _txt(request.form, c) for c in CHAMPS_MECENE}
            if valeurs["statut"] not in STATUTS_MECENE:
                valeurs["statut"] = mecene["statut"]
            if not valeurs["structure"]:
                flash("❌ La structure (entité du mécène) est obligatoire.", "danger")
            else:
                conn.execute(
                    f"UPDATE mecenat SET {', '.join(c + '=?' for c in CHAMPS_MECENE)}, date_modif=?, user_modif=? "
                    "WHERE id=?", [valeurs[c] for c in CHAMPS_MECENE] + [_maintenant(), current_user.email, mecene_id])
                conn.commit()
                upload_database()
                flash("✅ Mécène enregistré.", "success")
                return redirect(url_for("tresorerie.mecenat_mecenes"))
        dossiers = conn.execute(
            """SELECT dm.*, d.objet, d.famille FROM mecenat_demandes dm
               JOIN mecenat_dossiers d ON d.id = dm.dossier_id WHERE dm.mecene_id = ? ORDER BY d.date_prevue""",
            (mecene_id,)).fetchall()
        historique = _historique(conn, mecene_id)
        benevoles = _benevoles(conn)
        tous_dossiers = conn.execute("SELECT id, objet FROM mecenat_dossiers ORDER BY objet COLLATE NOCASE").fetchall()
    return render_template("tresorerie/mecenat/mecene_form.html", mecene=mecene, form=request.form, dossiers=dossiers,
                           historique=historique, benevoles=benevoles, tous_dossiers=tous_dossiers,
                           types_interaction=TYPES_INTERACTION, retour_dossier=None,
                           peut_ecrire=has_access("mecenat", "ecriture"))


@tresorerie_bp.route("/tresorerie/mecenat/mecene/<int:mecene_id>/supprimer", methods=["POST"])
@login_required
@acces("ecriture")
def mecenat_mecene_supprimer(mecene_id):
    with _connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM mecenat_demandes WHERE mecene_id=?", (mecene_id,)).fetchone()[0]
        if n:
            flash(f"❌ Suppression impossible : ce mécène est associé à {n} dossier(s). Retirez-le d'abord des dossiers.",
                  "danger")
            return redirect(url_for("tresorerie.mecenat_mecene", mecene_id=mecene_id))
        n = conn.execute("SELECT COUNT(*) FROM mecenat_interactions WHERE mecene_id=?", (mecene_id,)).fetchone()[0]
        if n:
            flash(f"❌ Suppression impossible : {n} échange(s) sont enregistrés. Passez le mécène en « Inactif » "
                  "plutôt que de le supprimer.", "danger")
            return redirect(url_for("tresorerie.mecenat_mecene", mecene_id=mecene_id))
        conn.execute("DELETE FROM mecenat WHERE id=?", (mecene_id,))
        conn.commit()
    upload_database()
    flash("🗑️ Mécène supprimé.", "success")
    return redirect(url_for("tresorerie.mecenat_mecenes"))
