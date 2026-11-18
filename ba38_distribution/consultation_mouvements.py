# ba38_distribution/consultation_mouvements.py
"""Consultation des mouvements de stock (journal mouvements_stock) : par article, lot, fournisseur ou partenaire.

Le journal est alimenté par la validation des ramasses (entrées RA) et des bons de livraison (sorties BL),
voir journal_stock.py. Les mouvements annulés (document rouvert) sont masqués sauf demande."""

from datetime import date, datetime, timedelta

from flask import render_template, request
from flask_login import login_required

from ba38_utilitaires.core import get_db_connection, require_access

from . import distribution_bp
from .bons_livraison import heure_locale
from .journal_stock import assurer_table
from .stock_basilic import _paris_vers_utc, assurer_table_ajouts_lots

LIMITE = 3000


def _iso(brut, defaut=None):
    try:
        return datetime.strptime((brut or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return defaut


@distribution_bp.route("/consultation_mouvements")
@login_required
@require_access("distribution", "lecture")
def consultation_mouvements():
    du = _iso(request.args.get("du"), date.today() - timedelta(days=30))
    au = _iso(request.args.get("au"), date.today())
    article = (request.args.get("article") or "").strip()
    lot = (request.args.get("lot") or "").strip()
    tiers = (request.args.get("tiers") or "").strip()          # « fournisseur:12 » ou « association:7 »
    type_mvt = request.args.get("type") if request.args.get("type") in ("RA", "BL", "TR") else ""
    avec_annules = bool(request.args.get("annules"))

    conn = get_db_connection()
    try:
        assurer_table(conn)
        assurer_table_ajouts_lots(conn)
        # bornes en heure de Paris converties en UTC (date_mvt est un CURRENT_TIMESTAMP SQLite)
        debut = _paris_vers_utc(f"{du.isoformat()} 00:00:00")
        fin = _paris_vers_utc(f"{(au + timedelta(days=1)).isoformat()} 00:00:00")
        where, params = ["m.date_mvt >= ? AND m.date_mvt < ?"], [debut, fin]
        if article:
            where.append("(m.article LIKE ? OR m.libelle LIKE ?)")
            params += [f"%{article}%"] * 2
        if lot:
            where.append("m.lot LIKE ?")
            params.append(f"%{lot}%")
        if ":" in tiers:
            t_type, _, t_id = tiers.partition(":")
            if t_type in ("fournisseur", "association") and t_id.isdigit():
                where.append("m.tiers_type = ? AND m.tiers_id = ?")
                params += [t_type, int(t_id)]
        if type_mvt:
            where.append("m.type_mvt = ?")
            params.append(type_mvt)
        if not avec_annules:
            where.append("m.annule = 0")
        mouvements = [dict(r) for r in conn.execute(
            f"SELECT m.*, COALESCE(r.date_ramasse, f.date_distribution) AS date_doc FROM mouvements_stock m "
            f"LEFT JOIN ramasses_jour r ON m.doc_type = 'ramasse_jour' AND r.id = m.doc_id "
            f"LEFT JOIN frais_ajouts_lots f ON m.doc_type = 'transfert_frais' AND f.id = m.doc_id WHERE {' AND '.join(where)} "
            f"ORDER BY m.date_mvt DESC, m.id DESC LIMIT {LIMITE + 1}", params)]
        tronque = len(mouvements) > LIMITE
        mouvements = mouvements[:LIMITE]
        liste_tiers = [dict(r) for r in conn.execute("""
            SELECT tiers_type, tiers_id, tiers_nom FROM mouvements_stock WHERE tiers_id IS NOT NULL
            GROUP BY tiers_type, tiers_id ORDER BY tiers_type, tiers_nom COLLATE NOCASE""")]
    finally:
        conn.close()

    for m in mouvements:
        loc = heure_locale(m["date_mvt"])
        m["date_fr"] = f"{loc[8:10]}/{loc[5:7]}/{loc[:4]} {loc[11:16]}" if loc else ""
    # totaux par unité (entrées / sorties actives affichées)
    totaux = {}
    for m in mouvements:
        if m["annule"]:
            continue
        t = totaux.setdefault(m["unite"], {"E": 0.0, "S": 0.0})
        t[m["sens"]] += m["quantite"]
    return render_template(
        "distribution/consultation_mouvements.html", mouvements=mouvements, totaux=totaux, tronque=tronque, limite=LIMITE,
        liste_tiers=liste_tiers,
        form={"du": du.isoformat(), "au": au.isoformat(), "article": article, "lot": lot, "tiers": tiers,
              "type": type_mvt, "annules": avec_annules})
