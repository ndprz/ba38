# ============================================================
# 📋 Référentiels hygiène (CRUD minimal : liste + activer/désactiver)
#     Accès "ecriture" uniquement — pas de suppression physique, pour ne
#     jamais casser l'historique des relevés déjà saisis.
# ============================================================

import re
import sqlite3
import unicodedata

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_production_cuisine import cuisine_hygiene_bp
from ba38_production_cuisine.utils import _connect


def _slugify(libelle: str) -> str:
    txt = unicodedata.normalize("NFKD", libelle).encode("ascii", "ignore").decode("ascii")
    txt = re.sub(r"[^a-zA-Z0-9]+", "_", txt).strip("_").lower()
    return txt


@cuisine_hygiene_bp.route("/referentiels")
@login_required
@require_access("cuisine_hygiene", "ecriture")
def referentiels():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        zones_temperature = conn.execute(
            "SELECT * FROM cuisine_hygiene_zones_temperature ORDER BY ordre"
        ).fetchall()
        zones_nettoyage = conn.execute(
            "SELECT * FROM cuisine_hygiene_zones_nettoyage ORDER BY ordre"
        ).fetchall()
        thermometres = conn.execute(
            "SELECT * FROM cuisine_hygiene_thermometres ORDER BY numero"
        ).fetchall()

    return render_template(
        "cuisine_hygiene/referentiels.html",
        zones_temperature=zones_temperature,
        zones_nettoyage=zones_nettoyage,
        thermometres=thermometres,
    )


# ------------------------------------------------------------
# Zones de température
# ------------------------------------------------------------
@cuisine_hygiene_bp.route("/referentiels/zones-temperature/ajouter", methods=["POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def ajouter_zone_temperature():
    libelle = (request.form.get("libelle") or "").strip()
    if libelle:
        with _connect() as conn:
            ordre = conn.execute(
                "SELECT COALESCE(MAX(ordre), 0) + 1 AS n FROM cuisine_hygiene_zones_temperature"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO cuisine_hygiene_zones_temperature (code, libelle, ordre) VALUES (?, ?, ?)",
                (_slugify(libelle), libelle, ordre),
            )
            conn.commit()
        upload_database()
        flash("✅ Zone ajoutée.", "success")
    return redirect(url_for("cuisine_hygiene.referentiels"))


@cuisine_hygiene_bp.route("/referentiels/zones-temperature/<int:zone_id>/toggle", methods=["POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def toggle_zone_temperature(zone_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE cuisine_hygiene_zones_temperature SET actif = 1 - actif WHERE id = ?",
            (zone_id,),
        )
        conn.commit()
    upload_database()
    flash("🔁 Statut de la zone mis à jour.", "info")
    return redirect(url_for("cuisine_hygiene.referentiels"))


# ------------------------------------------------------------
# Zones de nettoyage
# ------------------------------------------------------------
@cuisine_hygiene_bp.route("/referentiels/zones-nettoyage/ajouter", methods=["POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def ajouter_zone_nettoyage():
    libelle = (request.form.get("libelle") or "").strip()
    if libelle:
        with _connect() as conn:
            ordre = conn.execute(
                "SELECT COALESCE(MAX(ordre), 0) + 1 AS n FROM cuisine_hygiene_zones_nettoyage"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO cuisine_hygiene_zones_nettoyage (code, libelle, ordre) VALUES (?, ?, ?)",
                (_slugify(libelle), libelle, ordre),
            )
            conn.commit()
        upload_database()
        flash("✅ Zone ajoutée.", "success")
    return redirect(url_for("cuisine_hygiene.referentiels"))


@cuisine_hygiene_bp.route("/referentiels/zones-nettoyage/<int:zone_id>/toggle", methods=["POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def toggle_zone_nettoyage(zone_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE cuisine_hygiene_zones_nettoyage SET actif = 1 - actif WHERE id = ?",
            (zone_id,),
        )
        conn.commit()
    upload_database()
    flash("🔁 Statut de la zone mis à jour.", "info")
    return redirect(url_for("cuisine_hygiene.referentiels"))


# ------------------------------------------------------------
# Thermomètres
# ------------------------------------------------------------
@cuisine_hygiene_bp.route("/referentiels/thermometres/ajouter", methods=["POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def ajouter_thermometre():
    numero = request.form.get("numero")
    if numero:
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO cuisine_hygiene_thermometres (numero, libelle) VALUES (?, ?)",
                    (int(numero), f"Thermomètre N°{numero}"),
                )
                conn.commit()
            upload_database()
            flash("✅ Thermomètre ajouté.", "success")
        except Exception as e:
            write_log(f"❌ Erreur ajout thermomètre : {e}")
            flash("❌ Ce numéro de thermomètre existe déjà.", "danger")
    return redirect(url_for("cuisine_hygiene.referentiels"))


@cuisine_hygiene_bp.route("/referentiels/thermometres/<int:thermometre_id>/toggle", methods=["POST"])
@login_required
@require_access("cuisine_hygiene", "ecriture")
def toggle_thermometre(thermometre_id):
    with _connect() as conn:
        conn.execute(
            "UPDATE cuisine_hygiene_thermometres SET actif = 1 - actif WHERE id = ?",
            (thermometre_id,),
        )
        conn.commit()
    upload_database()
    flash("🔁 Statut du thermomètre mis à jour.", "info")
    return redirect(url_for("cuisine_hygiene.referentiels"))
