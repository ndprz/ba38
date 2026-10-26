# ============================================================
# 🖨️ Récapitulatif d'une production en PDF (A4 portrait) : matières
#     premières, étapes avec heures/températures, refroidissement,
#     conditionnement, validation et non-conformités — la fiche
#     production imprimable pour le classeur HACCP.
# ============================================================

import io
import sqlite3
from pathlib import Path

from flask import current_app, flash, redirect, send_file, url_for
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log
from ba38_utilitaires.organisation import get_organisation
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import (
    _connect, now_paris_str, motifs_non_conformite, heure_dr, duree_refroidissement,
    format_duree, DUREE_REFROIDISSEMENT_MAX,
)

CATEGORIES = {"carne": "Carné", "legumes": "Légumes"}
STATUTS = {"en_cours": "En cours", "terminee": "Terminée", "annulee": "Annulée"}


def _date_fr(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso and len(iso) >= 10 else "—"


def _horaire(valeur, date_production):
    """'14:05', ou '23/10 02:10' si l'étape a eu lieu un autre jour."""
    if not valeur:
        return ""
    if valeur[:10] == date_production:
        return valeur[11:16]
    return f"{valeur[8:10]}/{valeur[5:7]} {valeur[11:16]}"


def _temp(valeur):
    return "" if valeur is None else f"{valeur:g} °C"


def _charger(conn, production_id):
    production = conn.execute(
        "SELECT * FROM cuisine_productions WHERE id = ? AND actif = 1", (production_id,)
    ).fetchone()
    if not production:
        return None
    etapes_ref = conn.execute("SELECT * FROM cuisine_etapes_ref WHERE actif = 1 ORDER BY ordre").fetchall()
    etapes = {}
    for e in conn.execute(
        "SELECT * FROM cuisine_production_etapes WHERE production_id = ? ORDER BY id", (production_id,)
    ).fetchall():
        etapes[e["etape_code"]] = e  # dernière ligne par étape, comme la fiche
    receptions = conn.execute(
        """SELECT r.*, f.nom AS fournisseur_nom, ru.poids_kg AS poids_utilise, ru.forcage
           FROM cuisine_reception_utilisations ru
           JOIN cuisine_receptions r ON r.id = ru.reception_id
           LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
           WHERE ru.production_id = ?
           ORDER BY r.date_reception, r.heure_arrivee, ru.id""",
        (production_id,),
    ).fetchall()
    lots = conn.execute(
        """SELECT l.*, f.nom AS fournisseur_nom FROM cuisine_traca_lots l
           LEFT JOIN fournisseurs f ON f.id = l.fournisseur_id
           WHERE l.production_id = ? ORDER BY l.id""",
        (production_id,),
    ).fetchall()
    quantites = {
        q["taille"]: q["quantite"] for q in conn.execute(
            "SELECT taille, quantite FROM cuisine_production_quantites WHERE production_id = ?", (production_id,)
        ).fetchall()
    }
    libelles_barquettes = {
        r["taille"]: r["libelle"] for r in conn.execute(
            "SELECT taille, libelle FROM cuisine_articles_barquettes WHERE actif = 1"
        ).fetchall()
    }
    validation = conn.execute(
        "SELECT * FROM cuisine_production_validations WHERE production_id = ?", (production_id,)
    ).fetchone()
    return {
        "production": production, "etapes_ref": etapes_ref, "etapes": etapes,
        "receptions": receptions, "lots": lots, "quantites": quantites,
        "libelles_barquettes": libelles_barquettes, "validation": validation,
        "motifs": motifs_non_conformite(conn, production_id),
        "dr": heure_dr(conn, production_id),
        "duree_refroid": duree_refroidissement(conn, production_id),
    }


def generer_pdf_recap_production(d):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    p = d["production"]
    org = get_organisation()
    styles = getSampleStyleSheet()
    petit = styles["Normal"].clone("petit", fontSize=8.5, leading=10.5)
    section = styles["Normal"].clone("section", fontSize=11, leading=14, fontName="Helvetica-Bold", spaceBefore=4)
    gris = colors.HexColor("#eeeeee")
    style_tableau = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), gris),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=14 * mm,
        title=f"Production n°{p['id']} - {p['nom_recette']}",
    )
    largeur = A4[0] - 24 * mm
    edite_le = now_paris_str()

    def pied(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(12 * mm, 8 * mm, f"Production n°{p['id']} - {p['nom_recette']} - édité le {_date_fr(edite_le)} {edite_le[11:16]}")
        canvas.drawRightString(A4[0] - 12 * mm, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    elements = []

    # En-tête
    logo = None
    logo_path = Path(current_app.root_path) / (org.get("logo_path") or "")
    if org.get("logo_path") and logo_path.exists():
        logo = Image(str(logo_path), width=20 * mm, height=20 * mm, kind="proportional")
    titre = Paragraph(
        f"<font size=16><b>RÉCAPITULATIF DE PRODUCTION</b></font><br/>"
        f"<font size=12><b>{p['nom_recette']}</b></font><br/>"
        f"<font size=9>Production n°{p['id']} - {org['nom']}</font>",
        styles["Normal"],
    )
    entete = Table([[logo or "", titre]], colWidths=[26 * mm, None])
    entete.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elements += [entete, Spacer(1, 4 * mm)]

    # Cartouche
    validation = d["validation"]
    valide = validation is not None and validation["date_validation"]
    if d["motifs"]:
        conformite = "NON CONFORME"
    elif p["statut"] == "terminee":
        conformite = "Conforme"
    else:
        conformite = "-"
    cartouche = Table(
        [["Date de production", "Catégorie", "Espèce", "Mode de cuisson", "Sauteuse", "Statut", "Conformité"],
         [_date_fr(p["date_production"]), CATEGORIES.get(p["categorie_produit"], "-"), Paragraph(p["espece"] or "-", petit),
          Paragraph(p["mode_cuisson"] or "-", petit), p["numero_sauteuse"] or "-",
          STATUTS.get(p["statut"], p["statut"]), conformite]],
        colWidths=[28 * mm, 20 * mm, None, 28 * mm, 22 * mm, 20 * mm, 26 * mm],
    )
    cartouche.setStyle(TableStyle(style_tableau + [("ALIGN", (0, 0), (-1, -1), "CENTER")] + (
        [("TEXTCOLOR", (6, 1), (6, 1), colors.red), ("FONTNAME", (6, 1), (6, 1), "Helvetica-Bold")] if d["motifs"] else []
    )))
    elements += [cartouche]
    infos = [f"Créée par : {p['user_creation'] or '-'}"]
    if p["nom_recette_initial"] and p["nom_recette_initial"] != p["nom_recette"]:
        infos.append(f"Recette initiale : {p['nom_recette_initial']}")
    elements += [Paragraph(" - ".join(infos), petit), Spacer(1, 4 * mm)]

    # Matières premières
    elements.append(Paragraph("Matières premières", section))
    if d["receptions"] or d["lots"]:
        lignes = [["Produit", "Fournisseur", "N° lot", "Reçu le", "DLC / DDM", "T° réc.", "Poids utilisé"]]
        for r in d["receptions"]:
            produit = r["ingredient_produit"] or r["libelle_produit"] or "-"
            if r["forcage"]:
                produit += f"<br/><font color='#b02a37'>Forcé : {r['forcage']}</font>"
            dates = " / ".join(filter(None, [
                f"DLC {_date_fr(r['dlc_ddm'])}" if r["dlc_ddm"] else "",
                f"DDM {_date_fr(r['ddm'])}" if r["ddm"] else "",
            ])) or "-"
            lignes.append([
                Paragraph(produit, petit), Paragraph(r["fournisseur_nom"] or "-", petit),
                Paragraph(r["numero_lot"] or "-", petit), _date_fr(r["date_reception"]),
                Paragraph(dates, petit), _temp(r["temperature_mesuree"]),
                "" if r["poids_utilise"] is None else f"{r['poids_utilise']:g} kg",
            ])
        for l in d["lots"]:
            lignes.append([
                Paragraph(f"Lot saisi : {l['commentaire'] or '-'}", petit), Paragraph(l["fournisseur_nom"] or "-", petit),
                "", _date_fr(l["horodatage"]), "", "", "",
            ])
        t = Table(lignes, colWidths=[None, 32 * mm, 24 * mm, 18 * mm, 30 * mm, 15 * mm, 23 * mm], repeatRows=1)
        t.setStyle(TableStyle(style_tableau + [("ALIGN", (5, 1), (-1, -1), "RIGHT")]))
        elements.append(t)
    else:
        elements.append(Paragraph("Aucune matière première rattachée.", petit))
    if p["commentaire_tracabilite"]:
        from xml.sax.saxutils import escape
        texte = escape(p["commentaire_tracabilite"]).replace("\n", "<br/>")
        elements += [Spacer(1, 2 * mm), Paragraph(f"<b>Commentaire traçabilité :</b> {texte}", petit)]
    elements.append(Spacer(1, 4 * mm))

    # Étapes
    elements.append(Paragraph("Étapes de fabrication", section))
    lignes = [["Étape", "Début", "T° début", "Fin", "T° fin", "Cellule", "Conformité", "Par", "Commentaire"]]
    styles_nc = []
    for ref in d["etapes_ref"]:
        e = d["etapes"].get(ref["code"])
        if not e:
            if ref["optionnelle"]:
                continue
            lignes.append([Paragraph(ref["libelle"], petit), "Non renseignée", "", "", "", "", "", "", ""])
            continue
        if e["non_applicable"]:
            lignes.append([Paragraph(ref["libelle"], petit), "Non applicable", "", "", "", "", "", Paragraph(e["user_creation"] or "", petit), ""])
            continue
        conforme = {"conforme": "Conforme", "non_conforme": "NON CONFORME"}.get(e["conforme"], "")
        if e["conforme"] == "non_conforme":
            styles_nc.append(("TEXTCOLOR", (6, len(lignes)), (6, len(lignes)), colors.red))
        lignes.append([
            Paragraph(ref["libelle"], petit),
            _horaire(e["heure_debut"], p["date_production"]), _temp(e["temperature_debut"]),
            _horaire(e["heure_fin"], p["date_production"]) or "en cours", _temp(e["temperature"]),
            "" if e["cellule_numero"] is None else str(e["cellule_numero"]),
            conforme, Paragraph(e["user_modif"] or e["user_creation"] or "", petit),
            Paragraph(e["commentaire"] or "", petit),
        ])
    t = Table(lignes, colWidths=[30 * mm, 19 * mm, 15 * mm, 19 * mm, 15 * mm, 13 * mm, 22 * mm, 20 * mm, None], repeatRows=1)
    t.setStyle(TableStyle(style_tableau + styles_nc))
    elements.append(t)

    refroid = []
    if d["dr"]:
        refroid.append(f"Début de refroidissement (DR, dernier relevé >= 63 °C) : <b>{d['dr']}</b>")
    if d["duree_refroid"] is not None:
        hors_delai = d["duree_refroid"] > DUREE_REFROIDISSEMENT_MAX
        texte = f"Durée de refroidissement (DR - fin de mise en cellule) : <b>{format_duree(d['duree_refroid'])}</b>"
        if hors_delai:
            texte = f"<font color='red'>{texte} (> {format_duree(DUREE_REFROIDISSEMENT_MAX)})</font>"
        refroid.append(texte)
    if refroid:
        elements += [Spacer(1, 2 * mm)] + [Paragraph(x, petit) for x in refroid]
    elements.append(Spacer(1, 4 * mm))

    # Conditionnement et validation
    elements.append(Paragraph("Conditionnement et validation", section))
    tailles = ["1/2", "1/4", "1/8"]
    q = d["quantites"]
    lignes = [[d["libelles_barquettes"].get(tl, f"Barquette {tl}") for tl in tailles] + ["Plat témoin"],
              [str(q.get(tl) or 0) for tl in tailles]]
    if validation and validation["plat_temoin"] == "oui":
        poids = validation["plat_temoin_poids_g"]
        lignes[1].append("Oui" + (f" ({poids:g} g)" if poids else ""))
    else:
        lignes[1].append("Non" if validation else "-")
    t = Table(lignes, colWidths=[largeur / 4] * 4)
    t.setStyle(TableStyle(style_tableau + [("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    elements.append(t)
    if valide:
        texte = f"Validée le {_date_fr(validation['date_validation'])} {validation['date_validation'][11:16]}"
        if validation["valide_par"]:
            texte += f" par {validation['valide_par']}"
        if validation["commentaire"]:
            texte += f" - {validation['commentaire']}"
    else:
        texte = "Validation finale non effectuée."
    elements += [Spacer(1, 2 * mm), Paragraph(texte, petit)]

    if d["motifs"]:
        elements += [Spacer(1, 4 * mm), Paragraph("<font color='red'>Non-conformités</font>", section)]
        elements += [Paragraph(f"<font color='red'>- {m}</font>", petit) for m in d["motifs"]]

    elements += [Spacer(1, 8 * mm)]
    visa = Table([["Visa du responsable (nom, date, signature)"], ["\n\n\n"]], colWidths=[90 * mm], hAlign="RIGHT")
    visa.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    elements.append(visa)

    doc.build(elements, onFirstPage=pied, onLaterPages=pied)
    buffer.seek(0)
    return buffer


@production_cuisine_bp.route("/<int:production_id>/recap.pdf")
@login_required
@require_access("production_cuisine", "lecture")
def recap_production_pdf(production_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        donnees = _charger(conn, production_id)
    if not donnees:
        flash("⛔ Production introuvable.", "danger")
        return redirect(url_for("production_cuisine.liste_productions"))
    try:
        pdf = generer_pdf_recap_production(donnees)
    except Exception as e:
        write_log(f"❌ Erreur PDF récap production {production_id} : {e}")
        flash("❌ Erreur lors de la génération du PDF.", "danger")
        return redirect(url_for("production_cuisine.detail_production", production_id=production_id))
    return send_file(
        pdf, mimetype="application/pdf",
        download_name=f"production_{production_id}_{donnees['production']['date_production']}.pdf",
    )
