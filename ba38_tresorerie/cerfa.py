import os
import re
import time
import tempfile
from datetime import datetime

from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from flask import request, render_template, flash, redirect, url_for, session
from flask_login import login_required

from utils import write_log, envoyer_mail, require_access

from ba38_tresorerie import tresorerie_bp
from ba38_tresorerie.constants import MAX_TEST_PREVIEW, DATE_X, DATE_Y


@tresorerie_bp.route("/cerfa", methods=["GET", "POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cerfa():

    os.makedirs(os.getenv("TMP_DIR", "/srv/ba38/tmp"), exist_ok=True)
    tmp_dir = os.getenv("TMP_DIR", "/srv/ba38/tmp")

    mail_mode = session.get(
        "MAIL_MODE",
        os.getenv("MAIL_MODE", "TEST").upper()
    )

    mail_test_to = os.getenv(
        "MAIL_TEST_TO",
        "ba380.informatique2@banquealimentaire.org"
    )

    preview = []

    if request.method == "POST":

        # 🔁 CAS 1 : confirm (pas de fichier envoyé)
        if request.form.get("confirm") == "1":

            pdf_path = session.get("cerfa_pdf_path")

            if not pdf_path or not os.path.exists(pdf_path):
                flash("❌ Fichier introuvable, recommencez", "danger")
                return redirect(url_for("tresorerie.cerfa"))

            reader = PdfReader(pdf_path)
            pages = cerfa_extract_pages(pdf_path)

            sender = session.get("cerfa_sender")

            if not sender:
                sender = "ba380.informatique2@banquealimentaire.org"

        # 📂 CAS 2 : upload normal
        else:

            file = request.files.get("pdf_file")
            sender = request.form.get("mail_sender")
            session["cerfa_sender"] = sender

            if not file:
                flash("❌ Fichier manquant", "danger")
                return redirect(url_for("tresorerie.cerfa"))

            tmp_dir = os.getenv("TMP_DIR", "/srv/ba38/tmp")

            filename = f"cerfa_{int(time.time())}.pdf"
            tmp_path = os.path.join(tmp_dir, filename)

            file.save(tmp_path)

            # 🔍 Vérification fichier non vide
            if os.path.getsize(tmp_path) == 0:
                flash("❌ Fichier vide ou upload échoué", "danger")
                return redirect(url_for("tresorerie.cerfa"))

            session["cerfa_pdf_path"] = tmp_path

            reader = PdfReader(tmp_path)
            pages = cerfa_extract_pages(tmp_path)


        # ============================
        # Construction preview
        # ============================
        count_test = 0
        nb_sans_email = 0

        for p in pages:

            email_detecte = p.get("email")

            # ❌ CAS SANS EMAIL
            if not email_detecte:
                nb_sans_email += 1

                write_log(f"⚠️ CERFA sans email détecté : {p.get('nom')}")

                preview.append({
                    "nom": p.get("nom"),
                    "email": None,
                    "pdf": None,
                    "erreur": "Aucun email détecté dans le PDF"
                })

                continue

            # 🧪 MODE TEST
            email_envoi = email_detecte

            if mail_mode == "TEST":
                email_envoi = mail_test_to
                count_test += 1
                if count_test > MAX_TEST_PREVIEW:
                    break

            pdf_page = cerfa_extract_pdf_page(reader, p["page"])

            preview.append({
                "nom": p["nom"],
                "email": email_envoi,
                "email_reel": email_detecte,
                "pdf": pdf_page,
                "erreur": None
            })

        # ============================
        # ENVOI
        # ============================
        if request.form.get("confirm") == "1":

            # 🔒 sécurité PROD
            if mail_mode != "TEST" and not request.form.get("confirm_prod"):
                flash("⚠️ Confirmation PROD requise", "danger")
                return redirect(url_for("tresorerie.cerfa"))

            count = 0
            nb_ignores = 0

            for i, p in enumerate(preview):

                # ❌ IGNORER si pas d’email
                if not p["email"]:
                    nb_ignores += 1
                    continue

                # 🧪 MODE TEST → 1 seul envoi
                if mail_mode == "TEST" and i >= 1:
                    break

                signature_path = os.path.join(os.getenv("BASE_PATH", "/srv/ba38"), "static/signatures/signature_chantal_vivier.png")

                signed_pdf = ajouter_signature_pdf(
                    p["pdf"],
                    signature_path
                )

                envoyer_mail(
                    sujet="Votre reçu fiscal CERFA",
                    destinataires=[p["email"]],
                    texte="""Bonjour,

            Chers Collègues Bénévoles,

            Suite à votre déclaration d'abandons de frais kilométriques dans le cadre de vos activités régulières au sein de la BA38,
            pour l'année 2025,

            je vous prie de trouver en pièce jointe le Cerfa vous permettant de renseigner votre prochaine déclaration de revenus.

            Restant à votre disposition pour toute information complémentaire, bien cordialement.

            Dominique MELQUIOND
            Banque Alimentaire de l'Isère
            """,
                    sender_override=sender,
                    attachment_path=signed_pdf,
                    bcc=[sender]
                )

                count += 1

            write_log(f"CERFA envoyés : {count}")
            write_log(f"CERFA ignorés (sans email) : {nb_ignores}")

            flash(
                f"✅ {count} CERFA envoyés — ⚠️ {nb_ignores} sans email ignorés",
                "warning" if nb_ignores else "success"
            )

            # 🧹 nettoyage fichier temporaire
            pdf_path = session.get("cerfa_pdf_path")
            if pdf_path and os.path.exists(pdf_path):
                os.remove(pdf_path)

            session.pop("cerfa_pdf_path", None)

            return redirect(url_for("tresorerie.cerfa"))

        return render_template(
            "tresorerie/cerfa_preview.html",
            preview=preview,
            mail_mode=mail_mode,
            mail_test_to=mail_test_to
        )

    return render_template(
        "tresorerie/cerfa_upload.html",
        mail_mode=mail_mode,
        mail_test_to=mail_test_to
    )


def ajouter_signature_pdf(input_pdf, signature_png):

    reader = PdfReader(input_pdf)
    writer = PdfWriter()

    page = reader.pages[0]

    # taille page
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)

    # créer un PDF temporaire avec signature + date
    tmp_overlay = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")

    c = canvas.Canvas(tmp_overlay.name, pagesize=(width, height))

    # ============================
    # SIGNATURE
    # ============================
    x_signature = width - 200   # à droite
    y_signature = 50            # bas de page

    c.drawImage(signature_png, x_signature, y_signature,
                width=150, height=50, mask='auto')

    # ============================
    # DATE
    # ============================
    now = datetime.now()

    MOIS_FR = [
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre"
    ]

    date_str = f"{now.day} {MOIS_FR[now.month - 1]} {now.year}"

    c.setFont("Helvetica", 10)

    # utilise tes variables globales
    c.drawString(DATE_X, DATE_Y, date_str)

    c.save()

    # ============================
    # FUSION AVEC PAGE
    # ============================
    overlay_reader = PdfReader(tmp_overlay.name)
    page.merge_page(overlay_reader.pages[0])

    writer.add_page(page)

    # ============================
    # SORTIE
    # ============================
    output = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")

    with open(output.name, "wb") as f:
        writer.write(f)

    return output.name


def cerfa_extract_pages(pdf_path):

    reader = PdfReader(pdf_path)

    results = []

    for i, page in enumerate(reader.pages):

        try:
            text = page.extract_text() or ""
        except:
            text = ""

        # 🔍 email
        m_email = re.search(r'[\w\.-]+@[\w\.-]+', text)
        email = m_email.group(0) if m_email else None

        # 🔍 nom (plus robuste)
        m_nom = re.search(r"(Madame|Monsieur)\s+[A-ZÉÈÊÀÙÂÎÔÛÇ\-]+\s+[A-Za-zéèêàùâîôûç\-]+", text)
        nom = m_nom.group(0) if m_nom else None

        results.append({
            "page": i,
            "nom": nom,
            "email": email
        })

        # write_log(f"PAGE {i} → {nom} / {email}")

    return results


def cerfa_extract_pdf_page(reader, page_index):

    writer = PdfWriter()
    writer.add_page(reader.pages[page_index])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")

    with open(tmp.name, "wb") as f:
        writer.write(f)

    return tmp.name


@tresorerie_bp.route("/cerfa/toggle_mode", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cerfa_toggle_mode():

    current = session.get("MAIL_MODE", "TEST")

    if current == "TEST":
        session["MAIL_MODE"] = "PROD"
    else:
        session["MAIL_MODE"] = "TEST"

    write_log(f"CERFA - Changement mode → {session['MAIL_MODE']}")

    return redirect(url_for("tresorerie.cerfa"))
