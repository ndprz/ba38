# ba38_distribution/frais_repartition.py
"""Préparation des bons FRAIS : synthèse du jour (tonnage de la ramasse, bénéficiaires à servir,
panier par bénéficiaire) et ajout de produits du stock à la ramasse quand le panier est trop faible."""

from datetime import date

from flask import jsonify, request
from flask_login import current_user, login_required

from ba38_utilitaires.core import get_db_connection, require_access

from . import distribution_bp
from .bons_prepa import _parse_date, classer_passages, stock_lots

SEUIL_PANIER_KG = 3.0     # kg par bénéficiaire en dessous duquel on complète avec du stock


def totaux_a_repartir(conn, iso: str) -> list[dict]:
    """Par article : ramasse validée du jour + produits du stock ajoutés pour ce jour."""
    res = {}
    for r in conn.execute("""
            SELECT l.article, MAX(l.libelle) AS libelle, SUM(l.quantite_kg) AS kg
            FROM ramasses_jour_lignes l JOIN ramasses_jour r ON r.id = l.ramasse_id
            WHERE r.date_ramasse = ? AND r.statut = 'validee' GROUP BY l.article""", (iso,)):
        res[r["article"]] = {"article": r["article"], "libelle": r["libelle"] or "", "kg_ramasse": r["kg"] or 0, "kg_stock": 0}
    for r in conn.execute("""
            SELECT a.article, COALESCE(ar.libelle, '') AS libelle, a.quantite_kg FROM frais_ajouts_stock a
            LEFT JOIN articles ar ON ar.article = a.article WHERE a.date_distribution = ?""", (iso,)):
        x = res.setdefault(r["article"], {"article": r["article"], "libelle": r["libelle"], "kg_ramasse": 0, "kg_stock": 0})
        x["kg_stock"] += r["quantite_kg"] or 0
        x["libelle"] = x["libelle"] or r["libelle"]
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
    from .stock_basilic import DEPOT_SORTIE, snapshot_utc
    vif = conn.execute("SELECT COALESCE(SUM(kg_brut), 0) FROM stocks WHERE article = ? AND depot = ?",
                       (article, DEPOT_SORTIE)).fetchone()[0] or 0
    autres_sorties = conn.execute("""
        SELECT COALESCE(SUM(t.quantite), 0) FROM bons_preparation_lots t
        JOIN bons_preparation_lignes l ON l.id = t.ligne_id JOIN bons_preparation b ON b.id = l.bon_id
        WHERE l.article = ? AND t.unite = 'Kg' AND b.statut = 'bl' AND b.valide_le > ?
          AND NOT (b.type_bon = 'frais' AND b.date_distribution = ?)""",
        (article, snapshot_utc(conn), iso)).fetchone()[0] or 0
    return round(max(0.0, vif - autres_sorties), 3)


def lignes_ajoutables(conn, iso: str) -> list[tuple]:
    """(article, libellé, origine) : produits de la liste définie + articles de la ramasse du jour
    (on peut compléter un article ramassé avec du stock). origine = 'liste' | 'ramasse' | 'liste+ramasse'."""
    liste = {r["article"]: (r["libelle"], r["ordre"]) for r in conn.execute(
        """SELECT p.article, COALESCE(a.libelle, '') AS libelle, p.ordre FROM produits_ajoutables_frais p
           LEFT JOIN articles a ON a.article = p.article""")}
    ramasse = {r["article"]: r["libelle"] for r in conn.execute(
        """SELECT l.article, MAX(COALESCE(NULLIF(l.libelle, ''), '')) AS libelle FROM ramasses_jour_lignes l
           JOIN ramasses_jour r ON r.id = l.ramasse_id WHERE r.date_ramasse = ? AND r.statut = 'validee'
           GROUP BY l.article""", (iso,))}
    res = []
    for art in sorted(set(liste) | set(ramasse), key=lambda a: (a not in ramasse, liste.get(a, ("", 9999))[1], a)):
        origine = "liste+ramasse" if (art in liste and art in ramasse) else ("ramasse" if art in ramasse else "liste")
        res.append((art, (liste.get(art) or ("", 0))[0] or ramasse.get(art, ""), origine))
    return res


def synthese_frais(conn, d: date) -> dict:
    """Tout ce qui s'affiche au-dessus des bons FRAIS."""
    iso = d.isoformat()
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
    ramasses_kg = {t["article"]: t["kg_ramasse"] for t in totaux}
    deja = set()
    for art, libelle, origine in lignes_ajoutables(conn, iso):
        deja.add(art)
        stock = stock_ajoutable(conn, art, iso)
        ajoutables.append({"article": art, "libelle": libelle, "stock": stock, "ajoute": ajoutes.get(art, 0),
                           "ramasse": ramasses_kg.get(art, 0),
                           "dans_menu": art in articles_menus, "origine": origine})
    return {"nb_passages": len(a_generer), "benef": benef, "ignores": ignores, "ramasse_kg": ramasse_kg,
            "stock_kg": stock_kg, "total_kg": total_kg, "panier": panier, "seuil": SEUIL_PANIER_KG,
            "suffisant": panier is not None and panier >= SEUIL_PANIER_KG, "produits": produits,
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
        else:
            possible = stock_ajoutable(conn, article, iso)
            if qte > possible + 0.005:
                return jsonify(ok=False, erreur=f"Stock insuffisant : {possible:g} kg disponibles.", max=possible), 400
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
