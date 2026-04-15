# ba38_distribution/ramasse_assos.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash
from flask import Response
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, require_access
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

@distribution_bp.route("/ramasse-types")
@login_required
@require_access("distribution", "ecriture")
def ramasse_types_list():

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

@distribution_bp.route("/ramasse-types/create", methods=["GET", "POST"])
@distribution_bp.route("/ramasse-types/<int:id>/edit", methods=["GET", "POST"])
@login_required
@require_access("distribution", "ecriture")
def ramasse_type_form(id=None):

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
            SELECT id, nom
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

@distribution_bp.route("/ramasse-types/<int:id>/delete")
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
@distribution_bp.route("/ramasse/saisie/<int:id_modele>", methods=["GET", "POST"])
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
    # 🔹 POST
    # =====================================================
    if request.method == "POST":

        date_ramasse = request.form.get("date_saisie")
        user_id = session.get("user_id")

        # INSERT HEADER
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

        # INSERT LIGNES
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

    today = datetime.now().strftime("%Y-%m-%d")

    return render_template(
        "distribution/ramasse_assos/ramasse_saisie.html",
        entete=entete,
        lignes=lignes,
        today=today
    )



# ============================================================
#  HISTORIQUE
# ============================================================
@distribution_bp.route("/ramasses")
@login_required
@require_access("distribution", "lecture")
def liste_ramasses():

    conn = get_db_connection()
    cur = conn.cursor()

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
        ramasses=rows
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



@distribution_bp.route("/ramasse-types/<int:id>/duplicate", methods=["GET", "POST"])
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
            SELECT id, nom
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

    mois = request.form.get("mois")  # format YYYY-MM
    force = request.form.get("force")  # pour forcer même si export déjà fait
    
    date_du_jour_plus_2 = (datetime.now() + timedelta(days=2)).strftime("%d/%m/%Y")

    if not mois:
        flash("❌ Mois non sélectionné", "danger")
        return redirect(url_for("distribution.liste_ramasses"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 🔴 Vérification export déjà fait
    deja = cur.execute("""
        SELECT 1 FROM exports_vif
        WHERE mois = ? AND type_export = 'reception'
    """, (mois,)).fetchone()

    if deja and force != "1":
        flash("⚠️ Export réception déjà effectué pour ce mois", "warning")
        conn.close()
        return redirect(url_for("distribution.controle_ramasses_get", mois=mois, confirm_reception=1))


    rows = cur.execute("""
        SELECT
            r.id_fournisseur,
            f.code_vif,
            f.nom,
            l.code_vif AS article_code_vif,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
        LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_fournisseur, l.code_vif
        ORDER BY f.nom, l.code_vif
    """, (mois,)).fetchall()

    # 🔴 CONTROLE BLOQUANT
    erreurs = []

    for r in rows:
        if not r["code_vif"]:
            erreurs.append(f"Fournisseur sans code VIF : {r['nom']}")

    if erreurs:
        for e in erreurs:
            flash(f"❌ {e}", "danger")
        conn.close()
        return redirect(url_for("distribution.liste_ramasses"))

    # 🔹 date = dernier jour du mois
    annee, m = mois.split("-")
    dernier_jour = monthrange(int(annee), int(m))[1]
    date_recep = f"{dernier_jour}/{m}/{annee}"

    lignes_csv = []

    for r in rows:

        article = r["article_code_vif"].strip()

        annee, mois_num = mois.split("-")

        lot = f"RP{annee[-2:]}{mois_num}{article}"

        ligne = [
            "01",                       # 1STE
            "38",                       # 2ETAB
            date_recep,                 #3 DATE RECEP
            r["code_vif"],              #4 CODE VIF FOURNISSEUR
            "01",                       #5 LIEU
            "03",                       #6 DEPOT
            r["article_code_vif"],      #7 ARTICLE
            int(r["total_kg"]),         #8 QUANTITE
            "KG",                       #9 UNITE    
            lot,                        #10 LOT
            date_du_jour_plus_2,        #11 DLUO
            date_du_jour_plus_2,        #12 DLC
            "",                         #13 Lartlibel artic
            "RA"                        #14 ORIGINE
                      
        ]

        lignes_csv.append(";".join(map(str, ligne)))

    output = "\n".join(lignes_csv)

    # 🔹 Enregistrement export
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
            "Content-Disposition": f"attachment; filename=recepba38.csv"
        }
    )




@distribution_bp.route("/ramasses/export_excel_mensuel", methods=["POST"])
@login_required
@require_access("distribution", "lecture")
def export_ramasses_excel_mensuel():

    mois = request.form.get("mois")  # format YYYY-MM

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

    mois = request.form.get("mois")
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
        ORDER BY a.nom_association, l.code_vif
    """, (mois,)).fetchall()

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

    # 🔹 date BL = aujourd’hui
    date_bl = datetime.now().strftime("%d/%m/%Y")

    lignes_csv = []

    # # 🔹 entête
    # lignes_csv.append("Societe;BA;Type BL;Association;BL orig;Date BL;Article;NO LOT;Qte;Unite;Depot sorite;Commentaire")  

    # 🔹 génération lignes
    for r in rows:

        article = r["article"].strip()
        annee, mois_num = mois.split("-")

        # 🔹 LOT (13 caractères OK)
        lot = f"RP{annee[-2:]}{mois_num}{article}"

        ligne = [
            "01",                                               # 1 Société
            "38",                                               # 2 BA
            "NORM",                                             # 3 Type BL
            r["code_vif"],                                      # 4 Association
            date_bl,                                            # 5 Date BL
            article,                                            # 6 Article
            lot,                                                # 8 NO LOT
            f"{float(r['total_kg']):.3f}".replace(".", ","),    # 9 Qte
            "KG",                                               # 10 Unité
            "06",                                               # 11 Dépôt de sortie
            f"Ramasse Partenaires {annee}"                      # 12 Commentaire

        ]

        lignes_csv.append(";".join(ligne))

    output = "\n".join(lignes_csv)

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
        headers={"Content-Disposition": "attachment; filename=BL38.CSV"}
    )


@distribution_bp.route("/ramasses/controle", methods=["POST"])
@login_required
@require_access("distribution", "lecture")
def controle_ramasses():

    mois = request.form.get("mois")

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


    return render_template(
        "distribution/ramasse_assos/controle_ramasses.html",
        mois=mois,
        fournisseurs=fournisseurs,
        associations=associations,
        total_entrees=total_entrees,
        total_sorties=total_sorties,
        coherent=coherent
    )

    conn.close()

    return render_template(
        "distribution/ramasse_assos/controle_ramasses.html",
        mois=mois,
        fournisseurs=fournisseurs,
        associations=associations
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