# ============================================================
# 🍲 Productions du jour (écran principal "run" tablette)
# ============================================================

import sqlite3

from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_production_cuisine import production_cuisine_bp
from ba38_production_cuisine.utils import (
    _connect, today_paris, now_paris_str, decongelation_en_cours, etape_actuelle_libelle,
)

STATUTS = ("en_cours", "terminee", "annulee")


def _recettes_referentiel_json(conn):
    """Liste des recettes actives du référentiel, sérialisable pour la
    grille Tabulator de sélection (création de production, changement de
    recette)."""
    rows = conn.execute(
        """SELECT id, code, nom, famille, sous_famille_1, sous_famille_2, type_cuisson
           FROM cuisine_recettes_referentiel WHERE actif = 1 ORDER BY code"""
    ).fetchall()
    return [dict(r) for r in rows]


def _get_or_create_recette(conn, nom):
    """Retrouve une recette existante par nom (insensible à la casse) ou la
    crée à la volée si elle n'existe pas encore."""
    nom = (nom or "").strip()
    if not nom:
        return None, nom
    row = conn.execute(
        "SELECT id, nom FROM cuisine_recettes WHERE LOWER(nom) = LOWER(?) AND actif = 1",
        (nom,),
    ).fetchone()
    if row:
        return row["id"], row["nom"]
    cur = conn.cursor()
    cur.execute("INSERT INTO cuisine_recettes (nom) VALUES (?)", (nom,))
    return cur.lastrowid, nom


@production_cuisine_bp.route("/")
@login_required
@require_access("production_cuisine", "lecture")
def liste_productions():
    date_filtre = request.args.get("date") or today_paris()
    statut_filtre = request.args.get("statut") or ""

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM cuisine_productions WHERE date_production = ? AND actif = 1"
        params = [date_filtre]
        if statut_filtre in STATUTS:
            sql += " AND statut = ?"
            params.append(statut_filtre)
        sql += " ORDER BY id DESC"
        productions = conn.execute(sql, params).fetchall()

        # Étape en cours (ou prochaine) affichée à côté du statut — inutile
        # de calculer pour une production terminée/annulée.
        etapes_actuelles = {
            p["id"]: etape_actuelle_libelle(conn, p["id"])
            for p in productions if p["statut"] == "en_cours"
        }

        nb_receptions_attente = conn.execute(
            """SELECT COUNT(*) FROM cuisine_receptions
               WHERE date_reception = ? AND actif = 1 AND production_id IS NULL""",
            (date_filtre,),
        ).fetchone()[0]

    return render_template(
        "production_cuisine/productions_liste.html",
        productions=productions,
        date_filtre=date_filtre,
        statut_filtre=statut_filtre,
        statuts=STATUTS,
        nb_receptions_attente=nb_receptions_attente,
        etapes_actuelles=etapes_actuelles,
    )


@production_cuisine_bp.route("/api/heure_actuelle")
@login_required
def api_heure_actuelle():
    """Heure serveur (Europe/Paris) pour horodater côté client des actions
    ponctuelles (ex. départ décongélation avant même la création de la
    production) sans dépendre de l'horloge de la tablette."""
    return jsonify({"heure": now_paris_str()})


def _receptions_en_attente(conn, date_filtre):
    return conn.execute(
        """
        SELECT r.*, f.nom AS fournisseur_nom
        FROM cuisine_receptions r
        LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
        WHERE r.date_reception = ? AND r.actif = 1 AND r.production_id IS NULL
        ORDER BY r.heure_arrivee, r.id
        """,
        (date_filtre,),
    ).fetchall()


@production_cuisine_bp.route("/creer", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def creer_production():
    date_defaut = request.values.get("date_production") or today_paris()
    preselection = {int(v) for v in request.args.getlist("reception_id") if v.isdigit()}

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        recettes_referentiel = _recettes_referentiel_json(conn)
        types_cuisson = [
            r["param_value"] for r in conn.execute(
                "SELECT param_value FROM parametres WHERE param_name = 'cuisine_type_cuisson' ORDER BY param_value"
            ).fetchall()
        ]
        receptions_disponibles = _receptions_en_attente(conn, date_defaut)

    if request.method == "POST":
        benevole = (request.form.get("benevole") or "").strip()
        date_production = request.form.get("date_production") or today_paris()
        nom_recette = (request.form.get("nom_recette") or "").strip()
        espece = (request.form.get("espece") or "").strip() or None
        mode_cuisson = (request.form.get("mode_cuisson") or "").strip() or None
        recette_referentiel_id = request.form.get("recette_referentiel_id") or None
        reception_ids = [int(v) for v in request.form.getlist("reception_ids") if v.isdigit()]
        decongelation_heure_debut = (request.form.get("decongelation_heure_debut") or "").strip() or None
        decongelation_non_applicable = request.form.get("decongelation_non_applicable") == "1"

        if not nom_recette:
            flash("⚠️ Merci de saisir le nom de la recette.", "warning")
            return render_template(
                "production_cuisine/productions_creer.html",
                recettes_referentiel=recettes_referentiel, types_cuisson=types_cuisson,
                date_defaut=date_production, form=request.form,
                receptions_disponibles=receptions_disponibles, preselection=set(reception_ids),
            )

        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                recette_id, nom_normalise = _get_or_create_recette(conn, nom_recette)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO cuisine_productions
                    (date_production, recette_id, recette_referentiel_id, nom_recette,
                     nom_recette_initial, espece, mode_cuisson, statut, user_creation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'en_cours', ?)
                    """,
                    (date_production, recette_id, recette_referentiel_id, nom_normalise, nom_normalise,
                     espece, mode_cuisson, benevole or None),
                )
                production_id = cur.lastrowid

                if reception_ids:
                    placeholders = ",".join("?" * len(reception_ids))
                    cur.execute(
                        f"""UPDATE cuisine_receptions SET production_id = ?
                            WHERE id IN ({placeholders}) AND production_id IS NULL""",
                        (production_id, *reception_ids),
                    )

                if decongelation_non_applicable:
                    # Produit à température ambiante : pas de décongélation
                    # à suivre, l'étape est directement résolue.
                    cur.execute(
                        """INSERT INTO cuisine_production_etapes
                           (production_id, etape_code, heure_fin, non_applicable, user_creation)
                           VALUES (?, 'decongelation', ?, 1, ?)""",
                        (production_id, now_paris_str(), benevole or None),
                    )
                elif decongelation_heure_debut:
                    # Démarrée depuis l'écran de création (avant même que la
                    # production existe) — on journalise l'étape maintenant
                    # que production_id est connu ; elle reste "en cours"
                    # (heure_fin non renseignée) jusqu'à ce qu'on la termine
                    # depuis la fiche recette.
                    cur.execute(
                        """INSERT INTO cuisine_production_etapes
                           (production_id, etape_code, heure_debut, user_creation)
                           VALUES (?, 'decongelation', ?, ?)""",
                        (production_id, decongelation_heure_debut, benevole or None),
                    )

                conn.commit()
            upload_database()
            flash("✅ Production créée.", "success")
            return redirect(url_for("production_cuisine.detail_production", production_id=production_id))
        except Exception as e:
            write_log(f"❌ Erreur création production cuisine : {e}")
            flash("❌ Erreur lors de la création de la production.", "danger")

    return render_template(
        "production_cuisine/productions_creer.html",
        recettes_referentiel=recettes_referentiel, types_cuisson=types_cuisson,
        date_defaut=date_defaut, form={},
        receptions_disponibles=receptions_disponibles, preselection=preselection,
    )


@production_cuisine_bp.route("/<int:production_id>")
@login_required
@require_access("production_cuisine", "lecture")
def detail_production(production_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row

        production = conn.execute(
            "SELECT * FROM cuisine_productions WHERE id = ? AND actif = 1", (production_id,)
        ).fetchone()
        if not production:
            flash("⛔ Production introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_productions"))

        etapes_ref = conn.execute(
            "SELECT * FROM cuisine_etapes_ref WHERE actif = 1 ORDER BY ordre"
        ).fetchall()

        etapes_saisies = conn.execute(
            "SELECT * FROM cuisine_production_etapes WHERE production_id = ? ORDER BY id",
            (production_id,),
        ).fetchall()

        # Dernière ligne (ouverte ou fermée) par code d'étape, pour affichage état
        etapes_par_code = {}
        for e in etapes_saisies:
            etapes_par_code[e["etape_code"]] = e  # la dernière écrase les précédentes (ordre id ASC)

        # Ordre métier : une étape (non optionnelle) est "résolue" dès qu'une
        # ligne avec heure_fin existe (terminée normalement OU non applicable).
        # Sert à verrouiller les étapes suivantes tant que ce n'est pas le cas.
        codes_resolus = {e["etape_code"] for e in etapes_saisies if e["heure_fin"] is not None}
        bloque_par = {}
        for ref in etapes_ref:
            precedentes = [r for r in etapes_ref if r["ordre"] < ref["ordre"] and not r["optionnelle"]]
            bloquante = next((p["libelle"] for p in precedentes if p["code"] not in codes_resolus), None)
            bloque_par[ref["code"]] = bloquante

        # Décongélation en cours : priorité absolue sur toutes les autres
        # étapes (on ne fait rien d'autre pendant qu'un produit décongèle),
        # et affichée en tête de liste pour rester visible tant qu'elle
        # n'est pas terminée.
        if decongelation_en_cours(conn, production_id):
            for ref in etapes_ref:
                if ref["code"] != "decongelation":
                    bloque_par[ref["code"]] = "Décongélation"
        # decongelation.ordre = 0 : toujours en tête de liste naturellement
        # (ORDER BY ordre ci-dessus), pas besoin de tri supplémentaire.

        conformite_globale = None
        mise_en_cellule = etapes_par_code.get("refroidissement_cellule")
        if mise_en_cellule and mise_en_cellule["heure_fin"] is not None:
            conformite_globale = mise_en_cellule["conforme"]

        recettes_referentiel = _recettes_referentiel_json(conn)

        lots = conn.execute(
            """
            SELECT l.*, f.nom AS fournisseur_nom
            FROM cuisine_traca_lots l
            LEFT JOIN fournisseurs f ON f.id = l.fournisseur_id
            WHERE l.production_id = ?
            ORDER BY l.id DESC
            """,
            (production_id,),
        ).fetchall()

        photos_rows = conn.execute(
            """
            SELECT p.id, p.lot_id
            FROM cuisine_traca_photos p
            JOIN cuisine_traca_lots l ON l.id = p.lot_id
            WHERE l.production_id = ?
            ORDER BY p.lot_id, p.ordre
            """,
            (production_id,),
        ).fetchall()
        photos_par_lot = {}
        for p in photos_rows:
            photos_par_lot.setdefault(p["lot_id"], []).append(p["id"])

        receptions = conn.execute(
            """
            SELECT r.*, f.nom AS fournisseur_nom
            FROM cuisine_receptions r
            LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
            WHERE r.production_id = ?
            ORDER BY r.heure_arrivee, r.id
            """,
            (production_id,),
        ).fetchall()

        reception_photos_rows = conn.execute(
            """
            SELECT p.id, p.reception_id
            FROM cuisine_reception_photos p
            JOIN cuisine_receptions r ON r.id = p.reception_id
            WHERE r.production_id = ?
            ORDER BY p.reception_id, p.type_photo, p.ordre
            """,
            (production_id,),
        ).fetchall()
        photos_par_reception = {}
        for p in reception_photos_rows:
            photos_par_reception.setdefault(p["reception_id"], []).append(p["id"])

        receptions_disponibles = _receptions_en_attente(conn, production["date_production"])

        fournisseurs = conn.execute(
            "SELECT id, nom FROM fournisseurs WHERE actif = 'oui' ORDER BY nom COLLATE NOCASE"
        ).fetchall()

        quantites_rows = conn.execute(
            "SELECT taille, quantite FROM cuisine_production_quantites WHERE production_id = ?",
            (production_id,),
        ).fetchall()
        quantites = {"1/2": 0, "1/4": 0, "1/8": 0}
        for q in quantites_rows:
            quantites[q["taille"]] = q["quantite"]

        validation = conn.execute(
            "SELECT * FROM cuisine_production_validations WHERE production_id = ?",
            (production_id,),
        ).fetchone()

        renommages = conn.execute(
            "SELECT * FROM cuisine_productions_renommages WHERE production_id = ? ORDER BY id DESC",
            (production_id,),
        ).fetchall()

        stock_barquettes = conn.execute(
            """SELECT s.*, a.taille FROM cuisine_stock_barquettes s
               JOIN cuisine_articles_barquettes a ON a.id = s.article_id
               WHERE s.production_id = ? AND s.actif = 1 ORDER BY a.taille""",
            (production_id,),
        ).fetchall()

        libelles_barquettes = {
            row["taille"]: row["libelle"]
            for row in conn.execute(
                "SELECT taille, libelle FROM cuisine_articles_barquettes WHERE actif = 1"
            ).fetchall()
        }

    # Bouton "← Retour" contextuel : si on arrive depuis le stock barquettes
    # (lien recette du détail de stock), on y revient plutôt que sur la
    # liste générale des productions.
    if request.args.get("depuis_stock"):
        retour_url = url_for("production_cuisine.liste_stock_barquettes")
    else:
        retour_url = url_for("production_cuisine.liste_productions")

    return render_template(
        "production_cuisine/production_detail.html",
        production=production,
        etapes_ref=etapes_ref,
        etapes_par_code=etapes_par_code,
        bloque_par=bloque_par,
        lots=lots,
        photos_par_lot=photos_par_lot,
        receptions=receptions,
        photos_par_reception=photos_par_reception,
        receptions_disponibles=receptions_disponibles,
        fournisseurs=fournisseurs,
        quantites=quantites,
        validation=validation,
        renommages=renommages,
        today=today_paris(),
        conformite_globale=conformite_globale,
        recettes_referentiel=recettes_referentiel,
        stock_barquettes=stock_barquettes,
        retour_url=retour_url,
        libelles_barquettes=libelles_barquettes,
    )


@production_cuisine_bp.route("/<int:production_id>/renommer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def renommer_production(production_id):
    nouveau_nom = (request.form.get("nouveau_nom") or "").strip()
    benevole = (request.form.get("benevole") or "").strip()
    recette_referentiel_id = request.form.get("recette_referentiel_id") or None

    if not nouveau_nom:
        return jsonify({"ok": False, "error": "Nom vide."}), 400

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        production = conn.execute(
            "SELECT nom_recette, statut FROM cuisine_productions WHERE id = ?", (production_id,)
        ).fetchone()
        if not production:
            return jsonify({"ok": False, "error": "Production introuvable."}), 404
        if production["statut"] == "terminee":
            return jsonify({"ok": False, "error": "Production terminée : la recette ne peut plus être changée."}), 400

        ancien_nom = production["nom_recette"]
        cur = conn.cursor()
        if recette_referentiel_id is not None:
            # Une nouvelle recette a été choisie dans la grille : on met
            # aussi à jour le lien référentiel. Sinon (juste un nom modifié
            # à la main) on ne touche pas au lien existant.
            cur.execute(
                """UPDATE cuisine_productions
                   SET nom_recette = ?, recette_referentiel_id = ?, user_modif = ?
                   WHERE id = ?""",
                (nouveau_nom, recette_referentiel_id, benevole or None, production_id),
            )
        else:
            cur.execute(
                "UPDATE cuisine_productions SET nom_recette = ?, user_modif = ? WHERE id = ?",
                (nouveau_nom, benevole or None, production_id),
            )
        cur.execute(
            """INSERT INTO cuisine_productions_renommages
               (production_id, ancien_nom, nouveau_nom, date_renommage, user_creation)
               VALUES (?, ?, ?, ?, ?)""",
            (production_id, ancien_nom, nouveau_nom, now_paris_str(), benevole or None),
        )
        conn.commit()

    upload_database()
    return jsonify({"ok": True, "nouveau_nom": nouveau_nom})


@production_cuisine_bp.route("/<int:production_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_production(production_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        production = conn.execute(
            "SELECT id FROM cuisine_productions WHERE id = ?", (production_id,)
        ).fetchone()
        if not production:
            flash("⛔ Production introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_productions"))

        cur = conn.cursor()
        # Suppression douce (comme les autres tables du module : actif=0) —
        # on ne perd pas l'historique (étapes, lots, photos).
        cur.execute("UPDATE cuisine_productions SET actif = 0 WHERE id = ?", (production_id,))
        # Les réceptions qui alimentaient cette production redeviennent
        # disponibles pour être affectées à une autre recette.
        cur.execute(
            "UPDATE cuisine_receptions SET production_id = NULL WHERE production_id = ?",
            (production_id,),
        )
        conn.commit()

    upload_database()
    flash("🗑️ Production supprimée.", "success")
    return redirect(url_for("production_cuisine.liste_productions"))
