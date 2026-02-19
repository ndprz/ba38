# ba38_distribution.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, has_access
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime

import sqlite3

distribution_bp = Blueprint("distribution", __name__)

@distribution_bp.route("/distribution_main")
@login_required
def distribution_main():

    if not has_access("distribution", "lecture"):
        flash("⛔ Accès non autorisé au module Distribution.", "danger")
        return redirect(url_for("index"))

    return render_template("distribution_main.html")

@distribution_bp.route("/mouvements-stocks-depot")
@login_required
def mouvements_stocks_depot():

    db_path = get_db_path()
    today = datetime.now().strftime("%Y-%m-%d")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT
                s.article,
                a.libelle,
                a.famille,
                a.sous_famille,

                a.unite1_principale,
                a.unite2,
                a.cdt_unite2,
                a.unite3,
                a.cdt_unite3,
                a.unite4,
                a.cdt_unite4,
                a.coef_kgn_vers_kg,

                s.lot,
                s.dlc,
                s.ddm,
                s.depot,
                s.emplacement,

                s.kg_net,
                s.kg_brut,
                s.col,
                s.pal,

                COALESCE(SUM(
                    CASE
                        WHEN m.depot_depart = s.depot THEN -m.qte_kgn
                        WHEN m.depot_arrivee = s.depot THEN m.qte_kgn
                        ELSE 0
                    END
                ), 0) AS delta_jour

            FROM stocks s

            LEFT JOIN articles a
                ON s.article = a.article

            LEFT JOIN mvtstocks m
                ON m.article = s.article
                AND IFNULL(m.lot,'') = IFNULL(s.lot,'')
                AND m.date_mvt = ?

            GROUP BY
                s.article, s.lot, s.depot

            ORDER BY
                a.famille,
                a.sous_famille,
                a.libelle
        """, (today,))

        rows = cur.fetchall()


        cur.execute("SELECT date_import FROM stock_meta WHERE id = 1")
        row = cur.fetchone()
        date_stock = row[0] if row else None



    return render_template(
        "distribution_mouvements_stocks.html",
        articles=rows,
        date_stock=date_stock
    )





@distribution_bp.route("/export-stocks-excel")
@login_required
def export_stocks_excel():
    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT
                s.article,
                a.libelle,
                a.famille,
                a.sous_famille,
                s.lot,
                s.dlc,
                s.ddm,
                s.depot,
                s.emplacement,
                s.kg_net,
                s.kg_brut,
                s.col,
                s.pal
            FROM stocks s
            LEFT JOIN articles a
                ON s.article = a.article
            ORDER BY a.famille, a.sous_famille, a.libelle
        """)

        rows = cur.fetchall()

    # Création Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Stocks"

    headers = [
        "Article", "Libellé", "Famille", "Sous-famille",
        "Lot", "DLC", "DDM",
        "Dépôt", "Emplacement",
        "Kg net", "Kg brut", "COL", "PAL"
    ]

    ws.append(headers)

    for r in rows:
        ws.append([
            r["article"],
            r["libelle"],
            r["famille"],
            r["sous_famille"],
            r["lot"],
            r["dlc"],
            r["ddm"],
            r["depot"],
            r["emplacement"],
            r["kg_net"],
            r["kg_brut"],
            r["col"],
            r["pal"],
        ])

    # Ajustement largeur colonnes automatique simple
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[column].width = max_length + 2

    # Envoi fichier
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        download_name="stocks_depot.xlsx",
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

#   SAISIE DE MOUVEMENTS DE STOCKS DEPOT 

def generate_num_mvt():
    today = datetime.now().strftime("%Y%m%d")

    with sqlite3.connect(get_db_path()) as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(*) 
            FROM mvtstocks 
            WHERE num_mvt LIKE ?
        """, (f"MVT-{today}-%",))

        count = cur.fetchone()[0] + 1

    return f"MVT-{today}-{str(count).zfill(3)}"

@distribution_bp.route("/save_mvt", methods=["POST"])
@login_required
def save_mvt():

    if not has_access("distribution", "lecture"):
        flash("⛔ Accès non autorisé.", "danger")
        return redirect(url_for("index"))

    db_path = get_db_path()
    now = datetime.now()

    date_mvt = now.strftime("%Y-%m-%d")
    heure_mvt = now.strftime("%H:%M:%S")

    # Génération num_mvt
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(*) 
            FROM mvtstocks
            WHERE date_mvt = ?
        """, (date_mvt,))

        count = cur.fetchone()[0] + 1
        num_mvt = f"MVT-{date_mvt.replace('-','')}-{str(count).zfill(3)}"

    lignes = request.json.get("lignes", [])

    if not lignes:
        return jsonify({"error": "Aucune ligne transmise"}), 400

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        for ligne in lignes:

            article = ligne["article"]
            lot = ligne.get("lot", "")
            depot_depart = ligne["depot_depart"]
            depot_arrivee = ligne["depot_arrivee"]

            qte_kgn = float(ligne.get("qte_kgn", 0) or 0)
            qte_pal = float(ligne.get("qte_pal", 0) or 0)
            qte_col = float(ligne.get("qte_col", 0) or 0)

            # 🔎 Vérification stock corrigé
            cur.execute("""
                SELECT
                    s.kg_net,
                    COALESCE(SUM(
                        CASE
                            WHEN m.depot_depart = s.depot THEN -m.qte_kgn
                            WHEN m.depot_arrivee = s.depot THEN m.qte_kgn
                            ELSE 0
                        END
                    ), 0) AS delta_jour

                FROM stocks s

                LEFT JOIN mvtstocks m
                    ON m.article = s.article
                    AND IFNULL(m.lot,'') = IFNULL(s.lot,'')
                    AND m.date_mvt = ?

                WHERE s.article = ?
                AND IFNULL(s.lot,'') = IFNULL(?,'')
                AND s.depot = ?

                GROUP BY s.article, s.lot, s.depot
            """, (date_mvt, article, lot, depot_depart))

            row = cur.fetchone()

            if not row:
                return jsonify({"error": f"Ligne stock introuvable {article}"}), 400

            stock_corrige = (row["kg_net"] or 0) + (row["delta_jour"] or 0)

            if qte_kgn > stock_corrige:
                return jsonify({
                    "error": f"Stock insuffisant pour {article} lot {lot}"
                }), 400

            # 🟢 Insertion mouvement
            cur.execute("""
                INSERT INTO mvtstocks (
                    num_mvt,
                    date_mvt,
                    heure_mvt,
                    user_id,
                    article,
                    lot,
                    depot_depart,
                    depot_arrivee,
                    qte_pal,
                    qte_col,
                    qte_kgn
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                num_mvt,
                date_mvt,
                heure_mvt,
                current_user.id,
                article,
                lot,
                depot_depart,
                depot_arrivee,
                qte_pal,
                qte_col,
                qte_kgn
            ))

        conn.commit()

    return jsonify({"success": True, "num_mvt": num_mvt})


@distribution_bp.route("/save_mvt_brouillon", methods=["POST"])
@login_required
def save_mvt_brouillon():

    if not has_access("distribution", "lecture"):
        flash("⛔ Accès non autorisé.", "danger")
        return redirect(url_for("index"))

    lignes = request.json.get("lignes", [])

    if not lignes:
        return jsonify(success=False, error="Aucune ligne reçue")

    db_path = get_db_path()
    now = datetime.now()

    date_mvt = now.strftime("%Y-%m-%d")
    heure_mvt = now.strftime("%H:%M:%S")
    num_mvt = generate_num_mvt()

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        for ligne in lignes:
            cur.execute("""
                INSERT INTO mvtstocks (
                    num_mvt,
                    date_mvt,
                    heure_mvt,
                    user_id,
                    article,
                    lot,
                    depot_depart,
                    depot_arrivee,
                    qte_pal,
                    qte_col,
                    qte_kgn,
                    statut
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                num_mvt,
                date_mvt,
                heure_mvt,
                current_user.id,
                ligne["article"],
                ligne["lot"],
                ligne["depot_depart"],
                ligne["depot_arrivee"],
                ligne["qte_pal"],
                ligne["qte_col"],
                ligne["qte_kgn"],
                "BROUILLON"
            ))

        conn.commit()

    return jsonify(success=True, num_mvt=num_mvt)



@distribution_bp.route("/valider_mvt", methods=["POST"])
@login_required
def valider_mvt():

    if not has_access("distribution", "lecture"):
        flash("⛔ Accès non autorisé.", "danger")
        return redirect(url_for("index"))

    data = request.get_json()
    num_mvt = data.get("num_mvt")

    if not num_mvt:
        return jsonify(success=False, error="num_mvt manquant")

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        cur.execute("""
            UPDATE mvtstocks
            SET statut = 'VALIDE'
            WHERE num_mvt = ?
        """, (num_mvt,))

        if cur.rowcount == 0:
            return jsonify(success=False, error="Mouvement introuvable")

        conn.commit()

    return jsonify(success=True, num_mvt=num_mvt)



@distribution_bp.route("/mvt/<num_mvt>")
@login_required
def afficher_mvt(num_mvt):

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM mvtstocks
        WHERE num_mvt = ?
        ORDER BY article, lot
    """, (num_mvt,))

    lignes = cur.fetchall()

    if not lignes:
        return "Mouvement introuvable", 404

    return render_template(
        "distribution_mvt_bordereau.html",
        lignes=lignes,
        num_mvt=num_mvt
    )


@distribution_bp.route("/visualisation_stock")
@login_required
def visualisation_stock():
        
    if not has_access("distribution", "lecture"):
        flash("⛔ Accès non autorisé.", "danger")
        return redirect(url_for("index"))


    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT
                s.article,
                a.libelle,
                a.famille,
                a.sous_famille,

                s.depot,
                s.emplacement,
                s.lot,
                s.dlc,
                s.ddm,

                s.kg_net,
                s.kg_brut,
                s.col,
                s.pal,

                a.cdt_unite2,
                a.cdt_unite3,
                a.cdt_unite4

            FROM stocks s
            LEFT JOIN articles a ON a.article = s.article
            ORDER BY a.famille, a.sous_famille, s.article
        """)

        articles = cur.fetchall()


        cur.execute("SELECT date_import FROM stock_meta WHERE id = 1")
        row = cur.fetchone()
        date_stock = row[0] if row else None


    return render_template(
        "distribution_visualisation_stock.html",
        articles=articles,
        date_stock=date_stock
    )


@distribution_bp.route("/export_visualisation_stock_excel")
@login_required
def export_visualisation_stock_excel():

    if not has_access("distribution", "lecture"):
        flash("⛔ Accès non autorisé.", "danger")
        return redirect(url_for("index"))

    import io
    import pandas as pd
    from flask import send_file
    from datetime import datetime

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT 
            s.article,
            a.libelle,
            a.sous_famille,
            s.depot,
            s.emplacement,
            s.kg_net,
            s.kg_brut,
            s.col,
            s.pal,
            s.lot,
            s.dlc,
            s.ddm,
            a.unite1_principale,
            a.cdt_unite2,
            a.cdt_unite3,
            a.cdt_unite4,
            a.coef_kgn_vers_kg
        FROM stocks s
        LEFT JOIN articles a ON a.article = s.article
        ORDER BY a.famille, s.article
    """)

    rows = cur.fetchall()

    cur.execute("SELECT date_import FROM stock_meta WHERE id = 1")
    meta = cur.fetchone()
    date_stock = meta[0] if meta else ""

    conn.close()

    df = pd.DataFrame(rows, columns=[
        "Article",
        "Libellé",
        "Sous-famille",
        "Dépôt",
        "Emplacement",
        "Kgn ERP",
        "Kg brut",
        "COL",
        "PAL",
        "Lot",
        "DLC",
        "DDM",
        "Unité",
        "P (Kgn)",
        "COL (Kgn)",
        "PAL (Kgn)",
        "Coef brut"
    ])

    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    filename = f"visualisation_stock_{date_stock}.xlsx"

    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
