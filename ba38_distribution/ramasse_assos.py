# ba38_distribution/ramasse_assos.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash
from flask import Response
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, require_access, write_log
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime, timedelta
from calendar import monthrange
from collections import defaultdict

from . import distribution_bp

import sqlite3




# -------------------------------------------------------------------
#   TRAITEMENT DES RAMASSE PARTENAIRES
# -------------------------------------------------------------------


# ============================================================
# 📦 MODÈLES DE RAMASSE
# ============================================================

@distribution_bp.route("/ramasse_types_list")
@login_required
@require_access("distribution", "ecriture")
def ramasse_types_list():

    # # 🔥 mois prioritaire = session
    # mois = session.get("mois_ramasse")

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        types = cur.execute("""
            SELECT t.id, t.nom_type,
                   a.nom_association,
                   f.nom AS nom_magasin
            FROM ramasses_partenaire_type_entete t
            LEFT JOIN associations a ON a.id = t.id_association
            LEFT JOIN fournisseurs f ON f.id = t.id_magasin
            ORDER BY t.nom_type
        """).fetchall()

    return render_template(
        "distribution/ramasse_assos/ramasse_types_list.html",
        types=types
    )


# ============================================================
# ➕ CRÉATION / ✏️ MODIFICATION
# ============================================================

@distribution_bp.route("/ramasse_types_list/create", methods=["GET", "POST"])
@distribution_bp.route("/ramasse_types_list/<int:id>/edit", methods=["GET", "POST"])
@login_required
@require_access("distribution", "ecriture")
def ramasse_type_form(id=None):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # =====================================================
        # 🔹 LISTES
        # =====================================================
        associations = cur.execute("""
            SELECT id, nom_association
            FROM associations
            ORDER BY nom_association
        """).fetchall()

        fournisseurs = cur.execute("""
            SELECT id, nom, code_vif
            FROM fournisseurs
            ORDER BY nom
        """).fetchall()

        articles = cur.execute("""
            SELECT article, libelle
            FROM articles
            ORDER BY libelle
        """).fetchall()

        # =====================================================
        # 🔹 MODE EDITION
        # =====================================================
        lignes = []
        type_data = None

        if id:
            type_data = cur.execute("""
                SELECT * FROM ramasses_partenaire_type_entete
                WHERE id=?
            """, (id,)).fetchone()

            lignes = cur.execute("""
                SELECT * FROM ramasses_partenaire_type_detail
                WHERE id_ramasse_type=?
            """, (id,)).fetchall()

        # =====================================================
        # 🔹 POST
        # =====================================================
        if request.method == "POST":

            nom = request.form.get("nom_type")
            id_asso = request.form.get("id_association")
            id_magasin = request.form.get("id_magasin")

            codes = request.form.getlist("code_vif")
            reps = request.form.getlist("repartition")

            # =====================================================
            # 🔴 VALIDATION FOURNISSEUR (code_vif obligatoire)
            # =====================================================
            fournisseur = cur.execute("""
                SELECT nom, code_vif
                FROM fournisseurs
                WHERE id = ?
            """, (id_magasin,)).fetchone()

            if not fournisseur or not fournisseur["code_vif"]:
                flash(
                    f"❌ Fournisseur sans code VIF : {fournisseur['nom'] if fournisseur else 'inconnu'}",
                    "danger"
                )

                return render_template(
                    "distribution/ramasse_assos/ramasse_type_form.html",
                    type_data=type_data,
                    lignes=[{"code_vif": c, "repartition": r} for c, r in zip(codes, reps)],
                    associations=associations,
                    fournisseurs=fournisseurs,
                    articles=articles
                )

            # =====================================================
            # 🔴 VALIDATION POURCENTAGES
            # =====================================================
            total = 0
            lignes_valides = []

            for c, r in zip(codes, reps):

                if not c:
                    continue

                try:
                    rep = float(r or 0)
                except:
                    rep = 0

                total += rep
                lignes_valides.append((c.strip(), rep))

            if round(total, 2) != 100:
                flash(f"❌ Total des pourcentages = {total}% (doit être 100%)", "danger")

                return render_template(
                    "distribution/ramasse_assos/ramasse_type_form.html",
                    type_data=type_data,
                    lignes=[{"code_vif": c, "repartition": r} for c, r in lignes_valides],
                    associations=associations,
                    fournisseurs=fournisseurs,
                    articles=articles
                )

            # =====================================================
            # 🔄 INSERT / UPDATE ENTETE
            # =====================================================
            if id:
                cur.execute("""
                    UPDATE ramasses_partenaire_type_entete
                    SET nom_type=?, id_association=?, id_magasin=?
                    WHERE id=?
                """, (nom, id_asso, id_magasin, id))

                cur.execute("""
                    DELETE FROM ramasses_partenaire_type_detail
                    WHERE id_ramasse_type=?
                """, (id,))
            else:
                cur.execute("""
                    INSERT INTO ramasses_partenaire_type_entete
                    (nom_type, id_association, id_magasin)
                    VALUES (?, ?, ?)
                """, (nom, id_asso, id_magasin))

                id = cur.lastrowid

            # =====================================================
            # 🔹 INSERT LIGNES
            # =====================================================
            for c, rep in lignes_valides:
                cur.execute("""
                    INSERT INTO ramasses_partenaire_type_detail
                    (id_ramasse_type, code_vif, repartition)
                    VALUES (?, ?, ?)
                """, (id, c, rep))

            conn.commit()

            flash("✅ Modèle enregistré", "success")
            return redirect(url_for("distribution.ramasse_types_list"))

    # =====================================================
    # 🔹 AFFICHAGE
    # =====================================================
    return render_template(
        "distribution/ramasse_assos/ramasse_type_form.html",
        type_data=type_data,
        lignes=lignes,
        associations=associations,
        fournisseurs=fournisseurs,
        articles=articles
    )

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 🔹 listes
        associations = cur.execute("""
            SELECT id, nom_association
            FROM associations
            ORDER BY nom_association
        """).fetchall()

        fournisseurs = cur.execute("""
            SELECT id, nom, code_vif
            FROM fournisseurs
            ORDER BY nom
        """).fetchall()

        articles = cur.execute("""
            SELECT article, libelle
            FROM articles
            ORDER BY libelle
        """).fetchall()

        # 🔹 édition
        lignes = []
        type_data = None

        if id:
            type_data = cur.execute("""
                SELECT * FROM ramasses_partenaire_type_entete
                WHERE id=?
            """, (id,)).fetchone()

            lignes = cur.execute("""
                SELECT * FROM ramasses_partenaire_type_detail
                WHERE id_ramasse_type=?
            """, (id,)).fetchall()

        # 🔹 POST
        if request.method == "POST":

            nom = request.form.get("nom_type")
            id_asso = request.form.get("id_association")
            id_magasin = request.form.get("id_magasin")

            if id:
                # UPDATE
                cur.execute("""
                    UPDATE ramasses_partenaire_type_entete
                    SET nom_type=?, id_association=?, id_magasin=?
                    WHERE id=?
                """, (nom, id_asso, id_magasin, id))

                cur.execute("""
                    DELETE FROM ramasses_partenaire_type_detail
                    WHERE id_ramasse_type=?
                """, (id,))
            else:
                # INSERT
                cur.execute("""
                    INSERT INTO ramasses_partenaire_type_entete
                    (nom_type, id_association, id_magasin)
                    VALUES (?, ?, ?)
                """, (nom, id_asso, id_magasin))

                id = cur.lastrowid

            # 🔹 lignes
            codes = request.form.getlist("code_vif")
            reps = request.form.getlist("repartition")

            total = 0
            lignes_valides = []

            # 🔹 1. VALIDATION
            for c, r in zip(codes, reps):

                if not c:
                    continue

                try:
                    rep = float(r or 0)
                except:
                    rep = 0

                total += rep
                lignes_valides.append((c, rep))

            # 🔴 BLOQUANT
            if round(total, 2) != 100:
                flash(f"❌ Total des pourcentages = {total}% (doit être 100%)", "danger")
                return render_template(
                    "distribution/ramasse_assos/ramasse_type_form.html",
                    type_data=type_data,
                    lignes=[{"code_vif": c, "repartition": rep} for c, rep in lignes_valides],
                    associations=associations,
                    fournisseurs=fournisseurs,
                    articles=articles
                )

            # 🔹 2. SUPPRESSION (SEULEMENT SI OK)
            cur.execute("""
                DELETE FROM ramasses_partenaire_type_detail
                WHERE id_ramasse_type=?
            """, (id,))

            # 🔹 3. INSERTION
            for c, rep in lignes_valides:
                cur.execute("""
                    INSERT INTO ramasses_partenaire_type_detail
                    (id_ramasse_type, code_vif, repartition)
                    VALUES (?, ?, ?)
                """, (id, c, rep))

            conn.commit()

            flash("✅ Modèle enregistré", "success")
            return redirect(url_for("distribution.ramasse_types_list"))

    return render_template(
        "distribution/ramasse_assos/ramasse_type_form.html",
        type_data=type_data,
        lignes=lignes,
        associations=associations,
        fournisseurs=fournisseurs,
        articles=articles
    )


# ============================================================
# 🗑️ SUPPRESSION
# ============================================================

@distribution_bp.route("/ramasse_types_list/<int:id>/delete")
@login_required
@require_access("distribution", "ecriture")
def ramasse_type_delete(id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        cur.execute("DELETE FROM ramasses_partenaire_type_detail WHERE id_ramasse_type=?", (id,))
        cur.execute("DELETE FROM ramasses_partenaire_type_entete WHERE id=?", (id,))

        conn.commit()

    flash("🗑️ Modèle supprimé", "success")
    return redirect(url_for("distribution.ramasse_types_list"))


# ============================================================
# SAISIE RAMASSE
# ============================================================
@distribution_bp.route("/ramasse_saisie/<int:id_modele>", methods=["GET", "POST"])
@login_required
@require_access("distribution", "ecriture")
def saisie_ramasse(id_modele):

    conn = get_db_connection()
    cur = conn.cursor()

    # =====================================================
    # 🔹 ENTETE MODELE
    # =====================================================
    entete = cur.execute("""
        SELECT t.*,
               a.nom_association,
               f.nom AS nom_fournisseur
        FROM ramasses_partenaire_type_entete t
        LEFT JOIN associations a ON a.id = t.id_association
        LEFT JOIN fournisseurs f ON f.id = t.id_magasin
        WHERE t.id = ?
    """, (id_modele,)).fetchone()

    # =====================================================
    # 🔹 LIGNES MODELE
    # =====================================================
    lignes = cur.execute("""
        SELECT d.code_vif,
               d.repartition,
               ar.libelle
        FROM ramasses_partenaire_type_detail d
        LEFT JOIN articles ar ON ar.article = d.code_vif
        WHERE d.id_ramasse_type = ?
        ORDER BY d.id
    """, (id_modele,)).fetchall()



    # =====================================================
    # 🔹 LECTURE SAISIE PRECEDENTE
    # =====================================================
    mois = session.get("mois_ramasse")

    ramasse_existante = None
    lignes_existantes = {}

    if mois:
        ramasse_existante = cur.execute("""
            SELECT *
            FROM ramasses_partenaire
            WHERE id_association = ?
            AND id_fournisseur = ?
            AND strftime('%Y-%m', date_ramasse) = ?
        """, (
            entete["id_association"],
            entete["id_magasin"],
            mois
        )).fetchone()

        if ramasse_existante:
            rows = cur.execute("""
                SELECT code_vif, quantite_kg
                FROM ramasses_partenaire_lignes
                WHERE id_ramasse = ?
            """, (ramasse_existante["id"],)).fetchall()

            lignes_existantes = {
                r["code_vif"]: r["quantite_kg"]
                for r in rows
            }
    # =====================================================
    # 🔹 POST
    # =====================================================
    if request.method == "POST":

        date_ramasse = request.form.get("date_saisie")
        user_id = session.get("user_id")
        mois = session.get("mois_ramasse")

        # 🔥 RECHERCHE A REFAIRE ICI (IMPORTANT)
        ramasse_existante = cur.execute("""
            SELECT *
            FROM ramasses_partenaire
            WHERE id_association = ?
            AND id_fournisseur = ?
            AND strftime('%Y-%m', date_ramasse) = ?
        """, (
            entete["id_association"],
            entete["id_magasin"],
            mois
        )).fetchone()

        # =====================================================
        # 🔄 UPDATE ou INSERT
        # =====================================================
        if ramasse_existante:

            id_ramasse = ramasse_existante["id"]

            # 🔥 SUPPRESSION DES ANCIENNES LIGNES
            cur.execute("""
                DELETE FROM ramasses_partenaire_lignes
                WHERE id_ramasse = ?
            """, (id_ramasse,))

            # 🔄 mise à jour date
            cur.execute("""
                UPDATE ramasses_partenaire
                SET date_ramasse = ?
                WHERE id = ?
            """, (date_ramasse, id_ramasse))

        else:
            # ➕ CREATION
            cur.execute("""
                INSERT INTO ramasses_partenaire (
                    id_association,
                    id_fournisseur,
                    date_ramasse,
                    created_at,
                    created_by
                )
                VALUES (?, ?, ?, datetime('now'), ?)
            """, (
                entete["id_association"],
                entete["id_magasin"],
                date_ramasse,
                user_id
            ))

            id_ramasse = cur.lastrowid

        # =====================================================
        # 🔹 INSERT DES LIGNES
        # =====================================================
        for key, value in request.form.items():

            if key.startswith("poids_"):

                code_vif = key.replace("poids_", "")
                quantite = float(value) if value else 0

                if quantite > 0:
                    cur.execute("""
                        INSERT INTO ramasses_partenaire_lignes (
                            id_ramasse,
                            code_vif,
                            quantite_kg
                        )
                        VALUES (?, ?, ?)
                    """, (
                        id_ramasse,
                        code_vif,
                        quantite
                    ))

        conn.commit()
        conn.close()

        flash("✅ Ramasse enregistrée", "success")
        return redirect(url_for("distribution.ramasse_types_list"))

    conn.close()

    mois = session.get("mois_ramasse")

    if mois:
        annee, m = mois.split("-")
        dernier_jour = monthrange(int(annee), int(m))[1]

        date_defaut = f"{annee}-{m}-{dernier_jour:02d}"
    else:
        date_defaut = datetime.now().strftime("%Y-%m-%d")

    return render_template(
        "distribution/ramasse_assos/ramasse_saisie.html",
        entete=entete,
        lignes=lignes,
        today=date_defaut,
        ramasse_existante=ramasse_existante,
        lignes_existantes=lignes_existantes
    )



# ============================================================
#  HISTORIQUE
# ============================================================
@distribution_bp.route("/ramasses_list")
@login_required
@require_access("distribution", "lecture")
def liste_ramasses():

    # 🔥 mois prioritaire = session
    mois = session.get("mois_ramasse")

    conn = get_db_connection()
    cur = conn.cursor()

    if mois:
        rows = cur.execute("""
            SELECT r.id,
                   r.date_ramasse,
                   a.nom_association,
                   f.nom AS nom_fournisseur,
                   SUM(l.quantite_kg) AS total_kg
            FROM ramasses_partenaire r
            LEFT JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
            LEFT JOIN associations a ON a.id = r.id_association
            LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
            WHERE strftime('%Y-%m', r.date_ramasse) = ?
            GROUP BY r.id
            ORDER BY r.date_ramasse DESC
        """, (mois,)).fetchall()
    else:
        # fallback si pas de mois
        rows = cur.execute("""
            SELECT r.id,
                   r.date_ramasse,
                   a.nom_association,
                   f.nom AS nom_fournisseur,
                   SUM(l.quantite_kg) AS total_kg
            FROM ramasses_partenaire r
            LEFT JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
            LEFT JOIN associations a ON a.id = r.id_association
            LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
            GROUP BY r.id
            ORDER BY r.date_ramasse DESC
        """).fetchall()

    conn.close()


    return render_template(
        "distribution/ramasse_assos/ramasses_list.html",
        ramasses=rows,
        mois=mois
    )


@distribution_bp.route("/ramasses/<int:id>")
@login_required
@require_access("distribution", "lecture")
def detail_ramasse(id):

    conn = get_db_connection()
    cur = conn.cursor()

    entete = cur.execute("""
        SELECT r.*,
               a.nom_association,
               f.nom AS nom_fournisseur
        FROM ramasses_partenaire r
        LEFT JOIN associations a ON a.id = r.id_association
        LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
        WHERE r.id = ?
    """, (id,)).fetchone()

    lignes = cur.execute("""
        SELECT l.code_vif,
               l.quantite_kg,
               ar.libelle
        FROM ramasses_partenaire_lignes l
        LEFT JOIN articles ar ON ar.article = l.code_vif
        WHERE l.id_ramasse = ?
        ORDER BY ar.libelle
    """, (id,)).fetchall()

    conn.close()

    return render_template(
        "distribution/ramasse_assos/ramasse_detail.html",
        entete=entete,
        lignes=lignes
    )


@distribution_bp.route("/ramasses/<int:id>/delete")
@login_required
@require_access("distribution", "ecriture")
def delete_ramasse(id):

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM ramasses_partenaire_lignes WHERE id_ramasse=?", (id,))
    cur.execute("DELETE FROM ramasses_partenaire WHERE id=?", (id,))

    conn.commit()
    conn.close()

    flash("🗑️ Ramasse supprimée", "success")
    return redirect(url_for("distribution.liste_ramasses"))



@distribution_bp.route("/ramasse_type_duplicate/<int:id>/duplicate", methods=["GET", "POST"])
@login_required
@require_access("distribution", "ecriture")
def ramasse_type_duplicate(id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 🔹 données source
        source = cur.execute("""
            SELECT * FROM ramasses_partenaire_type_entete
            WHERE id = ?
        """, (id,)).fetchone()

        if not source:
            flash("❌ Modèle source introuvable", "danger")
            return redirect(url_for("distribution.ramasse_types_list"))

        lignes = cur.execute("""
            SELECT * FROM ramasses_partenaire_type_detail
            WHERE id_ramasse_type = ?
        """, (id,)).fetchall()

        # 🔹 listes pour formulaire
        associations = cur.execute("""
            SELECT id, nom_association
            FROM associations
            ORDER BY nom_association
        """).fetchall()

        fournisseurs = cur.execute("""
            SELECT id, nom, code_vif
            FROM fournisseurs
            ORDER BY nom
        """).fetchall()

        # =====================================================
        # 🔹 POST → duplication
        # =====================================================
        if request.method == "POST":

            nom = request.form.get("nom_type")
            id_asso = request.form.get("id_association")
            id_magasin = request.form.get("id_magasin")

            if not nom:
                flash("❌ Le nom est obligatoire", "danger")
                return redirect(request.url)

            # 🔹 création nouvel entête
            cur.execute("""
                INSERT INTO ramasses_partenaire_type_entete
                (nom_type, id_association, id_magasin)
                VALUES (?, ?, ?)
            """, (nom, id_asso, id_magasin))

            new_id = cur.lastrowid

            # 🔹 copie des lignes
            for l in lignes:
                cur.execute("""
                    INSERT INTO ramasses_partenaire_type_detail
                    (id_ramasse_type, code_vif, repartition)
                    VALUES (?, ?, ?)
                """, (
                    new_id,
                    l["code_vif"],
                    l["repartition"]
                ))

            conn.commit()

            flash("✅ Modèle dupliqué avec succès", "success")
            return redirect(url_for("distribution.ramasse_type_form", id=new_id))

    return render_template(
        "distribution/ramasse_assos/ramasse_type_duplicate.html",
        source=source,
        associations=associations,
        fournisseurs=fournisseurs
    )



@distribution_bp.route("/ramasses/export_vif_mensuel", methods=["POST"])
@login_required
@require_access("distribution", "lecture")
def export_ramasses_vif_mensuel():

    mois = request.form.get("mois") or session.get("mois_ramasse")
    force = request.form.get("force") == "1"

    date_du_jour_plus_2 = (datetime.now() + timedelta(days=2)).strftime("%d/%m/%Y")

    if not mois:
        flash("❌ Mois non sélectionné", "danger")
        return redirect(url_for("distribution.liste_ramasses"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 🔴 contrôle export déjà fait
    deja = cur.execute("""
        SELECT 1 FROM exports_vif
        WHERE mois = ? AND type_export = 'reception'
    """, (mois,)).fetchone()

    if deja and not force:
        flash("⚠️ Export réception déjà effectué pour ce mois", "warning")
        conn.close()
        return redirect(url_for("distribution.controle_ramasses_get", mois=mois, confirm_reception=1))

    # ================= DATA =================
    rows = cur.execute("""
        SELECT
            r.id_fournisseur,
            f.code_vif,
            f.nom,
            l.code_vif AS article_code_vif,
            ar.libelle,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l
            ON l.id_ramasse = r.id
        LEFT JOIN fournisseurs f
            ON f.id = r.id_fournisseur
        LEFT JOIN articles ar
            ON ar.article = l.code_vif
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_fournisseur, l.code_vif
        ORDER BY f.nom, l.code_vif
    """, (mois,)).fetchall()

    # 🔴 contrôle VIF
    erreurs = [f"Fournisseur sans code VIF : {r['nom']}" for r in rows if not r["code_vif"]]

    if erreurs:
        for e in erreurs:
            flash(f"❌ {e}", "danger")
        conn.close()
        return redirect(url_for("distribution.liste_ramasses"))

    if not mois:
        flash("❌ Aucun mois de travail sélectionné", "danger")
        return redirect(url_for("distribution.liste_ramasses"))

    if "-" not in mois:
        flash(f"❌ Format de mois invalide : {mois}", "danger")
        return redirect(url_for("distribution.liste_ramasses"))
    # 🔹 date réception

    annee, m = mois.split("-")
    dernier_jour = monthrange(int(annee), int(m))[1]
    date_recep = f"{dernier_jour}/{m}/{annee}"

    lignes_csv = []

    for r in rows:
        article = (r["article_code_vif"] or "").strip()
        libelle = (r["libelle"] or "").lower()

        if "non loti" in libelle:
            lot = ""
        else:
            lot = f"RP{annee[-2:]}{m}{article}"

        ligne = [
            "01",
            "38",
            date_recep,
            r["code_vif"],
            "01",
            "03",
            r["article_code_vif"],
            f"{float(r['total_kg'] or 0):.3f}",
            "KG",
            lot,
            date_du_jour_plus_2,
            date_du_jour_plus_2,
            "",
            "RA"
        ]

        lignes_csv.append(";".join(map(str, ligne)))

    output = "\n".join(lignes_csv)

    # 🔹 trace
    cur.execute("""
        INSERT INTO exports_vif (mois, type_export, date_export, user)
        VALUES (?, 'reception', datetime('now'), ?)
    """, (mois, current_user.email))

    conn.commit()
    conn.close()

    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=recepba38.csv"
        }
    )


@distribution_bp.route("/ramasses/export_excel_mensuel", methods=["POST"])
@login_required
@require_access("distribution", "lecture")
def export_ramasses_excel_mensuel():

    mois = request.form.get("mois") or session.get("mois_ramasse")  # format YYYY-MM

    if not mois:
        flash("❌ Mois non sélectionné", "danger")
        return redirect(url_for("distribution.liste_ramasses"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT
            r.date_ramasse,
            f.nom AS fournisseur,
            f.code_vif AS fournisseur_code_vif,
            a.nom_association,
            l.code_vif AS article_code_vif,
            ar.libelle AS article_libelle,
            l.quantite_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
        LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
        LEFT JOIN associations a ON a.id = r.id_association
        LEFT JOIN articles ar ON ar.article = l.code_vif
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        ORDER BY r.date_ramasse, f.nom, a.nom_association, ar.libelle
    """, (mois,)).fetchall()

    conn.close()

    # ======================================================
    # 📄 CREATION EXCEL
    # ======================================================
    wb = Workbook()
    ws = wb.active
    ws.title = "Ramasses"

    # 🔹 Entête
    headers = [
        "Date",
        "Fournisseur",
        "Code VIF Fournisseur",
        "Association",
        "Article",
        "Code VIF Article",
        "Quantité (kg)"
    ]

    ws.append(headers)

    # 🔹 Lignes
    for r in rows:
        ws.append([
            r["date_ramasse"],
            r["fournisseur"],
            r["fournisseur_code_vif"],
            r["nom_association"],
            r["article_libelle"],
            r["article_code_vif"],
            r["quantite_kg"]
        ])

    # 🔹 Mise en forme simple
    for col in ws.columns:
        max_length = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_length + 2, 40)

    # 🔹 Export mémoire
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=f"controle_ramasses_{mois}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )





@distribution_bp.route("/ramasses/export_bl_vif", methods=["POST"])
@login_required
@require_access("distribution", "lecture")
def export_bl_vif():

    mois = request.form.get("mois") or session.get("mois_ramasse")
    force = request.form.get("force") == "1"

    if not mois:
        flash("❌ Mois non sélectionné", "danger")
        return redirect(url_for("distribution.liste_ramasses"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 🔴 contrôle export déjà fait
    deja = cur.execute("""
        SELECT 1 FROM exports_vif
        WHERE mois = ? AND type_export = 'bl'
    """, (mois,)).fetchone()

    if deja and not force:
        flash("⚠️ Export BL déjà effectué pour ce mois", "warning")
        conn.close()
        return redirect(url_for("distribution.controle_ramasses_get", mois=mois, confirm_commande=1))

    # ==============================
    # 🔹 DATA
    # ==============================
    rows = cur.execute("""
        SELECT
            r.id_association,
            a.code_vif,
            a.nom_association,
            l.code_vif AS article,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
        LEFT JOIN associations a ON a.id = r.id_association
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_association, l.code_vif
        ORDER BY a.code_vif, l.code_vif
    """, (mois,)).fetchall()

    rows = sorted(rows, key=lambda x: (x["code_vif"], x["article"]))

    # 🔴 contrôle VIF
    erreurs = []
    for r in rows:
        if not r["code_vif"]:
            erreurs.append(f"Association sans code VIF : {r['nom_association']}")

    if erreurs:
        for e in erreurs:
            flash(f"❌ {e}", "danger")
        conn.close()
        return redirect(url_for("distribution.liste_ramasses"))

    # 🔹 date BL = dernier jour du mois
    annee, mois_num = mois.split("-")
    dernier_jour = monthrange(int(annee), int(mois_num))[1]
    date_bl = f"{dernier_jour:02d}/{mois_num}/{annee[-2:]}"

    lignes_csv = []


    # 🔹 génération lignes
    for r in rows:

        article = (r["article"] or "").strip()

        # =====================================================
        # Détermination du lot
        # Si le libellé contient "non loti" alors pas de lot
        # =====================================================
        row_article = cur.execute("""
            SELECT libelle
            FROM articles
            WHERE article = ?
        """, (article,)).fetchone()

        libelle = (row_article["libelle"] or "").lower() if row_article else ""

        if "non loti" in libelle:
            lot = ""
        else:
            lot = f"RP{annee[-2:]}{mois_num}{article}"

        ligne = [
            "01",                                                   # 1 Société
            "38",                                                   # 2 BA
            "01",                                                   # 3 Société
            "38",                                                   # 4 BA
            "NORM",                                                 # 5 Type BL
            r["code_vif"],                                          # 6 Association
            f"Ramasse partenaire {mois}",                           # 7 ref cde client
            f"Ramasse partenaire {mois}",                           # 8 commentaire ENTETE
            date_bl,                                                # 9 Date BL
            date_bl,                                                # 10 date livraison
            article,                                                # 11 Article
            lot,                                                    # 12 NO LOT
            f"{float(r['total_kg'] or 0):.3f}".replace(".", ","),   # 13 Qte
            "KG",                                                   # 14 Unité
            "03",                                                   # 15 Dépôt de sortie
            "1",                                                    # 16 Gratuit
            "0"                                                     # 17 Prix
        ]

        lignes_csv.append(";".join(ligne))

    output = "\r\n".join(lignes_csv) + "\r\n"

    # 🔹 trace export
    cur.execute("""
        INSERT INTO exports_vif (mois, type_export, date_export, user)
        VALUES (?, 'bl', datetime('now'), ?)
    """, (mois, current_user.email))

    conn.commit()
    conn.close()

    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=BL_38.CSV"}
    )


@distribution_bp.route("/ramasses/controle", methods=["POST"])
@login_required
@require_access("distribution", "lecture")
def controle_ramasses():

    mois = request.form.get("mois") or session.get("mois_ramasse")

    if not mois:
        flash("❌ Mois non sélectionné", "danger")
        return redirect(url_for("distribution.liste_ramasses"))

    conn = get_db_connection()
    cur = conn.cursor()

    # ==============================
    # 🔵 TOTAL PAR FOURNISSEUR
    # ==============================
    fournisseurs = cur.execute("""
        SELECT
            f.nom AS fournisseur,
            f.code_vif,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
        LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_fournisseur
        ORDER BY f.nom
    """, (mois,)).fetchall()

    # ==============================
    # 🟠 TOTAL PAR ASSOCIATION
    # ==============================
    associations = cur.execute("""
        SELECT
            a.nom_association,
            a.code_vif,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
        LEFT JOIN associations a ON a.id = r.id_association
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_association
        ORDER BY a.nom_association
    """, (mois,)).fetchall()

    # 🔹 calcul totaux
    total_entrees = sum([f["total_kg"] or 0 for f in fournisseurs])
    total_sorties = sum([a["total_kg"] or 0 for a in associations])

    # 🔹 cohérence (tolérance légère)
    coherent = abs(total_entrees - total_sorties) < 0.1

    if coherent:
        flash(
            f"Cohérence OK ({total_entrees:.1f} kg)",
            "success"
        )
    else:
        flash(
            f"Incohérence : Entrées {total_entrees:.1f} / Sorties {total_sorties:.1f}",
            "danger"
        )

    conn.close()

    return render_template(
        "distribution/ramasse_assos/controle_ramasses.html",
        mois=mois,
        fournisseurs=fournisseurs,
        associations=associations,
        total_entrees=total_entrees,
        total_sorties=total_sorties,
        coherent=coherent
    )


@distribution_bp.route("/ramasses/controle")
@login_required
@require_access("distribution", "lecture")
def controle_ramasses_get():

    mois = request.args.get("mois")
    confirm_reception = request.args.get("confirm_reception")
    confirm_commande = request.args.get("confirm_commande")

    if not mois:
        flash("❌ Mois non sélectionné", "danger")
        return redirect(url_for("distribution.liste_ramasses"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ==============================
    # 🔵 FOURNISSEURS
    # ==============================
    fournisseurs = cur.execute("""
        SELECT
            f.nom AS fournisseur,
            f.code_vif,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
        LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_fournisseur
        ORDER BY f.nom
    """, (mois,)).fetchall()

    # ==============================
    # 🟠 ASSOCIATIONS
    # ==============================
    associations = cur.execute("""
        SELECT
            a.nom_association,
            a.code_vif,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
        LEFT JOIN associations a ON a.id = r.id_association
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_association
        ORDER BY a.nom_association
    """, (mois,)).fetchall()

    # ==============================
    # 🔢 TOTAUX
    # ==============================
    total_entrees = sum([f["total_kg"] or 0 for f in fournisseurs])
    total_sorties = sum([a["total_kg"] or 0 for a in associations])

    coherent = abs(total_entrees - total_sorties) < 0.1

    conn.close()

    return render_template(
        "distribution/ramasse_assos/controle_ramasses.html",
        mois=mois,
        fournisseurs=fournisseurs,
        associations=associations,
        total_entrees=total_entrees,
        total_sorties=total_sorties,
        coherent=coherent,
        confirm_reception=confirm_reception,
        confirm_commande=confirm_commande
    )

# ============================================================================
# 📄 mois_ramasse disponible dans tous les templates
# ============================================================================
@distribution_bp.app_context_processor
def inject_mois_ramasse():
    return {
        "mois_ramasse": session.get("mois_ramasse")
    }


@distribution_bp.route("/ramasse/set_mois", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def set_mois_ramasse():

    mois = request.form.get("mois_ramasse")

    # 🔒 Sécurité minimale
    if not mois or len(mois) != 7:
        flash("❌ Mois invalide", "danger")
        return redirect(request.referrer or url_for("distribution.ramasse_types_list"))

    session["mois_ramasse"] = mois

    write_log(f"📅 Mois de travail défini : {mois} | user={current_user.email}")

    flash(f"📅 Mois de travail défini : {mois}", "success")

    return redirect(request.referrer or url_for("distribution.ramasse_types_list"))


@distribution_bp.route("/get_fournisseur_libelle/<code>")
def get_fournisseur_libelle(code):

    conn = get_db_connection()
    cur = conn.cursor()

    row = cur.execute("""
        SELECT nom FROM fournisseurs WHERE code_vif = ?
    """, (code,)).fetchone()

    conn.close()

    return jsonify({"libelle": row["nom"] if row else None})



@distribution_bp.route("/ramasses_bilan")
@login_required
@require_access("distribution", "lecture")
def ramasses_bilan():

    annee = request.args.get("annee")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ===========================
    # Liste années disponibles
    # ===========================
    annees = cur.execute("""
        SELECT DISTINCT strftime('%Y', date_ramasse) AS annee
        FROM ramasses_partenaire
        ORDER BY annee DESC
    """).fetchall()

    if not annee and annees:
        annee = annees[0]["annee"]

    # ===========================
    # Fournisseurs
    # ===========================
    fournisseurs = cur.execute("""
        SELECT
            strftime('%m', r.date_ramasse) AS mois,
            f.nom AS fournisseur,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l
            ON l.id_ramasse = r.id
        JOIN fournisseurs f
            ON f.id = r.id_fournisseur
        WHERE strftime('%Y', r.date_ramasse) = ?
        GROUP BY mois, f.id
        ORDER BY mois DESC, fournisseur
    """, (annee,)).fetchall()

    # ===========================
    # Associations
    # ===========================
    associations = cur.execute("""
        SELECT
            strftime('%m', r.date_ramasse) AS mois,
            a.nom_association,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l
            ON l.id_ramasse = r.id
        JOIN associations a
            ON a.id = r.id_association
        WHERE strftime('%Y', r.date_ramasse) = ?
        GROUP BY mois, a.id
        ORDER BY mois DESC, nom_association
    """, (annee,)).fetchall()

    # ===========================
    # Totaux mensuels
    # ===========================
    totaux_mensuels = cur.execute("""
        SELECT
            strftime('%m', r.date_ramasse) AS mois,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l
            ON l.id_ramasse = r.id
        WHERE strftime('%Y', r.date_ramasse) = ?
        GROUP BY mois
        ORDER BY mois DESC
    """, (annee,)).fetchall()

    total_annuel = sum(
        float(r["total_kg"] or 0)
        for r in totaux_mensuels
    )

    conn.close()

    return render_template(
        "distribution/ramasse_assos/ramasses_bilan.html",
        annee=annee,
        annees=annees,
        fournisseurs=fournisseurs,
        associations=associations,
        totaux_mensuels=totaux_mensuels,
        total_annuel=total_annuel
    )
