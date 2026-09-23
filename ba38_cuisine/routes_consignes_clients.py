# ============================================================
# 🚚 Consignes clients cuisine + simulation de répartition
#     - cuisine_consignes_clients : une consigne par association partenaire
#       (associations.partenaire_cuisine = 'oui') — tailles de barquettes,
#       périodicité, portions carnées/livraison, % légumes, prix portion.
#     - Simulation d'une journée : clients prévus ce jour-là (périodicité)
#       servis sur le stock barquettes actuel (recettes du jour cochées,
#       panachées de façon équilibrée par client), le reste allant au client
#       "reçoit le reliquat" (BAI Dépôt Pinéa). Lecture seule : aucune
#       sortie de stock n'est enregistrée (bons de livraison à venir).
# ============================================================

import math
import sqlite3
from datetime import date

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import _connect, today_paris
from ba38_cuisine.routes_stock_barquettes import TAILLES

JOURS_SEMAINE = [
    (1, "Lundi"), (2, "Mardi"), (3, "Mercredi"), (4, "Jeudi"),
    (5, "Vendredi"), (6, "Samedi"), (7, "Dimanche"),
]
PERIODICITES = [
    ("hebdomadaire", "Chaque semaine"),
    ("mensuelle", "Une fois par mois"),
    ("a_la_demande", "À la demande / autre"),
]
SEMAINES_DU_MOIS = [(1, "1er"), (2, "2e"), (3, "3e"), (4, "4e"), (5, "Dernier")]
CATEGORIES = (("carne", "🥩 Carné"), ("legumes", "🥬 Légumes"))


def _csv_list(valeur):
    return [v.strip() for v in (valeur or "").split(",") if v.strip()]


def parse_tailles(valeur):
    """"1/2:60,1/4:40" -> {"1/2": 60, "1/4": 40} ; "1/4" -> {"1/4": None}.
    Le pourcentage (part des portions livrées dans chaque taille) n'est
    renseigné que quand plusieurs tailles sont panachées ; format stocké
    dans la colonne tailles_barquettes existante (pas de nouvelle colonne :
    la table est exclue de la synchro dev→prod, qui n'y ajouterait rien)."""
    tailles = {}
    for item in _csv_list(valeur):
        taille, _, pct = item.partition(":")
        tailles[taille.strip()] = int(pct) if pct.strip().isdigit() else None
    return tailles


def _portions_par_taille(conn):
    return {
        r["taille"]: r["nb_portions"]
        for r in conn.execute("SELECT taille, nb_portions FROM cuisine_articles_barquettes WHERE actif = 1")
    }


def libelle_periodicite(consigne):
    """Texte lisible, ex. « Lundi, Mardi » ou « 1er Mardi du mois »."""
    noms = dict(JOURS_SEMAINE)
    jours = ", ".join(noms[int(j)] for j in _csv_list(consigne["jours_semaine"]) if j.isdigit() and int(j) in noms)
    if consigne["periodicite"] == "a_la_demande":
        return "À la demande"
    if consigne["periodicite"] == "mensuelle":
        rang = dict(SEMAINES_DU_MOIS).get(consigne["semaine_du_mois"], "?")
        return f"{rang} {jours or '?'} du mois"
    return jours or "Aucun jour coché"


def livre_le(consigne, jour: date) -> bool:
    """True si la périodicité de la consigne prévoit une livraison ce jour-là."""
    if consigne["periodicite"] == "a_la_demande":
        return False
    if str(jour.isoweekday()) not in _csv_list(consigne["jours_semaine"]):
        return False
    if consigne["periodicite"] == "mensuelle":
        rang = consigne["semaine_du_mois"]
        if rang == 5:
            # Dernier <jour> du mois : +7 jours tombe le mois suivant
            return (jour.day + 7) > _jours_dans_mois(jour)
        return (jour.day - 1) // 7 + 1 == rang
    return True


def _jours_dans_mois(jour: date) -> int:
    suivant = date(jour.year + (jour.month == 12), jour.month % 12 + 1, 1)
    return (suivant - date(jour.year, jour.month, 1)).days


def repartir(portions_voulues, tailles_acceptees, stock, portions_par_taille):
    """Sert `portions_voulues` avec les tailles acceptées, en puisant dans
    `stock` ({taille: nb barquettes}, modifié sur place).

    Règle : plus grosses barquettes d'abord sans dépasser la demande, puis
    si un reliquat de portions reste, UNE barquette de la plus petite taille
    acceptée encore en stock (on arrondit au-dessus plutôt que de livrer
    moins que la consigne). Retourne ({taille: nb}, portions_livrees)."""
    tailles = sorted(
        (t for t in tailles_acceptees if portions_par_taille.get(t)),
        key=lambda t: portions_par_taille[t], reverse=True,
    )
    livre = {}
    reste = portions_voulues
    for t in tailles:
        p = portions_par_taille[t]
        n = min(stock.get(t, 0), reste // p)
        if n:
            livre[t] = livre.get(t, 0) + n
            stock[t] -= n
            reste -= n * p
    if reste > 0:
        for t in reversed(tailles):
            if stock.get(t, 0) > 0:
                livre[t] = livre.get(t, 0) + 1
                stock[t] -= 1
                reste -= portions_par_taille[t]
                break
    portions = sum(n * portions_par_taille[t] for t, n in livre.items())
    return livre, portions


def repartir_selon_pourcentages(portions_voulues, pct_tailles, stock, portions_par_taille):
    """Comme `repartir`, mais en respectant la part (% des portions) de
    chaque taille quand le client en panache plusieurs : chaque taille sauf
    la plus petite reçoit round(portions × % / taille) barquettes, la plus
    petite complète (arrondi au-dessus). Si une taille manque de stock, le
    manque est comblé ensuite avec les autres tailles acceptées."""
    tailles = sorted(
        (t for t in pct_tailles if portions_par_taille.get(t)),
        key=lambda t: portions_par_taille[t], reverse=True,
    )
    if len(tailles) < 2 or any(pct_tailles[t] is None for t in tailles):
        return repartir(portions_voulues, tailles, stock, portions_par_taille)

    livre = {}
    servi = 0
    for i, t in enumerate(tailles):
        p = portions_par_taille[t]
        if i < len(tailles) - 1:
            n = round(portions_voulues * pct_tailles[t] / 100 / p)
        else:
            n = math.ceil(max(0, portions_voulues - servi) / p)
        n = min(n, stock.get(t, 0))
        if n:
            livre[t] = n
            stock[t] -= n
            servi += n * p
    if servi < portions_voulues:
        complement, _ = repartir(portions_voulues - servi, tailles, stock, portions_par_taille)
        for t, n in complement.items():
            livre[t] = livre.get(t, 0) + n
    portions = sum(n * portions_par_taille[t] for t, n in livre.items())
    return livre, portions


def panacher(livre, recettes, portions_par_taille, deja=None):
    """Répartit les barquettes `livre` ({taille: nb}) d'un client entre les
    `recettes` choisies (liste de dicts avec "stock" {taille: nb}, modifié
    sur place) pour un mélange le plus équilibré possible : chaque
    barquette, des plus grosses aux plus petites, va à la recette qui a
    encore du stock dans cette taille et qui totalise le MOINS de portions
    pour ce client (à égalité : la production la plus ancienne).
    Retourne {nom_recette: {taille: nb}}."""
    portions_client = dict(deja or {})
    detail = {}
    for t in sorted(livre, key=lambda t: portions_par_taille[t], reverse=True):
        for _ in range(livre[t]):
            candidates = [r for r in recettes if r["stock"].get(t, 0) > 0]
            if not candidates:
                break
            r = min(candidates, key=lambda r: (portions_client.get(r["nom"], 0), r["date"] or ""))
            r["stock"][t] -= 1
            detail.setdefault(r["nom"], {})
            detail[r["nom"]][t] = detail[r["nom"]].get(t, 0) + 1
            portions_client[r["nom"]] = portions_client.get(r["nom"], 0) + portions_par_taille[t]
    return detail


def _clients(conn):
    """Associations partenaires cuisine + celles qui ont encore une consigne
    sans être (plus) partenaires, avec leur consigne éventuelle."""
    rows = conn.execute(
        """
        SELECT a.id AS association_id, a.nom_association, a.partenaire_cuisine,
               a.jour_de_passage_a_la_BAI, c.id AS consigne_id, c.tailles_barquettes,
               c.periodicite, c.jours_semaine, c.semaine_du_mois, c.nb_portions_carne,
               c.pourcentage_legumes, c.prix_portion_carne, c.recoit_reliquat, c.commentaire
        FROM associations a
        LEFT JOIN cuisine_consignes_clients c ON c.association_id = a.id
        WHERE a.partenaire_cuisine = 'oui' OR c.id IS NOT NULL
        ORDER BY c.recoit_reliquat, a.nom_association
        """
    ).fetchall()
    clients = []
    for r in rows:
        d = dict(r)
        d["a_consigne"] = r["consigne_id"] is not None
        d["pct_tailles"] = parse_tailles(r["tailles_barquettes"])
        d["tailles"] = list(d["pct_tailles"])
        d["periodicite_label"] = libelle_periodicite(r) if d["a_consigne"] else ""
        clients.append(d)
    return clients


# ------------------------------------------------------------
# 📋 Liste des consignes
# ------------------------------------------------------------
@production_cuisine_bp.route("/consignes-clients")
@login_required
@require_access("production_cuisine", "lecture")
def liste_consignes_clients():
    with _connect() as conn:
        clients = _clients(conn)
        portions_par_taille = _portions_par_taille(conn)
    return render_template(
        "production_cuisine/consignes_clients_liste.html",
        clients=clients, portions_par_taille=portions_par_taille,
    )


@production_cuisine_bp.route("/consignes-clients/<int:association_id>", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def modifier_consigne_client(association_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        association = conn.execute(
            "SELECT id, nom_association, partenaire_cuisine, jour_de_passage_a_la_BAI FROM associations WHERE id = ?",
            (association_id,),
        ).fetchone()
        consigne = conn.execute(
            "SELECT * FROM cuisine_consignes_clients WHERE association_id = ?", (association_id,)
        ).fetchone()
        portions_par_taille = _portions_par_taille(conn)

    if not association:
        flash("⛔ Association introuvable.", "danger")
        return redirect(url_for("production_cuisine.liste_consignes_clients"))

    form = dict(consigne) if consigne else {"periodicite": "hebdomadaire", "pourcentage_legumes": 100}

    if request.method == "POST":
        f = request.form
        tailles = [t for t in f.getlist("tailles_barquettes") if t in TAILLES]
        periodicite = f.get("periodicite")
        jours = [j for j in f.getlist("jours_semaine") if j in {str(n) for n, _ in JOURS_SEMAINE}]
        semaine_du_mois = f.get("semaine_du_mois") or None
        recoit_reliquat = 1 if f.get("recoit_reliquat") else 0

        def _nombre(nom, cast):
            valeur = (f.get(nom) or "").strip().replace(",", ".")
            return cast(valeur) if valeur else None

        form = {
            "tailles_barquettes": ",".join(tailles),  # complété avec les % plus bas
            "periodicite": periodicite,
            "jours_semaine": ",".join(jours),
            "semaine_du_mois": int(semaine_du_mois) if semaine_du_mois else None,
            "recoit_reliquat": recoit_reliquat,
            "commentaire": (f.get("commentaire") or "").strip() or None,
        }
        erreurs = []
        try:
            form["nb_portions_carne"] = _nombre("nb_portions_carne", int)
            form["pourcentage_legumes"] = _nombre("pourcentage_legumes", int)
            form["prix_portion_carne"] = _nombre("prix_portion_carne", float)
        except ValueError:
            erreurs.append("Portions, pourcentage et prix doivent être des nombres.")

        if periodicite not in dict(PERIODICITES):
            erreurs.append("Périodicité invalide.")
        if not tailles:
            erreurs.append("Cochez au moins une taille de barquette.")
        elif len(tailles) > 1 and not recoit_reliquat:
            pcts = {}
            for t in tailles:
                valeur = (f.get(f"pct_{t}") or "").strip()
                pcts[t] = int(valeur) if valeur.isdigit() else None
            if any(v is None or v <= 0 for v in pcts.values()):
                erreurs.append("Indiquez le pourcentage (> 0) de chaque taille de barquette panachée.")
            elif sum(pcts.values()) != 100:
                erreurs.append(f"Les pourcentages des tailles doivent faire 100 % (actuellement {sum(pcts.values())} %).")
            form["tailles_barquettes"] = ",".join(
                f"{t}:{pcts[t]}" if pcts[t] is not None else t for t in tailles
            )
        if periodicite in ("hebdomadaire", "mensuelle") and not jours:
            erreurs.append("Cochez au moins un jour de livraison.")
        if periodicite == "mensuelle" and not form["semaine_du_mois"]:
            erreurs.append("Précisez la semaine du mois.")
        if not recoit_reliquat and not form.get("nb_portions_carne"):
            erreurs.append("Le nombre de portions carnées par livraison est obligatoire (sauf client « reliquat »).")

        if erreurs:
            for e in erreurs:
                flash(f"⚠️ {e}", "warning")
        else:
            if periodicite != "mensuelle":
                form["semaine_du_mois"] = None
            if periodicite == "a_la_demande":
                form["jours_semaine"] = ""
            utilisateur = getattr(current_user, "username", None) or getattr(current_user, "email", None)
            colonnes = ["tailles_barquettes", "periodicite", "jours_semaine", "semaine_du_mois",
                        "nb_portions_carne", "pourcentage_legumes", "prix_portion_carne",
                        "recoit_reliquat", "commentaire"]
            valeurs = [form[c] for c in colonnes]
            try:
                with _connect() as conn:
                    if recoit_reliquat:
                        # Un seul client reliquat à la fois
                        conn.execute(
                            "UPDATE cuisine_consignes_clients SET recoit_reliquat = 0 WHERE association_id != ?",
                            (association_id,),
                        )
                    if consigne:
                        conn.execute(
                            f"""UPDATE cuisine_consignes_clients
                                SET {", ".join(c + " = ?" for c in colonnes)},
                                    user_modif = ?, date_modif = datetime('now','utc')
                                WHERE association_id = ?""",
                            valeurs + [utilisateur, association_id],
                        )
                    else:
                        conn.execute(
                            f"""INSERT INTO cuisine_consignes_clients
                                (association_id, {", ".join(colonnes)}, user_creation)
                                VALUES (?, {", ".join("?" for _ in colonnes)}, ?)""",
                            [association_id] + valeurs + [utilisateur],
                        )
                    conn.commit()
                upload_database()
                flash(f"✅ Consigne enregistrée pour {association['nom_association']}.", "success")
                return redirect(url_for("production_cuisine.liste_consignes_clients"))
            except Exception as e:
                write_log(f"❌ Erreur enregistrement consigne cuisine (association {association_id}) : {e}")
                flash("❌ Erreur lors de l'enregistrement.", "danger")

    form["pct_tailles"] = parse_tailles(form.get("tailles_barquettes"))
    form["jours"] = _csv_list(form.get("jours_semaine"))
    return render_template(
        "production_cuisine/consignes_clients_form.html",
        association=association, consigne=consigne, form=form,
        tailles=TAILLES, portions_par_taille=portions_par_taille, jours_semaine=JOURS_SEMAINE, periodicites=PERIODICITES,
        semaines_du_mois=SEMAINES_DU_MOIS,
    )


@production_cuisine_bp.route("/consignes-clients/<int:association_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_consigne_client(association_id):
    with _connect() as conn:
        conn.execute("DELETE FROM cuisine_consignes_clients WHERE association_id = ?", (association_id,))
        conn.commit()
    upload_database()
    flash("🗑️ Consigne supprimée.", "success")
    return redirect(url_for("production_cuisine.liste_consignes_clients"))


# ------------------------------------------------------------
# 🧮 Simulation de répartition d'une journée
# ------------------------------------------------------------
@production_cuisine_bp.route("/consignes-clients/simulation")
@login_required
@require_access("production_cuisine", "lecture")
def simulation_repartition():
    date_str = request.args.get("date") or today_paris()
    try:
        jour = date.fromisoformat(date_str)
    except ValueError:
        jour = date.fromisoformat(today_paris())
        date_str = jour.isoformat()
    # "recalcul" présent = l'utilisateur a validé le formulaire : on respecte
    # ses cases cochées (clients, recettes) / portions modifiées au lieu de
    # la périodicité et de "toutes les recettes".
    recalcul = "recalcul" in request.args

    with _connect() as conn:
        clients = [c for c in _clients(conn) if c["a_consigne"]]
        articles = conn.execute(
            "SELECT taille, libelle, nb_portions FROM cuisine_articles_barquettes WHERE actif = 1 ORDER BY nb_portions DESC"
        ).fetchall()
        # Stock par recette (même nom = même recette, toutes productions
        # confondues ; la plus ancienne sert à départager le panachage).
        stock_rows = conn.execute(
            """SELECT TRIM(s.libelle_recette) AS nom, s.categorie_produit, a.taille,
                      SUM(s.quantite) AS quantite, MIN(s.date_fin_recette) AS date
               FROM cuisine_stock_barquettes s
               JOIN cuisine_articles_barquettes a ON a.id = s.article_id
               WHERE s.actif = 1 AND a.actif = 1
               GROUP BY TRIM(s.libelle_recette) COLLATE NOCASE, s.categorie_produit, a.taille"""
        ).fetchall()

    portions_par_taille = {a["taille"]: a["nb_portions"] for a in articles}
    libelle_taille = {a["taille"]: a["libelle"] for a in articles}
    categories = dict(CATEGORIES)

    recettes = {}
    non_classe = 0
    for r in stock_rows:
        if r["categorie_produit"] not in categories or r["taille"] not in portions_par_taille:
            non_classe += r["quantite"] or 0
            continue
        cle = f"{r['categorie_produit']}|{r['nom']}"
        rec = recettes.setdefault(cle, {
            "cle": cle, "nom": r["nom"], "cat": r["categorie_produit"], "date": r["date"],
            "initial": {t: 0 for t in portions_par_taille},
        })
        rec["initial"][r["taille"]] += r["quantite"] or 0
        rec["date"] = min(filter(None, [rec["date"], r["date"]]), default=None)
    recettes = sorted(recettes.values(), key=lambda r: (r["cat"], r["date"] or "", r["nom"].lower()))

    choisies = set(request.args.getlist("recettes")) if recalcul else {r["cle"] for r in recettes}
    for r in recettes:
        r["choisie"] = r["cle"] in choisies
        r["stock"] = dict(r["initial"])
    recettes_du_jour = {cat: [r for r in recettes if r["choisie"] and r["cat"] == cat] for cat in categories}

    def _stock_cumule(cat):
        cumul = {t: 0 for t in portions_par_taille}
        for r in recettes_du_jour[cat]:
            for t, n in r["stock"].items():
                cumul[t] += n
        return cumul

    def _portions(s):
        return sum(n * portions_par_taille[t] for t, n in s.items())

    reliquat = next((c for c in clients if c["recoit_reliquat"]), None)
    lignes = []
    for c in clients:
        if c is reliquat:
            continue
        prevu = livre_le(c, jour)
        aid = c["association_id"]
        if recalcul:
            inclus = f"inclure_{aid}" in request.args
            try:
                portions_carne = int(request.args[f"portions_{aid}"])
            except (KeyError, ValueError):
                portions_carne = c["nb_portions_carne"] or 0
        else:
            inclus = prevu
            portions_carne = c["nb_portions_carne"] or 0
        pct = c["pourcentage_legumes"] if c["pourcentage_legumes"] is not None else 100
        ligne = {
            "client": c, "prevu": prevu, "inclus": inclus,
            "voulu": {"carne": portions_carne, "legumes": round(portions_carne * pct / 100)},
            "livre": {}, "detail": {}, "portions": {}, "manque": {},
        }
        if inclus:
            for cat in categories:
                livre, portions = repartir_selon_pourcentages(
                    ligne["voulu"][cat], c["pct_tailles"], _stock_cumule(cat), portions_par_taille,
                )
                ligne["livre"][cat] = livre
                ligne["detail"][cat] = panacher(livre, recettes_du_jour[cat], portions_par_taille)
                ligne["portions"][cat] = portions
                ligne["manque"][cat] = max(0, ligne["voulu"][cat] - portions)
            ligne["montant"] = ligne["portions"]["carne"] * (c["prix_portion_carne"] or 0)
        lignes.append(ligne)

    # Reliquat : tout le reste des recettes du jour (tailles acceptées si
    # renseignées), seulement si le client reliquat est livré ce jour-là
    # (ou coché). Les recettes non choisies restent en stock.
    ligne_reliquat = None
    if reliquat:
        prevu = livre_le(reliquat, jour)
        inclus = (f"inclure_{reliquat['association_id']}" in request.args) if recalcul else prevu
        ligne_reliquat = {"client": reliquat, "prevu": prevu, "inclus": inclus,
                          "livre": {}, "detail": {}, "portions": {}}
        if inclus:
            tailles_ok = reliquat["tailles"] or list(portions_par_taille)
            for cat in categories:
                detail = {}
                for r in recettes_du_jour[cat]:
                    part = {t: n for t, n in r["stock"].items() if n and t in tailles_ok}
                    for t in part:
                        r["stock"][t] = 0
                    if part:
                        detail[r["nom"]] = part
                livre = {t: sum(d.get(t, 0) for d in detail.values()) for t in portions_par_taille}
                livre = {t: n for t, n in livre.items() if n}
                ligne_reliquat["livre"][cat] = livre
                ligne_reliquat["detail"][cat] = detail
                ligne_reliquat["portions"][cat] = _portions(livre)
            ligne_reliquat["montant"] = ligne_reliquat["portions"]["carne"] * (reliquat["prix_portion_carne"] or 0)

    for r in recettes:
        r["portions_initial"] = _portions(r["initial"])
        r["portions_reste"] = _portions(r["stock"])
    totaux = {
        cat: {
            "portions_initial": sum(r["portions_initial"] for r in recettes if r["cat"] == cat),
            "portions_choisies": sum(r["portions_initial"] for r in recettes_du_jour[cat]),
            "portions_reste": sum(r["portions_reste"] for r in recettes if r["cat"] == cat),
        }
        for cat in categories
    }
    return render_template(
        "production_cuisine/consignes_clients_simulation.html",
        date_str=date_str, jour_label=dict(JOURS_SEMAINE)[jour.isoweekday()],
        lignes=lignes, ligne_reliquat=ligne_reliquat, recettes=recettes, totaux=totaux,
        tailles=list(portions_par_taille), libelle_taille=libelle_taille,
        portions_par_taille=portions_par_taille, categories=CATEGORIES,
        non_classe=non_classe,
    )
