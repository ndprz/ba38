# ============================================================
# 🚚 Consignes clients cuisine + simulation de répartition
#     - cuisine_consignes_clients : une consigne par association partenaire
#       (associations.partenaire_cuisine = 'oui') — tailles de barquettes,
#       périodicité, portions carnées/livraison, % légumes, prix portion.
#     - Simulation d'une journée : clients prévus ce jour-là (périodicité)
#       servis sur le stock barquettes actuel, le reste allant au client
#       "reçoit le reliquat" (BAI Dépôt Pinéa). Lecture seule : aucune
#       sortie de stock n'est enregistrée (bons de livraison à venir).
# ============================================================

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
        d["tailles"] = _csv_list(r["tailles_barquettes"])
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
    return render_template("production_cuisine/consignes_clients_liste.html", clients=clients)


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
            "tailles_barquettes": ",".join(tailles),
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

    form["tailles"] = _csv_list(form.get("tailles_barquettes"))
    form["jours"] = _csv_list(form.get("jours_semaine"))
    return render_template(
        "production_cuisine/consignes_clients_form.html",
        association=association, consigne=consigne, form=form,
        tailles=TAILLES, jours_semaine=JOURS_SEMAINE, periodicites=PERIODICITES,
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
    # ses cases cochées / portions modifiées au lieu de la périodicité.
    recalcul = "recalcul" in request.args

    with _connect() as conn:
        clients = [c for c in _clients(conn) if c["a_consigne"]]
        articles = conn.execute(
            "SELECT taille, libelle, nb_portions FROM cuisine_articles_barquettes WHERE actif = 1 ORDER BY nb_portions DESC"
        ).fetchall()
        stock_rows = conn.execute(
            """SELECT a.taille, s.categorie_produit, SUM(s.quantite) AS quantite
               FROM cuisine_stock_barquettes s
               JOIN cuisine_articles_barquettes a ON a.id = s.article_id
               WHERE s.actif = 1 AND a.actif = 1
               GROUP BY a.taille, s.categorie_produit"""
        ).fetchall()

    portions_par_taille = {a["taille"]: a["nb_portions"] for a in articles}
    libelle_taille = {a["taille"]: a["libelle"] for a in articles}
    stock_initial = {cat: {t: 0 for t in portions_par_taille} for cat, _ in CATEGORIES}
    non_classe = 0
    for r in stock_rows:
        if r["categorie_produit"] in stock_initial and r["taille"] in portions_par_taille:
            stock_initial[r["categorie_produit"]][r["taille"]] += r["quantite"] or 0
        else:
            non_classe += r["quantite"] or 0
    stock = {cat: dict(v) for cat, v in stock_initial.items()}

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
            "livre": {}, "portions": {}, "manque": {},
        }
        if inclus:
            for cat, _ in CATEGORIES:
                livre, portions = repartir(ligne["voulu"][cat], c["tailles"], stock[cat], portions_par_taille)
                ligne["livre"][cat] = livre
                ligne["portions"][cat] = portions
                ligne["manque"][cat] = max(0, ligne["voulu"][cat] - portions)
            ligne["montant"] = ligne["portions"]["carne"] * (c["prix_portion_carne"] or 0)
        lignes.append(ligne)

    # Reliquat : tout le stock restant (tailles acceptées si renseignées),
    # seulement si le client reliquat est livré ce jour-là (ou coché).
    ligne_reliquat = None
    if reliquat:
        prevu = livre_le(reliquat, jour)
        inclus = (f"inclure_{reliquat['association_id']}" in request.args) if recalcul else prevu
        ligne_reliquat = {"client": reliquat, "prevu": prevu, "inclus": inclus, "livre": {}, "portions": {}}
        if inclus:
            tailles_ok = reliquat["tailles"] or list(portions_par_taille)
            for cat, _ in CATEGORIES:
                livre = {t: n for t, n in stock[cat].items() if n and t in tailles_ok}
                for t in livre:
                    stock[cat][t] = 0
                ligne_reliquat["livre"][cat] = livre
                ligne_reliquat["portions"][cat] = sum(n * portions_par_taille[t] for t, n in livre.items())
            ligne_reliquat["montant"] = ligne_reliquat["portions"]["carne"] * (reliquat["prix_portion_carne"] or 0)

    def _portions(s):
        return sum(n * portions_par_taille[t] for t, n in s.items())

    totaux = {
        cat: {
            "initial": stock_initial[cat], "reste": stock[cat],
            "portions_initial": _portions(stock_initial[cat]), "portions_reste": _portions(stock[cat]),
        }
        for cat, _ in CATEGORIES
    }
    return render_template(
        "production_cuisine/consignes_clients_simulation.html",
        date_str=date_str, jour_label=dict(JOURS_SEMAINE)[jour.isoweekday()],
        lignes=lignes, ligne_reliquat=ligne_reliquat, totaux=totaux,
        tailles=list(portions_par_taille), libelle_taille=libelle_taille,
        portions_par_taille=portions_par_taille, categories=CATEGORIES,
        non_classe=non_classe,
    )
