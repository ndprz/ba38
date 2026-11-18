# ba38_distribution/ramasse_jour.py
"""Ramasse journalière = la feuille de ramasse des camions.

Une ramasse = un jour + un fournisseur (magasin). Elle reprend le modèle papier : familles de
produits paramétrables (chacune avec son code VIF ; 8 par défaut) et 4 poids (kg) par famille :
  quantite_kg     « entrée bonne date à distribuer » : marchandise OK, c'est ELLE qui entre en stock
  poids_jour      DLC = date du jour : non distribuable, sauf à l'association qui distribue dans la journée
                  (pour l'instant les Restos du Cœur, qui viennent en fin de matinée)
  poids_rebut     pas OK ; mouvement de stock spécial à définir (règle à valider)
  poids_3etoiles  déposé directement à la cuisine 3 étoiles ; BL vers le partenaire à définir (à valider)

Les magasins du jour viennent du planning ramasse (plannings_ramasse -> tournée -> magasins + camion),
la feuille est imprimable en PDF pour les camions, et la saisie s'enregistre cellule par cellule.

À la validation (seul poids « bonne date » > 0) :
  - entrée en stock Basilic (lot fournisseur+article+date de 15 car., dépôt 03) ;
  - fichier de réception VIF (CSV, format recepba38) conservé avec la ramasse.
Le tonnage validé du jour est ensuite réparti sur les bons de préparation FRAIS.
⚠️ jour / rebut / 3 étoiles sont enregistrés mais n'ont encore AUCUN effet (stock, VIF, BL)."""

import re
from datetime import date, datetime, timedelta
from io import BytesIO

from flask import flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from ba38_utilitaires.core import get_db_connection, has_access, require_access

from . import distribution_bp
from .bons_prepa import _parse_date

# Familles de produits de la feuille : (code VIF, libellé imprimé), dans l'ordre de la feuille.
# Paramétrables depuis la page (bouton « Articles Ramasse », table ramasse_familles) ; cette liste ne sert plus
# que de valeur par défaut tant que rien n'a été enregistré (ou si la table n'existe pas encore).
FAMILLES_DEFAUT = [
    ("0110001", "PAIN"),
    ("0110041", "VIENNOISERIE EPICERIE"),
    ("4320001", "PRODUITS LAITIERS"),
    ("4420000", "FRUITS"),
    ("4520000", "LEGUMES"),
    ("4620001", "VIANDE"),
    ("4220031", "Plts cuisinés/autres"),
    ("4920001", "POISSON REFRIGERE"),
]


def charger_familles(conn, avec_inactives=False):
    """Familles de la feuille, dans l'ordre : liste de dicts {article, libelle, actif}.
    Valeurs par défaut (FAMILLES_DEFAUT) si la table est absente ou vide."""
    try:
        lignes = [dict(r) for r in conn.execute(
            "SELECT article, libelle, COALESCE(actif, 'oui') AS actif FROM ramasse_familles ORDER BY ordre, id")]
    except Exception:  # table pas encore créée
        lignes = []
    if not lignes:
        lignes = [{"article": c, "libelle": l, "actif": "oui"} for c, l in FAMILLES_DEFAUT]
    return lignes if avec_inactives else [f for f in lignes if f["actif"] != "non"]


def _enregistrer_familles(conn, familles):
    """Remplace la liste des familles (ordre = ordre reçu). Retourne un message d'erreur ou None."""
    propres, vus = [], set()
    for f in familles or []:
        code = str((f or {}).get("article") or "").strip()
        libelle = str((f or {}).get("libelle") or "").strip()
        if not code or not libelle:
            return "Chaque article ramasse doit avoir un article et un libellé."
        if len(libelle) > 60:
            return f"Libellé trop long (60 caractères max) : {libelle[:20]}…"
        if code in vus:
            return f"Article en double : {code}."
        if not conn.execute("SELECT 1 FROM articles WHERE article = ?", (code,)).fetchone():
            return f"Article inconnu : {code}."
        vus.add(code)
        propres.append((code, libelle, "non" if (f or {}).get("actif") == "non" else "oui"))
    if not any(p[2] == "oui" for p in propres):
        return "Au moins un article ramasse doit rester actif."
    conn.execute("""CREATE TABLE IF NOT EXISTS ramasse_familles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, article TEXT NOT NULL UNIQUE, libelle TEXT NOT NULL,
        ordre INTEGER NOT NULL DEFAULT 0, actif TEXT NOT NULL DEFAULT 'oui')""")
    conn.execute("DELETE FROM ramasse_familles")
    conn.executemany("INSERT INTO ramasse_familles (article, libelle, ordre, actif) VALUES (?,?,?,?)",
                     [(c, l, i, a) for i, (c, l, a) in enumerate(propres, 1)])
    conn.commit()
    return None


# (colonne de ramasses_jour_lignes, titre imprimé sur la feuille)
COLONNES = [
    ("quantite_kg", "POIDS entrée bonne date à distribuer"),
    ("poids_jour", "POIDS date du jour"),
    ("poids_rebut", "POIDS rebut"),
    ("poids_3etoiles", "POIDS donné 3 étoiles"),
]
NOTES_POIDS = ["POIDS 1 CARTON OU CAGETTE : 1 KG", "POIDS 1 CAISSE ROUGE : 1,7 KG"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
POIDS_MAX = 100000.0

_COLS = {c for c, _ in COLONNES}
_DLC = re.compile(r"\d{2}/\d{2}/\d{4}")


def lot_ramasse_jour(d: date, article: str, libelle: str, id_fournisseur: int) -> str:
    """Lot de traçabilité fournisseur / article / jour, 15 caractères (limite VIF) :
    id fournisseur Basilic (3) + article (7) + chiffre des unités de l'année (1) + MMJJ (4).
    Ex. fournisseur 12, article 0110001, le 16/11/2026 -> 012011000161116.
    Vide pour un article « non loti ». Alerte sanitaire : retrouver magasin + article + jour."""
    if "non loti" in (libelle or "").lower():
        return ""
    return f"{int(id_fournisseur):03d}{article}{d.year % 10}{d:%m%d}"


def _nombre(v):
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _poids(texte):
    """'12,5' -> 12.5 ; '' -> None ; ValueError si invalide, négatif ou énorme."""
    brut = (str(texte) if texte is not None else "").replace(",", ".").replace(" ", "").strip()
    if brut == "":
        return None
    v = float(brut)  # ValueError si texte invalide
    if v != v or v < 0 or v > POIDS_MAX:
        raise ValueError("hors limites")
    return v


def _fmt(v):
    """12.5 -> '12,5' ; 30.0 -> '30' ; None -> ''."""
    if v is None:
        return ""
    return ("%.2f" % v).rstrip("0").rstrip(".").replace(".", ",")


def _cell(v):
    """Valeur d'une case de poids : vide si non renseigné ou nul."""
    return "" if not v else _fmt(v)


def totaux_du_jour(conn, iso: str) -> list[dict]:
    """Tonnage validé du jour par article (à répartir sur les bons FRAIS)."""
    return [dict(r) for r in conn.execute("""
        SELECT l.article, MAX(l.libelle) AS libelle, SUM(l.quantite_kg) AS kg
        FROM ramasses_jour_lignes l JOIN ramasses_jour r ON r.id = l.ramasse_id
        WHERE r.date_ramasse = ? AND r.statut = 'validee' AND l.quantite_kg > 0
        GROUP BY l.article ORDER BY l.article""", (iso,))]


# ---------------------------------------------------------------- planning ramasse
def _etiquette_camion(t):
    if not t.get("camion"):
        return ""
    return f"{t['camion']}" + (f" ({t['immat']})" if t.get("immat") else "")


def tournees_du_jour(conn, d: date) -> list[dict]:
    """Tournées du planning ramasse pour ce jour (année + semaine ISO, jour de la semaine),
    avec camion et magasins dans l'ordre de passage."""
    annee, semaine, _ = d.isocalendar()
    jour = JOURS[d.weekday()]
    tournees = [dict(r) for r in conn.execute(
        """SELECT p.tournee_id, MIN(p.tournee) AS libelle, c.nom AS camion, c.immat
           FROM plannings_ramasse p LEFT JOIN camions c ON c.id = p.camion_id
           WHERE p.annee = ? AND p.semaine = ? AND p.jour = ? AND p.tournee_id IS NOT NULL
           GROUP BY p.tournee_id ORDER BY MIN(p.tournee)""", (annee, semaine, jour))]
    for t in tournees:
        t["camion_txt"] = _etiquette_camion(t)
        t["magasins"] = [dict(r) for r in conn.execute(
            """SELECT f.id, COALESCE(f.nom, tf.nom) AS nom, f.code_vif, tf.ordre
               FROM tournees_fournisseurs tf JOIN fournisseurs f ON f.id = tf.fournisseur_id
               WHERE tf.tournee_id = ? ORDER BY tf.ordre, f.nom COLLATE NOCASE""", (t["tournee_id"],))]
    return tournees


# ---------------------------------------------------------------- écran
def _carte(four: dict, ram: dict | None, familles_feuille) -> dict:
    """Données d'affichage d'un magasin : les 8 familles de la feuille (première ligne de la ramasse
    pour chaque code) puis les autres lignes (autres articles, ou doublons d'anciennes saisies)."""
    lignes = ram["lignes"] if ram else []
    premiere = {}
    for l in lignes:
        premiere.setdefault(l["article"], l)
    familles, utilises = [], set()
    for fam in familles_feuille:
        code, libelle = fam["article"], fam["libelle"]
        l = premiere.get(code)
        if l:
            utilises.add(l["id"])
        familles.append({"code": code, "libelle": libelle, "ligne": l})
    return {
        "id_fournisseur": four["id"], "nom": four["nom"], "code_vif": (four.get("code_vif") or "").strip(),
        "ram": ram, "statut": ram["statut"] if ram else "vide",
        "familles": familles, "autres": [l for l in lignes if l["id"] not in utilises],
        "totaux": {c: sum((l[c] or 0) for l in lignes) for c, _ in COLONNES},
        "nb_lignes": len(lignes),
    }


@distribution_bp.route("/ramasse_jour")
@login_required
@require_access("distribution", "lecture")
def ramasse_jour():
    d = _parse_date(request.args.get("date")) or date.today()
    conn = get_db_connection()
    try:
        ramasses = [dict(r) for r in conn.execute("""
            SELECT r.*, COALESCE(f.nom, '?') AS fournisseur, f.code_vif AS code_vif_fournisseur
            FROM ramasses_jour r LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
            WHERE r.date_ramasse = ? ORDER BY f.nom COLLATE NOCASE""", (d.isoformat(),))]
        for r in ramasses:
            r["lignes"] = [dict(x) for x in conn.execute(
                "SELECT * FROM ramasses_jour_lignes WHERE ramasse_id = ? ORDER BY ordre, id", (r["id"],))]
            r["total_kg"] = sum(x["quantite_kg"] or 0 for x in r["lignes"])
        par_frs = {r["id_fournisseur"]: r for r in ramasses}

        # magasins du planning ramasse : une carte par magasin, même si la ramasse n'est pas encore créée
        familles_feuille = charger_familles(conn)
        familles_config = charger_familles(conn, avec_inactives=True)
        tournees, vus = tournees_du_jour(conn, d), set()
        for t in tournees:
            t["cartes"] = []
            for m in t["magasins"]:
                if m["id"] in vus:
                    continue
                vus.add(m["id"])
                t["cartes"].append(_carte(m, par_frs.get(m["id"]), familles_feuille))
        autres_cartes = [
            _carte({"id": r["id_fournisseur"], "nom": r["fournisseur"], "code_vif": r["code_vif_fournisseur"]}, r, familles_feuille)
            for r in ramasses if r["id_fournisseur"] not in vus]
        affiches = vus | set(par_frs)
        fournisseurs = [dict(r) for r in conn.execute(
            """SELECT id, nom, code_vif FROM fournisseurs
               WHERE COALESCE(LOWER(actif), 'oui') != 'non' ORDER BY nom COLLATE NOCASE""") if r["id"] not in affiches]
        articles = [dict(r) for r in conn.execute("SELECT article, libelle FROM articles ORDER BY article")]
        totaux = totaux_du_jour(conn, d.isoformat())
        from .frais_repartition import totaux_a_repartir   # ramasse validée +/- ajouts de stock et retraits (page des bons FRAIS)
        a_repartir = [t for t in totaux_a_repartir(conn, d.isoformat()) if t["kg"] > 0 or t["kg_stock"]]
        nb_bons_frais = conn.execute(
            "SELECT COUNT(*) FROM bons_preparation WHERE date_distribution = ? AND type_bon = 'frais'",
            (d.isoformat(),)).fetchone()[0]
    finally:
        conn.close()
    return render_template(
        "distribution/ramasse_jour.html", date_choisie=d, jour=JOURS[d.weekday()], tournees=tournees,
        autres_cartes=autres_cartes, fournisseurs=fournisseurs, articles=articles, totaux=totaux,
        total_kg=sum(t["kg"] for t in totaux), a_repartir=a_repartir, total_a_repartir=round(sum(t["kg"] for t in a_repartir), 3), nb_bons_frais=nb_bons_frais, familles_config=familles_config, colonnes=COLONNES,
        notes=NOTES_POIDS, cell=_cell, fmt=_fmt, aujourdhui=date.today().isoformat(),
        peut_ecrire=has_access("distribution", "ecriture"))


def _reponse(ok, **kw):
    return jsonify(ok=ok, **kw), (200 if ok else 400)


def _sans_code_vif(nom):
    return (f"Saisie interdite : {nom} n'a pas de code VIF : le saisir dans sa fiche fournisseur "
            "(cocher « Sans coordonnées » s'il n'y a pas d'e-mail, sinon la fiche ne s'enregistre pas).")


def _totaux_ramasse(conn, rid):
    row = conn.execute(
        """SELECT COALESCE(SUM(quantite_kg),0) AS quantite_kg, COALESCE(SUM(poids_jour),0) AS poids_jour,
                  COALESCE(SUM(poids_rebut),0) AS poids_rebut, COALESCE(SUM(poids_3etoiles),0) AS poids_3etoiles
           FROM ramasses_jour_lignes WHERE ramasse_id = ?""", (rid,)).fetchone()
    return row


def _ramasse_ou_creer(conn, data, par):
    """La ramasse désignée par `ramasse_id`, ou (date + id_fournisseur) créée si elle n'existe pas encore.
    Retourne (ramasse, None) ou (None, réponse d'erreur)."""
    if data.get("ramasse_id"):
        ram = conn.execute("SELECT * FROM ramasses_jour WHERE id = ?", (data.get("ramasse_id"),)).fetchone()
        return (ram, None) if ram else (None, _reponse(False, erreur="Ramasse introuvable"))
    d = _parse_date(data.get("date"))
    try:
        fid = int(data.get("id_fournisseur"))
    except (TypeError, ValueError):
        fid = 0
    frs0 = conn.execute("SELECT nom, code_vif FROM fournisseurs WHERE id = ?", (fid,)).fetchone()
    if not d or not frs0:
        return None, _reponse(False, erreur="Date ou fournisseur invalide")
    if not (frs0["code_vif"] or "").strip():
        return None, _reponse(False, erreur=_sans_code_vif(frs0["nom"]))
    conn.execute("INSERT OR IGNORE INTO ramasses_jour (date_ramasse, id_fournisseur, cree_par) VALUES (?,?,?)",
                 (d.isoformat(), fid, par))
    ram = conn.execute("SELECT * FROM ramasses_jour WHERE date_ramasse = ? AND id_fournisseur = ?",
                       (d.isoformat(), fid)).fetchone()
    return ram, None


def _action_cellule(conn, data, par):
    """Enregistre UNE cellule de la feuille (sauvegarde automatique). Crée la ramasse et la ligne
    au besoin ; supprime la ligne quand elle redevient vide."""
    col = data.get("colonne")
    if col not in _COLS and col != "dlc":
        return _reponse(False, erreur="Colonne inconnue")
    code = str(data.get("article") or "").strip()
    art = conn.execute("SELECT article, libelle FROM articles WHERE article = ?", (code,)).fetchone()
    if not art:
        return _reponse(False, erreur=f"Article inconnu : {code or '(vide)'}")

    # la ramasse (créée à la première cellule saisie)
    ram, erreur = _ramasse_ou_creer(conn, data, par)
    if erreur:
        return erreur
    frs = conn.execute("SELECT nom, code_vif FROM fournisseurs WHERE id = ?", (ram["id_fournisseur"],)).fetchone()
    if not frs or not (frs["code_vif"] or "").strip():
        return _reponse(False, erreur=_sans_code_vif(frs["nom"] if frs else "fournisseur inconnu"))
    if ram["statut"] != "saisie":
        return _reponse(False, erreur="Ramasse validée : la rouvrir pour la modifier.")
    rid = ram["id"]

    # la valeur
    if col == "dlc":
        valeur = str(data.get("valeur") or "").strip()
        if valeur and not _DLC.fullmatch(valeur):
            return _reponse(False, erreur="DLC : jj/mm/aaaa")
    else:
        try:
            valeur = _poids(data.get("valeur"))
        except ValueError:
            return _reponse(False, erreur="Poids invalide : un nombre positif en kg (ex. 12,5).")

    # la ligne (une par article ; la première s'il existe d'anciens doublons)
    ligne = None
    if data.get("ligne_id"):
        ligne = conn.execute("SELECT * FROM ramasses_jour_lignes WHERE id = ? AND ramasse_id = ?",
                             (data.get("ligne_id"), rid)).fetchone()
    if not ligne:
        ligne = conn.execute("SELECT * FROM ramasses_jour_lignes WHERE ramasse_id = ? AND article = ? "
                             "ORDER BY ordre, id LIMIT 1", (rid, art["article"])).fetchone()
    if not ligne:
        ordre = conn.execute("SELECT COALESCE(MAX(ordre),0)+1 FROM ramasses_jour_lignes WHERE ramasse_id = ?",
                             (rid,)).fetchone()[0]
        cur = conn.execute("INSERT INTO ramasses_jour_lignes (ramasse_id, ordre, article, libelle, quantite_kg) "
                           "VALUES (?,?,?,?,0)", (rid, ordre, art["article"], art["libelle"] or ""))
        ligne = conn.execute("SELECT * FROM ramasses_jour_lignes WHERE id = ?", (cur.lastrowid,)).fetchone()
    lid = ligne["id"]

    # `col` vient de la liste blanche (_COLS ou « dlc ») : pas d'injection possible
    if col == "quantite_kg":
        conn.execute("UPDATE ramasses_jour_lignes SET quantite_kg = ? WHERE id = ?", (valeur or 0, lid))
        # le lot (traçabilité) apparaît dès qu'une quantité est saisie et disparaît quand elle revient à zéro ;
        # un lot d'une autre origine (ramasse importée d'un fichier VIF) n'est jamais écrasé ni effacé
        lot_calcule = lot_ramasse_jour(datetime.strptime(ram["date_ramasse"], "%Y-%m-%d").date(),
                                       art["article"], art["libelle"], ram["id_fournisseur"])
        if valeur and ligne["lot"] is None:
            conn.execute("UPDATE ramasses_jour_lignes SET lot = ? WHERE id = ?", (lot_calcule, lid))
        elif not valeur and ligne["lot"] is not None and ligne["lot"] == lot_calcule:
            conn.execute("UPDATE ramasses_jour_lignes SET lot = NULL WHERE id = ?", (lid,))
    else:
        conn.execute(f"UPDATE ramasses_jour_lignes SET {col} = ? WHERE id = ?", (valeur, lid))

    ligne = conn.execute("SELECT * FROM ramasses_jour_lignes WHERE id = ?", (lid,)).fetchone()
    vide = (not ligne["quantite_kg"] and not ligne["poids_jour"] and not ligne["poids_rebut"]
            and not ligne["poids_3etoiles"] and not (ligne["dlc"] or "").strip()
            and not (ligne["commentaire"] or "").strip() and ligne["lot"] is None)
    if vide:
        conn.execute("DELETE FROM ramasses_jour_lignes WHERE id = ?", (lid,))
    conn.commit()
    tot = _totaux_ramasse(conn, rid)
    return _reponse(True, ramasse_id=rid, ligne_id=None if vide else lid, lot=ligne["lot"] or "",
                    valeur=(valeur if col == "dlc" else _cell(valeur)) or "",
                    totaux={c: _fmt(tot[c]) or "0" for c, _ in COLONNES}, total_ok=tot["quantite_kg"])


@distribution_bp.route("/ramasse_jour/action", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def ramasse_jour_action():
    """Actions JSON : creer, cellule, ajouter_ligne, suppr_ligne, valider, rouvrir, supprimer."""
    from ba38_vif.generation import ErreursVif, generer_reception_ramasse_jour

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    par = getattr(current_user, "email", "") or ""
    conn = get_db_connection()
    try:
        if action == "cellule":
            return _action_cellule(conn, data, par)

        if action == "familles":
            erreur = _enregistrer_familles(conn, data.get("familles"))
            return _reponse(False, erreur=erreur) if erreur else _reponse(True)

        if action == "creer":
            d = _parse_date(data.get("date"))
            fid = int(data.get("id_fournisseur") or 0)
            frs = conn.execute("SELECT nom, code_vif FROM fournisseurs WHERE id=?", (fid,)).fetchone()
            if not d or not frs:
                return _reponse(False, erreur="Date ou fournisseur invalide")
            if not (frs["code_vif"] or "").strip():
                return _reponse(False, erreur=_sans_code_vif(frs["nom"]))
            conn.execute("INSERT OR IGNORE INTO ramasses_jour (date_ramasse, id_fournisseur, cree_par) VALUES (?,?,?)",
                         (d.isoformat(), fid, par))
            conn.commit()
            return _reponse(True)

        rid = data.get("ramasse_id")
        if action == "ajouter_ligne" and not rid:   # feuille pas encore créée : on la crée avec la ligne
            ram0, erreur = _ramasse_ou_creer(conn, data, par)
            if erreur:
                return erreur
            rid = ram0["id"]
        if action == "suppr_ligne":
            r = conn.execute("""SELECT r.id, r.statut FROM ramasses_jour r JOIN ramasses_jour_lignes l ON l.ramasse_id = r.id
                                WHERE l.id = ?""", (data.get("ligne_id"),)).fetchone()
            rid = r["id"] if r else None
        ram = conn.execute("SELECT * FROM ramasses_jour WHERE id = ?", (rid,)).fetchone() if rid else None
        if not ram:
            return _reponse(False, erreur="Ramasse introuvable")

        if action == "rouvrir":
            if ram["statut"] != "validee":
                return _reponse(False, erreur="Ramasse non validée")
            utilise = conn.execute("""
                SELECT 1 FROM bons_preparation_lots t
                JOIN bons_preparation_lignes bl ON bl.id = t.ligne_id
                JOIN bons_preparation b ON b.id = bl.bon_id
                WHERE b.statut = 'bl' AND t.lot IN (SELECT lot FROM ramasses_jour_lignes WHERE ramasse_id = ? AND lot != '')
                LIMIT 1""", (rid,)).fetchone()
            if utilise:
                return _reponse(False, erreur="Des bons de livraison utilisent déjà les lots de cette ramasse.")
            if ram["reception_depose_le"]:
                return _reponse(False, erreur="Le fichier de réception a déjà été déposé pour VIF.")
            reserves = [r[0] for r in conn.execute("""
                SELECT DISTINCT b.id FROM bons_preparation_lots t
                JOIN bons_preparation_lignes bl ON bl.id = t.ligne_id
                JOIN bons_preparation b ON b.id = bl.bon_id
                WHERE b.statut = 'preparation' AND t.lot IN (SELECT lot FROM ramasses_jour_lignes WHERE ramasse_id = ? AND lot != '')""", (rid,))]
            conn.execute("""UPDATE ramasses_jour SET statut='saisie', valide_le=NULL, valide_par=NULL,
                            reception_fichier=NULL, reception_csv=NULL WHERE id=?""", (rid,))
            from .journal_stock import annuler_document
            annuler_document(conn, "ramasse_jour", rid)
            if reserves:   # des bons de préparation réservaient des lots de cette ramasse : on les réserve à nouveau sur le stock restant
                from .bons_prepa import reserver_lots_bon
                for bid in reserves:
                    reserver_lots_bon(conn, bid)
            conn.commit()
            return _reponse(True)

        if ram["statut"] != "saisie":
            return _reponse(False, erreur="Ramasse validée : la rouvrir pour la modifier.")

        if action == "ajouter_ligne":   # pas de saisie sans code VIF du fournisseur
            frs0 = conn.execute("SELECT nom, code_vif FROM fournisseurs WHERE id=?", (ram["id_fournisseur"],)).fetchone()
            if not frs0 or not (frs0["code_vif"] or "").strip():
                return _reponse(False, erreur=_sans_code_vif(frs0["nom"] if frs0 else "fournisseur inconnu"))

        if action == "supprimer":
            conn.execute("DELETE FROM ramasses_jour_lignes WHERE ramasse_id=?", (rid,))
            conn.execute("DELETE FROM ramasses_jour WHERE id=?", (rid,))
        elif action == "ajouter_ligne":
            code = str(data.get("article") or "").strip()
            art = conn.execute("SELECT article, libelle FROM articles WHERE article=?", (code,)).fetchone()
            qte = _nombre(data.get("quantite_kg"))
            if not art:
                return _reponse(False, erreur=f"Article inconnu : {code or '(vide)'}")
            if qte is None or qte <= 0:
                return _reponse(False, erreur="Poids invalide (kg > 0)")
            if conn.execute("SELECT 1 FROM ramasses_jour_lignes WHERE ramasse_id=? AND article=?", (rid, art["article"])).fetchone():
                return _reponse(False, erreur=f"L'article {art['article']} est déjà sur la feuille : modifier sa ligne.")
            ordre = conn.execute("SELECT COALESCE(MAX(ordre),0)+1 FROM ramasses_jour_lignes WHERE ramasse_id=?",
                                 (rid,)).fetchone()[0]
            lot = lot_ramasse_jour(datetime.strptime(ram["date_ramasse"], "%Y-%m-%d").date(),
                                   art["article"], art["libelle"], ram["id_fournisseur"])
            conn.execute("""INSERT INTO ramasses_jour_lignes (ramasse_id, ordre, article, libelle, quantite_kg, dlc, lot)
                            VALUES (?,?,?,?,?,?,?)""",
                         (rid, ordre, art["article"], art["libelle"] or "", qte, (data.get("dlc") or "").strip(), lot))
        elif action == "suppr_ligne":
            conn.execute("DELETE FROM ramasses_jour_lignes WHERE id=?", (data.get("ligne_id"),))
        elif action == "valider":
            # seules les lignes avec un poids « entrée bonne date » > 0 entrent en stock ;
            # jour / rebut / 3 étoiles n'ont encore aucun effet (règles à valider)
            lignes = conn.execute("SELECT * FROM ramasses_jour_lignes WHERE ramasse_id=? AND quantite_kg > 0 "
                                  "ORDER BY ordre, id", (rid,)).fetchall()
            frs = conn.execute("SELECT nom, code_vif FROM fournisseurs WHERE id=?", (ram["id_fournisseur"],)).fetchone()
            d = datetime.strptime(ram["date_ramasse"], "%Y-%m-%d").date()
            if not (frs["code_vif"] or "").strip():
                return _reponse(False, erreur=(
                    f"{frs['nom']} n'a pas de code VIF. Le saisir dans sa fiche fournisseur ; s'il n'y a pas d'e-mail, "
                    "cocher « Sans coordonnées », sinon la fiche ne s'enregistre pas et le code est perdu."))
            if not lignes:
                return _reponse(False, erreur="Aucun poids « entrée bonne date » à valider.")
            # lot déjà renseigné (ex. ramasse importée d'un fichier VIF) conservé ; sinon fournisseur + article + date (voir lot_ramasse_jour)
            a_stocker = [(l["id"], l["lot"] if l["lot"] is not None else lot_ramasse_jour(d, l["article"], l["libelle"], ram["id_fournisseur"]))
                         for l in lignes]
            csv_lignes = [{"article": l["article"], "quantite_kg": l["quantite_kg"], "lot": lot}
                          for l, (_, lot) in zip(lignes, a_stocker)]
            try:
                csv = generer_reception_ramasse_jour((frs["code_vif"] or "").strip(), d, csv_lignes)
            except ErreursVif as e:
                return _reponse(False, erreur=" ; ".join(e.messages))
            nom = f"recepba38_{d:%Y%m%d}_{frs['code_vif']}_{rid:06d}.csv"
            for lid, lot in a_stocker:
                conn.execute("UPDATE ramasses_jour_lignes SET lot=? WHERE id=?", (lot, lid))
            conn.execute("""UPDATE ramasses_jour SET statut='validee', valide_le=CURRENT_TIMESTAMP, valide_par=?,
                            reception_fichier=?, reception_csv=? WHERE id=?""", (par, nom, csv, rid))
            from .journal_stock import enregistrer_ramasse
            enregistrer_ramasse(conn, rid)
        else:
            return _reponse(False, erreur="Action inconnue")
        conn.commit()
        return _reponse(True)
    finally:
        conn.close()


@distribution_bp.route("/ramasse_jour/<int:rid>/reception.csv")
@login_required
@require_access("distribution", "lecture")
def ramasse_jour_reception_csv(rid):
    conn = get_db_connection()
    try:
        r = conn.execute("SELECT reception_fichier, reception_csv, date_ramasse FROM ramasses_jour WHERE id=?",
                         (rid,)).fetchone()
    finally:
        conn.close()
    if not r or not r["reception_csv"]:
        flash("Aucun fichier de réception pour cette ramasse.", "warning")
        return redirect(url_for("distribution.ramasse_jour"))
    return send_file(BytesIO(r["reception_csv"].encode("utf-8")), mimetype="text/csv",
                     as_attachment=True, download_name=r["reception_fichier"])


# ---------------------------------------------------------------- PDF à imprimer pour les camions
@distribution_bp.route("/ramasse_jour/pdf")
@login_required
@require_access("distribution", "lecture")
def ramasse_jour_pdf():
    """Feuilles vierges pré-remplies (date, magasin, code VIF, camion) à imprimer pour les camions.
    ?date=AAAA-MM-JJ (défaut : aujourd'hui) pour un jour ;
    ?semaine=AAAA-Www (planning ramasse) pour toute la semaine, ou &jour=lundi… pour un seul jour de cette semaine ;
    &tournee_id=N pour un seul camion ; &f=ID (répétable) pour une sélection de magasins
    (y compris hors planning). Une feuille A4 par magasin, chacune avec SA date."""
    tournee_id = request.args.get("tournee_id", type=int)
    choisis = {int(x) for x in request.args.getlist("f") if x.isdigit()}

    semaine = (request.args.get("semaine") or "").strip()
    if semaine:
        try:
            annee, num = (int(x) for x in semaine.split("-W"))
            lundi = date.fromisocalendar(annee, num, 1)
        except (ValueError, TypeError):
            flash("Semaine invalide.", "danger")
            return redirect(url_for("distribution.ramasse_jour"))
        jour = (request.args.get("jour") or "").strip().lower()
        if jour and jour not in JOURS:
            flash("Jour invalide.", "danger")
            return redirect(url_for("distribution.ramasse_jour"))
        dates = [lundi + timedelta(days=JOURS.index(jour))] if jour else [lundi + timedelta(days=i) for i in range(7)]
        retour = {"date": dates[0].isoformat()}
        nom_fichier = f"feuilles_ramasse_{semaine}{'_' + jour if jour else ''}.pdf"
    else:
        d = _parse_date(request.args.get("date")) or date.today()
        dates = [d]
        retour = {"date": d.isoformat()}
        nom_fichier = f"feuilles_ramasse_{d.isoformat()}.pdf"

    conn = get_db_connection()
    try:
        familles_pdf = [(f["article"], f["libelle"]) for f in charger_familles(conn)]
        feuilles = []
        for d in dates:
            vus = set()   # un magasin n'a qu'une feuille par jour
            for t in tournees_du_jour(conn, d):
                if tournee_id and t["tournee_id"] != tournee_id:
                    continue
                for m in t["magasins"]:
                    if (choisis and m["id"] not in choisis) or m["id"] in vus:
                        continue
                    vus.add(m["id"])
                    feuilles.append({"nom": m["nom"], "code_vif": m["code_vif"], "tournee": t["libelle"],
                                     "camion": t["camion_txt"], "date_txt": d.strftime("%d/%m/%Y")})
            if choisis and not tournee_id and not semaine:  # magasins cochés hors planning du jour
                for r in conn.execute(
                        f"SELECT id, nom, code_vif FROM fournisseurs WHERE id IN ({','.join('?' * len(choisis))}) "
                        "ORDER BY nom COLLATE NOCASE", tuple(choisis)):
                    if r["id"] not in vus:
                        feuilles.append({"nom": r["nom"], "code_vif": r["code_vif"], "tournee": "Hors planning",
                                         "camion": "", "date_txt": d.strftime("%d/%m/%Y")})
    finally:
        conn.close()

    if not feuilles:
        flash("Aucun magasin au planning ramasse pour cette date : rien à imprimer.", "warning")
        return redirect(url_for("distribution.ramasse_jour", **retour))

    from weasyprint import HTML
    html = render_template("distribution/ramasse_jour_pdf.html", feuilles=feuilles,
                           familles=familles_pdf, colonnes=COLONNES, notes=NOTES_POIDS)
    buf = BytesIO()
    HTML(string=html).write_pdf(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", download_name=nom_fichier)
