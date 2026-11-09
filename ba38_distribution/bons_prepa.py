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
    articles = [dict(r) for r in conn.execute("SELECT article, libelle FROM articles ORDER BY article")]
    conn.close()
    ecriture = has_access("distribution", "ecriture")
    from .bons_livraison import heure_locale
    bon = dict(bon, valide_le=heure_locale(bon["valide_le"]))   # horodatage SQLite en UTC -> heure de Paris
    return render_template("distribution/bons_prepa_bon.html", bon=bon, lignes=lignes, articles=articles,
                           peut_ecrire=ecriture and bon["statut"] == "preparation",
                           peut_rouvrir=ecriture and bon["statut"] == "bl" and not bon["bl_depose_le"])


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
                if "quantite_livree" in data:
                    brut = str(data["quantite_livree"]).replace(",", ".").strip()
                    try:
                        livree = float(brut) if brut != "" else None
                    except ValueError:
                        return jsonify(ok=False, erreur="Quantité livrée invalide"), 400
                    if livree is not None and livree < 0:
                        return jsonify(ok=False, erreur="Quantité négative"), 400
                    conn.execute("UPDATE bons_preparation_lignes SET quantite_livree=? WHERE id=? AND bon_id=?",
                                 (livree, lid, bon_id))
                if "commentaire_ligne" in data:
                    conn.execute("UPDATE bons_preparation_lignes SET commentaire=? WHERE id=? AND bon_id=?",
                                 ((data["commentaire_ligne"] or "").strip(), lid, bon_id))
        aj = data.get("ajouter")
        if aj:
            code = str(aj.get("article") or "").strip()
            art = conn.execute("SELECT article, libelle FROM articles WHERE article=?", (code,)).fetchone()
            if not art:
                return jsonify(ok=False, erreur=f"Article inconnu : {code or '(vide)'}"), 400
            unite = UNITES_VIF.get(str(aj.get("unite") or "Kg").lower())
            if not unite:
                return jsonify(ok=False, erreur="Unité invalide"), 400
            try:
                qte = float(str(aj.get("quantite")).replace(",", ".").strip())
            except ValueError:
                return jsonify(ok=False, erreur="Quantité invalide"), 400
            if qte < 0:
                return jsonify(ok=False, erreur="Quantité négative"), 400
            ordre = conn.execute("SELECT COALESCE(MAX(ordre), 0) + 1 FROM bons_preparation_lignes WHERE bon_id=?",
                                 (bon_id,)).fetchone()[0]
            conn.execute("""INSERT INTO bons_preparation_lignes
                              (bon_id, ordre, menu_code, article, libelle, source, quantite, unite)
                            VALUES (?,?,?,?,?,?,?,?)""",
                         (bon_id, ordre, "", art["article"], art["libelle"] or "", "ajout", qte, unite))
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


# ------------------------------------------------------------
# Validation en BL : un bon validé = un BL = un fichier CSV (format SFTP VIF)
# avec choix des lots du stock (table stocks, une ligne CSV par lot)
# ------------------------------------------------------------
UNITES_VIF = {"kg": "Kg", "kgn": "Kgn", "col": "COL", "p": "P", "u": "U", "pal": "PAL"}
DEPOT_SORTIE = "03"
TOLERANCE = 0.005


def _cle_date(txt: str):
    """jj/mm/aaaa -> (0, aaaa-mm-jj) ; vide -> (1, '') pour passer en dernier."""
    t = (txt or "").strip()
    try:
        return (0, datetime.strptime(t, "%d/%m/%Y").strftime("%Y-%m-%d"))
    except ValueError:
        return (1, "")


def _base_unite(row, unite: str):
    """Quantité disponible d'une ligne de stock dans l'unité du bon (None = non contrôlable)."""
    return {"kgn": row["kg_net"], "kg": row["kg_brut"], "col": row["col"], "pal": row["pal"]}.get((unite or "").lower())


def stock_lots(conn, article: str, unite: str, exclure_bon_id: int = 0) -> list[dict]:
    """Lots disponibles d'un article dans le dépôt de sortie (03) uniquement, triés par DLC/DDM la plus proche.
    `dispo` = stock (dans l'unité du bon) moins les lots déjà affectés par d'autres BL créés
    mais pas encore déposés pour VIF (le stock VIF ne les a pas encore décomptés)."""
    reserve = {}
    for r in conn.execute("""
            SELECT t.lot, t.depot, SUM(t.quantite) q FROM bons_preparation_lots t
            JOIN bons_preparation_lignes l ON l.id = t.ligne_id
            JOIN bons_preparation b ON b.id = l.bon_id
            WHERE l.article = ? AND t.unite = ? AND b.statut = 'bl' AND b.bl_depose_le IS NULL AND b.id != ?
            GROUP BY t.lot, t.depot""", (article, unite, exclure_bon_id)):
        reserve[(r["lot"], r["depot"] or "")] = r["q"] or 0
    lots = []
    for r in conn.execute("""SELECT lot, depot, dlc, ddm, kg_net, kg_brut, col, pal FROM stocks
                             WHERE article = ? AND TRIM(COALESCE(lot, '')) != '' AND depot = ?""",
                          (article, DEPOT_SORTIE)):
        base = _base_unite(r, unite)
        if base is None:
            if not ((r["kg_net"] or 0) > 0 or (r["kg_brut"] or 0) > 0 or (r["col"] or 0) > 0):
                continue
            dispo = None
        else:
            dispo = round(base - reserve.get((r["lot"], r["depot"] or ""), 0), 3)
            if dispo <= 0:
                continue
        lots.append({"lot": r["lot"], "depot": r["depot"] or "", "dlc": r["dlc"] or "", "ddm": r["ddm"] or "",
                     "kg_net": r["kg_net"], "kg_brut": r["kg_brut"], "col": r["col"], "pal": r["pal"],
                     "dispo": dispo})
    lots.sort(key=lambda x: (_cle_date(x["dlc"] or x["ddm"]), x["lot"]))
    return lots


def proposer_lots(lots: list[dict], quantite: float) -> list[dict]:
    """Répartition FIFO de `quantite` sur les lots (déjà triés). Retourne [{lot, depot, dlc, ddm, quantite}]."""
    reste, res = round(quantite, 3), []
    for l in lots:
        if reste <= TOLERANCE:
            break
        prise = reste if l["dispo"] is None else min(reste, l["dispo"])
        if prise > 0:
            res.append({"lot": l["lot"], "depot": l["depot"], "dlc": l["dlc"], "ddm": l["ddm"],
                        "quantite": round(prise, 3)})
            reste = round(reste - prise, 3)
    return res


def _qte_bl(l):   # quantité livrée si saisie, sinon quantité préparée
    return l["quantite_livree"] if l["quantite_livree"] is not None else l["quantite"]


def lignes_a_valider(conn, bon_id: int) -> list[dict]:
    """Lignes du bon à livrer, avec leurs lots disponibles et la proposition FIFO."""
    res = []
    for l in conn.execute("SELECT * FROM bons_preparation_lignes WHERE bon_id = ? ORDER BY ordre, id", (bon_id,)):
        q = _qte_bl(l)
        d = dict(l, qte_bl=q, unite=UNITES_VIF.get((l["unite"] or "Kg").lower(), l["unite"]))
        d["lots"] = stock_lots(conn, l["article"], d["unite"], bon_id) if q else []
        # l'article a-t-il des lots en stock (dépôt 03), même entièrement réservés par d'autres BL ?
        d["stock_03"] = bool(conn.execute(
            """SELECT 1 FROM stocks WHERE article = ? AND TRIM(COALESCE(lot, '')) != '' AND depot = ?
               AND (COALESCE(kg_net, 0) > 0 OR COALESCE(kg_brut, 0) > 0 OR COALESCE(col, 0) > 0) LIMIT 1""",
            (l["article"], DEPOT_SORTIE)).fetchone()) if q else False
        d["propose"] = proposer_lots(d["lots"], q) if q else []
        d["reste_propose"] = round((q or 0) - sum(p["quantite"] for p in d["propose"]), 3)
        res.append(d)
    return res


def valider_bon(conn, bon_id: int, par: str = "", affectations: dict | None = None, sans_lot: set | None = None):
    """Passe le bon en statut « bl » et conserve son CSV. Retourne (ok, [messages]).

    `affectations` {ligne_id: [{lot, depot, quantite}]} = choix de l'utilisateur ; None = répartition
    FIFO automatique (validation groupée). Une ligne dont l'article a des lots en stock doit être
    entièrement répartie, sauf si elle figure dans `sans_lot`."""
    from ba38_vif.generation import ErreursVif, generer_bl_distribution

    bon = conn.execute("""SELECT b.*, a.code_VIF, a.nom_association FROM bons_preparation b
                          JOIN associations a ON a.id = b.association_id WHERE b.id = ?""", (bon_id,)).fetchone()
    if not bon:
        return False, ["Bon introuvable"]
    if bon["statut"] != "preparation":
        return False, ["Bon déjà validé"]
    sans_lot = sans_lot or set()
    erreurs, csv_lignes, a_stocker = [], [], []
    for l in lignes_a_valider(conn, bon_id):
        nom = f"{l['article']} {l['libelle'] or ''}".strip()
        q = l["qte_bl"]
        if q is None:
            erreurs.append(f"Ligne sans quantité : {nom}")
            continue
        if q <= 0:
            continue
        if l["id"] in sans_lot or (not l["lots"] and not l["stock_03"]):
            csv_lignes.append({"article": l["article"], "quantite": q, "unite": l["unite"]})
            continue
        if affectations is None:
            aff = l["propose"]
        else:
            aff = [a for a in affectations.get(l["id"], []) if (a.get("quantite") or 0) > 0]
        dispo = {(x["lot"], x["depot"]): x for x in l["lots"]}
        total = 0.0
        for a in aff:
            x = dispo.get((a["lot"], a.get("depot", "")))
            if x is None:
                erreurs.append(f"{nom} : lot {a['lot']} indisponible")
            elif x["dispo"] is not None and a["quantite"] > x["dispo"] + TOLERANCE:
                erreurs.append(f"{nom} : lot {a['lot']} insuffisant (disponible {x['dispo']:g} {l['unite']})")
            total += a["quantite"]
        if abs(total - q) > TOLERANCE:
            erreurs.append(f"{nom} : lots affectés {total:g} {l['unite']} pour {q:g} à livrer")
            continue
        for a in aff:
            x = dispo.get((a["lot"], a.get("depot", "")))
            if x is None:
                continue
            csv_lignes.append({"article": l["article"], "quantite": a["quantite"], "unite": l["unite"],
                               "lot": a["lot"]})   # sortie toujours du dépôt DEPOT_SORTIE (03)
            a_stocker.append((l["id"], a["lot"], x["depot"], x["dlc"], x["ddm"], a["quantite"], l["unite"]))
    if erreurs:
        return False, erreurs
    d = datetime.strptime(bon["date_distribution"], "%Y-%m-%d").date()
    try:
        csv = generer_bl_distribution(bon["code_VIF"], d, csv_lignes)
    except ErreursVif as e:
        return False, e.messages
    nom = f"blba38_{d:%Y%m%d}_{bon['code_VIF']}_{bon_id:06d}.csv"
    conn.execute("""UPDATE bons_preparation SET statut='bl', valide_le=CURRENT_TIMESTAMP, valide_par=?,
                    bl_fichier=?, bl_csv=?, modifie_le=CURRENT_TIMESTAMP WHERE id=? AND statut='preparation'""",
                 (par, nom, csv, bon_id))
    for t in a_stocker:
        conn.execute("""INSERT INTO bons_preparation_lots (ligne_id, lot, depot, dlc, ddm, quantite, unite)
                        VALUES (?,?,?,?,?,?,?)""", t)
    conn.commit()
    return True, [nom]


@distribution_bp.route("/bons_prepa/<int:bon_id>/valider", methods=["GET"])
@login_required
@require_access("distribution", "ecriture")
def bons_prepa_valider_page(bon_id):
    """Étape de validation : choix des lots du stock pour chaque ligne."""
    conn = get_db_connection()
    try:
        bon = conn.execute("""SELECT b.*, a.nom_association, a.code_VIF FROM bons_preparation b
                              JOIN associations a ON a.id = b.association_id WHERE b.id = ?""", (bon_id,)).fetchone()
        if not bon:
            flash("Bon introuvable.", "danger")
            return redirect(url_for("distribution.bons_prepa_liste"))
        if bon["statut"] != "preparation":
            flash("Ce bon est déjà validé.", "warning")
            return redirect(url_for("distribution.bons_prepa_voir", bon_id=bon_id))
        lignes = lignes_a_valider(conn, bon_id)
        maj = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    finally:
        conn.close()
    return render_template("distribution/bons_prepa_validation.html", bon=dict(bon), lignes=lignes,
                           depot_sortie=DEPOT_SORTIE, nb_stock=maj)


@distribution_bp.route("/bons_prepa/<int:bon_id>/valider", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def bons_prepa_valider(bon_id):
    """Corps JSON : {affectations: {ligne_id: [{lot, depot, quantite}]}, sans_lot: [ligne_id]}."""
    data = request.get_json(silent=True) or {}
    try:
        affectations = {int(k): [{"lot": str(a["lot"]), "depot": str(a.get("depot") or ""),
                                  "quantite": float(str(a["quantite"]).replace(",", ".") or 0)} for a in v]
                        for k, v in (data.get("affectations") or {}).items()}
        sans_lot = {int(x) for x in (data.get("sans_lot") or [])}
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, erreurs=["Données invalides"]), 400
    conn = get_db_connection()
    try:
        ok, msgs = valider_bon(conn, bon_id, getattr(current_user, "email", "") or "", affectations, sans_lot)
    finally:
        conn.close()
    if ok:
        flash(f"Bon validé : BL créé ({msgs[0]}).", "success")
        return jsonify(ok=True, url=url_for("distribution.bons_prepa_voir", bon_id=bon_id))
    return jsonify(ok=False, erreurs=msgs), 400


@distribution_bp.route("/bons_prepa/valider_jour", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def bons_prepa_valider_jour():
    """Valide tous les bons du jour encore en préparation avec une répartition FIFO des lots ;
    ceux qui ne passent pas (stock insuffisant, ligne sans quantité...) restent en préparation."""
    d = _parse_date(request.form.get("date"))
    type_bon = "frais" if request.form.get("type") == "frais" else "sec"
    if not d:
        flash("Date invalide.", "danger")
        return redirect(url_for("distribution.bons_prepa_liste"))
    conn = get_db_connection()
    try:
        bons = conn.execute("""SELECT b.id, a.nom_association FROM bons_preparation b
                               JOIN associations a ON a.id = b.association_id
                               WHERE b.date_distribution=? AND b.type_bon=? AND b.statut='preparation'
                               ORDER BY a.nom_association COLLATE NOCASE""", (d.isoformat(), type_bon)).fetchall()
        par = getattr(current_user, "email", "") or ""
        nb_ok, refus = 0, []
        for b in bons:
            ok, msgs = valider_bon(conn, b["id"], par)
            if ok:
                nb_ok += 1
            else:
                refus.append(f"{b['nom_association']} ({msgs[0]})")
    finally:
        conn.close()
    if nb_ok:
        flash(f"{nb_ok} bon(s) validé(s), BL créés (lots répartis automatiquement, plus ancienne date d'abord).", "success")
    if refus:
        flash(f"{len(refus)} bon(s) non validé(s) : " + " ; ".join(refus), "warning")
    if not bons:
        flash("Aucun bon en préparation à valider.", "info")
    return redirect(url_for("distribution.bons_prepa_liste", date=d.isoformat(), type=type_bon))


@distribution_bp.route("/bons_prepa/<int:bon_id>/rouvrir", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def bons_prepa_rouvrir(bon_id):
    """Annule la validation tant que le fichier n'a pas été déposé pour VIF."""
    conn = get_db_connection()
    try:
        bon = conn.execute("SELECT statut, bl_depose_le FROM bons_preparation WHERE id=?", (bon_id,)).fetchone()
        if not bon:
            flash("Bon introuvable.", "danger")
            return redirect(url_for("distribution.bons_prepa_liste"))
        if bon["statut"] != "bl":
            flash("Ce bon n'est pas validé.", "warning")
        elif bon["bl_depose_le"]:
            flash("Impossible : le fichier BL a déjà été déposé pour VIF.", "danger")
        else:
            conn.execute("""DELETE FROM bons_preparation_lots WHERE ligne_id IN
                            (SELECT id FROM bons_preparation_lignes WHERE bon_id=?)""", (bon_id,))
            conn.execute("""UPDATE bons_preparation SET statut='preparation', valide_le=NULL, valide_par=NULL,
                            bl_fichier=NULL, bl_csv=NULL, modifie_le=CURRENT_TIMESTAMP WHERE id=?""", (bon_id,))
            conn.commit()
            flash("Validation annulée : le bon est de nouveau modifiable, le CSV a été supprimé.", "success")
    finally:
        conn.close()
    return redirect(url_for("distribution.bons_prepa_voir", bon_id=bon_id))


@distribution_bp.route("/bons_prepa/<int:bon_id>/bl.csv")
@login_required
@require_access("distribution", "lecture")
def bons_prepa_bl_csv(bon_id):
    from io import BytesIO

    from flask import send_file
    conn = get_db_connection()
    try:
        bon = conn.execute("SELECT bl_fichier, bl_csv FROM bons_preparation WHERE id=?", (bon_id,)).fetchone()
    finally:
        conn.close()
    if not bon or not bon["bl_csv"]:
        flash("Aucun fichier BL pour ce bon.", "warning")
        return redirect(url_for("distribution.bons_prepa_voir", bon_id=bon_id))
    return send_file(BytesIO(bon["bl_csv"].encode("utf-8")), mimetype="text/csv",
                     as_attachment=True, download_name=bon["bl_fichier"])
