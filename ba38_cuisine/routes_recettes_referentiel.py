# ============================================================
# 📖 Référentiel des recettes C3ES (code, famille, mode de cuisson)
#     Import initial : scripts/import_recettes_referentiel.py
#     CRUD complet (création/mise à jour/suppression) + export Excel pour
#     l'étiqueteuse automatique (2 fichiers : code suffixé -05 ou -03
#     selon la DLC choisie, même table pour les deux).
# ============================================================

import sqlite3
from io import BytesIO

from flask import render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import _connect


def _get_int(nom_champ):
    valeur = (request.form.get(nom_champ) or "").strip()
    return int(valeur) if valeur.isdigit() else None


def _parametres_cuisine(conn):
    """Valeurs des 4 listes de paramètres cuisine (famille, sous-familles,
    type de cuisson), pour alimenter les <select> du formulaire recette."""
    resultat = {}
    for param_name in ("cuisine_famille", "cuisine_sous_famille_1", "cuisine_sous_famille_2", "cuisine_type_cuisson"):
        resultat[param_name] = [
            r[0] for r in conn.execute(
                "SELECT param_value FROM parametres WHERE param_name = ? ORDER BY param_value COLLATE NOCASE",
                (param_name,),
            ).fetchall()
        ]
    return resultat


@production_cuisine_bp.route("/recettes-referentiel")
@login_required
@require_access("production_cuisine", "lecture")
def liste_recettes_referentiel():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        recettes = conn.execute(
            """SELECT id, code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson
               FROM cuisine_recettes_referentiel WHERE actif = 1 ORDER BY code"""
        ).fetchall()
        recettes_json = [dict(r) for r in recettes]

    return render_template(
        "production_cuisine/recettes_referentiel_liste.html",
        nb_recettes=len(recettes_json),
        recettes_json=recettes_json,
    )


@production_cuisine_bp.route("/recettes-referentiel/creer", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def creer_recette_referentiel():
    with _connect() as conn:
        parametres = _parametres_cuisine(conn)

    if request.method == "POST":
        code = _get_int("code")
        nom = (request.form.get("nom") or "").strip()
        famille = (request.form.get("famille") or "").strip() or None
        sous_famille_1 = (request.form.get("sous_famille_1") or "").strip() or None
        sous_famille_2 = (request.form.get("sous_famille_2") or "").strip() or None
        type_cuisson = (request.form.get("type_cuisson") or "").strip() or None
        benevole = (request.form.get("benevole") or "").strip()

        if not nom:
            flash("⚠️ Le nom de la recette est obligatoire.", "warning")
            return render_template(
                "production_cuisine/recettes_referentiel_form.html",
                recette=None, form=request.form, parametres=parametres,
            )

        try:
            with _connect() as conn:
                if code is not None:
                    existe = conn.execute(
                        "SELECT id FROM cuisine_recettes_referentiel WHERE code = ?", (code,)
                    ).fetchone()
                    if existe:
                        flash(f"⚠️ Le code {code} est déjà utilisé par une autre recette.", "warning")
                        return render_template(
                            "production_cuisine/recettes_referentiel_form.html",
                            recette=None, form=request.form, parametres=parametres,
                        )
                conn.execute(
                    """INSERT INTO cuisine_recettes_referentiel
                       (code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson, user_creation)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson, benevole or None),
                )
                conn.commit()
            upload_database()
            flash("✅ Recette créée.", "success")
            return redirect(url_for("production_cuisine.liste_recettes_referentiel"))
        except Exception as e:
            write_log(f"❌ Erreur création recette référentiel : {e}")
            flash("❌ Erreur lors de la création.", "danger")

    return render_template(
        "production_cuisine/recettes_referentiel_form.html",
        recette=None, form={}, parametres=parametres,
    )


@production_cuisine_bp.route("/recettes-referentiel/<int:recette_id>/modifier", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def modifier_recette_referentiel(recette_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        recette = conn.execute(
            "SELECT * FROM cuisine_recettes_referentiel WHERE id = ?", (recette_id,)
        ).fetchone()
        parametres = _parametres_cuisine(conn)

    if not recette:
        flash("⛔ Recette introuvable.", "danger")
        return redirect(url_for("production_cuisine.liste_recettes_referentiel"))

    if request.method == "POST":
        code = _get_int("code")
        nom = (request.form.get("nom") or "").strip()
        famille = (request.form.get("famille") or "").strip() or None
        sous_famille_1 = (request.form.get("sous_famille_1") or "").strip() or None
        sous_famille_2 = (request.form.get("sous_famille_2") or "").strip() or None
        type_cuisson = (request.form.get("type_cuisson") or "").strip() or None
        benevole = (request.form.get("benevole") or "").strip()

        if not nom:
            flash("⚠️ Le nom de la recette est obligatoire.", "warning")
            return render_template(
                "production_cuisine/recettes_referentiel_form.html",
                recette=recette, form=request.form, parametres=parametres,
            )

        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                if code is not None:
                    existe = conn.execute(
                        "SELECT id FROM cuisine_recettes_referentiel WHERE code = ? AND id != ?",
                        (code, recette_id),
                    ).fetchone()
                    if existe:
                        flash(f"⚠️ Le code {code} est déjà utilisé par une autre recette.", "warning")
                        return render_template(
                            "production_cuisine/recettes_referentiel_form.html",
                            recette=recette, form=request.form, parametres=parametres,
                        )
                conn.execute(
                    """UPDATE cuisine_recettes_referentiel
                       SET code = ?, nom = ?, famille = ?, sous_famille_1 = ?,
                           sous_famille_2 = ?, type_cuisson = ?, user_modif = ?
                       WHERE id = ?""",
                    (code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson,
                     benevole or None, recette_id),
                )
                conn.commit()
            upload_database()
            flash("✅ Recette modifiée.", "success")
            return redirect(url_for("production_cuisine.liste_recettes_referentiel"))
        except Exception as e:
            write_log(f"❌ Erreur modification recette référentiel {recette_id} : {e}")
            flash("❌ Erreur lors de la modification.", "danger")

    return render_template(
        "production_cuisine/recettes_referentiel_form.html",
        recette=recette, form=dict(recette), parametres=parametres,
    )


@production_cuisine_bp.route("/recettes-referentiel/<int:recette_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_recette_referentiel(recette_id):
    with _connect() as conn:
        existe = conn.execute(
            "SELECT id FROM cuisine_recettes_referentiel WHERE id = ?", (recette_id,)
        ).fetchone()
        if not existe:
            flash("⛔ Recette introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_recettes_referentiel"))
        conn.execute(
            "UPDATE cuisine_recettes_referentiel SET actif = 0 WHERE id = ?", (recette_id,)
        )
        conn.commit()

    upload_database()
    flash("🗑️ Recette supprimée.", "success")
    return redirect(url_for("production_cuisine.liste_recettes_referentiel"))


@production_cuisine_bp.route("/recettes-referentiel/export/<suffixe>")
@login_required
@require_access("production_cuisine", "lecture")
def export_recettes_referentiel(suffixe):
    if suffixe not in ("05", "03"):
        flash("⛔ Export invalide.", "danger")
        return redirect(url_for("production_cuisine.liste_recettes_referentiel"))

    import pandas as pd

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        recettes = conn.execute(
            """SELECT * FROM cuisine_recettes_referentiel
               WHERE actif = 1 AND code IS NOT NULL
               ORDER BY code"""
        ).fetchall()

    lignes = [
        {
            "Code": f"{r['code']}-{suffixe}",
            "Nom de la recette": r["nom"],
            "Famille": r["famille"],
            "Sous-famille 1": r["sous_famille_1"],
            "Sous-famille-2": r["sous_famille_2"],
            "Type de cuisson": r["type_cuisson"],
        }
        for r in recettes
    ]
    df = pd.DataFrame(lignes)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=suffixe)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=f"recettes_C3ES_dlc_{suffixe}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
