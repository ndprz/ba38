# ba38_distribution/bons_prepa.py
"""Bons de préparation de la distribution.

SEC : calculé à partir du planning du jour, des menus (menus_articles) et des
portions (articles.portion, en kg NET par personne).
FRAIS : à venir (dépend de la ramasse du jour) ; la génération est déjà verrouillée
au jour même."""

import math
import sqlite3
from datetime import date, datetime

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ba38_utilitaires.core import get_db_connection, has_access, require_access

from . import distribution_bp
from .planning_jour import associations_du_jour


# ------------------------------------------------------------
# Calcul
# ------------------------------------------------------------
def _arrondi_sup(x: float, decimales: int = 1) -> float:
    f = 10 ** decimales
    return math.ceil(round(x * f, 6)) / f


def _entier(v):
    try:
        return int(float(str(v).replace(",", ".").strip()))
    except (TypeError, ValueError):
        return None


def _conditionnement(a, unite: str):
    """cdt (kg par P ou par COL) de l'article pour `unite`, ou None."""
    for u, cdt in (("unite2", "cdt_unite2"), ("unite3", "cdt_unite3"), ("unite4", "cdt_unite4")):
        if (a[u] or "").strip().upper() == unite and (a[cdt] or 0) > 0:
            return a[cdt]
    return None


def calculer_lignes(conn, codes_menus: list[str], beneficiaires: int) -> list[dict]:
    """Lignes d'un bon SEC pour un passage.

    - quantité fixe du menu : reprise telle quelle (par passage) ;
    - sinon portion (kgn) × bénéficiaires × coef net→brut, arrondi selon l'unité de la
      ligne de menu : COL / P = colis ou pièces entiers au supérieur (via le
      conditionnement de l'article, calculé sur le net pour un article géré en Kgn,
      sur le brut sinon) ; autre = kg brut arrondi au dixième supérieur ;
    - sans portion : ligne sans quantité, à saisir."""
    lignes, index = [], {}
    ordre = 0
    for code in codes_menus:
        rows = conn.execute("""
            SELECT ma.article, ma.quantite, ma.unite, ma.commentaire,
                   a.libelle, a.portion, a.coef_kgn_vers_kg, a.unite1_principale,
                   a.unite2, a.cdt_unite2, a.unite3, a.cdt_unite3, a.unite4, a.cdt_unite4
            FROM menus_articles ma LEFT JOIN articles a ON a.article = ma.article
            WHERE ma.menu_code = ? ORDER BY ma.ordre""", (code,)).fetchall()
        for r in rows:
            unite = (r["unite"] or "Kg").strip()
            portion = r["portion"] if (r["portion"] or 0) > 0 else None
            coef = r["coef_kgn_vers_kg"] if (r["coef_kgn_vers_kg"] or 0) > 0 else 1.0
            if r["quantite"] is not None:
                source, qte = "fixe", r["quantite"]
            elif portion:
                source = "portion"
                net = portion * beneficiaires
                brut = net * coef
                u = unite.upper()
                cdt = _conditionnement(r, u) if u in ("COL", "P") else None
                if cdt:
                    base = net if (r["unite1_principale"] or "").lower() == "kgn" else brut
                    qte, unite = float(math.ceil(round(base / cdt, 6))), u
                else:
                    qte, unite = _arrondi_sup(brut), "Kg"
            else:
                source, qte = "sans_portion", None
            cle = (r["article"], unite)
            if cle in index:                      # même article dans 2 menus du passage
                l = lignes[index[cle]]
                if qte is not None and l["quantite"] is not None:
                    l["quantite"] = l["quantite_calc"] = l["quantite"] + qte
                l["menu_code"] += f", {code}"
                continue
            ordre += 1
            index[cle] = len(lignes)
            lignes.append({
                "ordre": ordre, "menu_code": code, "article": r["article"],
                "libelle": r["libelle"] or "", "source": source, "portion": portion,
                "coef": coef if portion else None, "quantite_calc": qte, "quantite": qte,
                "unite": unite, "commentaire": r["commentaire"] or "",
            })
    return lignes


def _codes(texte: str) -> list[str]:
    return [c.strip() for c in (texte or "").split(",") if c.strip()]


def classer_passages(conn, d: date, type_menu: str = "sec"):
    """(à générer, ignorés) pour le jour d : passages actifs ayant un menu du type demandé.
    Ignorés : menu inexistant dans `menus`, ou bénéficiaires non renseignés."""
    connus = {r[0] for r in conn.execute("SELECT code FROM menus WHERE type_menu = ?", (type_menu,))}
    champ = "menu_sec" if type_menu == "sec" else "menu_frais"
    a_generer, ignores = [], []
    for a in associations_du_jour(d):
        if a["statut"] not in ("normal", "recue") or not a[champ]:
            continue
        codes = _codes(a[champ])
        inconnus = [c for c in codes if c not in connus]
        benef = _entier(a["beneficiaires"])
        if inconnus:
            ignores.append({**a, "raison": "Menu inexistant : " + ", ".join(inconnus)})
        elif not benef:
            ignores.append({**a, "raison": "Bénéficiaires non renseignés"})
        else:
            a_generer.append({**a, "codes": codes, "benef": benef})
    return a_generer, ignores


def generer_bons(d: date, type_menu: str, recalculer: bool = False, par: str = ""):
    """Crée les bons manquants du jour. `recalculer` supprime d'abord les bons encore
    en préparation (les modifications manuelles sont alors perdues)."""
    conn = get_db_connection()
    try:
        a_generer, ignores = classer_passages(conn, d, type_menu)
        iso = d.isoformat()
        if recalculer:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM bons_preparation WHERE date_distribution=? AND type_bon=? AND statut='preparation'",
                (iso, type_menu))]
            for i in ids:
                conn.execute("DELETE FROM bons_preparation_lignes WHERE bon_id=?", (i,))
                conn.execute("DELETE FROM bons_preparation WHERE id=?", (i,))
        crees = existants = 0
        for a in a_generer:
            cur = conn.execute("""
                INSERT OR IGNORE INTO bons_preparation
                  (date_distribution, type_bon, association_id, heure, emplacement, menus,
                   nbre_beneficiaires, besoins_particuliers, cree_par)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (iso, type_menu, a["association_id"], a["heure"] or "", a["emplacement"] or "",
                 ", ".join(a["codes"]), a["benef"], a["remarque"], par))
            if cur.rowcount == 0:
                existants += 1
                continue
            bon_id = cur.lastrowid
            for l in calculer_lignes(conn, a["codes"], a["benef"]):
                conn.execute("""
                    INSERT INTO bons_preparation_lignes
                      (bon_id, ordre, menu_code, article, libelle, source, portion, coef,
                       quantite_calc, quantite, unite, commentaire)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bon_id, l["ordre"], l["menu_code"], l["article"], l["libelle"], l["source"],
                     l["portion"], l["coef"], l["quantite_calc"], l["quantite"], l["unite"], l["commentaire"]))
            crees += 1
        conn.commit()
        return {"crees": crees, "existants": existants, "ignores": ignores}
    finally:
        conn.close()


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
def _parse_date(brut):
    try:
        return datetime.strptime((brut or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@distribution_bp.route("/bons_prepa")
@login_required
@require_access("distribution", "lecture")
def bons_prepa_liste():
    d = _parse_date(request.args.get("date")) or date.today()
    type_bon = "frais" if request.args.get("type") == "frais" else "sec"
    conn = get_db_connection()
    bons = [dict(r) for r in conn.execute("""
        SELECT b.*, a.nom_association, a.code_VIF,
               (SELECT COUNT(*) FROM bons_preparation_lignes l WHERE l.bon_id = b.id) AS nb_lignes,
               (SELECT COUNT(*) FROM bons_preparation_lignes l
                 WHERE l.bon_id = b.id AND l.quantite IS NULL) AS nb_a_saisir
        FROM bons_preparation b JOIN associations a ON a.id = b.association_id
        WHERE b.date_distribution = ? AND b.type_bon = ?
        ORDER BY a.nom_association COLLATE NOCASE, b.heure""", (d.isoformat(), type_bon))]
    ignores = classer_passages(conn, d, "sec")[1] if type_bon == "sec" else []
    conn.close()
    return render_template(
        "distribution/bons_prepa_liste.html", date_choisie=d, type_bon=type_bon, bons=bons,
        ignores=ignores, aujourdhui=date.today().isoformat(),
        peut_ecrire=has_access("distribution", "ecriture"))


@distribution_bp.route("/bons_prepa/generer", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def bons_prepa_generer():
    d = _parse_date(request.form.get("date"))
    type_bon = "frais" if request.form.get("type") == "frais" else "sec"
    if not d:
        flash("Date invalide.", "danger")
        return redirect(url_for("distribution.bons_prepa_liste"))
    if type_bon == "frais":
        if d != date.today():
            flash("Les bons de préparation FRAIS ne peuvent être générés que pour le jour même.", "danger")
        else:
            flash("Génération FRAIS : en attente du module de ramasse quotidienne.", "warning")
        return redirect(url_for("distribution.bons_prepa_liste", date=d.isoformat(), type="frais"))
    res = generer_bons(d, "sec", recalculer=bool(request.form.get("recalculer")),
                       par=getattr(current_user, "email", "") or "")
    msg = f"{res['crees']} bon(s) SEC créé(s)"
    if res["existants"]:
        msg += f", {res['existants']} déjà existant(s) conservé(s)"
    if res["ignores"]:
        msg += f", {len(res['ignores'])} association(s) ignorée(s) (voir liste)"
    flash(msg + ".", "success" if res["crees"] or res["existants"] else "warning")
    return redirect(url_for("distribution.bons_prepa_liste", date=d.isoformat(), type="sec"))


@distribution_bp.route("/bons_prepa/<int:bon_id>")
@login_required
@require_access("distribution", "lecture")
def bons_prepa_voir(bon_id):
    conn = get_db_connection()
    bon = conn.execute("""SELECT b.*, a.nom_association, a.code_VIF FROM bons_preparation b
                          JOIN associations a ON a.id = b.association_id WHERE b.id = ?""", (bon_id,)).fetchone()
    if not bon:
        conn.close()
        flash("Bon introuvable.", "danger")
        return redirect(url_for("distribution.bons_prepa_liste"))
    lignes = [dict(r) for r in conn.execute(
        "SELECT * FROM bons_preparation_lignes WHERE bon_id = ? ORDER BY ordre, id", (bon_id,))]
    conn.close()
    return render_template("distribution/bons_prepa_bon.html", bon=dict(bon), lignes=lignes,
                           peut_ecrire=has_access("distribution", "ecriture") and bon["statut"] == "preparation")


@distribution_bp.route("/bons_prepa/<int:bon_id>/save", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def bons_prepa_save(bon_id):
    """Enregistre commentaire du bon + quantités / commentaires des lignes (autosave)."""
    data = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        bon = conn.execute("SELECT statut FROM bons_preparation WHERE id=?", (bon_id,)).fetchone()
        if not bon:
            return jsonify(ok=False, erreur="Bon introuvable"), 404
        if bon["statut"] != "preparation":
            return jsonify(ok=False, erreur="Bon verrouillé"), 409
        if "commentaire" in data:
            conn.execute("UPDATE bons_preparation SET commentaire=? WHERE id=?",
                         ((data["commentaire"] or "").strip(), bon_id))
        lid = data.get("ligne_id")
        if lid is not None:
            if data.get("supprimer"):
                conn.execute("DELETE FROM bons_preparation_lignes WHERE id=? AND bon_id=?", (lid, bon_id))
            else:
                if "quantite" in data:
                    brut = str(data["quantite"]).replace(",", ".").strip()
                    try:
                        qte = float(brut) if brut != "" else None
                    except ValueError:
                        return jsonify(ok=False, erreur="Quantité invalide"), 400
                    if qte is not None and qte < 0:
                        return jsonify(ok=False, erreur="Quantité négative"), 400
                    conn.execute("UPDATE bons_preparation_lignes SET quantite=? WHERE id=? AND bon_id=?",
                                 (qte, lid, bon_id))
                if "commentaire_ligne" in data:
                    conn.execute("UPDATE bons_preparation_lignes SET commentaire=? WHERE id=? AND bon_id=?",
                                 ((data["commentaire_ligne"] or "").strip(), lid, bon_id))
        conn.execute("UPDATE bons_preparation SET modifie_le=CURRENT_TIMESTAMP WHERE id=?", (bon_id,))
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


# ------------------------------------------------------------
# Impression PDF (un bon, ou tous les bons du jour : une page par bon)
# ------------------------------------------------------------
def _charger_bons_pdf(conn, where: str, params: tuple) -> list[dict]:
    from .planning_jour import _rang_heure
    from ba38_partenaires.passage import _sans_accents
    bons = [dict(r) for r in conn.execute(f"""
        SELECT b.*, a.nom_association, a.code_VIF FROM bons_preparation b
        JOIN associations a ON a.id = b.association_id WHERE {where}""", params)]
    for b in bons:
        iso = b["date_distribution"]
        b["date_fr"] = f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}"
        b["lignes"] = [dict(r) for r in conn.execute(
            "SELECT * FROM bons_preparation_lignes WHERE bon_id = ? ORDER BY ordre, id", (b["id"],))]
    bons.sort(key=lambda b: (_rang_heure(b["heure"]), _sans_accents(b["nom_association"] or "")))
    return bons


def _reponse_pdf(bons: list[dict], nom_fichier: str):
    from io import BytesIO

    from flask import send_file
    from weasyprint import HTML

    from ba38_utilitaires.organisation import get_organisation
    html = render_template("distribution/bons_prepa_pdf.html", bons=bons, org=get_organisation())
    buf = BytesIO()
    HTML(string=html).write_pdf(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", download_name=nom_fichier)


@distribution_bp.route("/bons_prepa/<int:bon_id>/pdf")
@login_required
@require_access("distribution", "lecture")
def bons_prepa_pdf_bon(bon_id):
    conn = get_db_connection()
    try:
        bons = _charger_bons_pdf(conn, "b.id = ?", (bon_id,))
    finally:
        conn.close()
    if not bons:
        flash("Bon introuvable.", "danger")
        return redirect(url_for("distribution.bons_prepa_liste"))
    b = bons[0]
    return _reponse_pdf(bons, f"bon_prepa_{b['type_bon']}_{b['date_distribution']}_{b['id']}.pdf")


@distribution_bp.route("/bons_prepa/pdf")
@login_required
@require_access("distribution", "lecture")
def bons_prepa_pdf_jour():
    d = _parse_date(request.args.get("date")) or date.today()
    type_bon = "frais" if request.args.get("type") == "frais" else "sec"
    conn = get_db_connection()
    try:
        bons = _charger_bons_pdf(conn, "b.date_distribution = ? AND b.type_bon = ?", (d.isoformat(), type_bon))
    finally:
        conn.close()
    if not bons:
        flash("Aucun bon à imprimer pour ce jour.", "warning")
        return redirect(url_for("distribution.bons_prepa_liste", date=d.isoformat(), type=type_bon))
    return _reponse_pdf(bons, f"bons_prepa_{type_bon}_{d.isoformat()}.pdf")
