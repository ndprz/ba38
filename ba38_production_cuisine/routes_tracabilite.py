# ============================================================
# 🔎 Traçabilité de fabrication : lots fournisseurs, étapes,
#     quantités conditionnées, validation (plat témoin)
# ============================================================

import os
import sqlite3

from flask import request, redirect, url_for, flash, send_file, abort
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_production_cuisine import production_cuisine_bp
from ba38_production_cuisine.utils import (
    _connect, now_paris_str, upload_dir_traca_lot, save_uploaded_files,
    etape_bloquante, heure_fin_max_precedentes,
)

CONFORMITE_CHOICES = ("conforme", "non_conforme")


def _clean_conformite(val):
    return val if val in CONFORMITE_CHOICES else None


def _redirect_run(production_id):
    return redirect(url_for("production_cuisine.detail_production", production_id=production_id))


@production_cuisine_bp.route("/photo/traca/<int:photo_id>")
@login_required
@require_access("production_cuisine", "lecture")
def photo_traca(photo_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        photo = conn.execute(
            "SELECT chemin_fichier FROM cuisine_traca_photos WHERE id = ?", (photo_id,)
        ).fetchone()
    chemin = photo["chemin_fichier"] if photo else None
    if not chemin or not os.path.exists(chemin):
        abort(404)
    return send_file(chemin, as_attachment=False)


# ------------------------------------------------------------
# 📦 Lot de traçabilité fournisseur (photos produit + étiquette)
# ------------------------------------------------------------
@production_cuisine_bp.route("/<int:production_id>/lot", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def ajouter_lot(production_id):
    benevole = (request.form.get("benevole") or "").strip()
    fournisseur_id = request.form.get("fournisseur_id") or None
    commentaire = (request.form.get("commentaire") or "").strip() or None

    if not fournisseur_id:
        flash("⚠️ Merci de choisir un fournisseur.", "warning")
        return _redirect_run(production_id)

    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO cuisine_traca_lots
                   (production_id, fournisseur_id, commentaire, user_creation)
                   VALUES (?, ?, ?, ?)""",
                (production_id, fournisseur_id, commentaire, benevole or None),
            )
            lot_id = cur.lastrowid

            dossier = upload_dir_traca_lot(production_id, lot_id)
            fichiers = request.files.getlist("photos")
            chemins = save_uploaded_files(fichiers, dossier)
            for ordre, chemin in enumerate(chemins):
                cur.execute(
                    "INSERT INTO cuisine_traca_photos (lot_id, chemin_fichier, ordre) VALUES (?, ?, ?)",
                    (lot_id, chemin, ordre),
                )

            conn.commit()
        upload_database()
        flash("✅ Lot de traçabilité ajouté.", "success")
    except Exception as e:
        write_log(f"❌ Erreur ajout lot traçabilité cuisine : {e}")
        flash("❌ Erreur lors de l'ajout du lot.", "danger")

    return _redirect_run(production_id)


# ------------------------------------------------------------
# ⏱️ Étapes de fabrication (démarrer / terminer)
# ------------------------------------------------------------
@production_cuisine_bp.route("/<int:production_id>/etape", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def etape_production(production_id):
    action = request.form.get("action")  # 'demarrer' | 'terminer' | 'non_applicable'
    etape_code = request.form.get("etape_code")
    benevole = (request.form.get("benevole") or "").strip()

    if not etape_code or action not in ("demarrer", "terminer", "non_applicable"):
        flash("⚠️ Étape ou action invalide.", "warning")
        return _redirect_run(production_id)

    with _connect() as conn:
        conn.row_factory = sqlite3.Row

        etape_ref = conn.execute(
            "SELECT * FROM cuisine_etapes_ref WHERE code = ?", (etape_code,)
        ).fetchone()
        if not etape_ref:
            flash("⛔ Étape inconnue.", "danger")
            return _redirect_run(production_id)

        # Ordre métier : on ne peut pas démarrer/terminer/passer une étape
        # tant que les étapes précédentes (non optionnelles) ne sont pas
        # résolues (terminées ou marquées non applicables).
        if action in ("demarrer", "non_applicable"):
            bloquante = etape_bloquante(conn, production_id, etape_code)
            if bloquante:
                flash(f"🔒 Terminez d'abord « {bloquante} » avant « {etape_ref['libelle']} ».", "warning")
                return _redirect_run(production_id)

        cur = conn.cursor()

        if action == "non_applicable":
            existe_deja = conn.execute(
                """SELECT 1 FROM cuisine_production_etapes
                   WHERE production_id = ? AND etape_code = ?""",
                (production_id, etape_code),
            ).fetchone()
            if existe_deja:
                flash("⚠️ Cette étape a déjà été démarrée ou résolue.", "warning")
                return _redirect_run(production_id)
            cur.execute(
                """INSERT INTO cuisine_production_etapes
                   (production_id, etape_code, heure_fin, non_applicable, user_creation)
                   VALUES (?, ?, ?, 1, ?)""",
                (production_id, etape_code, now_paris_str(), benevole or None),
            )
            conn.commit()
            flash(f"🚫 {etape_ref['libelle']} marquée non applicable.", "success")

        elif action == "demarrer":
            temperature_debut = request.form.get("temperature_debut") or None
            cur.execute(
                """INSERT INTO cuisine_production_etapes
                   (production_id, etape_code, heure_debut, temperature_debut, user_creation)
                   VALUES (?, ?, ?, ?, ?)""",
                (production_id, etape_code, now_paris_str(), temperature_debut, benevole or None),
            )
            conn.commit()
            flash(f"▶️ {etape_ref['libelle']} démarrée.", "success")

        else:  # terminer
            derniere = conn.execute(
                """SELECT id FROM cuisine_production_etapes
                   WHERE production_id = ? AND etape_code = ? AND heure_fin IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (production_id, etape_code),
            ).fetchone()

            if not derniere and etape_ref["saisie_debut"]:
                flash("⚠️ Aucune étape en cours à terminer pour cette étape.", "warning")
                return _redirect_run(production_id)

            if not derniere:
                # Étape sans phase "démarrer" (ex. refroidissement à l'eau) :
                # elle n'est pas encore journalisée, donc l'ordre métier n'a
                # pas encore été vérifié (contrairement à "demarrer").
                bloquante = etape_bloquante(conn, production_id, etape_code)
                if bloquante:
                    flash(f"🔒 Terminez d'abord « {bloquante} » avant « {etape_ref['libelle']} ».", "warning")
                    return _redirect_run(production_id)

            temperature = request.form.get("temperature") or None
            cellule_numero = request.form.get("cellule_numero") or None
            conforme = _clean_conformite(request.form.get("conforme"))
            commentaire = (request.form.get("commentaire") or "").strip() or None
            heure_fin = now_paris_str()

            repere = heure_fin_max_precedentes(conn, production_id, etape_code)
            if repere and heure_fin < repere:
                flash(
                    f"⚠️ Heure incohérente : « {etape_ref['libelle']} » est enregistrée avant "
                    f"la fin d'une étape précédente ({repere}). Vérifiez, ou corrigez l'heure ensuite.",
                    "warning",
                )

            if derniere:
                cur.execute(
                    """UPDATE cuisine_production_etapes
                       SET heure_fin = ?, temperature = ?, cellule_numero = ?,
                           conforme = ?, commentaire = ?, user_modif = ?
                       WHERE id = ?""",
                    (heure_fin, temperature, cellule_numero, conforme, commentaire,
                     benevole or None, derniere["id"]),
                )
            else:
                # Étape sans phase "démarrer" (ex. cuisson : le document ne
                # donne que la "fin de cuisson") : on journalise directement
                # une ligne terminée, sans heure_debut.
                cur.execute(
                    """INSERT INTO cuisine_production_etapes
                       (production_id, etape_code, heure_fin, temperature,
                        cellule_numero, conforme, commentaire, user_creation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (production_id, etape_code, heure_fin, temperature,
                     cellule_numero, conforme, commentaire, benevole or None),
                )
            conn.commit()
            flash(f"⏹️ {etape_ref['libelle']} terminée.", "success")

    upload_database()
    return _redirect_run(production_id)


def _datetime_local_vers_stockage(valeur):
    """Convertit la valeur d'un <input type="datetime-local"> ('AAAA-MM-JJTHH:MM')
    vers le format de stockage 'AAAA-MM-JJ HH:MM:SS' utilisé partout ailleurs."""
    valeur = (valeur or "").strip()
    if not valeur:
        return None
    valeur = valeur.replace("T", " ")
    if len(valeur) == 16:  # pas de secondes
        valeur += ":00"
    return valeur


# ------------------------------------------------------------
# ✏️ Correction d'une étape déjà enregistrée (heure/température oubliées
#     sur le moment, à corriger a posteriori) — pas une ré-exécution de la
#     tâche, juste un ajustement des valeurs saisies.
# ------------------------------------------------------------
@production_cuisine_bp.route("/<int:production_id>/etape/<int:etape_id>/corriger", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def corriger_etape_production(production_id, etape_id):
    benevole = (request.form.get("benevole") or "").strip()

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        ligne = conn.execute(
            """SELECT e.*, r.libelle FROM cuisine_production_etapes e
               JOIN cuisine_etapes_ref r ON r.code = e.etape_code
               WHERE e.id = ? AND e.production_id = ?""",
            (etape_id, production_id),
        ).fetchone()
        if not ligne:
            flash("⛔ Étape introuvable.", "danger")
            return _redirect_run(production_id)

        heure_debut = _datetime_local_vers_stockage(request.form.get("heure_debut"))
        heure_fin = _datetime_local_vers_stockage(request.form.get("heure_fin"))
        temperature_debut = request.form.get("temperature_debut") or None
        temperature = request.form.get("temperature") or None
        cellule_numero = request.form.get("cellule_numero") or None
        conforme = _clean_conformite(request.form.get("conforme"))

        if not heure_fin:
            flash("⚠️ L'heure de fin est obligatoire.", "warning")
            return _redirect_run(production_id)

        if heure_debut and heure_fin and heure_debut > heure_fin:
            flash("⚠️ L'heure de début est après l'heure de fin — vérifiez la correction.", "warning")
            return _redirect_run(production_id)

        repere = heure_fin_max_precedentes(conn, production_id, ligne["etape_code"])
        if repere and heure_fin and heure_fin < repere:
            flash(
                f"⚠️ Heure incohérente : « {ligne['libelle']} » est maintenant enregistrée avant "
                f"la fin d'une étape précédente ({repere}).",
                "warning",
            )

        conn.execute(
            """UPDATE cuisine_production_etapes
               SET heure_debut = ?, heure_fin = ?, temperature_debut = ?, temperature = ?,
                   cellule_numero = ?, conforme = ?, user_modif = ?
               WHERE id = ?""",
            (heure_debut, heure_fin, temperature_debut, temperature,
             cellule_numero, conforme, benevole or None, etape_id),
        )
        conn.commit()

    upload_database()
    flash("✏️ Étape corrigée.", "success")
    return _redirect_run(production_id)


# ------------------------------------------------------------
# 🔢 Quantités conditionnées (1/2, 1/4, 1/8)
# ------------------------------------------------------------
@production_cuisine_bp.route("/<int:production_id>/quantites", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def quantites_production(production_id):
    try:
        with _connect() as conn:
            cur = conn.cursor()
            for taille in ("1/2", "1/4", "1/8"):
                champ = "qte_" + taille.replace("/", "_")
                try:
                    quantite = int(request.form.get(champ) or 0)
                except ValueError:
                    quantite = 0
                cur.execute(
                    """
                    INSERT INTO cuisine_production_quantites (production_id, taille, quantite)
                    VALUES (?, ?, ?)
                    ON CONFLICT(production_id, taille) DO UPDATE SET quantite = excluded.quantite
                    """,
                    (production_id, taille, quantite),
                )
            conn.commit()
        upload_database()
        flash("✅ Quantités enregistrées.", "success")
    except Exception as e:
        write_log(f"❌ Erreur enregistrement quantités cuisine : {e}")
        flash("❌ Erreur lors de l'enregistrement des quantités.", "danger")

    return _redirect_run(production_id)


# ------------------------------------------------------------
# ✅ Validation finale (plat témoin obligatoire si "oui")
# ------------------------------------------------------------
@production_cuisine_bp.route("/<int:production_id>/validation", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def validation_production(production_id):
    plat_temoin = request.form.get("plat_temoin")
    if plat_temoin not in ("oui", "non"):
        plat_temoin = "non"

    poids_g = request.form.get("plat_temoin_poids_g") or None
    valide_par = (request.form.get("valide_par") or request.form.get("benevole") or "").strip() or None
    commentaire = (request.form.get("commentaire") or "").strip() or None

    if plat_temoin == "oui" and not poids_g:
        flash("⚠️ Le poids du plat témoin est obligatoire si un plat témoin est conservé.", "warning")
        return _redirect_run(production_id)

    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO cuisine_production_validations
                (production_id, plat_temoin, plat_temoin_poids_g, valide_par, commentaire)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(production_id) DO UPDATE SET
                    plat_temoin = excluded.plat_temoin,
                    plat_temoin_poids_g = excluded.plat_temoin_poids_g,
                    valide_par = excluded.valide_par,
                    commentaire = excluded.commentaire,
                    date_validation = datetime('now','utc')
                """,
                (production_id, plat_temoin, poids_g if plat_temoin == "oui" else None,
                 valide_par, commentaire),
            )
            cur.execute(
                "UPDATE cuisine_productions SET statut = 'terminee', user_modif = ? WHERE id = ?",
                (valide_par, production_id),
            )
            conn.commit()
        upload_database()
        flash("✅ Production validée.", "success")
    except Exception as e:
        write_log(f"❌ Erreur validation production cuisine : {e}")
        flash("❌ Erreur lors de la validation.", "danger")

    return _redirect_run(production_id)
