"""Génération des PDF du module CERFA abandon de frais :
- reçu fiscal CERFA n° 11580*04 (bénévoles imposables), même mise en page
  que le reçu historique issu du publipostage Word ;
- courrier de remboursement forfaitaire tickets TAG (non-imposables).

Les PDF sont régénérés à la demande depuis la base (aucun fichier conservé),
signés avec l'image de signature de la table `organisation`."""

import io
import os
from datetime import datetime
from pathlib import Path

from flask import current_app
from PyPDF2 import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ba38_utilitaires.organisation import get_organisation

from ba38_cerfa.calculs import montant_en_lettres
from ba38_cerfa.constants import MOIS_FR_MINUSCULE


def date_longue(valeur):
    if not valeur:
        d = datetime.now()
    elif isinstance(valeur, datetime):
        d = valeur
    else:
        d = datetime.fromisoformat(str(valeur)[:19])
    return f"{d.day} {MOIS_FR_MINUSCULE[d.month - 1]} {d.year}"


def _chemin_image(relatif):
    if not relatif:
        return None
    # Racine de l'application, puis repli sur BASE_PATH (anciens chemins
    # d'images enregistrés avant que les images soient copiées par instance)
    for racine in (current_app.root_path, os.getenv("BASE_PATH", "/srv/ba38")):
        chemin = Path(racine) / relatif
        if chemin.exists():
            return str(chemin)
    return None


def _civilite_longue(civilite):
    c = (civilite or "").strip()
    return {"M": "Monsieur", "M.": "Monsieur", "Mme": "Madame", "Mlle": "Madame"}.get(c, c)


def _case(c, x, y, cochee, taille=3.2 * mm):
    if cochee:
        c.setFillColor(colors.black)
        c.rect(x, y, taille, taille, stroke=1, fill=1)
    else:
        c.setStrokeColor(colors.HexColor("#1f3864"))
        c.rect(x, y, taille, taille, stroke=1, fill=0)
        c.setStrokeColor(colors.black)


def _titre_grise(c, texte, x_centre, y, taille=13):
    c.setFont("Times-Roman", taille)
    largeur = c.stringWidth(texte, "Times-Roman", taille)
    c.setFillColor(colors.HexColor("#d9d9d9"))
    c.rect(x_centre - largeur / 2 - 1 * mm, y - 1.3 * mm, largeur + 2 * mm, taille * 1.05, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.drawCentredString(x_centre, y, texte)


# ============================================================================
# 🧾 CERFA 11580*04 — reçu au titre des dons (abandon de frais)
# ============================================================================
def generer_cerfa_pdf(d):
    """d : numero, civilite, nom, prenom, rue, complement_adresse, code_postal,
    ville, email, montant_arrondi, annee_frais, date_document. Retourne bytes."""
    org = get_organisation()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Reçu fiscal {d['numero']}")
    W, H = A4
    gauche, droite = 25 * mm, W - 15 * mm
    centre = (gauche + droite) / 2

    # --- En-tête -----------------------------------------------------------
    logo = _chemin_image(org.get("logo_path"))
    if logo:
        c.drawImage(ImageReader(logo), 30 * mm, H - 38 * mm, 18 * mm, 18 * mm,
                    preserveAspectRatio=True, mask="auto")

    c.setFont("Times-Roman", 13.5)
    c.drawString(57 * mm, H - 27 * mm, "Reçu au titre des dons à certains organismes")
    c.drawString(57 * mm, H - 33 * mm, "d'intérêt général")
    c.setFont("Times-Roman", 9.5)
    c.drawString(57 * mm, H - 38 * mm, "(Articles 200, 238 bis et 885-0 V bis A du Code Général des impôts)")

    c.setFont("Times-Roman", 7)
    c.drawString(150 * mm, H - 18 * mm, "Numéro d'ordre du reçu")
    c.rect(150 * mm, H - 31 * mm, 38 * mm, 9 * mm)
    c.setFont("Times-Roman", 12)
    c.drawString(153 * mm, H - 28 * mm, d["numero"])

    c.setFont("Times-Roman", 9)
    c.drawString(gauche - 3 * mm, H - 46 * mm, "Selon cerfa  N° 11580*04")

    # --- Bénéficiaire ------------------------------------------------------
    haut = H - 48 * mm
    bas_benef = H - 98 * mm
    c.rect(gauche, bas_benef, droite - gauche, haut - bas_benef)
    _titre_grise(c, "Bénéficiaire des versements", centre, haut - 7 * mm)

    adresse_org = [l.strip() for l in (org.get("adresse") or "").splitlines() if l.strip()]
    y = haut - 14 * mm
    c.setFont("Times-Roman", 13)
    c.drawCentredString(centre, y, (org.get("nom") or "").upper())
    c.setFont("Times-Roman", 10)
    for i, ligne in enumerate(adresse_org):
        y -= 5 * mm
        c.setFont("Times-Roman", 12 if i == len(adresse_org) - 1 else 10)
        c.drawCentredString(centre, y, ligne.upper() if i == len(adresse_org) - 1 else ligne)
    y -= 5 * mm
    c.setFont("Times-Roman", 9)
    c.drawCentredString(centre, y, f"N° SIREN {org.get('siren') or ''}")
    c.setLineWidth(0.5)
    c.line(gauche + 2 * mm, y - 3 * mm, droite - 15 * mm, y - 3 * mm)

    c.setFont("Times-Roman", 10)
    _case(c, gauche + 8 * mm, bas_benef + 9 * mm, True)
    c.drawString(gauche + 15 * mm, bas_benef + 9.3 * mm, "Œuvre ou organisme d'intérêt général")
    _case(c, gauche + 8 * mm, bas_benef + 3.5 * mm, True)
    c.drawString(gauche + 15 * mm, bas_benef + 3.8 * mm, "Association fournissant gratuitement une aide alimentaire")

    # --- Donateur ----------------------------------------------------------
    haut_don = bas_benef
    bas_don = haut_don - 32 * mm
    c.rect(gauche, bas_don, droite - gauche, haut_don - bas_don)
    _titre_grise(c, "Donateur", centre, haut_don - 7 * mm)
    lignes_donateur = [
        f"{_civilite_longue(d.get('civilite'))}   {(d.get('nom') or '').upper()}  {d.get('prenom') or ''}".strip(),
        (d.get("rue") or "").strip(),
        (d.get("complement_adresse") or "").strip(),
        f"{d.get('code_postal') or ''}  {(d.get('ville') or '').upper()}".strip(),
        (d.get("email") or "").strip(),
    ]
    y = haut_don - 12.5 * mm
    c.setFont("Times-Roman", 10)
    for ligne in [l for l in lignes_donateur if l]:
        c.drawString(gauche + 20 * mm, y, ligne)
        y -= 4.3 * mm

    # --- Montant -------------------------------------------------------------
    haut_m = bas_don
    bas_m = haut_m - 100 * mm
    c.rect(gauche, bas_m, droite - gauche, haut_m - bas_m)
    x = gauche + 3 * mm
    y = haut_m - 6 * mm
    c.setFont("Times-Roman", 10)
    c.drawString(x, y, "Le bénéficiaire reconnaît avoir reçu au titre des dons et versements, ouvrant droit à réduction d'impôt,")
    y -= 4.5 * mm
    c.drawString(x, y, "la somme de :")
    y -= 7 * mm
    montant_txt = f"{d['montant_arrondi']}  Euros"
    c.setFont("Times-Bold", 12)
    lm = c.stringWidth(montant_txt, "Times-Bold", 12)
    c.drawCentredString(centre, y, montant_txt)
    c.setFont("Times-Roman", 8)
    c.drawRightString(centre - lm / 2 - 3 * mm, y, "*********")
    c.drawString(centre + lm / 2 + 3 * mm, y, "****")

    y -= 12 * mm
    c.setFont("Times-Roman", 10)
    prefixe = "Somme en toutes lettres : "
    lettres = montant_en_lettres(d["montant_arrondi"])
    lp = c.stringWidth(prefixe, "Times-Roman", 10)
    ll = c.stringWidth(lettres, "Times-Bold", 11)
    x0 = centre - (lp + ll) / 2
    c.drawString(x0, y, prefixe)
    c.setFont("Times-Bold", 11)
    c.drawString(x0 + lp, y, lettres)

    y -= 11 * mm
    c.setFont("Times-Roman", 10)
    prefixe = "Date du versement ou du don : "
    c.drawString(x, y, prefixe)
    c.setFont("Times-Bold", 10)
    c.drawString(x + c.stringWidth(prefixe, "Times-Roman", 10), y, f"Année {d['annee_frais']}")

    y -= 10 * mm
    c.setFont("Times-Roman", 10)
    c.drawString(x, y, "Le bénéficiaire certifie sur l'honneur que les dons et versements qu'il reçoit ouvrent droit à la réduction")
    y -= 10 * mm
    c.drawString(x, y, "d'impôt prévue à l'article (3) :")
    xa = x + 52 * mm
    for libelle, cochee in (("200 du CGI", True), ("238 bis du CGI", False), ("885-0 V bis A du CGI", False)):
        _case(c, xa, y - 0.5 * mm, cochee)
        c.drawString(xa + 5 * mm, y, libelle)
        xa += 5 * mm + c.stringWidth(libelle, "Times-Roman", 10) + 8 * mm

    y -= 8 * mm
    c.setLineWidth(0.8)
    c.line(x, y, droite - 5 * mm, y)
    y -= 6 * mm
    c.drawString(x, y, "Forme du don :")
    y -= 5 * mm
    _case(c, x + 6 * mm, y - 0.5 * mm, False)
    c.drawString(x + 11 * mm, y, "Déclaration de don manuel")
    _case(c, x + 75 * mm, y - 0.5 * mm, True)
    c.drawString(x + 80 * mm, y, "Autres")

    y -= 5 * mm
    c.setLineWidth(0.3)
    c.setStrokeColor(colors.HexColor("#bfbfbf"))
    c.line(x, y, droite - 5 * mm, y)
    c.setStrokeColor(colors.black)
    y -= 6 * mm
    c.drawString(x, y, "Nature du don :")
    y -= 5 * mm
    _case(c, x + 6 * mm, y - 0.5 * mm, False)
    c.drawString(x + 11 * mm, y, "Numéraire")
    _case(c, x + 75 * mm, y - 0.5 * mm, True)
    c.drawString(x + 80 * mm, y, "Autres (4)")

    y -= 5 * mm
    c.setStrokeColor(colors.HexColor("#bfbfbf"))
    c.line(x, y, droite - 5 * mm, y)
    c.setStrokeColor(colors.black)
    y -= 6 * mm
    c.drawString(x, y, "En cas de don en numéraire, mode de versement du don :")

    # --- Notes -------------------------------------------------------------
    y = bas_m - 3.5 * mm
    c.setFont("Times-Roman", 6.8)
    notes = [
        "(3) L'organisme bénéficiaire peut cocher une ou plusieurs cases.",
        "      Il est rappelé que la délivrance irrégulière de reçus fiscaux par l'organisme bénéficiaire est susceptible de donner lieu, en application des",
        "      dispositions de l'article 1740 A du code général des impôts, à une amende égale à 25 % des sommes indûment mentionnées sur ces",
        "      documents.",
        "(4) Notamment : abandon de revenus ou de produits ; frais engagés par les bénévoles, dont ils renoncent expressément au remboursement.",
    ]
    for ligne in notes:
        c.drawString(gauche - 3 * mm, y, ligne)
        y -= 3 * mm

    # --- Date et signature -------------------------------------------------
    y -= 4 * mm
    c.setFont("Times-Roman", 10)
    c.drawCentredString(centre, y, "Date et signature du bénéficiaire :")
    y -= 6 * mm
    c.drawString(centre - 10 * mm, y, f"Le : {date_longue(d.get('date_document'))}")
    signature = _chemin_image(org.get("signature_path"))
    if signature:
        c.drawImage(ImageReader(signature), centre + 30 * mm, y - 14 * mm, 50 * mm, 20 * mm,
                    preserveAspectRatio=True, mask="auto", anchor="sw")

    # --- Pied de page ------------------------------------------------------
    y = 22 * mm
    c.setFont("Times-Roman", 9)
    c.drawString(gauche - 3 * mm, y, org.get("nom") or "")
    c.drawString(gauche - 3 * mm, y - 4 * mm, ", ".join(adresse_org))
    c.drawString(gauche - 3 * mm, y - 8 * mm, f"Tél. {org.get('tel') or ''}      {org.get('email') or ''}")

    c.showPage()
    c.save()
    return buf.getvalue()


# ============================================================================
# ✉️ Courrier de remboursement forfaitaire (non-imposables)
# ============================================================================
def generer_courrier_remboursement_pdf(d):
    """d : numero, civilite, nom, prenom, adresse (lignes), texte (déjà
    rendu), detail [(lieu, journees)], total_journees, nb_tickets, prix_ticket,
    montant, date_document, signataire_nom, signataire_qualite. Retourne bytes."""
    org = get_organisation()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=15 * mm, bottomMargin=20 * mm,
                            title=f"Remboursement trajets {d['numero']}")

    normal = ParagraphStyle("normal", fontName="Helvetica", fontSize=10, leading=14)
    justifie = ParagraphStyle("justifie", parent=normal, alignment=TA_JUSTIFY, spaceAfter=6)
    petit = ParagraphStyle("petit", parent=normal, fontSize=8.5, leading=11, textColor=colors.HexColor("#444444"))
    gras = ParagraphStyle("gras", parent=normal, fontName="Helvetica-Bold")

    elements = []

    logo = _chemin_image(org.get("logo_path"))
    entete_org = Paragraph(
        f"<b>{org.get('nom') or ''}</b><br/>" + "<br/>".join(_esc(l) for l in (org.get("adresse") or "").splitlines())
        + f"<br/>Tél. {_esc(org.get('tel') or '')} — {_esc(org.get('email') or '')}", petit)
    destinataire = Paragraph("<br/>".join(_esc(l) for l in d["adresse"] if l), normal)
    entete = Table(
        [[Image(logo, width=20 * mm, height=20 * mm, kind="proportional") if logo else "", entete_org, ""],
         ["", "", destinataire]],
        colWidths=[25 * mm, 75 * mm, 70 * mm],
    )
    entete.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 1), (-1, 1), 12 * mm),
    ]))
    elements += [entete, Spacer(1, 10 * mm)]

    ville_org = (org.get("adresse") or "").splitlines()[-1].split(" ", 1)[-1].title() if org.get("adresse") else ""
    elements.append(Paragraph(f"{_esc(ville_org)}, le {date_longue(d.get('date_document'))}", normal))
    elements.append(Spacer(1, 6 * mm))
    elements.append(Paragraph(f"<b>Objet :</b> remboursement forfaitaire de vos trajets de bénévole {d['annee_frais']} "
                              f"— réf. {_esc(d['numero'])}", normal))
    elements.append(Spacer(1, 6 * mm))

    paragraphes = [p for p in (d["texte"] or "").split("\n\n") if p.strip()]
    # Tableau du détail inséré avant le dernier paragraphe (formule de politesse)
    corps, fin = (paragraphes[:-1], paragraphes[-1:]) if len(paragraphes) > 1 else (paragraphes, [])
    for paragraphe in corps:
        elements.append(Paragraph(_esc(paragraphe).replace("\n", "<br/>"), justifie))

    elements.append(Spacer(1, 2 * mm))
    lignes = [["Lieu", "Journées"]]
    lignes += [[lieu, str(nb)] for lieu, nb in d["detail"] if nb]
    lignes.append(["Total journées", str(d["total_journees"])])
    lignes.append([f"Tickets TAG ({d['nb_tickets']} par journée × {_euros(d['prix_ticket'])})",
                   f"{d['total_journees'] * d['nb_tickets']} tickets"])
    lignes.append(["Montant du remboursement", _euros(d["montant"])])
    tableau = Table(lignes, colWidths=[110 * mm, 40 * mm])
    tableau.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9.5),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9.5),
        ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#fde9d9")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
    ]))
    elements += [tableau, Spacer(1, 6 * mm)]
    for paragraphe in fin:
        elements.append(Paragraph(_esc(paragraphe).replace("\n", "<br/>"), justifie))
    elements.append(Spacer(1, 6 * mm))

    signature = _chemin_image(org.get("signature_path"))
    bloc_signature = [Paragraph(_esc(d.get("signataire_qualite") or ""), normal)]
    if signature:
        bloc_signature.append(Image(signature, width=50 * mm, height=20 * mm, kind="proportional"))
    bloc_signature.append(Paragraph(_esc(d.get("signataire_nom") or ""), gras))
    sig = Table([["", bloc_signature]], colWidths=[95 * mm, 75 * mm])
    sig.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(sig)

    doc.build(elements)
    return buf.getvalue()


def _esc(texte):
    return (str(texte or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _euros(valeur):
    return f"{float(valeur or 0):,.2f} €".replace(",", " ").replace(".", ",")


def fusionner_pdfs(liste_bytes):
    writer = PdfWriter()
    for contenu in liste_bytes:
        for page in PdfReader(io.BytesIO(contenu)).pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
