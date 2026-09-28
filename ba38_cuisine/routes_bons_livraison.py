# ============================================================
# 🧾 Bons de livraison cuisine
#     - cuisine_bons_livraison / cuisine_bons_livraison_lignes : générés un
#       par un (un client, une date) depuis la simulation de répartition
#       (routes_consignes_clients.generer_bon_livraison).
#     - Le stock disponible est calculé (entrées − lignes des BL non
#       annulés, cf. utils.stock_lignes_disponibles) : annuler un BL remet
#       donc le stock sans autre écriture. Annulation impossible une fois
#       le BL facturé (statut 'facture', facturation à venir).
#     - PDF régénéré à la demande depuis les lignes enregistrées (figées).
#     - Étape préparation : la simulation crée un bon au statut
#       'preparation' (BP-AAAA-NNNN) qui réserve déjà le stock ; il reste
#       modifiable (quantités, autres barquettes, température de livraison)
#       jusqu'à la validation, qui lui donne son numéro BL-AAAA-NNNN.
# ============================================================

import io
import sqlite3
from datetime import date
from pathlib import Path

from flask import (
    current_app, flash, redirect, render_template, request, send_file, url_for,
)
from flask_login import current_user, login_required

from ba38_utilitaires.core import require_access, upload_database, write_log
from ba38_utilitaires.organisation import get_organisation
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import _connect, now_paris_str, parse_temperature, stock_lignes_disponibles

STATUTS = {"preparation": "📋 En préparation", "valide": "✅ Validé", "annule": "❌ Annulé", "facture": "💶 Facturé"}
# Température de livraison : au-delà, alerte (validation possible, signalée
# sur le BL). Plats cuisinés réfrigérés : ≤ 3°C.
TEMPERATURE_LIVRAISON_MAX = 3.0
CATEGORIES = {"carne": "🥩 Carné", "legumes": "🥬 Légumes"}


def _charger_bon(conn, bon_id):
    bon = conn.execute("SELECT * FROM cuisine_bons_livraison WHERE id = ?", (bon_id,)).fetchone()
    if not bon:
        return None, []
    lignes = conn.execute(
        """SELECT * FROM cuisine_bons_livraison_lignes WHERE bon_id = ?
           ORDER BY CASE categorie_produit WHEN 'carne' THEN 0 ELSE 1 END,
                    libelle_recette COLLATE NOCASE, nb_portions_barquette DESC, dlc, date_fin_recette""",
        (bon_id,),
    ).fetchall()
    return bon, lignes


@production_cuisine_bp.route("/bons-livraison")
@login_required
@require_access("production_cuisine", "lecture")
def liste_bons_livraison():
    with _connect() as conn:
        bons = conn.execute(
            """SELECT b.*, COALESCE(SUM(l.quantite), 0) AS nb_barquettes
               FROM cuisine_bons_livraison b
               LEFT JOIN cuisine_bons_livraison_lignes l ON l.bon_id = b.id
               GROUP BY b.id
               ORDER BY b.date_livraison DESC, b.numero DESC
               LIMIT 1000"""
        ).fetchall()
    bons_json = []
    for b in bons:
        d = dict(b)
        d["statut_label"] = STATUTS.get(b["statut"], b["statut"])
        bons_json.append(d)
    return render_template("production_cuisine/bons_livraison_liste.html", bons=bons_json)


@production_cuisine_bp.route("/bons-livraison/<int:bon_id>")
@login_required
@require_access("production_cuisine", "lecture")
def detail_bon_livraison(bon_id):
    with _connect() as conn:
        bon, lignes = _charger_bon(conn, bon_id)
        lots_ajout, max_par_ligne = [], {}
        if bon and bon["statut"] == "preparation":
            lots_ajout, max_par_ligne = _lots_pour_preparation(conn, bon, lignes)
    if not bon:
        flash("⛔ Bon de livraison introuvable.", "danger")
        return redirect(url_for("production_cuisine.liste_bons_livraison"))
    return render_template(
        "production_cuisine/bons_livraison_detail.html",
        bon=bon, lignes=lignes, statuts=STATUTS, categories=CATEGORIES,
        lots_ajout=lots_ajout, max_par_ligne=max_par_ligne,
        temperature_max=TEMPERATURE_LIVRAISON_MAX,
    )


def _lots_pour_preparation(conn, bon, lignes):
    """Pour un bon en préparation : lots de stock qu'on peut encore ajouter
    (disponibles, DLC non dépassée à la date de livraison) et quantité
    maximale de chaque ligne existante (sa quantité + le disponible du lot,
    puisque le disponible calculé déduit déjà ce bon)."""
    dispo = {(l["production_id"], l["article_id"]): dict(l) for l in stock_lignes_disponibles(conn)}
    max_par_ligne = {
        l["id"]: l["quantite"] + max(0, dispo.get((l["production_id"], l["article_id"]), {}).get("disponible", 0))
        for l in lignes
    }
    lots = [
        l for l in dispo.values()
        if l["disponible"] > 0 and not (l["dlc"] and l["dlc"] < bon["date_livraison"])
    ]
    lots.sort(key=lambda l: (0 if l["categorie_produit"] == "carne" else 1,
                             (l["libelle_recette"] or "").strip().lower(), -l["nb_portions"], l["dlc"] or ""))
    return lots, max_par_ligne


def _entier(valeur):
    try:
        return max(0, int(valeur or 0))
    except (TypeError, ValueError):
        return None


@production_cuisine_bp.route("/bons-livraison/<int:bon_id>/preparation", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def enregistrer_preparation(bon_id):
    """Enregistre le bon de préparation (quantités, barquettes ajoutées,
    température) et, si action=valider, le valide : il devient un bon de
    livraison (numéro BL). Tout est contrôlé contre le stock réel sous
    verrou d'écriture."""
    retour = redirect(url_for("production_cuisine.detail_bon_livraison", bon_id=bon_id))
    valider = request.form.get("action") == "valider"
    utilisateur = getattr(current_user, "username", None) or getattr(current_user, "email", None)

    temperature = None
    saisie_temperature = parse_temperature(request.form.get("temperature_livraison"))
    if saisie_temperature is not None:
        try:
            temperature = float(saisie_temperature)
        except ValueError:
            flash(f"⚠️ Température « {request.form.get('temperature_livraison')} » invalide.", "warning")
            return retour

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        bon, lignes = _charger_bon(conn, bon_id)
        if not bon:
            conn.rollback()
            flash("⛔ Bon introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_bons_livraison"))
        if bon["statut"] != "preparation":
            conn.rollback()
            flash(f"⛔ Le bon {bon['numero']} n'est plus en préparation : il ne peut plus être modifié.", "danger")
            return retour

        dispo = {(l["production_id"], l["article_id"]): dict(l) for l in stock_lignes_disponibles(conn)}
        actuel = {}
        for l in lignes:
            cle = (l["production_id"], l["article_id"])
            actuel[cle] = actuel.get(cle, 0) + l["quantite"]

        # Quantités voulues par lot (production × article).
        voulu = dict(actuel)
        for l in lignes:
            q = _entier(request.form.get(f"qte_{l['id']}", l["quantite"]))
            if q is None:
                conn.rollback()
                flash(f"⚠️ Quantité invalide pour {l['libelle_recette']} {l['taille']}.", "warning")
                return retour
            cle = (l["production_id"], l["article_id"])
            voulu[cle] += q - l["quantite"]
        for nom, valeur in request.form.items():
            if not nom.startswith("ajout_") or not valeur.strip():
                continue
            try:
                _, production_id, article_id = nom.split("_")
                cle = (int(production_id), int(article_id))
            except ValueError:
                continue
            q = _entier(valeur)
            if q is None:
                conn.rollback()
                flash("⚠️ Quantité ajoutée invalide.", "warning")
                return retour
            if q:
                lot = dispo.get(cle)
                if not lot or (lot["dlc"] and lot["dlc"] < bon["date_livraison"]):
                    conn.rollback()
                    flash("⛔ Barquettes ajoutées introuvables en stock ou DLC dépassée à la date de livraison.", "danger")
                    return retour
                voulu[cle] = voulu.get(cle, 0) + q

        # Contrôle stock : un lot ne peut pas dépasser ce qu'il contient
        # moins ce que les AUTRES bons ont déjà pris.
        for cle, q in voulu.items():
            maximum = actuel.get(cle, 0) + max(0, dispo.get(cle, {}).get("disponible", 0))
            if q > maximum:
                lot = dispo.get(cle) or next(dict(l) for l in lignes if (l["production_id"], l["article_id"]) == cle)
                flash(f"⛔ Stock insuffisant : {lot['libelle_recette'].strip()} {lot['taille']} — "
                      f"{q} demandée(s), {maximum} possible(s). Rien n'a été enregistré.", "danger")
                conn.rollback()
                return retour

        # Écriture : une ligne par lot (fusion si une barquette ajoutée
        # correspond à un lot déjà présent).
        ligne_par_cle = {}
        for l in lignes:
            cle = (l["production_id"], l["article_id"])
            if cle in ligne_par_cle:
                conn.execute("DELETE FROM cuisine_bons_livraison_lignes WHERE id = ?", (l["id"],))
            else:
                ligne_par_cle[cle] = l["id"]
        for cle, q in voulu.items():
            if cle in ligne_par_cle:
                if q:
                    conn.execute("UPDATE cuisine_bons_livraison_lignes SET quantite = ? WHERE id = ?", (q, ligne_par_cle[cle]))
                else:
                    conn.execute("DELETE FROM cuisine_bons_livraison_lignes WHERE id = ?", (ligne_par_cle[cle],))
            elif q:
                lot = dispo[cle]
                conn.execute(
                    """INSERT INTO cuisine_bons_livraison_lignes
                       (bon_id, production_id, article_id, libelle_recette, categorie_produit,
                        taille, nb_portions_barquette, quantite, date_fin_recette, dlc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (bon_id, lot["production_id"], lot["article_id"], (lot["libelle_recette"] or "").strip(),
                     lot["categorie_produit"], lot["taille"], lot["nb_portions"], q, lot["date_fin_recette"], lot["dlc"]),
                )

        totaux = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN categorie_produit = 'carne' THEN quantite * nb_portions_barquette END), 0),
                      COALESCE(SUM(CASE WHEN categorie_produit = 'legumes' THEN quantite * nb_portions_barquette END), 0),
                      COALESCE(SUM(quantite), 0)
               FROM cuisine_bons_livraison_lignes WHERE bon_id = ?""",
            (bon_id,),
        ).fetchone()
        portions_carne, portions_legumes, nb_barquettes = totaux
        conn.execute(
            """UPDATE cuisine_bons_livraison
               SET portions_carne = ?, portions_legumes = ?, montant = ?, temperature_livraison = ?
               WHERE id = ?""",
            (portions_carne, portions_legumes, portions_carne * (bon["prix_portion_carne"] or 0), temperature, bon_id),
        )

        numero_bl = None
        if valider:
            if temperature is None:
                conn.commit()
                flash("💾 Préparation enregistrée — ⚠️ saisissez la température de livraison pour valider.", "warning")
                upload_database()
                return retour
            if not nb_barquettes:
                conn.commit()
                flash("💾 Préparation enregistrée — ⚠️ aucune barquette : rien à livrer.", "warning")
                upload_database()
                return retour
            from ba38_cuisine.routes_consignes_clients import prochain_numero_bon
            numero_bl = prochain_numero_bon(conn, bon["date_livraison"][:4], "BL")
            conn.execute(
                """UPDATE cuisine_bons_livraison
                   SET statut = 'valide', numero = ?, numero_preparation = ?,
                       date_validation = ?, user_validation = ?
                   WHERE id = ? AND statut = 'preparation'""",
                (numero_bl, bon["numero"], now_paris_str(), utilisateur, bon_id),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        write_log(f"❌ Erreur enregistrement bon de préparation {bon_id} : {e}")
        flash("❌ Erreur lors de l'enregistrement — rien n'a été modifié.", "danger")
        return retour
    finally:
        conn.close()

    upload_database()
    alerte = ""
    if temperature is not None and temperature > TEMPERATURE_LIVRAISON_MAX:
        alerte = f" ⚠️ Température {temperature:g}°C supérieure à {TEMPERATURE_LIVRAISON_MAX:g}°C."
    if numero_bl:
        flash(f"✅ Préparation validée : bon de livraison {numero_bl} généré.{alerte}",
              "warning" if alerte else "success")
    else:
        flash(f"💾 Bon de préparation {bon['numero']} enregistré.{alerte}", "warning" if alerte else "success")
    return retour


@production_cuisine_bp.route("/bons-livraison/<int:bon_id>/annuler", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def annuler_bon_livraison(bon_id):
    motif = (request.form.get("motif") or "").strip() or None
    utilisateur = getattr(current_user, "username", None) or getattr(current_user, "email", None)
    with _connect() as conn:
        bon = conn.execute("SELECT numero, statut FROM cuisine_bons_livraison WHERE id = ?", (bon_id,)).fetchone()
        if not bon:
            flash("⛔ Bon de livraison introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_bons_livraison"))
        if bon["statut"] not in ("valide", "preparation"):
            flash(f"⛔ Le bon {bon['numero']} est {STATUTS.get(bon['statut'], bon['statut']).lower()} : annulation impossible.", "danger")
            return redirect(url_for("production_cuisine.detail_bon_livraison", bon_id=bon_id))
        # Condition sur le statut dans l'UPDATE : pas d'annulation d'un BL
        # facturé entre-temps par un autre poste.
        cur = conn.execute(
            """UPDATE cuisine_bons_livraison
               SET statut = 'annule', date_annulation = ?, user_annulation = ?, motif_annulation = ?
               WHERE id = ? AND statut IN ('valide', 'preparation')""",
            (now_paris_str(), utilisateur, motif, bon_id),
        )
        conn.commit()
    if cur.rowcount:
        upload_database()
        flash(f"↩️ Bon {bon['numero']} annulé — les barquettes sont remises en stock.", "success")
    else:
        flash("⛔ Annulation impossible (le bon a changé de statut entre-temps).", "danger")
    return redirect(url_for("production_cuisine.detail_bon_livraison", bon_id=bon_id))


# ------------------------------------------------------------
# 📄 PDF
# ------------------------------------------------------------
def _date_fr(valeur):
    try:
        return date.fromisoformat((valeur or "")[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return valeur or ""


def generer_pdf_bon_livraison(bon, lignes, association):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    org = get_organisation()
    preparation = bon["statut"] == "preparation"
    type_doc = "BON DE PRÉPARATION" if preparation else "BON DE LIVRAISON"
    styles = getSampleStyleSheet()
    petit = styles["Normal"].clone("petit", fontSize=9, leading=11)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm, bottomMargin=15 * mm,
        title=f"{type_doc.capitalize()} {bon['numero']}",
    )
    elements = []

    # En-tête : logo + titre, puis organisme (gauche) / client (droite)
    logo = None
    logo_path = Path(current_app.root_path) / (org.get("logo_path") or "")
    if org.get("logo_path") and logo_path.exists():
        logo = Image(str(logo_path), width=22 * mm, height=22 * mm, kind="proportional")
    sous_titre = bon["numero"]
    if bon["numero_preparation"]:
        sous_titre += f" (préparation {bon['numero_preparation']})"
    titre = Paragraph(f"<font size=18><b>{type_doc}</b></font><br/><font size=11>{sous_titre}</font>", styles["Normal"])
    entete = Table([[logo or "", titre]], colWidths=[28 * mm, None])
    entete.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elements += [entete, Spacer(1, 6 * mm)]

    bloc_org = "<b>{}</b><br/>{}<br/>{}{}".format(
        org["nom"], (org.get("adresse") or "").replace("\n", "<br/>"),
        f"Tél : {org['tel']}<br/>" if org.get("tel") else "",
        f"E-mail : {org['email']}" if org.get("email") else "",
    )
    adresse_client = ""
    if association:
        adresse_client = "<br/>".join(filter(None, [
            association["adresse_association_1"], association["adresse_association_2"],
            " ".join(filter(None, [association["CP"], association["COMMUNE"]])),
        ]))
    bloc_client = f"<b>Livré à :</b><br/><b>{bon['nom_association']}</b><br/>{adresse_client}"
    infos = Table([[Paragraph(bloc_org, petit), Paragraph(bloc_client, petit)]], colWidths=[90 * mm, None])
    infos.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (1, 0), (1, 0), 0.5, colors.grey),
        ("LEFTPADDING", (1, 0), (1, 0), 6),
    ]))
    elements += [infos, Spacer(1, 5 * mm)]

    temperature = bon["temperature_livraison"]
    if temperature is None:
        texte_temperature = "________ °C" if preparation else "—"
    else:
        texte_temperature = f"{temperature:g} °C"
        if temperature > TEMPERATURE_LIVRAISON_MAX:
            texte_temperature += f" (> {TEMPERATURE_LIVRAISON_MAX:g} °C)"
    cartouche = Table(
        [["Date de livraison", "Date d'édition", "Température livraison", "Statut"],
         [_date_fr(bon["date_livraison"]), _date_fr(bon["date_validation"] or bon["date_creation"]),
          texte_temperature, STATUTS.get(bon["statut"], bon["statut"]).split(" ", 1)[-1]]],
        colWidths=[38 * mm, 38 * mm, 42 * mm, 38 * mm],
    )
    cartouche.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ] + ([("TEXTCOLOR", (2, 1), (2, 1), colors.red), ("FONTNAME", (2, 1), (2, 1), "Helvetica-Bold")]
         if temperature is not None and temperature > TEMPERATURE_LIVRAISON_MAX else [])))
    elements += [cartouche, Spacer(1, 6 * mm)]

    # Lignes
    donnees = [["Catégorie", "Recette", "Production", "DLC", "Barquette", "Qté", "Portions"] + (["Préparé"] if preparation else [])]
    for l in lignes:
        donnees.append([
            CATEGORIES.get(l["categorie_produit"], "").split(" ", 1)[-1],
            Paragraph(l["libelle_recette"], petit),
            _date_fr(l["date_fin_recette"]),
            _date_fr(l["dlc"]),
            f"{l['taille']} ({l['nb_portions_barquette']}p)",
            str(l["quantite"]),
            str(l["quantite"] * l["nb_portions_barquette"]),
        ] + ([""] if preparation else []))
    nb_barquettes = sum(l["quantite"] for l in lignes)
    donnees.append(["", "Total", "", "", "", str(nb_barquettes), str((bon["portions_carne"] or 0) + (bon["portions_legumes"] or 0))]
                   + ([""] if preparation else []))
    largeurs = [20 * mm, None, 23 * mm, 23 * mm, 24 * mm, 13 * mm, 18 * mm] + ([16 * mm] if preparation else [])
    table = Table(donnees, colWidths=largeurs, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (5, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (3, 1), (3, -2), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements += [table, Spacer(1, 5 * mm)]

    # Récapitulatif portions / montant (légumes inclus dans le prix carné)
    recap = [
        ["Portions carnées", str(bon["portions_carne"] or 0)],
        ["Portions légumes (incluses)", str(bon["portions_legumes"] or 0)],
    ]
    if bon["prix_portion_carne"] is not None and not preparation:
        recap += [
            ["Prix par portion carnée", f"{bon['prix_portion_carne']:.2f} €"],
            ["Montant", f"{(bon['montant'] or 0):.2f} €"],
        ]
    t_recap = Table(recap, colWidths=[55 * mm, 30 * mm], hAlign="RIGHT")
    t_recap.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    elements += [t_recap, Spacer(1, 12 * mm)]

    signatures = Table(
        [["Préparé par — nom, date", "Contrôlé par — nom, date"] if preparation
         else ["Remis par (BAI)", "Reçu par (client) — nom, date, signature"], ["\n\n\n", ""]],
        colWidths=[85 * mm, None],
    )
    signatures.setStyle(TableStyle([
        ("BOX", (0, 0), (0, -1), 0.5, colors.grey),
        ("BOX", (1, 0), (1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    elements.append(signatures)
    if bon["statut"] == "annule":
        elements += [Spacer(1, 6 * mm), Paragraph(
            f"<font color='red' size=14><b>BON ANNULÉ le {_date_fr(bon['date_annulation'])}</b></font>", styles["Normal"])]

    doc.build(elements)
    buffer.seek(0)
    return buffer


@production_cuisine_bp.route("/bons-livraison/<int:bon_id>/pdf")
@login_required
@require_access("production_cuisine", "lecture")
def pdf_bon_livraison(bon_id):
    with _connect() as conn:
        bon, lignes = _charger_bon(conn, bon_id)
        association = None
        if bon:
            association = conn.execute(
                "SELECT adresse_association_1, adresse_association_2, CP, COMMUNE FROM associations WHERE id = ?",
                (bon["association_id"],),
            ).fetchone()
    if not bon:
        flash("⛔ Bon de livraison introuvable.", "danger")
        return redirect(url_for("production_cuisine.liste_bons_livraison"))
    try:
        pdf = generer_pdf_bon_livraison(bon, lignes, association)
    except Exception as e:
        write_log(f"❌ Erreur PDF bon de livraison {bon_id} : {e}")
        flash("❌ Erreur lors de la génération du PDF.", "danger")
        return redirect(url_for("production_cuisine.detail_bon_livraison", bon_id=bon_id))
    return send_file(pdf, mimetype="application/pdf", download_name=f"{bon['numero']}.pdf")
