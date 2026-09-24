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
from ba38_cuisine.utils import _connect, now_paris_str

STATUTS = {"valide": "✅ Validé", "annule": "❌ Annulé", "facture": "💶 Facturé"}
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
    if not bon:
        flash("⛔ Bon de livraison introuvable.", "danger")
        return redirect(url_for("production_cuisine.liste_bons_livraison"))
    return render_template(
        "production_cuisine/bons_livraison_detail.html",
        bon=bon, lignes=lignes, statuts=STATUTS, categories=CATEGORIES,
    )


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
        if bon["statut"] != "valide":
            flash(f"⛔ Le bon {bon['numero']} est {STATUTS.get(bon['statut'], bon['statut']).lower()} : annulation impossible.", "danger")
            return redirect(url_for("production_cuisine.detail_bon_livraison", bon_id=bon_id))
        # Condition sur le statut dans l'UPDATE : pas d'annulation d'un BL
        # facturé entre-temps par un autre poste.
        cur = conn.execute(
            """UPDATE cuisine_bons_livraison
               SET statut = 'annule', date_annulation = ?, user_annulation = ?, motif_annulation = ?
               WHERE id = ? AND statut = 'valide'""",
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
    styles = getSampleStyleSheet()
    petit = styles["Normal"].clone("petit", fontSize=9, leading=11)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm, bottomMargin=15 * mm,
        title=f"Bon de livraison {bon['numero']}",
    )
    elements = []

    # En-tête : logo + titre, puis organisme (gauche) / client (droite)
    logo = None
    logo_path = Path(current_app.root_path) / (org.get("logo_path") or "")
    if org.get("logo_path") and logo_path.exists():
        logo = Image(str(logo_path), width=22 * mm, height=22 * mm, kind="proportional")
    titre = Paragraph(f"<font size=18><b>BON DE LIVRAISON</b></font><br/><font size=11>{bon['numero']}</font>", styles["Normal"])
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

    cartouche = Table(
        [["Date de livraison", "Date d'édition", "Statut"],
         [_date_fr(bon["date_livraison"]), _date_fr(bon["date_creation"]), STATUTS.get(bon["statut"], bon["statut"]).split(" ", 1)[-1]]],
        colWidths=[40 * mm, 40 * mm, 40 * mm],
    )
    cartouche.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    elements += [cartouche, Spacer(1, 6 * mm)]

    # Lignes
    donnees = [["Catégorie", "Recette", "Production", "DLC", "Barquette", "Qté", "Portions"]]
    for l in lignes:
        donnees.append([
            CATEGORIES.get(l["categorie_produit"], "").split(" ", 1)[-1],
            Paragraph(l["libelle_recette"], petit),
            _date_fr(l["date_fin_recette"]),
            _date_fr(l["dlc"]),
            f"{l['taille']} ({l['nb_portions_barquette']}p)",
            str(l["quantite"]),
            str(l["quantite"] * l["nb_portions_barquette"]),
        ])
    nb_barquettes = sum(l["quantite"] for l in lignes)
    donnees.append(["", "Total", "", "", "", str(nb_barquettes), str((bon["portions_carne"] or 0) + (bon["portions_legumes"] or 0))])
    table = Table(donnees, colWidths=[20 * mm, None, 23 * mm, 23 * mm, 24 * mm, 13 * mm, 18 * mm], repeatRows=1)
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
    if bon["prix_portion_carne"] is not None:
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
        [["Remis par (BAI)", "Reçu par (client) — nom, date, signature"], ["\n\n\n", ""]],
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
