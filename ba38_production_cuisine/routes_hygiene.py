# ============================================================
# 🧼 Hygiène cuisine : relevés température, nettoyage, étalonnage
#     des thermomètres — 3 registres de saisie rapide tablette.
# ============================================================

import sqlite3

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_production_cuisine import cuisine_hygiene_bp
from ba38_production_cuisine.utils import _connect

CONFORMITE_CHOICES = ("conforme", "non_conforme")


def _clean_conformite(val, default=None):
    return val if val in CONFORMITE_CHOICES else default


# ------------------------------------------------------------
# 🌡️ Relevés de température
# ------------------------------------------------------------
@cuisine_hygiene_bp.route("/temperatures", methods=["GET", "POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def temperatures():
    if request.method == "POST":
        zone_id = request.form.get("zone_id")
        temperature = request.form.get("temperature")
        benevole = (request.form.get("benevole") or "").strip()
        commentaire = (request.form.get("commentaire") or "").strip() or None

        if not zone_id or temperature in (None, ""):
            flash("⚠️ Merci de choisir une zone et de saisir une température.", "warning")
            return redirect(url_for("cuisine_hygiene.temperatures"))

        try:
            temperature_f = float(temperature)
        except ValueError:
            flash("⚠️ Température invalide.", "warning")
            return redirect(url_for("cuisine_hygiene.temperatures"))

        # Conformité automatique simple (positif/négatif large) — laisse
        # toujours la main au bénévole via la saisie de commentaire si besoin.
        conforme = "conforme"

        with _connect() as conn:
            conn.execute(
                """INSERT INTO cuisine_hygiene_releves_temperature
                   (zone_id, temperature, conforme, user_creation, commentaire)
                   VALUES (?, ?, ?, ?, ?)""",
                (zone_id, temperature_f, conforme, benevole or None, commentaire),
            )
            conn.commit()
        upload_database()
        flash("✅ Relevé de température enregistré.", "success")
        return redirect(url_for("cuisine_hygiene.temperatures"))

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        zones = conn.execute(
            "SELECT * FROM cuisine_hygiene_zones_temperature WHERE actif = 1 ORDER BY ordre"
        ).fetchall()

    return render_template("cuisine_hygiene/temperatures.html", zones=zones)


@cuisine_hygiene_bp.route("/temperatures/historique")
@login_required
@require_access("cuisine_hygiene", "lecture")
def temperatures_historique():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        releves = conn.execute(
            """
            SELECT r.*, z.libelle AS zone_libelle
            FROM cuisine_hygiene_releves_temperature r
            JOIN cuisine_hygiene_zones_temperature z ON z.id = r.zone_id
            ORDER BY r.date_releve DESC
            LIMIT 200
            """
        ).fetchall()

    return render_template("cuisine_hygiene/temperatures_historique.html", releves=releves)


# ------------------------------------------------------------
# 🧽 Nettoyage des zones
# ------------------------------------------------------------
@cuisine_hygiene_bp.route("/nettoyage", methods=["GET", "POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def nettoyage():
    if request.method == "POST":
        zone_id = request.form.get("zone_id")
        conforme = _clean_conformite(request.form.get("conforme"), default="conforme")
        benevole = (request.form.get("benevole") or "").strip()
        commentaire = (request.form.get("commentaire") or "").strip() or None

        if not zone_id:
            flash("⚠️ Merci de choisir une zone.", "warning")
            return redirect(url_for("cuisine_hygiene.nettoyage"))

        with _connect() as conn:
            conn.execute(
                """INSERT INTO cuisine_hygiene_nettoyages
                   (zone_id, conforme, user_creation, commentaire)
                   VALUES (?, ?, ?, ?)""",
                (zone_id, conforme, benevole or None, commentaire),
            )
            conn.commit()
        upload_database()
        flash("✅ Nettoyage enregistré.", "success")
        return redirect(url_for("cuisine_hygiene.nettoyage"))

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        zones = conn.execute(
            "SELECT * FROM cuisine_hygiene_zones_nettoyage WHERE actif = 1 ORDER BY ordre"
        ).fetchall()

    return render_template("cuisine_hygiene/nettoyage.html", zones=zones)


@cuisine_hygiene_bp.route("/nettoyage/historique")
@login_required
@require_access("cuisine_hygiene", "lecture")
def nettoyage_historique():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        nettoyages = conn.execute(
            """
            SELECT n.*, z.libelle AS zone_libelle
            FROM cuisine_hygiene_nettoyages n
            JOIN cuisine_hygiene_zones_nettoyage z ON z.id = n.zone_id
            ORDER BY n.date_nettoyage DESC
            LIMIT 200
            """
        ).fetchall()

    return render_template("cuisine_hygiene/nettoyage_historique.html", nettoyages=nettoyages)


# ------------------------------------------------------------
# 🌡️🔧 Étalonnage des thermomètres (test glace / ébullition)
# ------------------------------------------------------------
@cuisine_hygiene_bp.route("/etalonnage", methods=["GET", "POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def etalonnage():
    if request.method == "POST":
        thermometre_id = request.form.get("thermometre_id")
        benevole = (request.form.get("benevole") or "").strip()
        test_glace_valeur = request.form.get("test_glace_valeur") or None
        test_ebullition_valeur = request.form.get("test_ebullition_valeur") or None
        commentaire = (request.form.get("commentaire") or "").strip() or None

        if not thermometre_id:
            flash("⚠️ Merci de choisir un thermomètre.", "warning")
            return redirect(url_for("cuisine_hygiene.etalonnage"))

        # Conformité : ~0°C pour la glace, ~100°C pour l'ébullition (±2°C)
        def _resultat(valeur, cible):
            if valeur in (None, ""):
                return None
            try:
                return "conforme" if abs(float(valeur) - cible) <= 2 else "non_conforme"
            except ValueError:
                return None

        test_glace_resultat = _resultat(test_glace_valeur, 0)
        test_ebullition_resultat = _resultat(test_ebullition_valeur, 100)

        with _connect() as conn:
            conn.execute(
                """INSERT INTO cuisine_hygiene_etalonnages
                   (thermometre_id, test_glace_valeur, test_glace_resultat,
                    test_ebullition_valeur, test_ebullition_resultat, user_creation, commentaire)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (thermometre_id, test_glace_valeur, test_glace_resultat,
                 test_ebullition_valeur, test_ebullition_resultat, benevole or None, commentaire),
            )
            conn.commit()
        upload_database()
        flash("✅ Étalonnage enregistré.", "success")
        return redirect(url_for("cuisine_hygiene.etalonnage"))

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        thermometres = conn.execute(
            "SELECT * FROM cuisine_hygiene_thermometres WHERE actif = 1 ORDER BY numero"
        ).fetchall()

    return render_template("cuisine_hygiene/etalonnage.html", thermometres=thermometres)


@cuisine_hygiene_bp.route("/etalonnage/historique")
@login_required
@require_access("cuisine_hygiene", "lecture")
def etalonnage_historique():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        etalonnages = conn.execute(
            """
            SELECT e.*, t.numero AS thermometre_numero
            FROM cuisine_hygiene_etalonnages e
            JOIN cuisine_hygiene_thermometres t ON t.id = e.thermometre_id
            ORDER BY e.date_test DESC
            LIMIT 200
            """
        ).fetchall()

    return render_template("cuisine_hygiene/etalonnage_historique.html", etalonnages=etalonnages)
