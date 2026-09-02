import sqlite3
from datetime import datetime
from threading import Thread

from flask import (
    render_template, request, redirect, url_for, flash, current_app
)
from flask_login import login_required, current_user

from ba38_sms import sms_bp
from ba38_sms.smsfactor_client import (
    normalize_phone_fr, envoyer_sms_reel, is_dev_environment, get_dev_test_number
)
from ba38_utilitaires.core import (
    get_db_connection, get_db_path, write_log, require_access, has_access, upload_database,
    envoyer_mail
)

ALERTE_MAIL_DESTINATAIRE = "ba380.informatique2@banquealimentaire.org"

TYPE_BENE_PARAM = "type_benevole"


def _get_fonction_fields():
    """Champs booléens 'fonction' des bénévoles (dupliqué depuis ba38_benevoles,
    volontairement, pour ne pas coupler les deux modules)."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT field_name
        FROM field_groups
        WHERE appli = 'benevoles' AND LOWER(group_name) LIKE '%fonction%'
        ORDER BY display_order
    """).fetchall()
    conn.close()
    return [(r[0], r[0].replace("_", " ").capitalize()) for r in rows]


def _get_type_benevole_options():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT param_value FROM parametres WHERE param_name = ? ORDER BY id",
        (TYPE_BENE_PARAM,)
    ).fetchall()
    conn.close()
    return [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]


def _charger_benevoles_sms(fonctions=None, types=None):
    """
    Bénévoles ayant un téléphone portable, filtrés (union) par fonction et/ou
    type_benevole. Sans filtre : tous les bénévoles avec téléphone.
    """
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    valid_fonctions = {f for f, _ in _get_fonction_fields()}
    fonctions = [f for f in (fonctions or []) if f in valid_fonctions]

    valid_types = set(_get_type_benevole_options())
    types = [t for t in (types or []) if t in valid_types]

    query = (
        "SELECT id, nom, prenom, telephone_portable, type_benevole "
        "FROM benevoles "
        "WHERE telephone_portable IS NOT NULL AND TRIM(telephone_portable) != ''"
    )
    params = []
    clauses = []

    if fonctions:
        clauses.append("(" + " OR ".join(f"{f}=?" for f in fonctions) + ")")
        params += ["oui"] * len(fonctions)

    if types:
        clauses.append(f"type_benevole IN ({','.join('?' * len(types))})")
        params += types

    if clauses:
        query += " AND (" + " OR ".join(clauses) + ")"

    query += " ORDER BY nom COLLATE NOCASE"

    rows = cur.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _charger_textes():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, titre, contenu FROM sms_textes ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_sms_alerte_active():
    """Dernière alerte SMS non résolue (ex: crédit SmsFactor épuisé), ou None.
    Enregistrée comme global Jinja (ba38.py) pour affichage en bandeau."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sms_alertes WHERE resolu = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def envoyer_sms_lot_background(app, db_path, lot_id, destinataires, texte):
    """
    Tourne dans un Thread séparé (même pattern que envoyer_indicateurs_background,
    ba38_utilitaires/emails.py) : envoie les SMS un par un via SmsFactor et met à
    jour la table de suivi sms_envois, hors du cycle requête HTTP.

    En environnement DEV, un seul SMS réel part (vers SMS_DEV_TEST_NUMBER) quel
    que soit le nombre de destinataires ; les autres lignes sont marquées
    'simule_dev' sans appel API.

    Si SmsFactor renvoie status=-3 ("Not enough credits"), l'envoi est
    interrompu (inutile de retenter les suivants, ils échoueront pareil),
    une alerte est enregistrée (sms_alertes, bandeau app) et un mail part à
    l'administrateur.
    """
    with app.app_context():
        mode_test = is_dev_environment()
        test_number = get_dev_test_number()
        real_sent = False
        credit_epuise = False

        conn = sqlite3.connect(db_path)

        for dest in destinataires:
            envoi_id = dest["envoi_id"]
            numero = dest["numero"]
            now_iso = datetime.now().isoformat(timespec="seconds")

            if credit_epuise:
                conn.execute(
                    "UPDATE sms_envois SET statut='erreur', erreur=?, date_envoi=? WHERE id=?",
                    ("Crédit SmsFactor épuisé, envoi interrompu", now_iso, envoi_id),
                )
                conn.commit()
                continue

            try:
                if mode_test and real_sent:
                    resultat = None  # destinataires suivants simulés, pas d'appel API
                elif mode_test:
                    resultat = envoyer_sms_reel(test_number, f"[TEST] {texte}")
                    real_sent = True
                else:
                    resultat = envoyer_sms_reel(numero, texte)

                if resultat is None:
                    statut, ticket, cout, erreur = "simule_dev", None, 0, None
                else:
                    code = resultat.get("status")
                    statut = "envoye" if code == 1 else "erreur"
                    ticket = resultat.get("ticket")
                    cout = resultat.get("cost")
                    erreur = None if statut == "envoye" else f"{resultat.get('message')} (code {code})"
                    if code == -3:
                        credit_epuise = True

                conn.execute(
                    "UPDATE sms_envois SET statut=?, ticket_smsfactor=?, cout=?, erreur=?, date_envoi=? WHERE id=?",
                    (statut, ticket, cout, erreur, now_iso, envoi_id),
                )
                conn.commit()

            except Exception as e:
                write_log(f"❌ Erreur envoi SMS (envoi id={envoi_id}) : {e}")
                conn.execute(
                    "UPDATE sms_envois SET statut='erreur', erreur=?, date_envoi=? WHERE id=?",
                    (str(e), now_iso, envoi_id),
                )
                conn.commit()

        nb_ok = conn.execute(
            "SELECT COUNT(*) FROM sms_envois WHERE lot_id=? AND statut='envoye'", (lot_id,)
        ).fetchone()[0]
        nb_erreur = conn.execute(
            "SELECT COUNT(*) FROM sms_envois WHERE lot_id=? AND statut='erreur'", (lot_id,)
        ).fetchone()[0]

        conn.execute(
            "UPDATE sms_lots SET nb_ok=?, nb_erreur=?, date_fin=? WHERE id=?",
            (nb_ok, nb_erreur, datetime.now().isoformat(timespec="seconds"), lot_id),
        )
        conn.commit()

        if credit_epuise:
            message = (
                f"⚠️ Crédit SmsFactor épuisé pendant l'envoi SMS #{lot_id} "
                f"({nb_ok} envoyé(s), {nb_erreur} en erreur sur {len(destinataires)} destinataire(s)). "
                f"Pensez à recharger le compte SmsFactor."
            )
            conn.execute(
                "INSERT INTO sms_alertes (message, lot_id, date_creation) VALUES (?, ?, ?)",
                (message, lot_id, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            try:
                envoyer_mail(
                    sujet="🚨 Crédit SmsFactor épuisé",
                    destinataires=[ALERTE_MAIL_DESTINATAIRE],
                    texte=message,
                )
            except Exception as e:
                write_log(f"⚠️ Erreur envoi mail alerte crédit SMS : {e}")

        conn.close()

        write_log(f"📤 Envoi SMS (lot {lot_id}) terminé : {nb_ok} ok, {nb_erreur} erreur(s), mode_test={mode_test}")


@sms_bp.route("/sms/envoi_benevoles", methods=["GET", "POST"])
@login_required
@require_access("sms_benevoles", "lecture")
def envoi_sms_benevoles():
    selected_fonctions = request.values.getlist("fonctions")
    selected_types = request.values.getlist("types")

    all_fonctions = _get_fonction_fields()
    all_types = _get_type_benevole_options()
    textes = _charger_textes()

    if request.method == "POST" and request.form.get("action") == "envoyer":
        selected_ids = [int(i) for i in request.form.getlist("destinataires") if i.strip().isdigit()]
        texte = (request.form.get("message") or "").strip()

        if not selected_ids:
            flash("❌ Merci de sélectionner au moins un destinataire.", "danger")
        elif not texte:
            flash("❌ Le message ne peut pas être vide.", "danger")
        elif not has_access("sms_benevoles", "ecriture"):
            flash("⛔ Vous n'avez pas le droit d'envoyer des SMS.", "danger")
        else:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(selected_ids))
            benevoles = conn.execute(
                f"SELECT id, nom, prenom, telephone_portable FROM benevoles WHERE id IN ({placeholders})",
                selected_ids,
            ).fetchall()

            now_iso = datetime.now().isoformat(timespec="seconds")
            mode_test = is_dev_environment()

            cur = conn.execute(
                "INSERT INTO sms_lots (envoye_par, texte, filtre_description, nb_destinataires, mode_test, date_creation) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    getattr(current_user, "email", "") or "",
                    texte,
                    f"{len(benevoles)} bénévole(s) sélectionné(s)",
                    len(benevoles),
                    1 if mode_test else 0,
                    now_iso,
                ),
            )
            lot_id = cur.lastrowid

            a_envoyer = []
            for b in benevoles:
                numero = normalize_phone_fr(b["telephone_portable"])
                row = conn.execute(
                    "INSERT INTO sms_envois (lot_id, benevole_id, numero, statut, erreur) VALUES (?, ?, ?, ?, ?)",
                    (
                        lot_id,
                        b["id"],
                        numero or (b["telephone_portable"] or ""),
                        "en_attente" if numero else "erreur",
                        None if numero else "Numéro de téléphone invalide",
                    ),
                )
                if numero:
                    a_envoyer.append({"envoi_id": row.lastrowid, "numero": numero})

            conn.commit()
            conn.close()
            upload_database()

            app_obj = current_app._get_current_object()
            Thread(
                target=envoyer_sms_lot_background,
                args=(app_obj, get_db_path(), lot_id, a_envoyer, texte),
                daemon=True,
            ).start()

            flash(f"✅ Envoi lancé pour {len(benevoles)} destinataire(s).", "success")
            return redirect(url_for("sms.lot_detail", lot_id=lot_id))

    destinataires = _charger_benevoles_sms(fonctions=selected_fonctions, types=selected_types)

    return render_template(
        "sms/envoi_sms_benevoles.html",
        all_fonctions=all_fonctions,
        all_types=all_types,
        selected_fonctions=selected_fonctions,
        selected_types=selected_types,
        destinataires=destinataires,
        textes=textes,
    )


@sms_bp.route("/sms/lot/<int:lot_id>")
@login_required
@require_access("sms_benevoles", "lecture")
def lot_detail(lot_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    lot = conn.execute("SELECT * FROM sms_lots WHERE id = ?", (lot_id,)).fetchone()
    if not lot:
        conn.close()
        flash("❌ Envoi introuvable.", "danger")
        return redirect(url_for("sms.envoi_sms_benevoles"))

    envois = conn.execute("""
        SELECT e.*, b.nom, b.prenom
        FROM sms_envois e
        JOIN benevoles b ON b.id = e.benevole_id
        WHERE e.lot_id = ?
        ORDER BY b.nom COLLATE NOCASE
    """, (lot_id,)).fetchall()
    conn.close()

    en_cours = lot["date_fin"] is None

    return render_template(
        "sms/lot_detail.html",
        lot=dict(lot),
        envois=[dict(e) for e in envois],
        en_cours=en_cours,
    )


@sms_bp.route("/sms/textes", methods=["GET", "POST"])
@login_required
@require_access("sms_benevoles", "ecriture")
def textes_predefinis_sms():
    if request.method == "POST":
        titre = (request.form.get("titre") or "").strip()
        contenu = (request.form.get("contenu") or "").rstrip()
        if not titre or not contenu:
            flash("❌ Merci de renseigner un titre et un contenu.", "danger")
            return redirect(url_for("sms.textes_predefinis_sms"))

        conn = get_db_connection()
        conn.execute(
            "INSERT INTO sms_textes (titre, contenu, date_modification) VALUES (?, ?, ?)",
            (titre, contenu, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()
        upload_database()
        flash("✅ Texte ajouté.", "success")
        return redirect(url_for("sms.textes_predefinis_sms"))

    return render_template("sms/textes.html", textes=_charger_textes())


@sms_bp.route("/sms/textes/<int:tid>/edit", methods=["GET", "POST"])
@login_required
@require_access("sms_benevoles", "ecriture")
def edit_texte_sms(tid):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    if request.method == "POST":
        titre = (request.form.get("titre") or "").strip()
        contenu = (request.form.get("contenu") or "").rstrip()
        if not titre or not contenu:
            conn.close()
            flash("❌ Merci de renseigner un titre et un contenu.", "danger")
            return redirect(url_for("sms.edit_texte_sms", tid=tid))

        conn.execute(
            "UPDATE sms_textes SET titre = ?, contenu = ?, date_modification = ? WHERE id = ?",
            (titre, contenu, datetime.now().isoformat(timespec="seconds"), tid),
        )
        conn.commit()
        conn.close()
        upload_database()
        flash("✅ Texte mis à jour.", "success")
        return redirect(url_for("sms.textes_predefinis_sms"))

    row = conn.execute("SELECT id, titre, contenu FROM sms_textes WHERE id = ?", (tid,)).fetchone()
    conn.close()

    if not row:
        flash("❌ Texte introuvable.", "danger")
        return redirect(url_for("sms.textes_predefinis_sms"))

    return render_template(
        "sms/textes.html",
        textes=[dict(row)],
        edit_mode=True,
        edit_id=row["id"],
        edit_titre=row["titre"],
        edit_contenu=row["contenu"],
    )


@sms_bp.route("/sms/textes/<int:tid>/delete", methods=["POST"])
@login_required
@require_access("sms_benevoles", "ecriture")
def delete_texte_sms(tid):
    conn = get_db_connection()
    conn.execute("DELETE FROM sms_textes WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    upload_database()
    flash("🗑️ Texte supprimé.", "warning")
    return redirect(url_for("sms.textes_predefinis_sms"))


@sms_bp.route("/sms/alertes/<int:alerte_id>/resoudre", methods=["POST"])
@login_required
@require_access("sms_benevoles", "ecriture")
def resoudre_alerte_sms(alerte_id):
    conn = get_db_connection()
    conn.execute(
        "UPDATE sms_alertes SET resolu=1, resolu_par=?, date_resolu=? WHERE id=?",
        (
            getattr(current_user, "email", "") or "",
            datetime.now().isoformat(timespec="seconds"),
            alerte_id,
        ),
    )
    conn.commit()
    conn.close()
    flash("✅ Alerte marquée comme résolue.", "success")
    return redirect(request.referrer or url_for("sms.envoi_sms_benevoles"))
