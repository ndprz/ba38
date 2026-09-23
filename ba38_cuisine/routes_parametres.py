# ============================================================
# ⚙️ Paramètres cuisine (famille / sous-familles / type-mode de cuisson)
#     Réutilise la table générique `parametres` (param_name/param_value/
#     categorie) déjà utilisée ailleurs dans l'appli (ex.
#     ba38_fournisseurs/routes.py::create_fournisseur) — jamais de table
#     dédiée. Toutes les routes filtrent explicitement sur les 4
#     `param_name` cuisine ci-dessous : ne touchent jamais aux autres
#     paramètres existants (type_frs, enseigne, etc.).
# ============================================================

import sqlite3

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required

from ba38_utilitaires.core import require_access, upload_database
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import _connect

PARAM_NAMES = {
    "cuisine_famille": "Famille",
    "cuisine_sous_famille_1": "Sous-famille 1",
    "cuisine_sous_famille_2": "Sous-famille 2",
    "cuisine_type_cuisson": "Type / mode de cuisson",
}


@production_cuisine_bp.route("/parametres")
@login_required
@require_access("production_cuisine", "ecriture")
def parametres():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        categories = {}
        for param_name, label in PARAM_NAMES.items():
            valeurs = conn.execute(
                "SELECT id, param_value FROM parametres WHERE param_name = ? ORDER BY param_value COLLATE NOCASE",
                (param_name,),
            ).fetchall()
            categories[param_name] = {"label": label, "valeurs": valeurs}

    return render_template("production_cuisine/parametres.html", categories=categories)


@production_cuisine_bp.route("/parametres/<param_name>/ajouter", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def ajouter_parametre(param_name):
    if param_name not in PARAM_NAMES:
        flash("⛔ Paramètre inconnu.", "danger")
        return redirect(url_for("production_cuisine.parametres"))

    valeur = (request.form.get("valeur") or "").strip()
    if not valeur:
        flash("⚠️ Merci de saisir une valeur.", "warning")
        return redirect(url_for("production_cuisine.parametres"))

    with _connect() as conn:
        existe = conn.execute(
            "SELECT 1 FROM parametres WHERE param_name = ? AND param_value = ?",
            (param_name, valeur),
        ).fetchone()
        if existe:
            flash("⚠️ Cette valeur existe déjà.", "warning")
            return redirect(url_for("production_cuisine.parametres"))
        conn.execute(
            "INSERT INTO parametres (param_name, param_value, categorie) VALUES (?, ?, 'liste')",
            (param_name, valeur),
        )
        conn.commit()

    upload_database()
    flash("✅ Valeur ajoutée.", "success")
    return redirect(url_for("production_cuisine.parametres"))


@production_cuisine_bp.route("/parametres/<param_name>/<int:parametre_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_parametre(param_name, parametre_id):
    if param_name not in PARAM_NAMES:
        flash("⛔ Paramètre inconnu.", "danger")
        return redirect(url_for("production_cuisine.parametres"))

    with _connect() as conn:
        conn.execute(
            "DELETE FROM parametres WHERE id = ? AND param_name = ?",
            (parametre_id, param_name),
        )
        conn.commit()

    upload_database()
    flash("🗑️ Valeur supprimée.", "success")
    return redirect(url_for("production_cuisine.parametres"))
