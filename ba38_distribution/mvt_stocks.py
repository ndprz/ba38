# ba38_distribution/mvt_stocks.py

from flask import render_template, send_file, request, jsonify, redirect, url_for, flash
from flask import Response
from flask_login import login_required
from utils import get_db_path, get_db_connection, require_access
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime, timedelta

from . import distribution_bp

import sqlite3



@distribution_bp.route("/export-stocks-excel")
@login_required
@require_access("distribution", "lecture")
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



@distribution_bp.route("/visualisation_stock")
@login_required
@require_access("distribution", "lecture")
def visualisation_stock():


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
        "distribution/mvt_stocks/distribution_visualisation_stock.html",
        articles=articles,
        date_stock=date_stock
    )


@distribution_bp.route("/export_visualisation_stock_excel")
@login_required
@require_access("distribution", "lecture")
def export_visualisation_stock_excel():

    import io
    import pandas as pd
    from flask import send_file

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



@distribution_bp.route('/mouvements-stocks-v2')
@login_required
def mouvements_stocks_v2():

    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT
                s.article,
                a.libelle,
                a.sous_famille,
                a.famille,

                s.depot,
                s.emplacement,
                s.dlc,
                s.ddm,
                s.kg_net,
                s.lot,
                s.dlc,
                s.ddm,

                a.cdt_unite3,
                a.cdt_unite4

            FROM stocks s
            LEFT JOIN articles a ON a.article = s.article

            ORDER BY
                a.famille,
                s.article,
                CAST(s.depot AS INTEGER)
        """)

        articles = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT date_import FROM stock_meta WHERE id = 1")
        row = cur.fetchone()
        date_stock = row[0] if row else None

        # =====================================================
        # ALERTE STOCK ANCIEN
        # =====================================================

        stock_warning = False
        last_num_mvt = None

        if date_stock:

            try:

                date_stock_dt = datetime.strptime(
                    date_stock,
                    "%d/%m/%Y"
                ).date()

                hier = (datetime.now() - timedelta(days=1)).date()

                # 🔥 alerte si pas la veille
                if date_stock_dt != hier:
                    stock_warning = True

            except Exception:
                stock_warning = True

            # =====================================================
            # DERNIER BROUILLON UTILISATEUR
            # (uniquement si daté d'aujourd'hui : un nouveau jour
            # doit repartir sur un num_mvt neuf, pas réutiliser
            # indéfiniment le dernier mouvement jamais enregistré)
            # =====================================================

            cur.execute("""
                SELECT num_mvt, date_mvt
                FROM mvt_stocks_v2
                ORDER BY id DESC
                LIMIT 1
            """)

            row = cur.fetchone()

            aujourdhui = datetime.now().strftime("%Y-%m-%d")

            if row and row["date_mvt"] and row["date_mvt"].startswith(aujourdhui):
                last_num_mvt = row["num_mvt"]
            else:
                last_num_mvt = None

    return render_template(
        "distribution/mvt_stocks/mvt_stocks_v2.html",
        articles=articles,
        date_stock=date_stock,
        stock_warning=stock_warning,
        last_num_mvt=last_num_mvt
    )


@distribution_bp.route("/get_mvt_brouillon")
@login_required
@require_access("distribution", "lecture")
def get_mvt_brouillon():

    num_mvt = request.args.get("num_mvt")

    if not num_mvt:
        return jsonify([])

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            SELECT
                article,
                lot,
                depot_from,
                depot_to,
                qte_kgn,
                qte_col,
                qte_pal,
                statut
            FROM mvt_stocks_v2
            WHERE num_mvt = ?
            ORDER BY id
        """, (num_mvt,))

        rows = [dict(r) for r in cur.fetchall()]

    return jsonify(rows)

@distribution_bp.route("/save-mvt-v2", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def save_mvt_v2():

    data = request.get_json()
    mouvements = data.get("mouvements", [])

    num_mvt = data.get("num_mvt")

    if not num_mvt:
        num_mvt = datetime.now().strftime("MVT%Y%m%d%H%M%S")

    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        cur.execute("""
            DELETE FROM mvt_stocks_v2
            WHERE num_mvt = ?
            AND statut = 'BROUILLON'
        """, (num_mvt,))

        for m in mouvements:
            cur.execute("""
            INSERT INTO mvt_stocks_v2 (
                num_mvt,
                article,
                lot,
                depot_from,
                depot_to,
                qte_kgn,
                qte_col,
                qte_pal,
                date_mvt,
                statut
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                num_mvt,
                m.get("article"),
                m.get("lot"),
                m.get("from"),
                m.get("to"),
                m.get("kgn", 0),
                m.get("col", 0),
                m.get("pal", 0),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "BROUILLON"
            ))

        conn.commit()

    return jsonify({
        "success": True,
        "num_mvt": num_mvt
    })

@distribution_bp.route("/confirm_export_mvt_v2/<num_mvt>")
@login_required
@require_access("distribution", "ecriture")
def confirm_export_mvt_v2(num_mvt):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ============================================
        # BROUILLON
        # ============================================

        cur.execute("""
            SELECT COUNT(*) as nb
            FROM mvt_stocks_v2
            WHERE num_mvt = ?
            AND statut = 'BROUILLON'
        """, (num_mvt,))

        nb_brouillon = cur.fetchone()["nb"]

        # ============================================
        # EXPORTE
        # ============================================

        cur.execute("""
            SELECT COUNT(*) as nb
            FROM mvt_stocks_v2
            WHERE num_mvt = ?
            AND statut = 'EXPORTE'
        """, (num_mvt,))

        nb_exporte = cur.fetchone()["nb"]

        # ============================================
        # TOTAL
        # ============================================

        cur.execute("""
            SELECT COUNT(*) as nb
            FROM mvt_stocks_v2
            WHERE num_mvt = ?
        """, (num_mvt,))

        nb_total = cur.fetchone()["nb"]

        # ============================================
        # DETAIL MOUVEMENTS
        # ============================================

        cur.execute("""
            SELECT
                m.id,
                m.statut,
                m.article,
                m.lot,
                m.depot_from,
                m.depot_to,
                m.qte_kgn,
                m.qte_col,
                m.qte_pal,
                m.date_mvt,
                s.emplacement
            FROM mvt_stocks_v2 m
            LEFT JOIN stocks s
                ON s.article = m.article
                AND COALESCE(s.lot, '') = COALESCE(m.lot, '')
                AND s.depot = m.depot_from
            WHERE m.num_mvt = ?
            ORDER BY m.id
        """, (num_mvt,))

        mouvements = [dict(r) for r in cur.fetchall()]

    return render_template(
        "distribution/mvt_stocks/confirm_export_mvt_v2.html",
        num_mvt=num_mvt,
        nb_brouillon=nb_brouillon,
        nb_exporte=nb_exporte,
        nb_total=nb_total,
        mouvements=mouvements
    )


@distribution_bp.route("/export_mvt_vif_v2/<num_mvt>")
@login_required
@require_access("distribution", "ecriture")
def export_mvt_vif_v2(num_mvt):



    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        mode = request.args.get("mode", "new")

        ids = []

        # =====================================================
        # LECTURE MOUVEMENTS
        # =====================================================

        if mode == "selected":

            ids = request.args.getlist("ids", type=int)

            if not ids:
                flash("⚠️ Aucune ligne sélectionnée.", "warning")
                return redirect(url_for(
                    "distribution.confirm_export_mvt_v2",
                    num_mvt=num_mvt
                ))

            placeholders = ",".join("?" * len(ids))

            cur.execute(f"""
                SELECT *
                FROM mvt_stocks_v2
                WHERE num_mvt = ?
                AND id IN ({placeholders})
                ORDER BY article, lot
            """, (num_mvt, *ids))

        elif mode == "all":

            cur.execute("""
                SELECT *
                FROM mvt_stocks_v2
                WHERE num_mvt = ?
                ORDER BY article, lot
            """, (num_mvt,))

        else:

            cur.execute("""
                SELECT *
                FROM mvt_stocks_v2
                WHERE num_mvt = ?
                AND statut = 'BROUILLON'
                ORDER BY article, lot
            """, (num_mvt,))

        lignes = cur.fetchall()

        if not lignes:
            flash("ℹ️ Aucun mouvement à exporter.", "info")
            return redirect(url_for(
                "distribution.confirm_export_mvt_v2",
                num_mvt=num_mvt
            ))

        # =====================================================
        # EMPLACEMENTS REELS (table stocks)
        # =====================================================

        cur.execute("""
            SELECT article, lot, depot, emplacement
            FROM stocks
        """)

        emplacements_stock = {
            (
                str(r["article"]),
                r["lot"] or "",
                str(r["depot"]).zfill(2)
            ): (r["emplacement"] or "")
            for r in cur.fetchall()
        }

        # =====================================================
        # ENTETE CSV VIF
        # =====================================================

        header = [

            "Ste",
            "Etab",
            "Prechro",
            "Chrono",
            "ligne",

            "Nature mouvement de stock",

            "Date",
            "Heure",
            "Libellé du mouvement",

            "Motif",
            "Nature Tiers",
            "Tiers",
            "Transporteur",

            "Article entrant",
            "Lot entrant",
            "Dépôt",
            "Emplacement entrant",

            "Quantité 1 entrant",
            "Unité 1 entrant",

            "Qte2",
            "UN2",

            "Qte3",
            "UN3",

            "Article sortant",
            "Lot sortant",
            "Dépôt sortant",
            "Emplacement sortant",

            "Quantité 1 sortant",
            "Unité 1 sortant",

            "Quantité 2 sortant",
            "Unité 2 sortant",

            "Quantité 3 sortant",
            "Unité 3 sortant",

            "Utilisateur",

            "Evolutions futures",
            "Evolutions futures",
            "Evolutions futures",
            "Evolutions futures",
            "Evolutions futures",
            "Evolutions futures",
            "Evolutions futures",

            "Critère 1 du lot entrant",
            "Valeur du critère 1",

            "Critère 2 du lot entrant",
            "Valeur du critère 2",

            "Critère 3 du lot entrant",
            "Valeur du critère 3"
        ]

        # header conservé ci-dessus comme documentation des colonnes,
        # mais pas inclus dans le CSV : VIF n'attend pas de ligne d'entête
        rows = []

        # =====================================================
        # DATE
        # =====================================================

        now = datetime.now()

        date_vif = now.strftime("%d/%m/%Y")

        # =====================================================
        # LIGNES CSV
        # =====================================================

        for i, l in enumerate(lignes, start=1):

            qte = float(l["qte_kgn"] or 0)

            depot_to = str(l["depot_to"]).zfill(2)
            depot_from = str(l["depot_from"]).zfill(2)

            article = str(l["article"])

            lot = l["lot"] or ""

            # =================================================
            # EMPLACEMENTS
            # =================================================

            emplacement_entree = emplacements_stock.get(
                (article, lot, depot_to), ""
            )

            emplacement_sortie = emplacements_stock.get(
                (article, lot, depot_from), ""
            )

            # =================================================
            # LIGNE
            # =================================================

            rows.append([

                # =============================================
                # ENTETE
                # =============================================

                "01",
                "38",
                "1OD",
                "0",
                str(i - 1),

                "TRANSCO",

                date_vif,
                "",

                "Transfert",

                "",
                "",
                "",
                "",

                # =============================================
                # ENTREE
                # =============================================

                article,
                lot,

                depot_to,

                emplacement_entree,

                str(qte).replace(".", ","),

                "KG",

                "",
                "",

                "",
                "",

                # =============================================
                # SORTIE
                # =============================================

                article,
                lot,

                depot_from,

                emplacement_sortie,

                str(qte).replace(".", ","),

                "KG",

                "",
                "",

                "",
                "",

                # =============================================
                # UTILISATEUR
                # (champ conservé pour le format VIF, valeur vidée)
                # =============================================

                "",

                # =============================================
                # EVOLUTIONS FUTURES
                # =============================================

                "",
                "",
                "",
                "",
                "",
                "",
                "",

                # =============================================
                # CRITERES LOT
                # =============================================

                "",
                "",

                "",
                "",

                "",
                ""

            ])

        # =====================================================
        # MARQUAGE EXPORTE
        # =====================================================

        if mode == "selected":

            cur.execute(f"""
                UPDATE mvt_stocks_v2
                SET statut = 'EXPORTE'
                WHERE num_mvt = ?
                AND statut = 'BROUILLON'
                AND id IN ({placeholders})
            """, (num_mvt, *ids))

        elif mode != "all":

            cur.execute("""
                UPDATE mvt_stocks_v2
                SET statut = 'EXPORTE'
                WHERE num_mvt = ?
                AND statut = 'BROUILLON'
            """, (num_mvt,))

        conn.commit()

    # =========================================================
    # GENERATION CSV
    # =========================================================

    output = "\n".join([
        ";".join(map(str, r))
        for r in rows
    ])

    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition":
            f"attachment; filename=mvt_{num_mvt}.csv"
        }
    )


@distribution_bp.route("/delete_mvt_v2_line/<int:id>", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def delete_mvt_v2_line(id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # récupérer num_mvt avant suppression
        cur.execute("""
            SELECT num_mvt
            FROM mvt_stocks_v2
            WHERE id = ?
        """, (id,))

        row = cur.fetchone()

        if not row:
            flash("❌ Ligne introuvable", "danger")
            return redirect(
                url_for("distribution.mouvements_stocks_v2")
            )

        num_mvt = row["num_mvt"]

        # suppression
        cur.execute("""
            DELETE FROM mvt_stocks_v2
            WHERE id = ?
            AND statut = 'BROUILLON'
        """, (id,))

        conn.commit()

    flash("🗑️ Mouvement supprimé", "success")

    return redirect(
        url_for(
            "distribution.confirm_export_mvt_v2",
            num_mvt=num_mvt
        )
    )
