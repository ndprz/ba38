# ============================================================
# 🍲 Productions du jour (écran principal "run" tablette)
# ============================================================

import sqlite3

from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_production_cuisine import production_cuisine_bp
from ba38_production_cuisine.utils import _connect, today_paris, now_paris_str

STATUTS = ("en_cours", "terminee", "annulee")


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
    )


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
        recettes = conn.execute(
            "SELECT nom FROM cuisine_recettes WHERE actif = 1 ORDER BY nom COLLATE NOCASE"
        ).fetchall()
        receptions_disponibles = _receptions_en_attente(conn, date_defaut)

    if request.method == "POST":
        benevole = (request.form.get("benevole") or "").strip()
        date_production = request.form.get("date_production") or today_paris()
        nom_recette = (request.form.get("nom_recette") or "").strip()
        espece = (request.form.get("espece") or "").strip() or None
        mode_cuisson = (request.form.get("mode_cuisson") or "").strip() or None
        reception_ids = [int(v) for v in request.form.getlist("reception_ids") if v.isdigit()]

        if not nom_recette:
            flash("⚠️ Merci de saisir le nom de la recette.", "warning")
            return render_template(
                "production_cuisine/productions_creer.html",
                recettes=recettes, date_defaut=date_production, form=request.form,
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
                    (date_production, recette_id, nom_recette, nom_recette_initial,
                     espece, mode_cuisson, statut, user_creation)
                    VALUES (?, ?, ?, ?, ?, ?, 'en_cours', ?)
                    """,
                    (date_production, recette_id, nom_normalise, nom_normalise,
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

                conn.commit()
            upload_database()
            flash("✅ Production créée.", "success")
            return redirect(url_for("production_cuisine.detail_production", production_id=production_id))
        except Exception as e:
            write_log(f"❌ Erreur création production cuisine : {e}")
            flash("❌ Erreur lors de la création de la production.", "danger")

    return render_template(
        "production_cuisine/productions_creer.html",
        recettes=recettes, date_defaut=date_defaut, form={},
        receptions_disponibles=receptions_disponibles, preselection=preselection,
    )


@production_cuisine_bp.route("/<int:production_id>")
@login_required
@require_access("production_cuisine", "lecture")
def detail_production(production_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row

        production = conn.execute(
            "SELECT * FROM cuisine_productions WHERE id = ?", (production_id,)
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

    return render_template(
        "production_cuisine/production_detail.html",
        production=production,
        etapes_ref=etapes_ref,
        etapes_par_code=etapes_par_code,
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
    )


@production_cuisine_bp.route("/<int:production_id>/renommer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def renommer_production(production_id):
    nouveau_nom = (request.form.get("nouveau_nom") or "").strip()
    benevole = (request.form.get("benevole") or "").strip()

    if not nouveau_nom:
        return jsonify({"ok": False, "error": "Nom vide."}), 400

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        production = conn.execute(
            "SELECT nom_recette FROM cuisine_productions WHERE id = ?", (production_id,)
        ).fetchone()
        if not production:
            return jsonify({"ok": False, "error": "Production introuvable."}), 404

        ancien_nom = production["nom_recette"]
        cur = conn.cursor()
        cur.execute(
            "UPDATE cuisine_productions SET nom_recette = ?, user_modif = ? WHERE id = ?",
            (nouveau_nom, benevole or None, production_id),
        )
        cur.execute(
            """INSERT INTO cuisine_productions_renommages
               (production_id, ancien_nom, nouveau_nom, user_creation)
               VALUES (?, ?, ?, ?)""",
            (production_id, ancien_nom, nouveau_nom, benevole or None),
        )
        conn.commit()

    upload_database()
    return jsonify({"ok": True, "nouveau_nom": nouveau_nom})
