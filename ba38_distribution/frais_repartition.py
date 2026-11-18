# ba38_distribution/frais_repartition.py
"""Préparation des bons FRAIS : synthèse du jour (tonnage de la ramasse, bénéficiaires à servir,
panier par bénéficiaire) et ajout de produits du stock à la ramasse quand le panier est trop faible."""

from datetime import date, datetime

from flask import jsonify, request
from flask_login import current_user, login_required

from ba38_utilitaires.core import get_db_connection, require_access

from . import distribution_bp
from .bons_prepa import _parse_date, classer_passages, lignes_stock_03, stock_lots
from .stock_basilic import DEPOT_SORTIE, assurer_table_ajouts_lots, reservations_prepa, sorties_bl, snapshot_utc, transferts_depuis_snapshot

DEPOT_ORIGINE = "02"
SEUIL_PANIER_KG = 3.0     # valeur par défaut de l'objectif de panier (kg / bénéficiaire) ; réglable depuis la page (table parametres)
PARAM_OBJECTIF = "frais_panier_objectif_kg"


def objectif_panier(conn) -> float:
    """Objectif de panier FRAIS (kg par bénéficiaire) : réglage enregistré, sinon SEUIL_PANIER_KG."""
    try:
        r = conn.execute("SELECT param_value FROM parametres WHERE param_name = ? ORDER BY id LIMIT 1", (PARAM_OBJECTIF,)).fetchone()
        v = float(str(r[0]).replace(",", ".")) if r and r[0] not in (None, "") else None
        return v if v and v > 0 else SEUIL_PANIER_KG
    except Exception:
        return SEUIL_PANIER_KG


def totaux_a_repartir(conn, iso: str) -> list[dict]:
    """Par article : ramasse validée du jour + produits du stock ajoutés pour ce jour."""
    res = {}
    for r in conn.execute("""
            SELECT l.article, MAX(l.libelle) AS libelle, SUM(l.quantite_kg) AS kg
            FROM ramasses_jour_lignes l JOIN ramasses_jour r ON r.id = l.ramasse_id
            WHERE r.date_ramasse = ? AND r.statut = 'validee' AND l.quantite_kg > 0 GROUP BY l.article""", (iso,)):
        res[r["article"]] = {"article": r["article"], "libelle": r["libelle"] or "", "kg_ramasse": r["kg"] or 0, "kg_stock": 0}
    for r in conn.execute("""
            SELECT a.article, COALESCE(ar.libelle, '') AS libelle, a.quantite_kg FROM frais_ajouts_stock a
            LEFT JOIN articles ar ON ar.article = a.article WHERE a.date_distribution = ?""", (iso,)):
        x = res.setdefault(r["article"], {"article": r["article"], "libelle": r["libelle"], "kg_ramasse": 0, "kg_stock": 0})
        x["kg_stock"] += r["quantite_kg"] or 0
        x["libelle"] = x["libelle"] or r["libelle"]
    try:   # lots choisis dans « Produits ajoutables » (dépôts 02 / 03)
        for r in conn.execute("""
                SELECT a.article, COALESCE(ar.libelle, '') AS libelle, SUM(a.quantite_kg) AS kg FROM frais_ajouts_lots a
                LEFT JOIN articles ar ON ar.article = a.article WHERE a.date_distribution = ? AND a.quantite_kg > 0
                GROUP BY a.article""", (iso,)):
            x = res.setdefault(r["article"], {"article": r["article"], "libelle": r["libelle"], "kg_ramasse": 0, "kg_stock": 0})
            x["kg_stock"] += r["kg"] or 0
            x["libelle"] = x["libelle"] or r["libelle"]
    except Exception:      # table pas encore créée
        pass
    out = []
    for x in res.values():
        x["kg"] = round(x["kg_ramasse"] + x["kg_stock"], 3)
        out.append(x)
    return sorted(out, key=lambda x: x["article"])


def stock_ajoutable(conn, article: str, iso: str) -> float:
    """Stock (kg brut, dépôt 03) que l'on peut ajouter à la distribution FRAIS du jour pour cet article :
    stock du dernier fichier VIF au dépôt de sortie, moins les sorties déjà faites depuis (BL validés, hors
    bons FRAIS du jour qui sont justement alimentés par la ramasse et ces ajouts).
    Ne dépend pas des lots : un article ramassé peut porter le même numéro de lot que son stock."""
    vif = conn.execute("SELECT COALESCE(SUM(kg_brut), 0) FROM stocks WHERE article = ? AND depot = ?",
                       (article, DEPOT_SORTIE)).fetchone()[0] or 0
    vif += sum(t["kg"] for t in transferts_depuis_snapshot(conn, article))   # lots déjà transférés 02 -> 03 dans Basilic
    autres_sorties = conn.execute("""
        SELECT COALESCE(SUM(t.quantite), 0) FROM bons_preparation_lots t
        JOIN bons_preparation_lignes l ON l.id = t.ligne_id JOIN bons_preparation b ON b.id = l.bon_id
        WHERE l.article = ? AND t.unite = 'Kg' AND b.statut = 'bl' AND b.valide_le > ?
          AND NOT (b.type_bon = 'frais' AND b.date_distribution = ?)""",
        (article, snapshot_utc(conn), iso)).fetchone()[0] or 0
    return round(max(0.0, vif - autres_sorties), 3)


def stock_depot02(conn, article: str) -> float:
    """Stock (kg brut) de l'article au dépôt 02 : dernier stock VIF moins ce qui en a été transféré depuis."""
    vif = conn.execute("SELECT COALESCE(SUM(kg_brut), 0) FROM stocks WHERE article = ? AND depot = ?",
                       (article, DEPOT_ORIGINE)).fetchone()[0] or 0
    return round(max(0.0, vif - sum(t["kg"] for t in transferts_depuis_snapshot(conn, article))), 3)


def lots_stock_frais(conn, article: str, iso: str) -> list[dict]:
    """Lots en stock des dépôts 02 et 03 pour un article, en kg brut, avec ce qui est déjà choisi pour ce jour :
    [{depot, lot, dlc, ddm, dispo, choisi, transfert}] triés par date limite puis dépôt.
    - dépôt 03 : stock VIF + entrées de ramasse + lots transférés, moins les BL validés et les lots réservés par
      les autres bons (les bons FRAIS du jour sont exclus : ils sont alimentés par ces ajouts) ;
    - dépôt 02 : stock VIF moins ce qui en a déjà été transféré."""
    try:
        choisis = {(r["depot"], r["lot"]): dict(r) for r in conn.execute(
            "SELECT depot, lot, quantite_kg, transfert_kg FROM frais_ajouts_lots WHERE date_distribution = ? AND article = ?",
            (iso, article))}
    except Exception:
        choisis = {}
    sorties = sorties_bl(conn, article, "Kg")
    reserve = reservations_prepa(conn, article, "Kg", 0, iso)
    # ce qui a été ajouté ce jour depuis un lot du dépôt 02 est pris sur le stock transféré (pas réajoutable depuis le 03)
    via_02 = {lot: c["quantite_kg"] for (dep, lot), c in choisis.items() if dep == DEPOT_ORIGINE}
    res = []
    for r in lignes_stock_03(conn, article):
        dispo = (r["kg_brut"] or 0) - sorties.get((r["lot"], r["depot"] or ""), 0) - reserve.get((r["lot"], r["depot"] or ""), 0) \
            - via_02.get(r["lot"], 0)
        c = choisis.get((DEPOT_SORTIE, r["lot"]))
        if dispo > 0.0005 or c:
            res.append({"depot": DEPOT_SORTIE, "lot": r["lot"], "dlc": r["dlc"] or "", "ddm": r["ddm"] or "",
                        "dispo": round(dispo, 3), "choisi": c["quantite_kg"] if c else 0, "transfert": 0})
    transferes = {t["lot"]: t["kg"] for t in transferts_depuis_snapshot(conn, article)}
    for r in conn.execute("SELECT COALESCE(lot, '') AS lot, dlc, ddm, SUM(kg_brut) AS kg FROM stocks WHERE article = ? AND depot = ? "
                          "GROUP BY COALESCE(lot, ''), dlc, ddm", (article, DEPOT_ORIGINE)):
        c = choisis.get((DEPOT_ORIGINE, r["lot"]))
        deja = transferes.get(r["lot"], 0) - (c["transfert_kg"] if c else 0)    # transferts faits par ailleurs
        dispo = (r["kg"] or 0) - deja
        if dispo > 0.0005 or c:
            res.append({"depot": DEPOT_ORIGINE, "lot": r["lot"], "dlc": r["dlc"] or "", "ddm": r["ddm"] or "",
                        "dispo": round(dispo, 3), "choisi": c["quantite_kg"] if c else 0,
                        "transfert": c["transfert_kg"] if c else 0})
    from .bons_prepa import _cle_date
    res.sort(key=lambda x: (_cle_date(x["dlc"] or x["ddm"]), x["depot"], x["lot"]))
    return res


def lignes_ajoutables(conn, iso: str) -> list[tuple]:
    """(article, libellé, origine) : produits de la liste définie + articles de la ramasse du jour
    (on peut compléter un article ramassé avec du stock). origine = 'liste' | 'ramasse' | 'liste+ramasse'."""
    liste = {r["article"]: (r["libelle"], r["ordre"]) for r in conn.execute(
        """SELECT p.article, COALESCE(a.libelle, '') AS libelle, p.ordre FROM produits_ajoutables_frais p
           LEFT JOIN articles a ON a.article = p.article""")}
    ramasse = {r["article"]: r["libelle"] for r in conn.execute(
        """SELECT l.article, MAX(COALESCE(NULLIF(l.libelle, ''), '')) AS libelle FROM ramasses_jour_lignes l
           JOIN ramasses_jour r ON r.id = l.ramasse_id WHERE r.date_ramasse = ? AND r.statut = 'validee' AND l.quantite_kg > 0
           GROUP BY l.article""", (iso,))}
    res = []
    for art in sorted(set(liste) | set(ramasse), key=lambda a: (a not in ramasse, liste.get(a, ("", 9999))[1], a)):
        origine = "liste+ramasse" if (art in liste and art in ramasse) else ("ramasse" if art in ramasse else "liste")
        res.append((art, (liste.get(art) or ("", 0))[0] or ramasse.get(art, ""), origine))
    return res


def synthese_frais(conn, d: date) -> dict:
    """Tout ce qui s'affiche au-dessus des bons FRAIS."""
    iso = d.isoformat()
    seuil = objectif_panier(conn)
    a_generer, ignores = classer_passages(conn, d, "frais")
    benef = sum(a["benef"] for a in a_generer)
    from .bons_prepa import articles_menu_passage, exceptions_passage
    articles_menus = {}
    for a in a_generer:        # articles du passage = menus avec les exceptions de l'association
        for art in articles_menu_passage(conn, a["codes"], exceptions_passage(conn, a.get("ids"))):
            articles_menus.setdefault(art, set()).add(id(a))
    totaux = totaux_a_repartir(conn, iso)
    benef_par = {}
    for art, ids in articles_menus.items():
        benef_par[art] = sum(a["benef"] for a in a_generer if id(a) in ids)
    produits, hors_menu = [], []
    for t in totaux:
        if t["kg"] <= 0:
            continue
        if t["article"] in articles_menus:
            produits.append(dict(t, nb_passages=len(articles_menus[t["article"]]), benef=benef_par[t["article"]]))
        else:
            hors_menu.append(t)
    total_kg = round(sum(p["kg"] for p in produits), 2)
    panier = round(total_kg / benef, 2) if benef else None
    ramasse_kg = round(sum(p["kg_ramasse"] for p in produits), 2)
    stock_kg = round(sum(p["kg_stock"] for p in produits), 2)
    ajoutables = []
    ajoutes = {t["article"]: t["kg_stock"] for t in totaux}
    try:
        ajoutes_lots = {r["article"]: r["kg"] for r in conn.execute(
            "SELECT article, SUM(quantite_kg) AS kg FROM frais_ajouts_lots WHERE date_distribution = ? GROUP BY article", (iso,))}
    except Exception:
        ajoutes_lots = {}
    ramasses_kg = {t["article"]: t["kg_ramasse"] for t in totaux}
    deja = set()
    for art, libelle, origine in lignes_ajoutables(conn, iso):
        deja.add(art)
        stock = stock_ajoutable(conn, art, iso)
        ajoutables.append({"article": art, "libelle": libelle, "stock": stock, "stock02": stock_depot02(conn, art),
                           "ajoute": ajoutes.get(art, 0), "ajoute_lots": ajoutes_lots.get(art, 0),
                           "retrait": round(ajoutes.get(art, 0) - ajoutes_lots.get(art, 0), 3),
                           "ramasse": ramasses_kg.get(art, 0),
                           "dans_menu": art in articles_menus, "origine": origine})
    # d'abord les articles de la ramasse du jour, ensuite les autres ; chaque groupe par code article (code VIF)
    ajoutables.sort(key=lambda x: ("ramasse" not in x["origine"], x["article"]))
    return {"nb_passages": len(a_generer), "benef": benef, "ignores": ignores, "ramasse_kg": ramasse_kg,
            "stock_kg": stock_kg, "total_kg": total_kg, "panier": panier, "seuil": seuil,
            "suffisant": panier is not None and panier >= seuil, "produits": produits,
            "hors_menu": hors_menu, "ajoutables": ajoutables}


def _nombre(v):
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


@distribution_bp.route("/bons_prepa/frais/ajout", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def frais_ajout_stock():
    """Fixe la quantité de stock ajoutée à la ramasse du jour pour un article (0 = retirer)."""
    data = request.get_json(silent=True) or {}
    d = _parse_date(data.get("date"))
    article = str(data.get("article") or "").strip()
    qte = _nombre(data.get("quantite_kg"))
    if not d or d != date.today():
        return jsonify(ok=False, erreur="Les ajouts ne se font que pour le jour même."), 400
    if qte is None:
        return jsonify(ok=False, erreur="Quantité invalide."), 400
    conn = get_db_connection()
    try:
        iso = d.isoformat()
        if article not in {a for a, _, _ in lignes_ajoutables(conn, iso)}:
            return jsonify(ok=False, erreur="Ce produit n'est ni dans la liste des produits ajoutables ni dans la ramasse du jour."), 400
        if qte < 0:   # retrait : seulement sur un article de la ramasse, et pas plus que ce qui a été ramassé
            ramasse = (conn.execute("""SELECT COALESCE(SUM(l.quantite_kg), 0) FROM ramasses_jour_lignes l
                                       JOIN ramasses_jour r ON r.id = l.ramasse_id
                                       WHERE r.date_ramasse = ? AND r.statut = 'validee' AND l.article = ?""",
                                    (iso, article)).fetchone() or [0])[0] or 0
            if ramasse <= 0:
                return jsonify(ok=False, erreur="On ne peut retirer que des articles de la ramasse du jour."), 400
            if -qte > ramasse + 0.005:
                return jsonify(ok=False, erreur=f"Retrait impossible : {ramasse:g} kg ramassés seulement.", max=-ramasse), 400
        elif qte > 0:   # un ajout de stock passe par le choix des lots (traçabilité, transfert 02 -> 03)
            return jsonify(ok=False, erreur="Pour ajouter du stock, choisir les lots avec le bouton « Lots… »."), 400
        if abs(qte) < 0.0005:
            conn.execute("DELETE FROM frais_ajouts_stock WHERE date_distribution=? AND article=?", (iso, article))
        else:
            conn.execute("""INSERT INTO frais_ajouts_stock (date_distribution, article, quantite_kg, cree_par)
                            VALUES (?,?,?,?) ON CONFLICT(date_distribution, article)
                            DO UPDATE SET quantite_kg = excluded.quantite_kg, cree_par = excluded.cree_par""",
                         (iso, article, round(qte, 3), getattr(current_user, "email", "") or ""))
        conn.commit()
        s = synthese_frais(conn, d)
        return jsonify(ok=True, total_kg=s["total_kg"], panier=s["panier"], suffisant=s["suffisant"])
    finally:
        conn.close()


@distribution_bp.route("/bons_prepa/frais/ajoutables", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def frais_ajoutables_gerer():
    """Gère la liste des produits ajoutables : {action: 'ajouter'|'retirer', article}."""
    data = request.get_json(silent=True) or {}
    article = str(data.get("article") or "").strip()
    conn = get_db_connection()
    try:
        if data.get("action") == "ajouter":
            if not conn.execute("SELECT 1 FROM articles WHERE article = ?", (article,)).fetchone():
                return jsonify(ok=False, erreur=f"Article inconnu : {article or '(vide)'}"), 400
            ordre = conn.execute("SELECT COALESCE(MAX(ordre), 0) + 1 FROM produits_ajoutables_frais").fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO produits_ajoutables_frais (article, ordre) VALUES (?, ?)", (article, ordre))
        elif data.get("action") == "retirer":
            conn.execute("DELETE FROM produits_ajoutables_frais WHERE article = ?", (article,))
        else:
            return jsonify(ok=False, erreur="Action inconnue"), 400
        conn.commit()
        return jsonify(ok=True)
    finally:
        conn.close()


# ------------------------------------------------------------
# Produits ajoutables : choix des lots (dépôts 02 / 03) et transfert 02 -> 03
# ------------------------------------------------------------
def _coef_net(conn, article: str) -> float:
    r = conn.execute("SELECT coef_kgn_vers_kg FROM articles WHERE article = ?", (article,)).fetchone()
    return r[0] if r and (r[0] or 0) > 0 else 1.0


def _maj_ligne_transfert(conn, iso: str, article: str, lot: str, transfert_kg: float, mvt_id):
    """Crée / met à jour / supprime la ligne de transfert 02 -> 03 dans « Mouvements de stocks » (mvt_stocks_v2,
    brouillon, exportable vers VIF depuis cet écran). Retourne (mvt_id, message d'erreur ou None)."""
    existant = conn.execute("SELECT id, statut, qte_kgn FROM mvt_stocks_v2 WHERE id = ?", (mvt_id,)).fetchone() if mvt_id else None
    qte_kgn = round(transfert_kg / _coef_net(conn, article), 3)
    if transfert_kg <= 0.0005:
        if existant:
            if existant["statut"] != "BROUILLON":
                return mvt_id, "Le transfert a déjà été exporté vers VIF : il ne peut plus être retiré ici (voir Mouvements de stocks)."
            conn.execute("DELETE FROM mvt_stocks_v2 WHERE id = ?", (mvt_id,))
        return None, None
    if existant:
        if existant["statut"] != "BROUILLON":
            if abs((existant["qte_kgn"] or 0) - qte_kgn) > 0.0005:
                return mvt_id, "Le transfert a déjà été exporté vers VIF : sa quantité ne peut plus être modifiée ici."
            return mvt_id, None
        conn.execute("UPDATE mvt_stocks_v2 SET qte_kgn = ? WHERE id = ?", (qte_kgn, mvt_id))
        return mvt_id, None
    num = f"MVT{iso.replace('-', '')}FRAIS"
    cur = conn.execute("""INSERT INTO mvt_stocks_v2 (num_mvt, article, lot, depot_from, depot_to, qte_kgn, qte_col, qte_pal,
                            date_mvt, statut) VALUES (?,?,?,?,?,?,0,0,?, 'BROUILLON')""",
                       (num, article, lot, DEPOT_ORIGINE, DEPOT_SORTIE, qte_kgn, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return cur.lastrowid, None


@distribution_bp.route("/bons_prepa/frais/lots")
@login_required
@require_access("distribution", "lecture")
def frais_lots():
    """Lots en stock (dépôts 02 et 03) d'un article, avec les quantités déjà choisies pour le jour."""
    d = _parse_date(request.args.get("date")) or date.today()
    article = (request.args.get("article") or "").strip()
    conn = get_db_connection()
    try:
        return jsonify(ok=True, lots=lots_stock_frais(conn, article, d.isoformat()),
                       modifiable=d == date.today())
    finally:
        conn.close()


@distribution_bp.route("/bons_prepa/frais/ajout_lot", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def frais_ajout_lot():
    """Fixe, pour un lot d'un dépôt (02 ou 03), la quantité affectée à la distribution FRAIS du jour (kg brut).
    Dépôt 02 : `transfert_kg` = quantité transférée vers le 03 (une palette entière possible, >= quantité affectée)."""
    from .journal_stock import annuler_document, enregistrer_transfert_frais
    data = request.get_json(silent=True) or {}
    d = _parse_date(data.get("date"))
    article = str(data.get("article") or "").strip()
    lot = str(data.get("lot") or "").strip()
    depot = str(data.get("depot") or "").strip()
    qte = _nombre(data.get("quantite_kg"))
    if not d or d != date.today():
        return jsonify(ok=False, erreur="Les ajouts ne se font que pour le jour même."), 400
    if depot not in (DEPOT_ORIGINE, DEPOT_SORTIE):
        return jsonify(ok=False, erreur="Dépôt invalide (02 ou 03)."), 400
    if qte is None or qte < 0:
        return jsonify(ok=False, erreur="Quantité invalide."), 400
    iso = d.isoformat()
    conn = get_db_connection()
    try:
        assurer_table_ajouts_lots(conn)
        if article not in {a for a, _, _ in lignes_ajoutables(conn, iso)}:
            return jsonify(ok=False, erreur="Ce produit n'est ni dans la liste des produits ajoutables ni dans la ramasse du jour."), 400
        lots = {(x["depot"], x["lot"]): x for x in lots_stock_frais(conn, article, iso)}
        info = lots.get((depot, lot))
        if info is None and qte > 0:
            return jsonify(ok=False, erreur="Lot introuvable dans ce dépôt."), 400
        dispo = info["dispo"] if info else 0
        if depot == DEPOT_SORTIE:
            transfert = 0.0
            if qte > dispo + 0.005:
                return jsonify(ok=False, erreur=f"Stock insuffisant : {dispo:g} kg disponibles sur ce lot.", max=dispo), 400
        else:
            transfert = _nombre(data.get("transfert_kg"))
            transfert = qte if transfert is None else transfert
            if transfert + 0.0005 < qte:
                return jsonify(ok=False, erreur="La quantité transférée ne peut pas être inférieure à la quantité affectée."), 400
            if transfert > dispo + 0.005:
                return jsonify(ok=False, erreur=f"Stock insuffisant au dépôt 02 : {dispo:g} kg disponibles sur ce lot.", max=dispo), 400
        ligne = conn.execute("SELECT * FROM frais_ajouts_lots WHERE date_distribution=? AND article=? AND lot=? AND depot=?",
                             (iso, article, lot, depot)).fetchone()
        mvt_id = ligne["mvt_id"] if ligne else None
        if depot == DEPOT_ORIGINE:
            mvt_id, erreur = _maj_ligne_transfert(conn, iso, article, lot, transfert, mvt_id)
            if erreur:
                conn.rollback()
                return jsonify(ok=False, erreur=erreur), 409
        par = getattr(current_user, "email", "") or ""
        if qte < 0.0005 and transfert < 0.0005:
            if ligne:
                annuler_document(conn, "transfert_frais", ligne["id"])
                conn.execute("DELETE FROM frais_ajouts_lots WHERE id = ?", (ligne["id"],))
        else:
            if ligne:
                conn.execute("UPDATE frais_ajouts_lots SET quantite_kg=?, transfert_kg=?, mvt_id=?, cree_par=? WHERE id=?",
                             (round(qte, 3), round(transfert, 3), mvt_id, par, ligne["id"]))
                ligne_id = ligne["id"]
            else:
                ligne_id = conn.execute("""INSERT INTO frais_ajouts_lots (date_distribution, article, lot, depot, quantite_kg,
                                           transfert_kg, mvt_id, cree_par) VALUES (?,?,?,?,?,?,?,?)""",
                                        (iso, article, lot, depot, round(qte, 3), round(transfert, 3), mvt_id, par)).lastrowid
            if depot == DEPOT_ORIGINE:
                enregistrer_transfert_frais(conn, ligne_id)
        conn.commit()
        s = synthese_frais(conn, d)
        return jsonify(ok=True, total_kg=s["total_kg"], panier=s["panier"], suffisant=s["suffisant"])
    finally:
        conn.close()


@distribution_bp.route("/bons_prepa/frais/objectif", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def frais_objectif():
    """Règle l'objectif de panier FRAIS (kg par bénéficiaire), utilisé pour signaler un panier insuffisant."""
    data = request.get_json(silent=True) or {}
    v = _nombre(data.get("valeur"))
    if v is None or not (0 < v <= 50):
        return jsonify(ok=False, erreur="Objectif invalide : un nombre de kg par bénéficiaire entre 0 et 50 (ex. 3 ou 2,5)."), 400
    conn = get_db_connection()
    try:
        if conn.execute("SELECT 1 FROM parametres WHERE param_name = ?", (PARAM_OBJECTIF,)).fetchone():
            conn.execute("UPDATE parametres SET param_value = ? WHERE param_name = ?", (f"{v:g}", PARAM_OBJECTIF))
        else:
            conn.execute("INSERT INTO parametres (param_name, param_value, categorie) VALUES (?, ?, 'distribution')", (PARAM_OBJECTIF, f"{v:g}"))
        conn.commit()
        return jsonify(ok=True, valeur=v)
    finally:
        conn.close()
