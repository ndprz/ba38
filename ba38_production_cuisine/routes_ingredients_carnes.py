# ============================================================
# 🥩 Référentiel des ingrédients carnés (groupe / produit)
#     Import initial : scripts/import_ingredients_carnes.py
#     CRUD complet — sert au sélecteur en 2 temps de l'écran de
#     réception (champ "Produit réceptionné").
# ============================================================

import sqlite3

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_production_cuisine import production_cuisine_bp
from ba38_production_cuisine.utils import _connect


@production_cuisine_bp.route("/ingredients-carnes")
@login_required
@require_access("production_cuisine", "lecture")
def liste_ingredients_carnes():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        ingredients = conn.execute(
            """SELECT id, groupe, produit FROM cuisine_ingredients_carnes
               WHERE actif = 1 ORDER BY groupe COLLATE NOCASE, produit COLLATE NOCASE"""
        ).fetchall()
        groupes_existants = sorted({r["groupe"] for r in ingredients}, key=str.lower)
        ingredients_json = [dict(r) for r in ingredients]

    return render_template(
        "production_cuisine/ingredients_carnes_liste.html",
        nb_ingredients=len(ingredients_json),
        ingredients_json=ingredients_json,
        groupes_existants=groupes_existants,
    )


@production_cuisine_bp.route("/ingredients-carnes/creer", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def creer_ingredient_carne():
    with _connect() as conn:
        groupes_existants = sorted(
            {r[0] for r in conn.execute(
                "SELECT DISTINCT groupe FROM cuisine_ingredients_carnes WHERE actif = 1"
            ).fetchall()},
            key=str.lower,
        )

    if request.method == "POST":
        groupe = (request.form.get("groupe") or "").strip()
        produit = (request.form.get("produit") or "").strip()

        if not groupe or not produit:
            flash("⚠️ Le groupe et le produit sont obligatoires.", "warning")
            return render_template(
                "production_cuisine/ingredients_carnes_form.html",
                ingredient=None, form=request.form, groupes_existants=groupes_existants,
            )

        try:
            with _connect() as conn:
                # Pas de filtre actif : la contrainte UNIQUE(groupe, produit)
                # porte sur toute la table, y compris les lignes supprimées
                # (actif=0) — un doublon avec l'une d'elles ferait échouer
                # l'INSERT plus bas si on ne le détectait pas ici.
                existe = conn.execute(
                    """SELECT id, actif FROM cuisine_ingredients_carnes
                       WHERE groupe = ? AND produit = ?""",
                    (groupe, produit),
                ).fetchone()
                if existe:
                    if not existe[1]:
                        flash(
                            f"⚠️ « {groupe} / {produit} » a déjà existé et a été supprimé — "
                            "contactez un administrateur pour le réactiver.",
                            "warning",
                        )
                    else:
                        flash(f"⚠️ « {groupe} / {produit} » existe déjà dans le référentiel.", "warning")
                    return render_template(
                        "production_cuisine/ingredients_carnes_form.html",
                        ingredient=None, form=request.form, groupes_existants=groupes_existants,
                    )
                conn.execute(
                    "INSERT INTO cuisine_ingredients_carnes (groupe, produit) VALUES (?, ?)",
                    (groupe, produit),
                )
                conn.commit()
            upload_database()
            flash("✅ Ingrédient ajouté.", "success")
            return redirect(url_for("production_cuisine.liste_ingredients_carnes"))
        except Exception as e:
            write_log(f"❌ Erreur création ingrédient carné : {e}")
            flash("❌ Erreur lors de la création.", "danger")

    return render_template(
        "production_cuisine/ingredients_carnes_form.html",
        ingredient=None, form={}, groupes_existants=groupes_existants,
    )


@production_cuisine_bp.route("/ingredients-carnes/<int:ingredient_id>/modifier", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def modifier_ingredient_carne(ingredient_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        ingredient = conn.execute(
            "SELECT * FROM cuisine_ingredients_carnes WHERE id = ?", (ingredient_id,)
        ).fetchone()
        groupes_existants = sorted(
            {r[0] for r in conn.execute(
                "SELECT DISTINCT groupe FROM cuisine_ingredients_carnes WHERE actif = 1"
            ).fetchall()},
            key=str.lower,
        )

    if not ingredient:
        flash("⛔ Ingrédient introuvable.", "danger")
        return redirect(url_for("production_cuisine.liste_ingredients_carnes"))

    if request.method == "POST":
        groupe = (request.form.get("groupe") or "").strip()
        produit = (request.form.get("produit") or "").strip()

        if not groupe or not produit:
            flash("⚠️ Le groupe et le produit sont obligatoires.", "warning")
            return render_template(
                "production_cuisine/ingredients_carnes_form.html",
                ingredient=ingredient, form=request.form, groupes_existants=groupes_existants,
            )

        try:
            with _connect() as conn:
                existe = conn.execute(
                    """SELECT id, actif FROM cuisine_ingredients_carnes
                       WHERE groupe = ? AND produit = ? AND id != ?""",
                    (groupe, produit, ingredient_id),
                ).fetchone()
                if existe:
                    if not existe[1]:
                        flash(
                            f"⚠️ « {groupe} / {produit} » a déjà existé et a été supprimé — "
                            "contactez un administrateur pour le réactiver.",
                            "warning",
                        )
                    else:
                        flash(f"⚠️ « {groupe} / {produit} » existe déjà dans le référentiel.", "warning")
                    return render_template(
                        "production_cuisine/ingredients_carnes_form.html",
                        ingredient=ingredient, form=request.form, groupes_existants=groupes_existants,
                    )
                conn.execute(
                    "UPDATE cuisine_ingredients_carnes SET groupe = ?, produit = ? WHERE id = ?",
                    (groupe, produit, ingredient_id),
                )
                conn.commit()
            upload_database()
            flash("✅ Ingrédient modifié.", "success")
            return redirect(url_for("production_cuisine.liste_ingredients_carnes"))
        except Exception as e:
            write_log(f"❌ Erreur modification ingrédient carné {ingredient_id} : {e}")
            flash("❌ Erreur lors de la modification.", "danger")

    return render_template(
        "production_cuisine/ingredients_carnes_form.html",
        ingredient=ingredient, form=dict(ingredient), groupes_existants=groupes_existants,
    )


@production_cuisine_bp.route("/ingredients-carnes/<int:ingredient_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_ingredient_carne(ingredient_id):
    with _connect() as conn:
        existe = conn.execute(
            "SELECT id FROM cuisine_ingredients_carnes WHERE id = ?", (ingredient_id,)
        ).fetchone()
        if not existe:
            flash("⛔ Ingrédient introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_ingredients_carnes"))
        conn.execute(
            "UPDATE cuisine_ingredients_carnes SET actif = 0 WHERE id = ?", (ingredient_id,)
        )
        conn.commit()

    upload_database()
    flash("🗑️ Ingrédient supprimé.", "success")
    return redirect(url_for("production_cuisine.liste_ingredients_carnes"))
