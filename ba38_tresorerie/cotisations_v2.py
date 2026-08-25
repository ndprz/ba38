import os
import io
import json
import sqlite3
import tempfile
from datetime import datetime, date
from pathlib import Path
from threading import Thread

from flask import request, render_template, flash, redirect, url_for, jsonify, send_file, current_app, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

from ba38_utilitaires.core import get_db_path, require_access, write_log, envoyer_mail, render_modele_email, split_emails, mailjet_get_message_status

from ba38_tresorerie import tresorerie_bp
from ba38_tresorerie.constants import (
    BAI_NOM, BAI_ADRESSE, BAI_TEL, BAI_MAIL, BAI_IBAN, BAI_BIC, BAI_SIREN, BAI_NAF,
)
from ba38_tresorerie.cotisations import parse_parsol2l_annuel, calculer_cotisations_par_annee


# ============================================================================
# 📄 GÉNÉRATION PDF (adapté de ba38_tresorerie/cotisations.py::generer_facture_pdf,
# sans le branchement mode_relance — en V2 la relance renvoie la même facture)
# ============================================================================
def generer_facture_cotisation_v2_pdf(data, output_path):
    """
    data doit contenir : nom_association, adresse (multi-lignes), cotisation,
    annee, numero_facture, commentaire_regroupement (optionnel).
    """

    c = canvas.Canvas(str(output_path), pagesize=A4)
    largeur, hauteur = A4

    logo_path = Path(current_app.root_path) / "static" / "images" / "logo_ba_complet.png"

    if logo_path.exists():
        logo = ImageReader(str(logo_path))
        logo_width = 120 * mm
        logo_height = 46 * mm
        correction_x = -20 * mm
        x_logo = (largeur - logo_width) / 2 - correction_x
        y_logo = hauteur - logo_height - 5 * mm
        c.drawImage(
            logo, x_logo, y_logo, width=logo_width, height=logo_height,
            preserveAspectRatio=True, mask="auto"
        )

    y = hauteur - 20 * mm - 40 * mm

    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, BAI_NOM)
    c.setFont("Helvetica", 9)
    y -= 12
    for line in BAI_ADRESSE.split("\n"):
        c.drawString(20 * mm, y, line)
        y -= 10
    c.drawString(20 * mm, y, f"Tél : {BAI_TEL}")
    y -= 10
    c.drawString(20 * mm, y, f"Mail : {BAI_MAIL}")

    y_fact = hauteur - 20 * mm - 40 * mm
    c.setFont("Helvetica", 9)
    c.drawRightString(largeur - 20 * mm, y_fact, f"Date : {date.today().strftime('%d/%m/%Y')}")
    y_fact -= 15
    c.drawRightString(largeur - 20 * mm, y_fact, f"Échéance : 28/02/{data['annee']}")
    y_fact -= 15
    c.drawRightString(largeur - 20 * mm, y_fact, f"Facture n° {data.get('numero_facture')}")
    if data.get("code_vif"):
        y_fact -= 15
        c.drawRightString(largeur - 20 * mm, y_fact, f"Code VIF : {data['code_vif']}")

    y -= 40
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, y, data["nom_association"])
    c.setFont("Helvetica", 9)
    y -= 12
    for line in (data.get("adresse") or "").split("\n"):
        if line.strip():
            c.drawString(20 * mm, y, line)
            y -= 10

    y -= 20
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y, "Désignation")
    c.drawRightString(largeur - 20 * mm, y, "Montant")
    y -= 10
    c.line(20 * mm, y, largeur - 20 * mm, y)
    y -= 15
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, y, f"COTISATION {data['annee']}")
    c.drawRightString(largeur - 20 * mm, y, f"{data['cotisation']:.2f} €")

    y -= 30
    c.line(120 * mm, y, largeur - 20 * mm, y)
    y -= 15
    c.setFont("Helvetica-Bold", 10)
    c.drawString(120 * mm, y, "Net à payer")
    c.drawRightString(largeur - 20 * mm, y, f"{data['cotisation']:.2f} €")

    y -= 30
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y, "TVA non applicable, art. 293B du CGI")
    y -= 15
    c.drawString(20 * mm, y, f"IBAN : {BAI_IBAN}")
    y -= 10
    c.drawString(20 * mm, y, f"BIC : {BAI_BIC}")
    y -= 20
    c.drawString(20 * mm, y, f"SIREN : {BAI_SIREN}")
    y -= 10
    c.drawString(20 * mm, y, f"NAF : {BAI_NAF}")

    commentaire = data.get("commentaire_regroupement")
    if commentaire:
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph
        from reportlab.lib.enums import TA_LEFT

        styles = getSampleStyleSheet()
        style = styles["Normal"]
        style.fontName = "Helvetica-Oblique"
        style.fontSize = 8
        style.leading = 10
        style.alignment = TA_LEFT

        largeur_bloc = largeur - 40 * mm
        p = Paragraph(commentaire.replace("\n", "<br/>"), style)
        p.wrap(largeur_bloc, 60 * mm)
        p.drawOn(c, 20 * mm, 35 * mm)

    c.showPage()
    c.save()


# ============================================================================
# 🔁 RATTACHEMENT AUTOMATIQUE DES ORPHELINES
# ============================================================================
def _rattacher_orphelines(conn, campagne_id):
    """
    Réessaie de rapprocher les factures orphelines (code VIF sans
    correspondance en base au moment du traitement du fichier PARSOL2L) avec
    la table associations — utile quand l'association, ou son code VIF, a
    été créée/corrigée après coup (même mécanisme que Participation V2).
    Le montant est recalculé via le barème sur les seuls bénéficiaires de la
    ligne orpheline (un éventuel regroupement vif_regroup_cotisation ne peut
    pas être reconstitué rétroactivement — cas rare, à traiter par
    retraitement manuel si besoin).
    """
    orphelines = conn.execute("""
        SELECT id, code_vif, beneficiaires FROM cotisations_v2_factures
        WHERE campagne_id = ? AND association_id IS NULL
    """, (campagne_id,)).fetchall()

    if not orphelines:
        return []

    max_numero = conn.execute("""
        SELECT MAX(numero_facture) FROM cotisations_v2_factures WHERE campagne_id = ?
    """, (campagne_id,)).fetchone()[0] or 0

    rattachees = []

    for o in orphelines:
        assoc = conn.execute(
            "SELECT * FROM associations WHERE code_VIF = ?", (o["code_vif"],)
        ).fetchone()
        if not assoc:
            continue

        beneficiaires = o["beneficiaires"] or 0
        if beneficiaires <= 1000:
            montant = 50
        elif beneficiaires <= 10000:
            montant = 80
        else:
            montant = 110

        max_numero += 1
        email = assoc["courriel_resp_tresorerie"] or assoc["courriel_association"]
        detail_json = json.dumps({"codes_vif_inclus": [o["code_vif"]], "commentaire_regroupement": None})

        conn.execute("""
            UPDATE cotisations_v2_factures
            SET association_id = ?, numero_facture = ?, nom_association = ?,
                nom_association_affichage = ?, montant = ?, email = ?, detail_json = ?
            WHERE id = ?
        """, (assoc["Id"], max_numero, assoc["nom_association"], assoc["nom_association"],
              montant, email, detail_json, o["id"]))

        rattachees.append((assoc["nom_association"], max_numero))

    if rattachees:
        conn.commit()

    return rattachees


# ============================================================================
# 🚀 ENVOI — TRAITEMENT ARRIÈRE-PLAN
# ============================================================================
def envoyer_cotisations_v2_background(app, db_path, campagne_id, items, mail_mode, mail_test_to, current_user_email):
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

            pdf_path = f"/tmp/cotisations_v2_{item['facture_id']}.pdf"

            try:
                if not destinataires:
                    raise ValueError(f"Aucune adresse email valide pour {item['nom_association']}")

                assoc = conn.execute(
                    "SELECT * FROM associations WHERE Id = ?", (item["association_id"],)
                ).fetchone()

                adresse = "\n".join(filter(None, [
                    assoc["adresse_association_1"] if assoc else "",
                    assoc["adresse_association_2"] if assoc else "",
                    " ".join(filter(None, [assoc["CP"], assoc["COMMUNE"]])) if assoc else "",
                ]))

                detail = json.loads(item["detail_json"]) if item["detail_json"] else {}

                data_pdf = {
                    "nom_association": item["nom_association"],
                    "adresse": adresse,
                    "cotisation": item["montant"],
                    "annee": item["annee"],
                    "numero_facture": item["numero_facture"],
                    "code_vif": item["code_vif"],
                    "commentaire_regroupement": detail.get("commentaire_regroupement"),
                }

                generer_facture_cotisation_v2_pdf(data_pdf, pdf_path)

                write_log(f"📧 Envoi facture cotisation V2 à {email_envoi} | facture_id={item['facture_id']}")

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
                    UPDATE cotisations_v2_factures
                    SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = NULL,
                        mail_mailjet_status = ?, mail_mailjet_message_ids = ?,
                        mail_modele_id = ?, sujet = ?, corps = ?
                    WHERE id = ?
                """, (now_iso, 1 if mail_mode == "TEST" else 0, mj_status, mj_ids,
                      item["modele_id"], sujet_envoi, item["corps"], item["facture_id"]))
                conn.commit()

                envoyes += 1

            except Exception as e:
                write_log(f"❌ Erreur envoi facture cotisation V2 (facture_id={item['facture_id']}) : {e}")
                nb_erreurs += 1

                conn.execute("""
                    UPDATE cotisations_v2_factures
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
            UPDATE cotisations_v2_campagnes
            SET dernier_envoi_le = ?, dernier_envoi_par = ?, dernier_envoi_mode_test = ?,
                dernier_envoi_nb_ok = ?, dernier_envoi_nb_erreur = ?
            WHERE id = ?
        """, (now_iso, current_user_email, 1 if mail_mode == "TEST" else 0, envoyes, nb_erreurs, campagne_id))
        conn.commit()
        conn.close()

        write_log(f"📤 Envoi cotisations V2 (background) terminé : {envoyes} envoyés, {nb_erreurs} erreur(s)")


# ============================================================================
# 📅 SÉLECTION DE L'ANNÉE
# ============================================================================
@tresorerie_bp.route("/cotisations_v2")
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_selection():
    annee_now = datetime.now().year
    annees = sorted({annee_now - 1, annee_now, annee_now + 1}, reverse=True)
    return render_template("tresorerie/cotisations_v2/selection.html", annees=annees)


@tresorerie_bp.route("/cotisations_v2/check_annee")
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_check_annee():
    try:
        annee = int(request.args.get("annee"))
    except (TypeError, ValueError):
        return jsonify({"exists": False, "id": None})

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    campagne = conn.execute("""
        SELECT id FROM cotisations_v2_campagnes WHERE annee = ? ORDER BY id DESC LIMIT 1
    """, (annee,)).fetchone()
    conn.close()

    return jsonify({"exists": campagne is not None, "id": campagne["id"] if campagne else None})


# ============================================================================
# 📂 TRAITEMENT DU FICHIER PARSOL2L
# ============================================================================
@tresorerie_bp.route("/cotisations_v2/traiter", methods=["GET", "POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_traiter():

    annee = request.values.get("annee", "")

    if request.method == "GET":
        return render_template("tresorerie/cotisations_v2/upload.html", annee=annee)

    fichier = request.files.get("parsol_file")
    numero_facture_depart = request.form.get("numero_facture_depart", "").strip()

    if not fichier:
        flash("❌ Fichier manquant", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_traiter", annee=annee))

    if not numero_facture_depart.isdigit():
        flash("❌ Numéro de facture de départ invalide", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_traiter", annee=annee))

    numero_facture_depart = int(numero_facture_depart)
    annee_int = int(annee) if annee else datetime.now().year

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        fichier.save(tmp.name)
        parsol_path = tmp.name

    try:
        benefs = parse_parsol2l_annuel(parsol_path)
    finally:
        os.remove(parsol_path)

    if not benefs:
        flash("❌ Aucune donnée détectée dans le fichier — vérifiez le format.", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_traiter", annee=annee))

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ==================================================
    # 🔒 Campagne existante pour cette année ?
    # ==================================================
    existante = conn.execute("""
        SELECT id FROM cotisations_v2_campagnes WHERE annee = ? ORDER BY id DESC LIMIT 1
    """, (annee_int,)).fetchone()

    if existante:
        nb_envois_reels = conn.execute("""
            SELECT COUNT(*) FROM cotisations_v2_factures
            WHERE campagne_id = ? AND mail_envoye_le IS NOT NULL AND mail_mode_test = 0
        """, (existante["id"],)).fetchone()[0]

        if nb_envois_reels > 0:
            conn.close()
            flash(
                f"⛔ Un envoi réel a déjà eu lieu pour {annee_int} ({nb_envois_reels} facture(s)) — "
                "retraitement bloqué pour ne pas créer de doublon. Consultez d'abord l'écran Résultats.",
                "danger"
            )
            return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=existante["id"]))

        campagne_id = existante["id"]
        conn.execute("DELETE FROM cotisations_v2_factures WHERE campagne_id = ?", (campagne_id,))
        conn.execute("""
            UPDATE cotisations_v2_campagnes
            SET fichier_source = ?, numero_facture_depart = ?, date_creation = ?, cree_par = ?,
                dernier_envoi_le = NULL, dernier_envoi_par = NULL, dernier_envoi_mode_test = 0,
                dernier_envoi_nb_ok = NULL, dernier_envoi_nb_erreur = NULL
            WHERE id = ?
        """, (secure_filename(fichier.filename) if fichier.filename else "parsol2l_annuel.txt",
              numero_facture_depart, datetime.now().isoformat(timespec="seconds"), current_user.email,
              campagne_id))
    else:
        cur = conn.execute("""
            INSERT INTO cotisations_v2_campagnes (annee, fichier_source, numero_facture_depart, date_creation, cree_par)
            VALUES (?, ?, ?, ?, ?)
        """, (annee_int, secure_filename(fichier.filename) if fichier.filename else "parsol2l_annuel.txt",
              numero_facture_depart, datetime.now().isoformat(timespec="seconds"), current_user.email))
        campagne_id = cur.lastrowid

    data = calculer_cotisations_par_annee(db_path, benefs)
    facturables = sorted(data["facturables"], key=lambda x: (x.get("compte_comptable") or ""))
    orphelines = data["orphelines"]

    for i, r in enumerate(facturables):
        numero_facture = numero_facture_depart + i

        # Préférence courriel_resp_tresorerie > courriel_association, comme
        # le fait l'ancien flux au moment de l'envoi (calculer_cotisations_par_annee
        # ne renvoie que courriel_association).
        resp = conn.execute(
            "SELECT courriel_resp_tresorerie FROM associations WHERE Id = ?", (r["id_association"],)
        ).fetchone()
        email = (resp["courriel_resp_tresorerie"] if resp else None) or r.get("email")

        detail_json = json.dumps({
            "codes_vif_inclus": r["codes_vif_inclus"],
            "commentaire_regroupement": r.get("commentaire_regroupement"),
        })

        conn.execute("""
            INSERT INTO cotisations_v2_factures
            (campagne_id, association_id, code_vif, numero_facture, nom_association,
             nom_association_affichage, beneficiaires, montant, detail_json, email)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (campagne_id, r["id_association"], r["code_vif_facture"], numero_facture,
              r["nom_association"], r["nom_association_affichage"], r["beneficiaires"],
              r["cotisation"], detail_json, email))

    for code_vif, nb in orphelines.items():
        conn.execute("""
            INSERT INTO cotisations_v2_factures (campagne_id, code_vif, beneficiaires)
            VALUES (?, ?, ?)
        """, (campagne_id, code_vif, nb))

    conn.commit()
    conn.close()

    verbe = "recréée(s)" if existante else "créée(s)"
    msg = f"📊 {len(facturables)} facture(s) {verbe}"
    if orphelines:
        msg += f" — ⚠️ {len(orphelines)} code(s) VIF non trouvé(s) en base : " + ", ".join(orphelines.keys())
    flash(msg, "warning" if orphelines else "info")

    return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=campagne_id))


# ============================================================================
# 📊 ÉCRAN RÉSULTATS
# ============================================================================
@tresorerie_bp.route("/cotisations_v2/resultats/<int:campagne_id>")
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_resultats(campagne_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    campagne = conn.execute("SELECT * FROM cotisations_v2_campagnes WHERE id = ?", (campagne_id,)).fetchone()

    if not campagne:
        conn.close()
        flash("❌ Campagne introuvable", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_selection"))

    rattachees = _rattacher_orphelines(conn, campagne_id)
    if rattachees:
        detail = ", ".join(f"{nom} (facture n°{num})" for nom, num in rattachees)
        flash(
            f"✅ {len(rattachees)} facture(s) orpheline(s) rattachée(s) automatiquement "
            f"(association créée ou corrigée depuis) : {detail}",
            "success"
        )

    factures = conn.execute("""
        SELECT * FROM cotisations_v2_factures
        WHERE campagne_id = ? AND association_id IS NOT NULL
        ORDER BY numero_facture
    """, (campagne_id,)).fetchall()

    orphelines = conn.execute("""
        SELECT * FROM cotisations_v2_factures
        WHERE campagne_id = ? AND association_id IS NULL
        ORDER BY code_vif
    """, (campagne_id,)).fetchall()

    modeles = conn.execute("""
        SELECT * FROM modeles_emails WHERE type_periode = 'facture' ORDER BY TRIM(code_modele) COLLATE NOCASE
    """).fetchall()

    conn.close()

    reste_a_envoyer = any(f["email"] and not f["mail_envoye_le"] for f in factures)
    mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")
    montant_total_campagne = sum(f["montant"] or 0 for f in factures)

    return render_template(
        "tresorerie/cotisations_v2/resultats.html",
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
# 📊 EXPORT EXCEL
# ============================================================================
@tresorerie_bp.route("/cotisations_v2/export_excel/<int:campagne_id>")
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_export_excel(campagne_id):

    import pandas as pd

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    campagne = conn.execute("SELECT * FROM cotisations_v2_campagnes WHERE id = ?", (campagne_id,)).fetchone()

    if not campagne:
        conn.close()
        flash("❌ Campagne introuvable", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_selection"))

    df = pd.read_sql_query("""
        SELECT
            numero_facture AS "N° facture",
            code_vif AS "Code VIF",
            COALESCE(nom_association_affichage, nom_association) AS "Association",
            email AS "Email",
            beneficiaires AS "Bénéficiaires",
            montant AS "Montant",
            CASE WHEN date_paiement IS NOT NULL THEN 'Payé' ELSE 'Impayé' END AS "Statut paiement",
            date_paiement AS "Date paiement",
            mail_envoye_le AS "Mail envoyé le",
            relance_niveau AS "Niveau de relance"
        FROM cotisations_v2_factures
        WHERE campagne_id = ? AND association_id IS NOT NULL
        ORDER BY numero_facture
    """, conn, params=(campagne_id,))

    conn.close()

    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=f"cotisations_v2_{campagne['annee']}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ============================================================================
# 💳 PAIEMENT — TOGGLE "PAYÉ"
# ============================================================================
@tresorerie_bp.route("/cotisations_v2/toggle_paye/<int:facture_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_toggle_paye(facture_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    f = conn.execute(
        "SELECT date_paiement FROM cotisations_v2_factures WHERE id = ?", (facture_id,)
    ).fetchone()

    if not f:
        conn.close()
        return jsonify({"error": "introuvable"}), 404

    nouveau = None if f["date_paiement"] else datetime.now().strftime("%Y-%m-%d")

    conn.execute(
        "UPDATE cotisations_v2_factures SET date_paiement = ? WHERE id = ?",
        (nouveau, facture_id)
    )
    conn.commit()
    conn.close()

    return jsonify({"date_paiement": nouveau})


# ============================================================================
# 📧 ENVOYER LES MAILS
# ============================================================================
@tresorerie_bp.route("/cotisations_v2/envoyer/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_envoyer(campagne_id):

    modele_id = request.form.get("modele_id")
    if not modele_id:
        flash("❌ Aucun modèle de mail sélectionné", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=campagne_id))

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    campagne = conn.execute("SELECT * FROM cotisations_v2_campagnes WHERE id = ?", (campagne_id,)).fetchone()
    if not campagne:
        conn.close()
        flash("❌ Campagne introuvable", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_selection"))

    modele = conn.execute("SELECT * FROM modeles_emails WHERE id = ?", (modele_id,)).fetchone()
    if not modele:
        conn.close()
        flash("❌ Modèle introuvable", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=campagne_id))

    a_envoyer = conn.execute("""
        SELECT * FROM cotisations_v2_factures
        WHERE campagne_id = ? AND email IS NOT NULL AND mail_envoye_le IS NULL
    """, (campagne_id,)).fetchall()

    conn.close()

    if not a_envoyer:
        flash("ℹ️ Rien à envoyer : toutes les factures de cette campagne ont déjà été traitées.", "warning")
        return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=campagne_id))

    items = []
    for f in a_envoyer:
        contexte = {"nom_association": f["nom_association"], "annee": campagne["annee"]}
        items.append({
            "facture_id": f["id"],
            "association_id": f["association_id"],
            "email": f["email"],
            "sujet": render_modele_email(modele["sujet"], contexte).strip(),
            "corps": render_modele_email(modele["corps"], contexte),
            "nom_association": f["nom_association"],
            "numero_facture": f["numero_facture"],
            "code_vif": f["code_vif"],
            "montant": f["montant"],
            "annee": campagne["annee"],
            "detail_json": f["detail_json"],
            "modele_id": modele_id,
        })

    mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")

    app_reel = current_app._get_current_object()
    current_user_email = current_user.email

    Thread(
        target=envoyer_cotisations_v2_background,
        args=(app_reel, db_path, campagne_id, items, mail_mode, mail_test_to, current_user_email)
    ).start()

    if mail_mode == "TEST":
        flash("🧪 Envoi TEST lancé en arrière-plan (2 mails max vers l'adresse de test).", "warning")
    else:
        flash(f"🚀 Envoi réel lancé en arrière-plan pour {len(items)} facture(s).", "info")

    return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=campagne_id))


# ============================================================================
# 👁️ VOIR LE PDF D'UNE FACTURE (régénéré à la demande)
# ============================================================================
@tresorerie_bp.route("/cotisations_v2/voir_pdf/<int:facture_id>")
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_voir_pdf(facture_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    f = conn.execute("SELECT * FROM cotisations_v2_factures WHERE id = ?", (facture_id,)).fetchone()

    if not f:
        conn.close()
        return "Ligne introuvable", 404

    campagne = conn.execute("SELECT * FROM cotisations_v2_campagnes WHERE id = ?", (f["campagne_id"],)).fetchone()

    assoc = None
    if f["association_id"]:
        assoc = conn.execute("SELECT * FROM associations WHERE Id = ?", (f["association_id"],)).fetchone()

    conn.close()

    adresse = "\n".join(filter(None, [
        assoc["adresse_association_1"] if assoc else "",
        assoc["adresse_association_2"] if assoc else "",
        " ".join(filter(None, [assoc["CP"], assoc["COMMUNE"]])) if assoc else "",
    ]))

    detail = json.loads(f["detail_json"]) if f["detail_json"] else {}

    data_pdf = {
        "nom_association": f["nom_association"] or "—",
        "adresse": adresse,
        "cotisation": f["montant"] or 0,
        "annee": campagne["annee"] if campagne else "",
        "numero_facture": f["numero_facture"] or "—",
        "code_vif": f["code_vif"],
        "commentaire_regroupement": detail.get("commentaire_regroupement"),
    }

    pdf_path = f"/tmp/cotisations_v2_voir_{facture_id}.pdf"
    generer_facture_cotisation_v2_pdf(data_pdf, pdf_path)

    return send_file(pdf_path, mimetype="application/pdf")


# ============================================================================
# 🔄 VÉRIFIER STATUT MAILJET
# ============================================================================
@tresorerie_bp.route("/cotisations_v2/verifier_statut_mailjet/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_verifier_statut_mailjet(campagne_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    lignes = conn.execute("""
        SELECT id, mail_mailjet_message_ids
        FROM cotisations_v2_factures
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
            UPDATE cotisations_v2_factures
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

    return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=campagne_id))


# ============================================================================
# 🔁 RENVOYER VIA GMAIL
# ============================================================================
@tresorerie_bp.route("/cotisations_v2/renvoyer_gmail/<int:facture_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_renvoyer_gmail(facture_id):
    from ba38_utilitaires.gmail_send import envoyer_mail_gmail, GmailSendError

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    f = conn.execute("SELECT * FROM cotisations_v2_factures WHERE id = ?", (facture_id,)).fetchone()

    if not f:
        conn.close()
        flash("❌ Ligne introuvable", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_selection"))

    destinataires = split_emails(f["email"])
    if not destinataires:
        conn.close()
        flash(f"❌ Aucune adresse email valide pour {f['nom_association']}", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=f["campagne_id"]))

    if not f["sujet"] or not f["corps"]:
        conn.close()
        flash(f"⛔ Aucun envoi précédent connu pour {f['nom_association']} — utilisez d'abord « Envoyer les mails ».", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=f["campagne_id"]))

    if f["mail_mode_test"]:
        conn.close()
        flash(f"⛔ Le dernier envoi pour {f['nom_association']} était en Mode TEST — un renvoi Gmail partirait, lui, pour de vrai. Refaites d'abord un envoi réel.", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=f["campagne_id"]))

    campagne = conn.execute("SELECT * FROM cotisations_v2_campagnes WHERE id = ?", (f["campagne_id"],)).fetchone()

    assoc = None
    if f["association_id"]:
        assoc = conn.execute("SELECT * FROM associations WHERE Id = ?", (f["association_id"],)).fetchone()

    conn.close()

    adresse = "\n".join(filter(None, [
        assoc["adresse_association_1"] if assoc else "",
        assoc["adresse_association_2"] if assoc else "",
        " ".join(filter(None, [assoc["CP"], assoc["COMMUNE"]])) if assoc else "",
    ]))

    detail = json.loads(f["detail_json"]) if f["detail_json"] else {}

    data_pdf = {
        "nom_association": f["nom_association"],
        "adresse": adresse,
        "cotisation": f["montant"] or 0,
        "annee": campagne["annee"] if campagne else "",
        "numero_facture": f["numero_facture"],
        "code_vif": f["code_vif"],
        "commentaire_regroupement": detail.get("commentaire_regroupement"),
    }

    pdf_path = f"/tmp/cotisations_v2_gmail_{facture_id}.pdf"
    generer_facture_cotisation_v2_pdf(data_pdf, pdf_path)

    conn = sqlite3.connect(get_db_path())

    try:
        envoyer_mail_gmail(
            sujet=f["sujet"],
            destinataires=destinataires,
            texte=f["corps"],
            attachment_path=pdf_path
        )

        conn.execute("""
            UPDATE cotisations_v2_factures SET mail_renvoi_gmail_le = ? WHERE id = ?
        """, (datetime.now().isoformat(timespec="seconds"), facture_id))
        conn.commit()

        flash(f"📧 Facture renvoyée via Gmail à {f['email']} pour {f['nom_association']}", "success")

    except GmailSendError as e:
        write_log(f"❌ Erreur renvoi Gmail cotisation V2 pour {f['nom_association']} : {e}")
        flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")

    finally:
        conn.close()
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

    return redirect(url_for("tresorerie.cotisations_v2_resultats", campagne_id=f["campagne_id"]))
