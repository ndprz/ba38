# ============================================================
# 📦 Stock & Ventes barquettes
#     - cuisine_articles_barquettes : référentiel taille -> portions
#     - cuisine_stock_barquettes : stock entrant, alimenté depuis les
#       "Quantités conditionnées" d'une production (bouton dédié sur la
#       fiche recette), jamais saisi à la main.
#     Sorties = bons de livraison (routes_bons_livraison.py) : le stock
#     disponible est calculé (entrée − BL non annulés), les lignes d'entrée
#     ne sont jamais décrémentées.
# ============================================================

import sqlite3

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import _connect, stock_lignes_disponibles, quantites_livrees_production

TAILLES = ("1/2", "1/4", "1/8")


# ------------------------------------------------------------
# 📖 Référentiel articles barquettes (maintenance)
# ------------------------------------------------------------
@production_cuisine_bp.route("/articles-barquettes")
@login_required
@require_access("production_cuisine", "lecture")
def liste_articles_barquettes():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        articles = conn.execute(
            """SELECT id, taille, code_article, libelle, nb_portions FROM cuisine_articles_barquettes
               WHERE actif = 1 ORDER BY taille"""
        ).fetchall()
        articles_json = [dict(a) for a in articles]

    return render_template(
        "production_cuisine/articles_barquettes_liste.html",
        nb_articles=len(articles_json),
        articles_json=articles_json,
    )


@production_cuisine_bp.route("/articles-barquettes/creer", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def creer_article_barquette():
    if request.method == "POST":
        taille = (request.form.get("taille") or "").strip()
        code_article = (request.form.get("code_article") or "").strip() or None
        libelle = (request.form.get("libelle") or "").strip()
        nb_portions = request.form.get("nb_portions") or None
        benevole = (request.form.get("benevole") or "").strip()

        if taille not in TAILLES or not libelle or not nb_portions:
            flash("⚠️ Taille, libellé et nombre de portions sont obligatoires.", "warning")
            return render_template(
                "production_cuisine/articles_barquettes_form.html",
                article=None, form=request.form, tailles=TAILLES,
            )

        try:
            with _connect() as conn:
                existe = conn.execute(
                    "SELECT id FROM cuisine_articles_barquettes WHERE taille = ?", (taille,)
                ).fetchone()
                if existe:
                    flash(f"⚠️ La taille « {taille} » a déjà un article dans le référentiel.", "warning")
                    return render_template(
                        "production_cuisine/articles_barquettes_form.html",
                        article=None, form=request.form, tailles=TAILLES,
                    )
                conn.execute(
                    """INSERT INTO cuisine_articles_barquettes
                       (taille, code_article, libelle, nb_portions, user_creation)
                       VALUES (?, ?, ?, ?, ?)""",
                    (taille, code_article, libelle, nb_portions, benevole or None),
                )
                conn.commit()
            upload_database()
            flash("✅ Article créé.", "success")
            return redirect(url_for("production_cuisine.liste_articles_barquettes"))
        except Exception as e:
            write_log(f"❌ Erreur création article barquette : {e}")
            flash("❌ Erreur lors de la création.", "danger")

    return render_template(
        "production_cuisine/articles_barquettes_form.html",
        article=None, form={}, tailles=TAILLES,
    )


@production_cuisine_bp.route("/articles-barquettes/<int:article_id>/modifier", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def modifier_article_barquette(article_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        article = conn.execute(
            "SELECT * FROM cuisine_articles_barquettes WHERE id = ?", (article_id,)
        ).fetchone()

    if not article:
        flash("⛔ Article introuvable.", "danger")
        return redirect(url_for("production_cuisine.liste_articles_barquettes"))

    if request.method == "POST":
        taille = (request.form.get("taille") or "").strip()
        code_article = (request.form.get("code_article") or "").strip() or None
        libelle = (request.form.get("libelle") or "").strip()
        nb_portions = request.form.get("nb_portions") or None
        benevole = (request.form.get("benevole") or "").strip()

        if taille not in TAILLES or not libelle or not nb_portions:
            flash("⚠️ Taille, libellé et nombre de portions sont obligatoires.", "warning")
            return render_template(
                "production_cuisine/articles_barquettes_form.html",
                article=article, form=request.form, tailles=TAILLES,
            )

        try:
            with _connect() as conn:
                existe = conn.execute(
                    "SELECT id FROM cuisine_articles_barquettes WHERE taille = ? AND id != ?",
                    (taille, article_id),
                ).fetchone()
                if existe:
                    flash(f"⚠️ La taille « {taille} » a déjà un article dans le référentiel.", "warning")
                    return render_template(
                        "production_cuisine/articles_barquettes_form.html",
                        article=article, form=request.form, tailles=TAILLES,
                    )
                conn.execute(
                    """UPDATE cuisine_articles_barquettes
                       SET taille = ?, code_article = ?, libelle = ?, nb_portions = ?, user_modif = ?
                       WHERE id = ?""",
                    (taille, code_article, libelle, nb_portions, benevole or None, article_id),
                )
                conn.commit()
            upload_database()
            flash("✅ Article modifié.", "success")
            return redirect(url_for("production_cuisine.liste_articles_barquettes"))
        except Exception as e:
            write_log(f"❌ Erreur modification article barquette {article_id} : {e}")
            flash("❌ Erreur lors de la modification.", "danger")

    return render_template(
        "production_cuisine/articles_barquettes_form.html",
        article=article, form=dict(article), tailles=TAILLES,
    )


@production_cuisine_bp.route("/articles-barquettes/<int:article_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_article_barquette(article_id):
    with _connect() as conn:
        existe = conn.execute(
            "SELECT id FROM cuisine_articles_barquettes WHERE id = ?", (article_id,)
        ).fetchone()
        if not existe:
            flash("⛔ Article introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_articles_barquettes"))
        conn.execute(
            "UPDATE cuisine_articles_barquettes SET actif = 0 WHERE id = ?", (article_id,)
        )
        conn.commit()

    upload_database()
    flash("🗑️ Article supprimé.", "success")
    return redirect(url_for("production_cuisine.liste_articles_barquettes"))


# ------------------------------------------------------------
# 📦 Stock barquettes : alimentation depuis une production
# ------------------------------------------------------------
@production_cuisine_bp.route("/<int:production_id>/stock-barquettes/ajouter", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def ajouter_stock_barquettes(production_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row

        production = conn.execute(
            "SELECT * FROM cuisine_productions WHERE id = ? AND actif = 1", (production_id,)
        ).fetchone()
        if not production:
            flash("⛔ Production introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_productions"))

        mise_en_cellule = conn.execute(
            """SELECT heure_fin, cellule_numero FROM cuisine_production_etapes
               WHERE production_id = ? AND etape_code = 'refroidissement_cellule'
               ORDER BY id DESC LIMIT 1""",
            (production_id,),
        ).fetchone()
        if not mise_en_cellule or mise_en_cellule["heure_fin"] is None:
            flash(
                "⚠️ La mise en cellule doit être terminée avant d'ajouter cette production au stock.",
                "warning",
            )
            return redirect(url_for("production_cuisine.detail_production", production_id=production_id))

        quantites = conn.execute(
            "SELECT taille, quantite FROM cuisine_production_quantites WHERE production_id = ?",
            (production_id,),
        ).fetchall()
        quantites_par_taille = {q["taille"]: q["quantite"] for q in quantites}

        articles = conn.execute(
            "SELECT id, taille FROM cuisine_articles_barquettes WHERE actif = 1"
        ).fetchall()
        article_id_par_taille = {a["taille"]: a["id"] for a in articles}

        benevole = (request.form.get("benevole") or "").strip()

        # Des barquettes de cette production déjà parties sur un bon de
        # livraison ne peuvent pas "disparaître" du stock par resynchro.
        livrees = quantites_livrees_production(conn, production_id)
        taille_par_article_id = {v: k for k, v in article_id_par_taille.items()}
        conflits = [
            f"{taille_par_article_id.get(aid, '?')} : {n} livrée(s), {quantites_par_taille.get(taille_par_article_id.get(aid), 0) or 0} conditionnée(s)"
            for aid, n in livrees.items()
            if n > (quantites_par_taille.get(taille_par_article_id.get(aid)) or 0)
        ]
        if conflits:
            flash(
                "⛔ Impossible : des barquettes de cette production sont déjà sur des bons de livraison ("
                + " ; ".join(conflits) + "). Annulez d'abord les bons concernés.",
                "danger",
            )
            return redirect(url_for("production_cuisine.detail_production", production_id=production_id))

        try:
            cur = conn.cursor()
            # Resynchronisation idempotente : on repart des quantités
            # actuelles à chaque clic (permet de corriger une quantité
            # conditionnée après un premier ajout au stock).
            cur.execute("DELETE FROM cuisine_stock_barquettes WHERE production_id = ?", (production_id,))

            nb_lignes = 0
            for taille, quantite in quantites_par_taille.items():
                if not quantite:
                    continue
                article_id = article_id_par_taille.get(taille)
                if not article_id:
                    continue
                cur.execute(
                    """INSERT INTO cuisine_stock_barquettes
                       (article_id, production_id, libelle_recette, date_fin_recette,
                        cellule_numero, quantite, categorie_produit, user_creation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (article_id, production_id, production["nom_recette"],
                     mise_en_cellule["heure_fin"], mise_en_cellule["cellule_numero"],
                     quantite, production["categorie_produit"], benevole or None),
                )
                nb_lignes += 1

            conn.commit()
            upload_database()
            if nb_lignes:
                flash(f"✅ {nb_lignes} ligne(s) ajoutée(s) au stock barquettes.", "success")
            else:
                flash("⚠️ Aucune quantité conditionnée renseignée — rien à ajouter au stock.", "warning")
        except Exception as e:
            write_log(f"❌ Erreur ajout stock barquettes (production {production_id}) : {e}")
            flash("❌ Erreur lors de l'ajout au stock.", "danger")

    return redirect(url_for("production_cuisine.detail_production", production_id=production_id))


@production_cuisine_bp.route("/stock-barquettes")
@login_required
@require_access("production_cuisine", "lecture")
def liste_stock_barquettes():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row

        articles = conn.execute(
            "SELECT id, taille, libelle, nb_portions FROM cuisine_articles_barquettes WHERE actif = 1 ORDER BY taille"
        ).fetchall()
        stock_total = {
            a["id"]: {
                "taille": a["taille"], "libelle": a["libelle"], "nb_portions": a["nb_portions"],
                "quantite_stock": 0, "quantite_livree": 0,
                "quantite_carne": 0, "quantite_legumes": 0, "quantite_non_classe": 0,
            }
            for a in articles
        }
        lignes = stock_lignes_disponibles(conn)
        for l in lignes:
            t = stock_total.get(l["article_id"])
            if not t:
                continue
            t["quantite_stock"] += l["disponible"]
            t["quantite_livree"] += l["quantite_livree"]
            cle = {"carne": "quantite_carne", "legumes": "quantite_legumes"}.get(l["categorie_produit"], "quantite_non_classe")
            t[cle] += l["disponible"]
        stock_total = list(stock_total.values())

        mouvements_json = sorted((dict(m) for m in lignes), key=lambda m: m["date_creation"] or "", reverse=True)[:300]
        libelles_categorie = {"carne": "🥩 Carné", "legumes": "🥬 Légumes"}
        for m in mouvements_json:
            m["categorie_label"] = libelles_categorie.get(m["categorie_produit"], "❓ Non classé")

    return render_template(
        "production_cuisine/stock_barquettes_liste.html",
        stock_total=stock_total,
        mouvements=mouvements_json,
    )


@production_cuisine_bp.route("/stock-barquettes/<int:stock_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_stock_barquette(stock_id):
    with _connect() as conn:
        existe = conn.execute(
            "SELECT id, production_id, article_id FROM cuisine_stock_barquettes WHERE id = ?", (stock_id,)
        ).fetchone()
        if not existe:
            flash("⛔ Ligne de stock introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_stock_barquettes"))
        if quantites_livrees_production(conn, existe["production_id"]).get(existe["article_id"]):
            flash("⛔ Des barquettes de cette ligne sont sur un bon de livraison : annulez d'abord le bon.", "danger")
            return redirect(url_for("production_cuisine.liste_stock_barquettes"))
        conn.execute("UPDATE cuisine_stock_barquettes SET actif = 0 WHERE id = ?", (stock_id,))
        conn.commit()

    upload_database()
    flash("🗑️ Ligne de stock supprimée.", "success")
    return redirect(url_for("production_cuisine.liste_stock_barquettes"))
