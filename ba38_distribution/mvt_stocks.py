# ba38_distribution/mvt_stocks.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, require_access
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime

from . import distribution_bp

import sqlite3


# -------------------------------------------------------------------
#   MOUVEMENTS DE STOCK ENTREPOT
# -------------------------------------------------------------------


@distribution_bp.route("/mouvements-stocks-depot")
@login_required
@require_access("distribution", "lecture")
def mouvements_stocks_depot():
    """
    Vue principale des mouvements de stocks.

    OBJECTIFS :
    - Afficher le stock actuel (table stocks)
    - Intégrer les mouvements (BROUILLON + EXPORTE)
    - Convertir PAL / COL en Kgn
    - Afficher les dépôts créés par mouvement (même si pas en stock initial)
    - Ajouter un flag 'stock_cree' pour le rendu HTML

    IMPORTANT :
    - delta_jour = impact des mouvements
    - stock_corrige = kg_net + delta_jour (calcul fait côté template)
    """

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # =========================================================
        # 1️⃣ STOCK EXISTANT + MOUVEMENTS
        # =========================================================
        # -> base = table stocks
        # -> on applique les mouvements dessus
        # =========================================================

        query = """
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

            0 as stock_cree,

            MAX(m.statut) as statut_mvt,

            COALESCE(SUM(
                CASE
                    WHEN m.depot_depart = s.depot THEN
                        -(
                            COALESCE(m.qte_kgn,0)
                            + COALESCE(m.qte_col,0) * COALESCE(a.cdt_unite3,0)
                            + COALESCE(m.qte_pal,0) * COALESCE(a.cdt_unite4,0)
                        )

                    WHEN m.depot_arrivee = s.depot THEN
                        (
                            COALESCE(m.qte_kgn,0)
                            + COALESCE(m.qte_col,0) * COALESCE(a.cdt_unite3,0)
                            + COALESCE(m.qte_pal,0) * COALESCE(a.cdt_unite4,0)
                        )

                    ELSE 0
                END
            ), 0) AS delta_jour

        FROM stocks s

        LEFT JOIN articles a
            ON s.article = a.article

        LEFT JOIN mvtstocks m
            ON m.article = s.article
            AND IFNULL(m.lot,'') = IFNULL(s.lot,'')
            AND m.statut IN ('BROUILLON', 'EXPORTE')

        GROUP BY
            s.article, s.lot, s.depot
        """

        # =========================================================
        # 2️⃣ DEPOTS CREES PAR MOUVEMENTS (UNION)
        # =========================================================
        # -> cas où le dépôt n’existe pas dans stocks
        # -> on le crée virtuellement
        # =========================================================

        query_union = """
        UNION

        SELECT
            m.article,
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

            m.lot,
            NULL as dlc,
            NULL as ddm,
            m.depot_arrivee as depot,
            '' as emplacement,

            0 as kg_net,
            0 as kg_brut,
            0 as col,
            0 as pal,

            1 as stock_cree,

            m.statut as statut_mvt,

            (
                COALESCE(m.qte_kgn,0)
                + COALESCE(m.qte_col,0) * COALESCE(a.cdt_unite3,0)
                + COALESCE(m.qte_pal,0) * COALESCE(a.cdt_unite4,0)
            ) as delta_jour

        FROM mvtstocks m

        LEFT JOIN stocks s
            ON s.article = m.article
            AND IFNULL(s.lot,'') = IFNULL(m.lot,'')
            AND s.depot = m.depot_arrivee

        LEFT JOIN articles a
            ON a.article = m.article

        WHERE s.article IS NULL
        AND m.statut IN ('BROUILLON','EXPORTE')
        """

        # =========================================================
        # 3️⃣ EXECUTION + TRI GLOBAL
        # =========================================================

        final_query = f"""
        {query}
        {query_union}
        ORDER BY
            famille,
            sous_famille,
            libelle
        """

        cur.execute(final_query)
        rows = cur.fetchall()

        # =========================================================
        # 4️⃣ DATE STOCK
        # =========================================================
        cur.execute("SELECT date_import FROM stock_meta WHERE id = 1")
        row = cur.fetchone()
        date_stock = row[0] if row else None

    return render_template(
        "distribution/mvt_stocks/distribution_mouvements_stocks.html",
        articles=rows,
        date_stock=date_stock
    )

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

                MAX(m.statut) as statut_mvt,

                COALESCE(SUM(
                    CASE
                        WHEN m.depot_depart = s.depot THEN
                            -(
                                COALESCE(m.qte_kgn,0)
                                + COALESCE(m.qte_col,0) * COALESCE(a.cdt_unite3,0)
                                + COALESCE(m.qte_pal,0) * COALESCE(a.cdt_unite4,0)
                            )

                        WHEN m.depot_arrivee = s.depot THEN
                            (
                                COALESCE(m.qte_kgn,0)
                                + COALESCE(m.qte_col,0) * COALESCE(a.cdt_unite3,0)
                                + COALESCE(m.qte_pal,0) * COALESCE(a.cdt_unite4,0)
                            )

                        ELSE 0
                    END
                ), 0) AS delta_jour

            FROM stocks s

            LEFT JOIN articles a
                ON s.article = a.article

            LEFT JOIN mvtstocks m
                ON m.article = s.article
                AND IFNULL(m.lot,'') = IFNULL(s.lot,'')
                AND m.statut IN ('BROUILLON', 'EXPORTE')

            GROUP BY
                s.article, s.lot, s.depot

            ORDER BY
                a.famille,
                a.sous_famille,
                a.libelle
        """)

        rows = cur.fetchall()


        cur.execute("SELECT date_import FROM stock_meta WHERE id = 1")
        row = cur.fetchone()
        date_stock = row[0] if row else None



    return render_template(
        "distribution/mvt_stocks/distribution_mouvements_stocks.html",
        articles=rows,
        date_stock=date_stock
    )





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
@require_access("distribution", "ecriture")
def save_mvt():

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

    lignes = request.json.get("lignes", [])
    if not lignes:
        return jsonify(success=False, error="Aucune ligne")

    db_path = get_db_path()
    now = datetime.now()

    date_mvt = now.strftime("%Y-%m-%d")
    heure_mvt = now.strftime("%H:%M:%S")

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        # 🔥 récupérer brouillon existant
        cur.execute("""
            SELECT DISTINCT num_mvt
            FROM mvtstocks
            WHERE user_id = ?
            AND statut = 'BROUILLON'
        """, (current_user.id,))

        row = cur.fetchone()

        if row:
            num_mvt = row[0]

            # 🔥 on supprime tout (REPLACE logique)
            cur.execute("""
                DELETE FROM mvtstocks
                WHERE num_mvt = ?
            """, (num_mvt,))
        else:
            num_mvt = generate_num_mvt()

        # 🔥 insertion propre
        for ligne in lignes:
            cur.execute("""
                INSERT INTO mvtstocks (
                    num_mvt, date_mvt, heure_mvt,
                    user_id,
                    article, lot,
                    depot_depart, depot_arrivee,
                    qte_pal, qte_col, qte_kgn,
                    statut
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'BROUILLON')
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
                ligne["qte_kgn"]
            ))

        conn.commit()

    return jsonify(success=True, num_mvt=num_mvt)


@distribution_bp.route("/valider_mvt", methods=["POST"])
def valider_mvt():
    num_mvt = request.json.get("num_mvt")

    return jsonify(
        success=True,
        redirect=url_for("distribution.afficher_mvt", num_mvt=num_mvt)
    )


@distribution_bp.route("/mvt/<num_mvt>")
@login_required
@require_access("distribution", "lecture")
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
        "distribution/mvt_stocks/distribution_mvt_bordereau.html",
        lignes=lignes,
        num_mvt=num_mvt
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



@distribution_bp.route("/get_mvt_brouillon")
def get_mvt_brouillon():
    num_mvt = request.args.get("num_mvt")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM mvtstocks
        WHERE num_mvt = ?
        AND statut = 'BROUILLON'
    """, (num_mvt,))

    lignes = [dict(l) for l in cur.fetchall()]

    return jsonify(lignes)


from flask import Response

@distribution_bp.route("/export_mvt_vif/<num_mvt>")
def export_mvt_vif(num_mvt):

    # génération CSV ...

    # 🔥 marquer comme exporté
    cur.execute("""
        UPDATE mvtstocks
        SET statut = 'EXPORTE'
        WHERE num_mvt = ?
    """, (num_mvt,))

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM mvtstocks
        WHERE num_mvt = ?
        AND statut = 'VALIDE'
        ORDER BY article, lot
    """, (num_mvt,))

    lignes = cur.fetchall()

    if not lignes:
        return "Aucun mouvement", 404

    # =========================
    # ENTETE CSV VIF
    # =========================
    header = [
        "Ste","Etab","Prechro","Chrono","ligne",
        "Nature mouvement de stock",
        "Date","Heure","Libellé du mouvement",
        "Motif","Nature Tiers","Tiers","Transporteur",
        "Article entrant","Lot entrant","Dépôt","Emplacement entrant",
        "Quantité 1 entrant","Unité 1 entrant","Qte2","UN2","Qte3","UN3",
        "Article sortant","Lot sortant","Dépôt sortant","Emplacement sortant",
        "Quantité 1 sortant","Unité 1 sortant",
        "Quantité 2 sortant","Unité 2 sortant",
        "Quantité 3 sortant","Unité 3 sortant",
        "Utilisateur"
    ]

    rows = [header]

    today = datetime.now().strftime("%d/%m/%Y")

    for i, l in enumerate(lignes, start=1):

        qte = float(l["qte_kgn"] or 0)

        rows.append([
            "01",                      # Ste
            "38",                      # Etab
            "1OD",                     # Prechro
            "0",                       # Chrono
            str(i),                    # ligne
            "TRANSCO",
            today,
            "",
            f"Transfert {num_mvt}",
            "", "", "", "",

            # ===== ENTREE =====
            l["article"],
            l["lot"] or "",
            l["depot_arrivee"],
            "",
            str(qte).replace(".", ","),
            "KG",
            "", "", "", "",

            # ===== SORTIE =====
            l["article"],
            l["lot"] or "",
            l["depot_depart"],
            "",
            str(qte).replace(".", ","),
            "KG",
            "", "", "", "",

            current_user.email
        ])

    # =========================
    # GENERATION CSV
    # =========================
    output = "\n".join([";".join(map(str, r)) for r in rows])

    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=mvt_{num_mvt}.csv"}
    )
