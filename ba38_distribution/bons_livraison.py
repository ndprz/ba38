# ba38_distribution/bons_livraison.py
"""Consultation des bons de livraison (bons de préparation validés, statut « bl ») :
filtres, recherches (association, code VIF, article, lot, n° de fichier) et réimpression PDF."""

from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

from flask import flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from ba38_utilitaires.core import get_db_connection, require_access

from . import distribution_bp
from .bons_prepa import _parse_date, _qte_bl

PARIS = ZoneInfo("Europe/Paris")
MAX_PDF = 300


def heure_locale(ts: str) -> str:
    """CURRENT_TIMESTAMP SQLite (UTC) -> « AAAA-MM-JJ HH:MM:SS » heure de Paris."""
    try:
        d = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return d.astimezone(PARIS).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ts or ""


def _filtres():
    """Paramètres de la requête -> (où SQL, paramètres, valeurs pour le formulaire)."""
    du = _parse_date(request.args.get("du"))
    au = _parse_date(request.args.get("au"))
    if "du" not in request.args and "au" not in request.args:
        au = date.today()
        du = au - timedelta(days=30)
    type_bon = request.args.get("type", "")
    depose = request.args.get("depose", "")
    q = (request.args.get("q") or "").strip()
    where, params = ["b.statut = 'bl'"], []
    if du:
        where.append("b.date_distribution >= ?"); params.append(du.isoformat())
    if au:
        where.append("b.date_distribution <= ?"); params.append(au.isoformat())
    if type_bon in ("sec", "frais"):
        where.append("b.type_bon = ?"); params.append(type_bon)
    if depose == "oui":
        where.append("b.bl_depose_le IS NOT NULL")
    elif depose == "non":
        where.append("b.bl_depose_le IS NULL")
    for terme in q.split():                      # tous les termes doivent correspondre
        like = f"%{terme}%"
        where.append("""(a.nom_association LIKE ? OR a.code_VIF LIKE ? OR b.bl_fichier LIKE ? OR b.menus LIKE ?
                         OR b.valide_par LIKE ? OR b.commentaire LIKE ?
                         OR EXISTS (SELECT 1 FROM bons_preparation_lignes l WHERE l.bon_id = b.id
                                    AND (l.article LIKE ? OR l.libelle LIKE ?))
                         OR EXISTS (SELECT 1 FROM bons_preparation_lots t
                                    JOIN bons_preparation_lignes l ON l.id = t.ligne_id
                                    WHERE l.bon_id = b.id AND t.lot LIKE ?))""")
        params += [like] * 9
    form = {"du": du.isoformat() if du else "", "au": au.isoformat() if au else "",
            "type": type_bon, "depose": depose, "q": q}
    return " AND ".join(where), params, form


def _bons(conn, where, params):
    return [dict(r) for r in conn.execute(f"""
        SELECT b.id, b.date_distribution, b.type_bon, b.heure, b.emplacement, b.nbre_beneficiaires,
               b.menus, b.valide_le, b.valide_par, b.bl_fichier, b.bl_depose_le, b.commentaire,
               a.nom_association, a.code_VIF,
               (SELECT COUNT(*) FROM bons_preparation_lignes l WHERE l.bon_id = b.id) AS nb_lignes
        FROM bons_preparation b JOIN associations a ON a.id = b.association_id
        WHERE {where}
        ORDER BY b.date_distribution DESC, a.nom_association COLLATE NOCASE, b.heure""", params)]


@distribution_bp.route("/bons_livraison")
@login_required
@require_access("distribution", "lecture")
def bons_livraison_liste():
    where, params, form = _filtres()
    conn = get_db_connection()
    try:
        bons = _bons(conn, where, params)
    finally:
        conn.close()
    for b in bons:
        b["valide_le"] = heure_locale(b["valide_le"])
        b["depose"] = bool(b["bl_depose_le"])
        b["url_voir"] = url_for("distribution.bons_prepa_voir", bon_id=b["id"])
        b["url_pdf"] = url_for("distribution.bons_livraison_pdf_bon", bon_id=b["id"])
        b["url_csv"] = url_for("distribution.bons_prepa_bl_csv", bon_id=b["id"])
    return render_template("distribution/bons_livraison_liste.html", bons=bons, form=form, max_pdf=MAX_PDF,
                           query=request.query_string.decode())


def _charger_pdf(conn, where, params) -> list[dict]:
    bons = _bons(conn, where, params)
    for b in bons:
        iso = b["date_distribution"]
        b["date_fr"] = f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}"
        loc = heure_locale(b["valide_le"])
        b["valide_fr"] = f"{loc[8:10]}/{loc[5:7]}/{loc[:4]} {loc[11:16]}" if loc else ""
        lignes = []
        for l in conn.execute("SELECT * FROM bons_preparation_lignes WHERE bon_id = ? ORDER BY ordre, id", (b["id"],)):
            q = _qte_bl(l)
            if not q or q <= 0:
                continue
            lots = conn.execute("SELECT lot, dlc, ddm, quantite FROM bons_preparation_lots WHERE ligne_id = ? ORDER BY id",
                                (l["id"],)).fetchall()
            if lots:
                for t in lots:
                    lignes.append({"article": l["article"], "libelle": l["libelle"], "lot": t["lot"],
                                   "peremption": t["dlc"] or t["ddm"] or "", "quantite": t["quantite"],
                                   "unite": l["unite"], "commentaire": l["commentaire"] or ""})
            else:
                lignes.append({"article": l["article"], "libelle": l["libelle"], "lot": "", "peremption": "",
                               "quantite": q, "unite": l["unite"], "commentaire": l["commentaire"] or ""})
        b["lignes"] = lignes
    return bons


def _reponse_pdf(bons, nom):
    from weasyprint import HTML

    from ba38_utilitaires.organisation import get_organisation
    html = render_template("distribution/bons_livraison_pdf.html", bons=bons, org=get_organisation())
    buf = BytesIO()
    HTML(string=html).write_pdf(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", download_name=nom)


@distribution_bp.route("/bons_livraison/<int:bon_id>/pdf")
@login_required
@require_access("distribution", "lecture")
def bons_livraison_pdf_bon(bon_id):
    conn = get_db_connection()
    try:
        bons = _charger_pdf(conn, "b.statut = 'bl' AND b.id = ?", [bon_id])
    finally:
        conn.close()
    if not bons:
        flash("Bon de livraison introuvable.", "danger")
        return redirect(url_for("distribution.bons_livraison_liste"))
    return _reponse_pdf(bons, (bons[0]["bl_fichier"] or f"bl_{bon_id}").replace(".csv", ".pdf"))


@distribution_bp.route("/bons_livraison/pdf")
@login_required
@require_access("distribution", "lecture")
def bons_livraison_pdf_selection():
    """Réimpression groupée : tous les BL correspondant aux filtres de la consultation (max MAX_PDF)."""
    where, params, _ = _filtres()
    conn = get_db_connection()
    try:
        bons = _charger_pdf(conn, where, params)
    finally:
        conn.close()
    if not bons:
        flash("Aucun bon de livraison à imprimer.", "warning")
        return redirect(url_for("distribution.bons_livraison_liste", **request.args))
    if len(bons) > MAX_PDF:
        flash(f"Trop de bons ({len(bons)}) : affinez les filtres (maximum {MAX_PDF}).", "warning")
        return redirect(url_for("distribution.bons_livraison_liste", **request.args))
    return _reponse_pdf(bons, f"bons_livraison_{date.today():%Y%m%d}.pdf")
