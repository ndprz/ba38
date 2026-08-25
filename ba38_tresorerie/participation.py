# =========================================
# 📊 Module Facturation Participation V2
# =========================================
# Regroupe en un seul flux ce qui était avant séparé : nettoyage du fichier
# PARSOL (déjà fait par ba38_tresorerie.py::traitement_participation, mais
# qui ne faisait que déposer des fichiers texte sur Drive), génération des
# PDF de facture (avant produits en externe par EBP), et envoi/suivi (repris
# du mécanisme construit pour ba38_tresorerie.py::factures_pdf).
#
# Coexiste avec l'ancien flux (upload d'un PDF déjà généré) tant que cette
# V2 n'est pas validée sur un vrai trimestre — voir mémoire "Facturation
# participation V2".

import os
import re
import json
import time
import sqlite3
from datetime import datetime
from threading import Thread

from flask import Blueprint, request, render_template, flash, redirect, url_for, jsonify, send_file, current_app, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from pathlib import Path

from utils import get_db_path, require_access, write_log, envoyer_mail, render_modele_email, mailjet_get_message_status, split_emails
from utils_gmail_send import envoyer_mail_gmail, GmailSendError
from ba38_tresorerie.constants import BAI_NOM, BAI_ADRESSE, BAI_TEL, BAI_MAIL, BAI_IBAN, BAI_BIC

participation_bp = Blueprint("participation", __name__)

# Constantes propres à la facture "participation" : SIRET/RNA différents de
# ceux utilisés pour les cotisations (BAI_SIREN/BAI_NAF dans
# ba38_tresorerie.py) — vérifiés sur un exemple réel de facture EBP
# (/srv/ba38/tmp/factures_*.pdf), ne pas les confondre.
BAI_SIRET = "38809213200025"
BAI_RNA = "W381001970"

def fmt_eur(valeur):
    """Formate un montant avec virgule décimale (convention française)."""
    return f"{valeur:.2f}".replace(".", ",")


MENTIONS_LEGALES = [
    "Cette cotisation est valorisée uniquement sur le poids brut des produits, hors produits FSE+ gratuits.** *TVA non applicable.*",
    "Les produits FSE+ sont obligatoirement gratuits jusqu'au bénéficiaire final. Sur le BL : G gratuit, RP remise produit.",
]

BAI_ORANGE = "#f27830"  # couleur primaire de l'appli (bootstrap-custom.css)


# ============================================================================
# 🔧 PARSING DU FICHIER PARSOL PARTICIPATION
# ============================================================================
PAT_DETAIL = re.compile(r"^(\d{2}/\d{2}/\d{4})\t(\d+)\t([\d,\.]+)\t([\d,\.]+)$")


def parser_parsol_participation(contenu):
    """
    Parse un fichier PARSOL participation (texte, blocs séparés par une
    ligne "BA. de l'Isère"). Retourne une liste de blocs :
        {code_vif, nom_fichier, lignes_gardees, lignes_supprimees,
         montant_total}
    lignes_gardees/lignes_supprimees : listes de
        {date, nb_beneficiaires, participation_unitaire, montant}
    Les passages du vendredi/samedi/dimanche (non facturés) sont retirés de
    lignes_gardees et placés dans lignes_supprimees (traçabilité).
    """
    lignes = contenu.splitlines()

    blocs_bruts = []
    bloc_courant = None

    for ligne in lignes:
        if ligne.strip() == "BA. de l'Isère":
            if bloc_courant is not None:
                blocs_bruts.append(bloc_courant)
            bloc_courant = []
            continue
        if bloc_courant is not None:
            bloc_courant.append(ligne)

    if bloc_courant is not None:
        blocs_bruts.append(bloc_courant)

    resultats = []

    for bloc in blocs_bruts:
        code_vif = None
        nom_fichier = None
        lignes_gardees = []
        lignes_supprimees = []

        for ligne in bloc:
            ls = ligne.strip()

            m_asso = re.match(r"^Association\s*:\s*(.+)$", ls)
            if m_asso:
                parts = re.split(r"\s*-\s*", m_asso.group(1).strip(), maxsplit=1)
                code_vif = parts[0].strip()
                nom_fichier = parts[1].strip() if len(parts) > 1 else ""
                continue

            m_det = PAT_DETAIL.match(ls)
            if m_det:
                date_str, nb_str, part_str, montant_str = m_det.groups()

                try:
                    date_obj = datetime.strptime(date_str, "%d/%m/%Y")
                except ValueError:
                    continue

                item = {
                    "date": date_str,
                    "nb_beneficiaires": int(nb_str),
                    "participation_unitaire": float(part_str.replace(",", ".")),
                    "montant": float(montant_str.replace(",", ".")),
                }

                if date_obj.weekday() in (4, 5, 6):  # ven/sam/dim
                    lignes_supprimees.append(item)
                else:
                    lignes_gardees.append(item)

        if not code_vif:
            continue

        resultats.append({
            "code_vif": code_vif,
            "nom_fichier": nom_fichier,
            "lignes_gardees": lignes_gardees,
            "lignes_supprimees": lignes_supprimees,
            "montant_total": round(sum(l["montant"] for l in lignes_gardees), 2),
        })

    return resultats


def deduire_trimestre(blocs):
    """Déduit année/trimestre à partir de la première date rencontrée."""
    dates = []
    for b in blocs:
        for l in b["lignes_gardees"] + b["lignes_supprimees"]:
            try:
                dates.append(datetime.strptime(l["date"], "%d/%m/%Y"))
            except ValueError:
                pass

    if not dates:
        return None, None

    premiere = min(dates)
    trimestre = (premiere.month - 1) // 3 + 1
    return premiere.year, trimestre


def _draw_star(c, cx, cy, r_outer, r_inner, color):
    import math
    points = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        r = r_outer if i % 2 == 0 else r_inner
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    p = c.beginPath()
    p.moveTo(*points[0])
    for pt in points[1:]:
        p.lineTo(*pt)
    p.close()
    c.setFillColor(color)
    c.drawPath(p, fill=1, stroke=0)


def draw_eu_flag(c, x, y, width, height):
    """Dessine le drapeau européen (12 étoiles) directement, sans fichier image."""
    import math
    from reportlab.lib.colors import HexColor

    c.setFillColor(HexColor("#003399"))
    c.rect(x, y, width, height, fill=1, stroke=0)

    cx, cy = x + width / 2, y + height / 2
    radius = min(width, height) * 0.32
    r_outer = min(width, height) * 0.09
    r_inner = r_outer * 0.4
    gold = HexColor("#FFCC00")

    for i in range(12):
        angle = math.pi / 2 - i * (2 * math.pi / 12)
        sx = cx + radius * math.cos(angle)
        sy = cy + radius * math.sin(angle)
        _draw_star(c, sx, sy, r_outer, r_inner, gold)

    c.setFillColor(HexColor("#000000"))


# ============================================================================
# 📄 GÉNÉRATION PDF DE LA FACTURE PARTICIPATION
# ============================================================================
def generer_facture_participation_pdf(data, output_path):
    """
    Génère une facture de participation PDF, mise en forme identique à
    l'exemple EBP de référence (/srv/ba38/tmp/factures_*.pdf).

    data doit contenir :
      - nom_association, contact (peut être vide), adresse (multi-lignes),
        email, numero_facture, periode, lignes (liste de
        {date, nb_beneficiaires, participation_unitaire, montant}),
        montant_total
    """
    from reportlab.lib.colors import HexColor

    c = canvas.Canvas(str(output_path), pagesize=A4)
    largeur, hauteur = A4
    marge_g, marge_d = 20 * mm, largeur - 20 * mm

    def nouvelle_page_entete():
        """Bandeau logo + titre en haut de chaque page (grand, orange)."""
        y_top = hauteur - 15 * mm
        logo_path = Path(current_app.root_path) / "static" / "images" / "logo.png"
        logo_size = 24 * mm

        if logo_path.exists():
            logo = ImageReader(str(logo_path))
            c.drawImage(logo, marge_g, y_top - logo_size, width=logo_size, height=logo_size,
                        preserveAspectRatio=True, mask="auto")

        c.setFont("Helvetica-Bold", 20)
        c.setFillColor(HexColor(BAI_ORANGE))
        c.drawString(marge_g + logo_size + 8 * mm, y_top - logo_size / 2 - 3, "Participation de solidarité")
        c.setFillColor(HexColor("#000000"))

        return y_top - logo_size - 8 * mm

    y = nouvelle_page_entete()

    # ============================
    # BLOC BAI (gauche) / BLOC ASSOCIATION (droite)
    # ============================
    x_droite = marge_g + 95 * mm

    y_gauche = y
    c.setFont("Helvetica-Bold", 11)
    c.drawString(marge_g, y_gauche, BAI_NOM)
    c.setFont("Helvetica", 9)
    y_gauche -= 12
    for line in BAI_ADRESSE.split("\n"):
        c.drawString(marge_g, y_gauche, line)
        y_gauche -= 10
    c.drawString(marge_g, y_gauche, f"Tél : {BAI_TEL}")
    y_gauche -= 10
    c.drawString(marge_g, y_gauche, f"E-mail : {BAI_MAIL}")

    y_droite = y
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x_droite, y_droite, data["nom_association"])
    c.setFont("Helvetica", 9)
    y_droite -= 12

    if data.get("contact"):
        c.drawString(x_droite, y_droite, data["contact"])
        y_droite -= 10

    for line in (data.get("adresse") or "").split("\n"):
        if line.strip():
            c.drawString(x_droite, y_droite, line)
            y_droite -= 10

    if data.get("email"):
        c.drawString(x_droite, y_droite, f"E-mail : {data['email']}")
        y_droite -= 10

    y = min(y_gauche, y_droite) - 15

    # ============================
    # N° FACTURE / DATE / ÉCHÉANCE (cases encadrées)
    # ============================
    aujourdhui = datetime.now().strftime("%d/%m/%Y")
    box_w, box_h = 36 * mm, 14 * mm
    box_labels = [
        ("N° BORDEREAU", str(data.get("numero_facture") or "—")),
        ("DATE", aujourdhui),
        ("ÉCHÉANCE", aujourdhui),
    ]

    y -= box_h
    x_box = marge_g
    for label, valeur in box_labels:
        c.rect(x_box, y, box_w, box_h, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(x_box + box_w / 2, y + box_h - 9, label)
        c.setFont("Helvetica", 9)
        c.drawCentredString(x_box + box_w / 2, y + 5, valeur)
        x_box += box_w + 2 * mm

    y -= 15

    # ============================
    # TABLE DÉSIGNATION / MONTANT
    # ============================
    c.setFont("Helvetica-Bold", 9)
    c.drawString(marge_g, y, "Désignation")
    c.drawRightString(marge_d, y, "Montant")
    y -= 10
    c.line(marge_g, y, marge_d, y)
    y -= 15

    c.setFont("Helvetica", 9)
    for ligne in data["lignes"]:
        if y < 40 * mm:
            c.showPage()
            y = nouvelle_page_entete() - 20 * mm

        texte = f"Passage du {ligne['date']} ({ligne['nb_beneficiaires']}) : {fmt_eur(ligne['montant'])}€"
        c.drawString(marge_g, y, texte)
        y -= 12

    c.drawRightString(marge_d, y, fmt_eur(data["montant_total"]))
    y -= 5

    # ============================
    # NET À PAYER
    # ============================
    y -= 10
    c.line(100 * mm, y, marge_d, y)
    y -= 15
    c.setFont("Helvetica-Bold", 10)
    c.drawString(marge_g, y, "NET À PAYER EN EUROS")
    c.drawRightString(marge_d, y, fmt_eur(data["montant_total"]))

    # ============================
    # NOTES (Siret/RNA puis mentions FSE+)
    # ============================
    if y < 60 * mm:
        c.showPage()
        y = nouvelle_page_entete()

    y -= 25
    c.setFont("Helvetica-Bold", 8)
    c.drawString(marge_g, y, "Notes")
    y -= 10

    c.setFont("Helvetica", 7)
    c.drawString(marge_g, y, f"Siret {BAI_SIRET} RNA {BAI_RNA}")
    y -= 9
    for texte in MENTIONS_LEGALES:
        c.drawString(marge_g, y, texte)
        y -= 9

    # ============================
    # PAIEMENT (drapeau UE + IBAN/BIC)
    # ============================
    y -= 10
    box_top = y
    box_bottom = y - 24 * mm
    box_left = marge_g + 32 * mm

    c.roundRect(box_left, box_bottom, marge_d - box_left, box_top - box_bottom, 4, stroke=1, fill=0)
    draw_eu_flag(c, marge_g, box_bottom, 26 * mm, box_top - box_bottom)

    ty = box_top - 10
    c.setFont("Helvetica-Bold", 8)
    c.drawString(box_left + 4 * mm, ty, "Merci d'effectuer le règlement par virement sur notre compte :")
    ty -= 12
    c.setFont("Helvetica", 8)
    c.drawString(box_left + 4 * mm, ty, f"Compte bancaire Crédit Mutuel {BAI_IBAN.replace(' ', '')}")
    ty -= 12
    c.drawString(box_left + 4 * mm, ty, f"BIC : {BAI_BIC}")

    c.showPage()
    c.save()


# ============================================================================
# 🚀 ENVOI — TRAITEMENT ARRIÈRE-PLAN
# ============================================================================
def envoyer_participation_background(app, db_path, campagne_id, items, mail_mode, mail_test_to, current_user_email):
    with app.app_context():
        envoyes = 0
        nb_erreurs = 0
        count_test = 0
        now_iso = datetime.now().isoformat(timespec="seconds")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        for item in items:

            sujet_envoi = item["sujet"]

            if mail_mode == "TEST":
                if count_test >= 2:
                    break
                count_test += 1
                destinataires = [mail_test_to]
                email_envoi = mail_test_to
                sujet_envoi = f"🧪 [TEST] {sujet_envoi}"
            else:
                destinataires = split_emails(item["email"])
                email_envoi = ", ".join(destinataires) if destinataires else item["email"]

            pdf_path = f"/tmp/participation_{item['facture_id']}.pdf"

            try:
                if not destinataires:
                    raise ValueError(
                        f"Aucune adresse email valide pour {item['nom_association']}"
                    )

                assoc = conn.execute(
                    "SELECT * FROM associations WHERE Id = ?", (item["association_id"],)
                ).fetchone()

                adresse = "\n".join(filter(None, [
                    assoc["adresse_association_1"] if assoc else "",
                    assoc["adresse_association_2"] if assoc else "",
                    " ".join(filter(None, [assoc["CP"], assoc["COMMUNE"]])) if assoc else "",
                ]))

                data_pdf = {
                    "nom_association": item["nom_association"],
                    "contact": (assoc["responsable_tresorerie"] if assoc else "") or "",
                    "adresse": adresse,
                    "email": item["email"],
                    "numero_facture": item["numero_facture"],
                    "lignes": json.loads(item["detail_json"]),
                    "montant_total": item["montant_total"],
                }

                generer_facture_participation_pdf(data_pdf, pdf_path)

                write_log(f"📧 Envoi facture participation à {email_envoi} | facture_id={item['facture_id']}")

                resultat = envoyer_mail(
                    sujet=sujet_envoi,
                    destinataires=destinataires,
                    texte=item["corps"],
                    sender_override="ba380.comptable@banquealimentaire.org",
                    attachment_path=pdf_path,
                    bcc=["ba380.comptable@banquealimentaire.org"]
                )

                mj_status, mj_ids = None, None
                if resultat and resultat.get("Messages"):
                    mj_message = resultat["Messages"][0]
                    mj_status = mj_message.get("Status")
                    mj_ids = ",".join(
                        str(t["MessageID"]) for t in mj_message.get("To", []) if "MessageID" in t
                    ) or None

                conn.execute("""
                    UPDATE participation_factures
                    SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = NULL,
                        mail_mailjet_status = ?, mail_mailjet_message_ids = ?,
                        mail_modele_id = ?, pdf_genere_le = ?, sujet = ?, corps = ?
                    WHERE id = ?
                """, (now_iso, 1 if mail_mode == "TEST" else 0, mj_status, mj_ids,
                      item["modele_id"], now_iso, sujet_envoi, item["corps"], item["facture_id"]))
                conn.commit()

                envoyes += 1

            except Exception as e:
                write_log(f"❌ Erreur envoi facture participation (facture_id={item['facture_id']}) : {e}")
                nb_erreurs += 1

                conn.execute("""
                    UPDATE participation_factures
                    SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = ?, mail_modele_id = ?,
                        sujet = ?, corps = ?
                    WHERE id = ?
                """, (now_iso, 1 if mail_mode == "TEST" else 0, str(e), item["modele_id"],
                      item["sujet"], item["corps"], item["facture_id"]))
                conn.commit()

            finally:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)

        conn.execute("""
            UPDATE participation_campagnes
            SET dernier_envoi_le = ?, dernier_envoi_par = ?, dernier_envoi_mode_test = ?,
                dernier_envoi_nb_ok = ?, dernier_envoi_nb_erreur = ?
            WHERE id = ?
        """, (now_iso, current_user_email, 1 if mail_mode == "TEST" else 0, envoyes, nb_erreurs, campagne_id))
        conn.commit()
        conn.close()

        write_log(f"📤 Envoi participation (background) terminé : {envoyes} envoyés, {nb_erreurs} erreur(s)")


# ============================================================================
# 💳 PAIEMENT — TOGGLE "PAYÉ"
# ============================================================================
@participation_bp.route("/participation/toggle_paye/<int:facture_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def toggle_paye(facture_id):
    """
    Bascule le statut payé/impayé en AJAX (pas de redirect) : la liste peut
    être longue, on évite de faire remonter la page en haut à chaque clic.
    """

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    f = conn.execute(
        "SELECT date_paiement FROM participation_factures WHERE id = ?",
        (facture_id,)
    ).fetchone()

    if not f:
        conn.close()
        return jsonify({"error": "introuvable"}), 404

    nouveau = None if f["date_paiement"] else datetime.now().strftime("%Y-%m-%d")

    conn.execute(
        "UPDATE participation_factures SET date_paiement = ? WHERE id = ?",
        (nouveau, facture_id)
    )
    conn.commit()
    conn.close()

    return jsonify({"date_paiement": nouveau})


# ============================================================================
# 📅 SÉLECTION DU TRIMESTRE
# ============================================================================
@participation_bp.route("/participation")
@login_required
@require_access("tresorerie", "ecriture")
def selection():
    annee_now = datetime.now().year
    periodes = [(annee_now, t) for t in (1, 2, 3, 4)]
    return render_template("tresorerie/participation/selection.html", periodes=periodes)


@participation_bp.route("/participation/check_trimestre")
@login_required
@require_access("tresorerie", "ecriture")
def check_trimestre():
    try:
        annee = int(request.args.get("annee"))
        trimestre = int(request.args.get("trimestre"))
    except (TypeError, ValueError):
        return jsonify({"exists": False, "id": None})

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    campagne = conn.execute("""
        SELECT id FROM participation_campagnes
        WHERE annee = ? AND trimestre = ?
        ORDER BY id DESC LIMIT 1
    """, (annee, trimestre)).fetchone()
    conn.close()

    return jsonify({"exists": campagne is not None, "id": campagne["id"] if campagne else None})


# ============================================================================
# 📂 TRAITEMENT DU FICHIER PARSOL
# ============================================================================
@participation_bp.route("/participation/traiter", methods=["GET", "POST"])
@login_required
@require_access("tresorerie", "ecriture")
def traiter():

    annee = request.values.get("annee", "")
    trimestre = request.values.get("trimestre", "")

    if request.method == "GET":
        return render_template("tresorerie/participation/upload.html", annee=annee, trimestre=trimestre)

    fichier = request.files.get("parsol_file")
    numero_facture_depart = request.form.get("numero_facture_depart", "").strip()

    if not fichier:
        flash("❌ Fichier manquant", "danger")
        return redirect(url_for("participation.traiter", annee=annee, trimestre=trimestre))

    if not numero_facture_depart.isdigit():
        flash("❌ Numéro de facture de départ invalide", "danger")
        return redirect(url_for("participation.traiter", annee=annee, trimestre=trimestre))

    numero_facture_depart = int(numero_facture_depart)

    contenu_bytes = fichier.read()
    try:
        contenu = contenu_bytes.decode("utf-8")
    except UnicodeDecodeError:
        contenu = contenu_bytes.decode("cp1252")

    blocs = parser_parsol_participation(contenu)

    if not blocs:
        flash("❌ Aucune association détectée dans le fichier — vérifiez le format.", "danger")
        return redirect(url_for("participation.traiter", annee=annee, trimestre=trimestre))

    annee_deduite, trimestre_deduite = deduire_trimestre(blocs)
    annee_finale = int(annee) if annee else annee_deduite
    trimestre_finale = int(trimestre) if trimestre else trimestre_deduite

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ==================================================
    # 🔒 Campagne existante pour ce trimestre ?
    # ==================================================
    # Autorisé à écraser tant qu'aucun envoi réel n'a eu lieu (mode TEST ou
    # brouillon jamais envoyé) — sinon bloqué pour ne jamais perdre une
    # campagne déjà réellement envoyée aux associations.
    existante = conn.execute("""
        SELECT id FROM participation_campagnes WHERE annee = ? AND trimestre = ?
        ORDER BY id DESC LIMIT 1
    """, (annee_finale, trimestre_finale)).fetchone()

    if existante:
        nb_envois_reels = conn.execute("""
            SELECT COUNT(*) FROM participation_factures
            WHERE campagne_id = ? AND mail_envoye_le IS NOT NULL AND mail_mode_test = 0
        """, (existante["id"],)).fetchone()[0]

        if nb_envois_reels > 0:
            conn.close()
            flash(
                f"⛔ Un envoi réel a déjà eu lieu pour T{trimestre_finale} {annee_finale} "
                f"({nb_envois_reels} facture(s)) — retraitement bloqué pour ne pas créer de "
                f"doublon avec une autre numérotation. Consultez d'abord l'écran Résultats.",
                "danger"
            )
            return redirect(url_for("participation.resultats", campagne_id=existante["id"]))

        # Rien d'envoyé réellement pour l'instant : on écrase (même campagne_id)
        campagne_id = existante["id"]
        conn.execute("DELETE FROM participation_factures WHERE campagne_id = ?", (campagne_id,))
        conn.execute("""
            UPDATE participation_campagnes
            SET fichier_source = ?, numero_facture_depart = ?, date_creation = ?, cree_par = ?,
                dernier_envoi_le = NULL, dernier_envoi_par = NULL, dernier_envoi_mode_test = 0,
                dernier_envoi_nb_ok = NULL, dernier_envoi_nb_erreur = NULL
            WHERE id = ?
        """, (secure_filename(fichier.filename) if fichier.filename else "parsol.txt",
              numero_facture_depart, datetime.now().isoformat(timespec="seconds"), current_user.email,
              campagne_id))

    else:
        cur = conn.execute("""
            INSERT INTO participation_campagnes
            (annee, trimestre, fichier_source, numero_facture_depart, date_creation, cree_par)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (annee_finale, trimestre_finale,
              secure_filename(fichier.filename) if fichier.filename else "parsol.txt",
              numero_facture_depart, datetime.now().isoformat(timespec="seconds"), current_user.email))
        campagne_id = cur.lastrowid

    matches = []
    orphelines = []

    for b in blocs:
        assoc = conn.execute("SELECT * FROM associations WHERE code_VIF = ?", (b["code_vif"],)).fetchone()
        if assoc:
            matches.append((assoc, b))
        else:
            orphelines.append(b)

    # Numérotation dans l'ordre du fichier PARSOL (pas de tri par compte
    # comptable — contrairement aux cotisations, l'utilisateur veut que la
    # 1ère association du fichier reçoive le 1er numéro).

    for i, (assoc, b) in enumerate(matches):
        conn.execute("""
            INSERT INTO participation_factures
            (campagne_id, association_id, code_vif, numero_facture, nom_association,
             montant_total, detail_json, lignes_supprimees_json, email)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (campagne_id, assoc["Id"], b["code_vif"], numero_facture_depart + i, assoc["nom_association"],
              b["montant_total"], json.dumps(b["lignes_gardees"]), json.dumps(b["lignes_supprimees"]),
              assoc["courriel_resp_tresorerie"] or assoc["courriel_association"]))

    for b in orphelines:
        conn.execute("""
            INSERT INTO participation_factures
            (campagne_id, association_id, code_vif, numero_facture, nom_association,
             montant_total, detail_json, lignes_supprimees_json, email)
            VALUES (?, NULL, ?, NULL, ?, ?, ?, ?, NULL)
        """, (campagne_id, b["code_vif"], b["nom_fichier"],
              b["montant_total"], json.dumps(b["lignes_gardees"]), json.dumps(b["lignes_supprimees"])))

    conn.commit()
    conn.close()

    verbe = "recréée(s)" if existante else "créée(s)"
    msg = f"📊 {len(matches)} facture(s) {verbe}"
    if orphelines:
        msg += f" — ⚠️ {len(orphelines)} code(s) VIF non trouvé(s) en base : " + \
               ", ".join(f"{b['code_vif']} ({b['nom_fichier']})" for b in orphelines)
    flash(msg, "warning" if orphelines else "info")

    return redirect(url_for("participation.resultats", campagne_id=campagne_id))


def _rattacher_orphelines(conn, campagne_id):
    """
    Réessaie de rapprocher les factures orphelines (code VIF sans
    correspondance en base au moment du traitement du fichier PARSOL) avec
    la table associations — utile quand l'association, ou son code VIF, a
    été créée/corrigée après coup. Ne fait rien si aucune correspondance
    n'est trouvée. Retourne la liste des (nom_association, numero_facture)
    nouvellement rattachées.
    """
    orphelines = conn.execute("""
        SELECT id, code_vif FROM participation_factures
        WHERE campagne_id = ? AND association_id IS NULL
    """, (campagne_id,)).fetchall()

    if not orphelines:
        return []

    max_numero = conn.execute("""
        SELECT MAX(numero_facture) FROM participation_factures WHERE campagne_id = ?
    """, (campagne_id,)).fetchone()[0] or 0

    rattachees = []

    for o in orphelines:
        assoc = conn.execute(
            "SELECT * FROM associations WHERE code_VIF = ?", (o["code_vif"],)
        ).fetchone()
        if not assoc:
            continue

        max_numero += 1
        email = assoc["courriel_resp_tresorerie"] or assoc["courriel_association"]

        conn.execute("""
            UPDATE participation_factures
            SET association_id = ?, numero_facture = ?, nom_association = ?, email = ?
            WHERE id = ?
        """, (assoc["Id"], max_numero, assoc["nom_association"], email, o["id"]))

        rattachees.append((assoc["nom_association"], max_numero))

    if rattachees:
        conn.commit()

    return rattachees


# ============================================================================
# 📊 ÉCRAN RÉSULTATS
# ============================================================================
@participation_bp.route("/participation/resultats/<int:campagne_id>")
@login_required
@require_access("tresorerie", "ecriture")
def resultats(campagne_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    campagne = conn.execute("SELECT * FROM participation_campagnes WHERE id = ?", (campagne_id,)).fetchone()

    if not campagne:
        conn.close()
        flash("❌ Campagne introuvable", "danger")
        return redirect(url_for("participation.selection"))

    rattachees = _rattacher_orphelines(conn, campagne_id)
    if rattachees:
        detail = ", ".join(f"{nom} (facture n°{num})" for nom, num in rattachees)
        flash(
            f"✅ {len(rattachees)} facture(s) orpheline(s) rattachée(s) automatiquement "
            f"(association créée ou corrigée depuis) : {detail}",
            "success"
        )

    factures = conn.execute("""
        SELECT * FROM participation_factures
        WHERE campagne_id = ? AND association_id IS NOT NULL
        ORDER BY numero_facture
    """, (campagne_id,)).fetchall()
    factures = [dict(f, beneficiaires=_compter_beneficiaires(f["detail_json"])) for f in factures]

    orphelines = conn.execute("""
        SELECT * FROM participation_factures
        WHERE campagne_id = ? AND association_id IS NULL
        ORDER BY nom_association
    """, (campagne_id,)).fetchall()
    orphelines = [dict(o, beneficiaires=_compter_beneficiaires(o["detail_json"])) for o in orphelines]

    modeles = conn.execute("""
        SELECT * FROM modeles_emails WHERE type_periode = 'facture' ORDER BY TRIM(code_modele) COLLATE NOCASE
    """).fetchall()

    conn.close()

    reste_a_envoyer = any(f["email"] and not f["mail_envoye_le"] for f in factures)
    mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")
    montant_total_campagne = sum(f["montant_total"] or 0 for f in factures)

    return render_template(
        "tresorerie/participation/resultats.html",
        campagne=campagne,
        factures=factures,
        orphelines=orphelines,
        modeles=modeles,
        reste_a_envoyer=reste_a_envoyer,
        mail_mode=mail_mode,
        mail_test_to=mail_test_to,
        montant_total_campagne=montant_total_campagne
    )


# ============================================================================
# 📧 ENVOYER LES MAILS
# ============================================================================
@participation_bp.route("/participation/envoyer/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def envoyer(campagne_id):

    modele_id = request.form.get("modele_id")
    if not modele_id:
        flash("❌ Aucun modèle de mail sélectionné", "danger")
        return redirect(url_for("participation.resultats", campagne_id=campagne_id))

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    campagne = conn.execute("SELECT * FROM participation_campagnes WHERE id = ?", (campagne_id,)).fetchone()
    if not campagne:
        conn.close()
        flash("❌ Campagne introuvable", "danger")
        return redirect(url_for("participation.selection"))

    modele = conn.execute("SELECT * FROM modeles_emails WHERE id = ?", (modele_id,)).fetchone()
    if not modele:
        conn.close()
        flash("❌ Modèle introuvable", "danger")
        return redirect(url_for("participation.resultats", campagne_id=campagne_id))

    a_envoyer = conn.execute("""
        SELECT * FROM participation_factures
        WHERE campagne_id = ? AND email IS NOT NULL AND mail_envoye_le IS NULL
    """, (campagne_id,)).fetchall()

    conn.close()

    if not a_envoyer:
        flash("ℹ️ Rien à envoyer : toutes les factures de cette campagne ont déjà été traitées.", "warning")
        return redirect(url_for("participation.resultats", campagne_id=campagne_id))

    periode = f"T{campagne['trimestre']} {campagne['annee']}"

    items = []
    for f in a_envoyer:
        contexte = {"nom_association": f["nom_association"], "periode": periode}
        items.append({
            "facture_id": f["id"],
            "association_id": f["association_id"],
            "email": f["email"],
            "sujet": render_modele_email(modele["sujet"], contexte).strip(),
            "corps": render_modele_email(modele["corps"], contexte),
            "detail_json": f["detail_json"],
            "nom_association": f["nom_association"],
            "numero_facture": f["numero_facture"],
            "montant_total": f["montant_total"],
            "modele_id": modele_id,
        })

    mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")

    app_reel = current_app._get_current_object()
    current_user_email = current_user.email

    Thread(
        target=envoyer_participation_background,
        args=(app_reel, db_path, campagne_id, items, mail_mode, mail_test_to, current_user_email)
    ).start()

    if mail_mode == "TEST":
        flash("🧪 Envoi TEST lancé en arrière-plan (2 mails max vers l'adresse de test).", "warning")
    else:
        flash(f"🚀 Envoi réel lancé en arrière-plan pour {len(items)} facture(s).", "info")

    return redirect(url_for("participation.resultats", campagne_id=campagne_id))


# ============================================================================
# 👁️ VOIR LE PDF D'UNE FACTURE (régénéré à la demande)
# ============================================================================
@participation_bp.route("/participation/voir_pdf/<int:facture_id>")
@login_required
@require_access("tresorerie", "ecriture")
def voir_pdf(facture_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    f = conn.execute("SELECT * FROM participation_factures WHERE id = ?", (facture_id,)).fetchone()

    if not f:
        conn.close()
        return "Ligne introuvable", 404

    assoc = None
    if f["association_id"]:
        assoc = conn.execute("SELECT * FROM associations WHERE Id = ?", (f["association_id"],)).fetchone()

    conn.close()

    adresse = "\n".join(filter(None, [
        assoc["adresse_association_1"] if assoc else "",
        assoc["adresse_association_2"] if assoc else "",
        " ".join(filter(None, [assoc["CP"], assoc["COMMUNE"]])) if assoc else "",
    ]))

    data_pdf = {
        "nom_association": f["nom_association"],
        "contact": (assoc["responsable_tresorerie"] if assoc else "") or "",
        "adresse": adresse,
        "email": f["email"],
        "numero_facture": f["numero_facture"] or "—",
        "lignes": json.loads(f["detail_json"]) if f["detail_json"] else [],
        "montant_total": f["montant_total"] or 0,
    }

    pdf_path = f"/tmp/participation_voir_{facture_id}.pdf"
    generer_facture_participation_pdf(data_pdf, pdf_path)

    return send_file(pdf_path, mimetype="application/pdf")


# ============================================================================
# 🔄 VÉRIFIER STATUT MAILJET
# ============================================================================
@participation_bp.route("/participation/verifier_statut_mailjet/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def verifier_statut_mailjet(campagne_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    lignes = conn.execute("""
        SELECT id, mail_mailjet_message_ids
        FROM participation_factures
        WHERE campagne_id = ?
          AND mail_mailjet_message_ids IS NOT NULL
          AND mail_mailjet_message_ids != ''
    """, (campagne_id,)).fetchall()

    counts = {}
    verifies = 0

    for ligne in lignes:
        premier_id = ligne["mail_mailjet_message_ids"].split(",")[0]
        statut = mailjet_get_message_status(premier_id)

        if not statut:
            continue

        verifies += 1
        counts[statut] = counts.get(statut, 0) + 1

        conn.execute("""
            UPDATE participation_factures
            SET mail_statut_final = ?, mail_statut_verifie_le = ?
            WHERE id = ?
        """, (statut, datetime.now().isoformat(timespec="seconds"), ligne["id"]))

    conn.commit()
    conn.close()

    if verifies:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        flash(f"🔄 Statut Mailjet vérifié pour {verifies} mail(s) : {detail}", "info")
    else:
        flash("ℹ️ Aucun mail avec un identifiant Mailjet à vérifier pour cette campagne.", "warning")

    return redirect(url_for("participation.resultats", campagne_id=campagne_id))


# ============================================================================
# 🔁 RENVOYER VIA GMAIL
# ============================================================================
@participation_bp.route("/participation/renvoyer_gmail/<int:facture_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def renvoyer_gmail(facture_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    f = conn.execute("SELECT * FROM participation_factures WHERE id = ?", (facture_id,)).fetchone()

    if not f:
        conn.close()
        flash("❌ Ligne introuvable", "danger")
        return redirect(url_for("participation.selection"))

    destinataires = split_emails(f["email"])
    if not destinataires:
        conn.close()
        flash(f"❌ Aucune adresse email valide pour {f['nom_association']}", "danger")
        return redirect(url_for("participation.resultats", campagne_id=f["campagne_id"]))

    if not f["sujet"] or not f["corps"]:
        conn.close()
        flash(f"⛔ Aucun envoi précédent connu pour {f['nom_association']} — utilisez d'abord « Envoyer les mails ».", "danger")
        return redirect(url_for("participation.resultats", campagne_id=f["campagne_id"]))

    if f["mail_mode_test"]:
        conn.close()
        flash(f"⛔ Le dernier envoi pour {f['nom_association']} était en Mode TEST — un renvoi Gmail partirait, lui, pour de vrai. Refaites d'abord un envoi réel.", "danger")
        return redirect(url_for("participation.resultats", campagne_id=f["campagne_id"]))

    assoc = None
    if f["association_id"]:
        assoc = conn.execute("SELECT * FROM associations WHERE Id = ?", (f["association_id"],)).fetchone()

    conn.close()

    adresse = "\n".join(filter(None, [
        assoc["adresse_association_1"] if assoc else "",
        assoc["adresse_association_2"] if assoc else "",
        " ".join(filter(None, [assoc["CP"], assoc["COMMUNE"]])) if assoc else "",
    ]))

    data_pdf = {
        "nom_association": f["nom_association"],
        "contact": (assoc["responsable_tresorerie"] if assoc else "") or "",
        "adresse": adresse,
        "email": f["email"],
        "numero_facture": f["numero_facture"],
        "lignes": json.loads(f["detail_json"]) if f["detail_json"] else [],
        "montant_total": f["montant_total"] or 0,
    }

    pdf_path = f"/tmp/participation_gmail_{facture_id}.pdf"
    generer_facture_participation_pdf(data_pdf, pdf_path)

    conn = sqlite3.connect(get_db_path())

    try:
        envoyer_mail_gmail(
            sujet=f["sujet"],
            destinataires=destinataires,
            texte=f["corps"],
            attachment_path=pdf_path
        )

        conn.execute("""
            UPDATE participation_factures SET mail_renvoi_gmail_le = ? WHERE id = ?
        """, (datetime.now().isoformat(timespec="seconds"), facture_id))
        conn.commit()

        flash(f"📧 Facture renvoyée via Gmail à {f['email']} pour {f['nom_association']}", "success")

    except GmailSendError as e:
        write_log(f"❌ Erreur renvoi Gmail participation pour {f['nom_association']} : {e}")
        flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")

    finally:
        conn.close()
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

    return redirect(url_for("participation.resultats", campagne_id=f["campagne_id"]))


# ============================================================================
# 🔔 RELANCE DES FACTURES IMPAYÉES
# ============================================================================
def _compter_beneficiaires(detail_json):
    """Somme des nb_beneficiaires des passages retenus (detail_json) d'une facture."""
    if not detail_json:
        return 0
    try:
        lignes = json.loads(detail_json)
        return sum(l.get("nb_beneficiaires", 0) for l in lignes)
    except Exception:
        return 0


def _resoudre_lignes_email(rows):
    """
    Convertit les lignes SQL (jointes à associations) en dicts, complète
    l'email de la facture — capturé une fois au traitement PARSOL, donc
    potentiellement obsolète si l'association n'avait pas encore d'email à
    ce moment — par l'adresse actuelle de l'association si absent, et
    ajoute le total de bénéficiaires (calculé depuis detail_json).
    """
    lignes = []
    for row in rows:
        d = dict(row)
        assoc_email = d.pop("_assoc_tresorerie", None) or d.pop("_assoc_association", None)
        if not d.get("email"):
            d["email"] = assoc_email
        d["beneficiaires"] = _compter_beneficiaires(d.get("detail_json"))
        lignes.append(d)
    return lignes


def envoyer_relances_participation_background(app, db_path, items, sujet_modele, corps_modele,
                                               numero_relance, annee, trimestre, mail_sender,
                                               mail_mode, mail_test_to):
    """
    Envoi des relances de participation en arrière-plan (Thread).

    Reproduit le dispositif déjà en place pour les relances cotisations
    (ba38_tresorerie.py::envoyer_relances_background), avec une différence :
    ici le PDF n'est pas cherché sur Drive, il est régénéré à la demande et
    joint en pièce attachée (même mécanisme que envoyer_participation_background).
    """
    with app.app_context():

        nb_mails = 0
        nb_erreurs = 0

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        lignes_traitement = items
        if mail_mode == "TEST":
            lignes_traitement = items[:2]

        for item in lignes_traitement:

            pdf_path = f"/tmp/participation_relance_{item['facture_id']}.pdf"

            try:
                sujet = sujet_modele.format(
                    numero_relance=numero_relance + 1,
                    annee=annee,
                    trimestre=trimestre,
                )

                texte_mail = corps_modele.format(
                    numero_relance=numero_relance + 1,
                    annee=annee,
                    trimestre=trimestre,
                    nom_association=item["nom_association"],
                    montant="{:.2f}".format(item["montant_total"] or 0)
                )

                if mail_mode == "TEST":
                    destinataire = [mail_test_to]
                    sujet_envoi = f"🧪 [TEST] {sujet}"
                else:
                    destinataire = split_emails(item["email"])
                    if not destinataire:
                        raise ValueError(
                            f"Aucune adresse email valide pour {item['nom_association']}"
                        )
                    sujet_envoi = sujet

                assoc = conn.execute(
                    "SELECT * FROM associations WHERE Id = ?", (item["association_id"],)
                ).fetchone()

                adresse = "\n".join(filter(None, [
                    assoc["adresse_association_1"] if assoc else "",
                    assoc["adresse_association_2"] if assoc else "",
                    " ".join(filter(None, [assoc["CP"], assoc["COMMUNE"]])) if assoc else "",
                ]))

                data_pdf = {
                    "nom_association": item["nom_association"],
                    "contact": (assoc["responsable_tresorerie"] if assoc else "") or "",
                    "adresse": adresse,
                    "email": item["email"],
                    "numero_facture": item["numero_facture"],
                    "lignes": json.loads(item["detail_json"]) if item["detail_json"] else [],
                    "montant_total": item["montant_total"],
                }

                generer_facture_participation_pdf(data_pdf, pdf_path)

                resultat = envoyer_mail(
                    sujet=sujet_envoi,
                    destinataires=destinataire,
                    texte=texte_mail,
                    sender_override=mail_sender,
                    attachment_path=pdf_path,
                    bcc=[mail_sender]
                )

                mj_status, mj_ids = None, None
                if resultat and resultat.get("Messages"):
                    mj_message = resultat["Messages"][0]
                    mj_status = mj_message.get("Status")
                    mj_ids = ",".join(
                        str(t["MessageID"]) for t in mj_message.get("To", []) if "MessageID" in t
                    ) or None

                conn.execute("""
                    UPDATE participation_factures
                    SET relance_niveau = COALESCE(relance_niveau,0)+1,
                        date_derniere_relance = ?,
                        mode_test_relance = ?,
                        relance_sujet = ?,
                        relance_corps = ?,
                        relance_mail_erreur = NULL,
                        relance_mailjet_status = ?,
                        relance_mailjet_message_ids = ?,
                        email = COALESCE(NULLIF(email, ''), ?)
                    WHERE id = ?
                """, (
                    datetime.now().isoformat(timespec="seconds"),
                    1 if mail_mode == "TEST" else 0,
                    sujet_envoi,
                    texte_mail,
                    mj_status,
                    mj_ids,
                    item["email"],
                    item["facture_id"]
                ))
                conn.commit()

                nb_mails += 1

            except Exception as e:
                write_log(f"❌ Erreur relance participation (facture_id={item['facture_id']}) : {e}")
                nb_erreurs += 1
                conn.execute("""
                    UPDATE participation_factures
                    SET relance_mail_erreur = ?
                    WHERE id = ?
                """, (str(e), item["facture_id"]))
                conn.commit()

            finally:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)

        conn.close()

        write_log(
            f"📤 Relances participation T{trimestre} {annee} (arrière-plan) terminées : "
            f"{nb_mails} envoyée(s), {nb_erreurs} erreur(s)."
        )


@participation_bp.route("/participation/relance/<int:campagne_id>", methods=["GET"])
@login_required
@require_access("tresorerie", "ecriture")
def relance_start(campagne_id):

    mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")
    mail_sender = request.args.get("mail_sender", "ba380.comptable@banquealimentaire.org")
    numero_relance = int(request.args.get("numero_relance", 0))

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    campagne = conn.execute(
        "SELECT * FROM participation_campagnes WHERE id = ?", (campagne_id,)
    ).fetchone()

    if not campagne:
        conn.close()
        flash("❌ Campagne introuvable", "danger")
        return redirect(url_for("participation.selection"))

    lignes = conn.execute("""
        SELECT pf.*,
               a.courriel_resp_tresorerie AS _assoc_tresorerie,
               a.courriel_association AS _assoc_association
        FROM participation_factures pf
        LEFT JOIN associations a ON a.Id = pf.association_id
        WHERE pf.campagne_id = ?
          AND pf.mail_envoye_le IS NOT NULL
          AND pf.date_paiement IS NULL
        ORDER BY pf.numero_facture
    """, (campagne_id,)).fetchall()

    conn.close()

    lignes = _resoudre_lignes_email(lignes)

    sans_email = [l["nom_association"] for l in lignes if not l["email"]]
    if sans_email:
        flash(
            f"⚠️ {len(sans_email)} association(s) sans adresse email, non relançable(s) : "
            + ", ".join(sans_email),
            "warning"
        )

    return render_template(
        "tresorerie/participation/relance.html",
        campagne=campagne,
        mail_mode=mail_mode,
        mail_test_to=mail_test_to,
        mail_sender=mail_sender,
        numero_relance=numero_relance,
        lignes=lignes,
        preview=False
    )


@participation_bp.route("/participation/relance/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def relance(campagne_id):

    try:
        numero_relance = int(request.form.get("numero_relance"))
        confirm_envoi = request.form.get("confirm_envoi")
        confirm_production = request.form.get("confirm_production")

        mail_sender = request.form.get("mail_sender", "ba380.comptable@banquealimentaire.org")
        mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
        mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")

        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row

        campagne = conn.execute(
            "SELECT * FROM participation_campagnes WHERE id = ?", (campagne_id,)
        ).fetchone()

        if not campagne:
            conn.close()
            flash("❌ Campagne introuvable", "danger")
            return redirect(url_for("participation.selection"))

        code_modele = f"PARTICIPATION Relance {numero_relance + 1}"

        modele = conn.execute("""
            SELECT sujet, corps FROM modeles_emails WHERE code_modele = ? LIMIT 1
        """, (code_modele,)).fetchone()

        if not modele:
            conn.close()
            flash(f"❌ Modèle '{code_modele}' introuvable.", "danger")
            return redirect(url_for("participation.relance_start", campagne_id=campagne_id))

        sujet_modele = modele["sujet"]
        corps_modele = modele["corps"]

        lignes = conn.execute("""
            SELECT pf.*,
                   a.courriel_resp_tresorerie AS _assoc_tresorerie,
                   a.courriel_association AS _assoc_association
            FROM participation_factures pf
            LEFT JOIN associations a ON a.Id = pf.association_id
            WHERE pf.campagne_id = ?
              AND pf.mail_envoye_le IS NOT NULL
              AND pf.date_paiement IS NULL
            ORDER BY pf.numero_facture
        """, (campagne_id,)).fetchall()

        lignes = _resoudre_lignes_email(lignes)

        a_relancer = [l for l in lignes if (l["relance_niveau"] or 0) == numero_relance]

        total_relances = sum(float(l["montant_total"] or 0) for l in a_relancer)

        sans_email = [l["nom_association"] for l in a_relancer if not l["email"]]
        if sans_email:
            flash(
                f"⚠️ {len(sans_email)} association(s) sans adresse email, non relancée(s) : "
                + ", ".join(sans_email),
                "warning"
            )

        if not a_relancer:
            conn.close()
            return render_template(
                "tresorerie/participation/relance.html",
                campagne=campagne,
                mail_mode=mail_mode,
                mail_test_to=mail_test_to,
                mail_sender=mail_sender,
                numero_relance=numero_relance,
                lignes=[],
                preview=False,
                total_relances=0
            )

        if not confirm_envoi:
            conn.close()
            return render_template(
                "tresorerie/participation/relance.html",
                campagne=campagne,
                mail_mode=mail_mode,
                mail_test_to=mail_test_to,
                mail_sender=mail_sender,
                numero_relance=numero_relance,
                lignes=a_relancer,
                preview=True,
                total_relances=total_relances
            )

        if mail_mode == "PROD" and not confirm_production:
            conn.close()
            flash("⚠ Confirmation obligatoire en PRODUCTION.", "danger")
            return redirect(url_for("participation.relance_start", campagne_id=campagne_id))

        conn.close()

        items = [
            {
                "facture_id": l["id"],
                "association_id": l["association_id"],
                "nom_association": l["nom_association"],
                "numero_facture": l["numero_facture"],
                "montant_total": l["montant_total"],
                "detail_json": l["detail_json"],
                "email": l["email"],
            }
            for l in a_relancer
            if l["email"]
        ]

        if not items:
            flash("❌ Aucune association avec une adresse email valide à relancer.", "danger")
            return redirect(url_for("participation.relance_start", campagne_id=campagne_id))

        app_reel = current_app._get_current_object()
        db_path = get_db_path()

        Thread(
            target=envoyer_relances_participation_background,
            args=(app_reel, db_path, items, sujet_modele, corps_modele,
                  numero_relance, campagne["annee"], campagne["trimestre"],
                  mail_sender, mail_mode, mail_test_to)
        ).start()

        if mail_mode == "TEST":
            flash("🧪 Envoi TEST des relances lancé en arrière-plan (2 mails max vers l'adresse de test).", "warning")
        else:
            flash(f"🚀 Envoi des relances lancé en arrière-plan pour {len(items)} association(s).", "info")

        return redirect(url_for("participation.relance_start", campagne_id=campagne_id))

    except Exception:
        current_app.logger.exception("Erreur relance participation")
        flash("Erreur lors des relances.", "danger")
        return redirect(url_for("participation.relance_start", campagne_id=campagne_id))


@participation_bp.route("/participation/relance/<int:campagne_id>/verifier_statut_mailjet", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def relance_verifier_statut_mailjet(campagne_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    lignes = conn.execute("""
        SELECT id, relance_mailjet_message_ids
        FROM participation_factures
        WHERE campagne_id = ?
          AND relance_mailjet_message_ids IS NOT NULL
          AND relance_mailjet_message_ids != ''
    """, (campagne_id,)).fetchall()

    counts = {}
    verifies = 0

    for ligne in lignes:
        premier_id = ligne["relance_mailjet_message_ids"].split(",")[0]
        statut = mailjet_get_message_status(premier_id)

        if not statut:
            continue

        verifies += 1
        counts[statut] = counts.get(statut, 0) + 1

        conn.execute("""
            UPDATE participation_factures
            SET relance_statut_final = ?, relance_statut_verifie_le = ?
            WHERE id = ?
        """, (statut, datetime.now().isoformat(timespec="seconds"), ligne["id"]))

    conn.commit()
    conn.close()

    if verifies:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        flash(f"🔄 Statut Mailjet vérifié pour {verifies} mail(s) : {detail}", "info")
    else:
        flash("ℹ️ Aucun mail avec un identifiant Mailjet à vérifier pour cette campagne.", "warning")

    return redirect(url_for("participation.relance_start", campagne_id=campagne_id))


@participation_bp.route("/participation/relance/renvoyer_gmail/<int:facture_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def relance_renvoyer_gmail(facture_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    f = conn.execute("SELECT * FROM participation_factures WHERE id = ?", (facture_id,)).fetchone()

    if not f:
        conn.close()
        flash("❌ Ligne introuvable", "danger")
        return redirect(url_for("participation.selection"))

    destinataires = split_emails(f["email"])
    if not destinataires:
        conn.close()
        flash(f"❌ Aucune adresse email valide pour {f['nom_association']}", "danger")
        return redirect(url_for("participation.relance_start", campagne_id=f["campagne_id"]))

    if not f["relance_sujet"] or not f["relance_corps"]:
        conn.close()
        flash(f"⛔ Aucune relance précédente connue pour {f['nom_association']} — envoyez d'abord une relance.", "danger")
        return redirect(url_for("participation.relance_start", campagne_id=f["campagne_id"]))

    if f["mode_test_relance"]:
        conn.close()
        flash(f"⛔ La dernière relance pour {f['nom_association']} était en Mode TEST — un renvoi Gmail partirait, lui, pour de vrai. Refaites d'abord une relance réelle.", "danger")
        return redirect(url_for("participation.relance_start", campagne_id=f["campagne_id"]))

    assoc = None
    if f["association_id"]:
        assoc = conn.execute("SELECT * FROM associations WHERE Id = ?", (f["association_id"],)).fetchone()

    conn.close()

    adresse = "\n".join(filter(None, [
        assoc["adresse_association_1"] if assoc else "",
        assoc["adresse_association_2"] if assoc else "",
        " ".join(filter(None, [assoc["CP"], assoc["COMMUNE"]])) if assoc else "",
    ]))

    data_pdf = {
        "nom_association": f["nom_association"],
        "contact": (assoc["responsable_tresorerie"] if assoc else "") or "",
        "adresse": adresse,
        "email": f["email"],
        "numero_facture": f["numero_facture"],
        "lignes": json.loads(f["detail_json"]) if f["detail_json"] else [],
        "montant_total": f["montant_total"] or 0,
    }

    pdf_path = f"/tmp/participation_relance_gmail_{facture_id}.pdf"
    generer_facture_participation_pdf(data_pdf, pdf_path)

    conn = sqlite3.connect(get_db_path())

    try:
        envoyer_mail_gmail(
            sujet=f["relance_sujet"],
            destinataires=destinataires,
            texte=f["relance_corps"],
            attachment_path=pdf_path
        )

        conn.execute("""
            UPDATE participation_factures SET relance_renvoi_gmail_le = ? WHERE id = ?
        """, (datetime.now().isoformat(timespec="seconds"), facture_id))
        conn.commit()

        flash(f"📧 Relance renvoyée via Gmail à {f['email']} pour {f['nom_association']}", "success")

    except GmailSendError as e:
        write_log(f"❌ Erreur renvoi Gmail relance participation pour {f['nom_association']} : {e}")
        flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")

    finally:
        conn.close()
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

    return redirect(url_for("participation.relance_start", campagne_id=f["campagne_id"]))
