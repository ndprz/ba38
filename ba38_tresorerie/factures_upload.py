import os
import re
import time
import sqlite3
import tempfile
from datetime import datetime
from threading import Thread

from PyPDF2 import PdfReader, PdfWriter

from flask import (
    request, render_template, flash, redirect, url_for, send_file,
    current_app, session, jsonify,
)
from flask_login import login_required, current_user

from utils import get_db_path, write_log, envoyer_mail, require_access, render_modele_email

from ba38_tresorerie import tresorerie_bp


# ==========================================================
# 🔧 EXTRACTION PDF
# ==========================================================
def extract_pages(pdf_path):

    import pdfplumber

    result = []
    current = None

    with pdfplumber.open(pdf_path) as pdf:

        for i, page in enumerate(pdf.pages, start=1):

            text = page.extract_text() or ""

            write_log(f"PAGE {i+1} / LEN={len(text)} / FIRST100={text[:100]}")

            # ❌ IGNORER pages parasites
            if "EBP FFBA" in text:
                continue

            nom, email = extract_infos_facture(text)

            if nom:
                current = {
                    "nom": nom,
                    "email": email,
                    "pages": [i]
                }
                result.append(current)

            elif current:
                current["pages"].append(i)

    return result


# ==========================================================
# 🔧 EXTRACTION NOM + EMAIL
# ==========================================================
def extract_infos_facture(text):

    lignes = text.split("\n")

    nom = None

    if len(lignes) > 6:
        candidat = lignes[6].strip()

        if (
            "siret" not in candidat.lower()
            and "rna" not in candidat.lower()
            and not candidat.isdigit()
            and len(candidat) > 5
        ):
            nom = candidat

    EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    emails = []
    for l in lignes:
        emails.extend(EMAIL_REGEX.findall(l))

    emails_valides = [
        e for e in emails
        if "banquealimentaire" not in e.lower()
        and not e.lower().startswith("ba380")
    ]

    email = emails_valides[-1] if emails_valides else None

    return nom, email


# ==========================================================
# 🔧 BUILD PDF (extraction d'un sous-ensemble de pages)
# ==========================================================
def build_pdf(reader, pages):

    import tempfile
    from PyPDF2 import PdfWriter

    writer = PdfWriter()

    for p in pages:
        writer.add_page(reader.pages[p - 1])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")

    with open(tmp.name, "wb") as f:
        writer.write(f)

    return tmp.name


# ==========================================================
# 🚀 ENVOI FACTURES — TRAITEMENT ARRIÈRE-PLAN
# ==========================================================
# Tourne dans un Thread séparé (même pattern que
# ba38_emails.py::envoyer_indicateurs_background). Persiste chaque tentative
# en base (factures_envois) pour permettre le suivi, le statut Mailjet et le
# renvoi manuel via Gmail. Un try/except par facture évite qu'une erreur sur
# l'une d'elles n'interrompe l'envoi des suivantes (bug de l'ancienne version
# non persistante).
def envoyer_factures_background(app, db_path, lot_id, items, pdf_path,
                                 mail_mode, mail_test_to, mail_sender,
                                 current_user_email):
    with app.app_context():
        reader = PdfReader(pdf_path)

        envoyes = 0
        nb_erreurs = 0
        count_test = 0
        now_iso = datetime.now().isoformat(timespec="seconds")

        conn = sqlite3.connect(db_path)

        for item in items:

            envoi_id = item["envoi_id"]
            pages = item["pages"]
            email = item["email"]
            sujet = item["sujet"]
            corps = item["corps"]

            if mail_mode == "TEST":
                if count_test >= 2:
                    break
                count_test += 1
                email_envoi = mail_test_to
                sujet = f"🧪 [TEST] {sujet}"
            else:
                email_envoi = email

            try:
                fichier = build_pdf(reader, pages)

                write_log(f"📧 Envoi facture à {email_envoi} | envoi_id={envoi_id}")

                resultat = envoyer_mail(
                    sujet=sujet,
                    destinataires=[email_envoi],
                    texte=corps,
                    sender_override=mail_sender,
                    attachment_path=fichier,
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
                    UPDATE factures_envois
                    SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = NULL,
                        mail_mailjet_status = ?, mail_mailjet_message_ids = ?
                    WHERE id = ?
                """, (now_iso, 1 if mail_mode == "TEST" else 0, mj_status, mj_ids, envoi_id))
                conn.commit()

                envoyes += 1

                if os.path.exists(fichier):
                    os.remove(fichier)

            except Exception as e:
                write_log(f"❌ Erreur envoi facture (envoi_id={envoi_id}) : {e}")
                nb_erreurs += 1

                conn.execute("""
                    UPDATE factures_envois
                    SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = ?,
                        mail_mailjet_status = NULL, mail_mailjet_message_ids = NULL
                    WHERE id = ?
                """, (now_iso, 1 if mail_mode == "TEST" else 0, str(e), envoi_id))
                conn.commit()

        conn.execute("""
            UPDATE factures_lots
            SET dernier_envoi_le = ?, dernier_envoi_par = ?, dernier_envoi_mode_test = ?,
                dernier_envoi_nb_ok = ?, dernier_envoi_nb_erreur = ?
            WHERE id = ?
        """, (now_iso, current_user_email, 1 if mail_mode == "TEST" else 0, envoyes, nb_erreurs, lot_id))
        conn.commit()
        conn.close()

        write_log(f"📤 Envoi factures (background) terminé : {envoyes} envoyés, {nb_erreurs} erreur(s), mode_test={mail_mode == 'TEST'}")


# ============================
# 📅 SÉLECTION DU TRIMESTRE (FACTURES PARTICIPATION)
# ============================
@tresorerie_bp.route('/factures')
@login_required
@require_access("tresorerie", "ecriture")
def factures_selection():

    annee_now = datetime.now().year

    periodes = [
        (annee_now, 1), (annee_now, 2), (annee_now, 3), (annee_now, 4),
    ]

    return render_template(
        "tresorerie/factures_selection.html",
        periodes=periodes,
        annee_now=annee_now
    )


@tresorerie_bp.route('/factures/check_trimestre')
@login_required
@require_access("tresorerie", "ecriture")
def factures_check_trimestre():

    try:
        annee = int(request.args.get("annee"))
        trimestre = int(request.args.get("trimestre"))
    except (TypeError, ValueError):
        return jsonify({"exists": False, "id": None})

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    lot = conn.execute("""
        SELECT id FROM factures_lots
        WHERE annee = ? AND trimestre = ?
        ORDER BY id DESC LIMIT 1
    """, (annee, trimestre)).fetchone()

    conn.close()

    return jsonify({
        "exists": lot is not None,
        "id": lot["id"] if lot else None
    })


# ============================
# ENVOI PARTICIPATION PAR MAIL
# ============================
@tresorerie_bp.route('/factures_upload', methods=['GET', 'POST'])
@login_required
@require_access("tresorerie", "ecriture")
def factures_pdf():

    import os
    import time
    import tempfile
    from threading import Thread
    from PyPDF2 import PdfReader, PdfWriter

    os.makedirs(os.getenv("TMP_DIR", "/srv/ba38/tmp"), exist_ok=True)

    # ==========================================================
    # 🔧 CONFIG MAIL
    # ==========================================================
    mail_mode = session.get(
        "MAIL_MODE",
        os.getenv("MAIL_MODE", "TEST").upper()
    )

    mail_test_to = os.getenv(
        "MAIL_TEST_TO",
        "ba380.informatique2@banquealimentaire.org"
    )

    mail_sender = session.get(
        "factures_sender",
        "ba380.comptable@banquealimentaire.org"
    )

    conn_modeles = sqlite3.connect(get_db_path())
    conn_modeles.row_factory = sqlite3.Row
    modeles_facture = conn_modeles.execute(
        "SELECT * FROM modeles_emails WHERE type_periode = 'facture' ORDER BY TRIM(code_modele) COLLATE NOCASE"
    ).fetchall()
    conn_modeles.close()

    # Arrivée depuis l'écran de sélection (§ /factures) : annee/trimestre en
    # query string prévalent sur ce qui traînait en session d'un lot précédent.
    annee_qs = request.args.get("annee")
    trimestre_qs = request.args.get("trimestre")

    if annee_qs and trimestre_qs:
        session["factures_annee"] = annee_qs
        session["factures_trimestre"] = trimestre_qs
        session["factures_periode"] = f"T{trimestre_qs} {annee_qs}"

    periode = session.get("factures_periode", "")
    annee = session.get("factures_annee", "")
    trimestre = session.get("factures_trimestre", "")

    # ==========================================================
    # POST → ANALYSE PDF (crée un lot "brouillon", pas encore envoyé)
    # ==========================================================
    if request.method == "POST":

        sender = request.form.get("mail_sender")
        if sender:
            session["factures_sender"] = sender
            mail_sender = sender

        file = request.files.get("pdf_file")
        modele_id = request.form.get("modele_id")
        annee = request.form.get("annee", "").strip()
        trimestre = request.form.get("trimestre", "").strip()
        periode = f"T{trimestre} {annee}" if annee and trimestre else request.form.get("periode", "").strip()

        if not file:
            flash("❌ Fichier manquant", "danger")
            return redirect(url_for("tresorerie.factures_pdf"))

        if not modele_id:
            flash("❌ Aucun modèle de mail sélectionné", "danger")
            return redirect(url_for("tresorerie.factures_pdf"))

        db_path = get_db_path()
        conn_modeles = sqlite3.connect(db_path)
        conn_modeles.row_factory = sqlite3.Row
        modele = conn_modeles.execute(
            "SELECT * FROM modeles_emails WHERE id = ?", (modele_id,)
        ).fetchone()
        conn_modeles.close()

        if not modele:
            flash("❌ Modèle de mail introuvable, merci de le resélectionner.", "danger")
            return redirect(url_for("tresorerie.factures_pdf"))

        session["factures_modele_id"] = modele_id
        session["factures_periode"] = periode
        session["factures_annee"] = annee
        session["factures_trimestre"] = trimestre

        tmp_path = os.path.join(os.getenv("TMP_DIR", "/srv/ba38/tmp"), f"factures_{int(time.time())}.pdf")
        file.save(tmp_path)

        pages = extract_pages(tmp_path)

        # ==================================================
        # 📝 Création du lot (brouillon) + des lignes
        # ==================================================
        conn_lot = sqlite3.connect(db_path)
        cur = conn_lot.execute("""
            INSERT INTO factures_lots
            (periode, annee, trimestre, sender, modele_id, pdf_path, date_creation, cree_par)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (periode, int(annee) if annee else None, int(trimestre) if trimestre else None, mail_sender, modele_id, tmp_path,
              datetime.now().isoformat(timespec="seconds"), current_user.email))
        lot_id = cur.lastrowid

        for p in pages:
            contexte = {"nom_association": p["nom"], "periode": periode}
            sujet = render_modele_email(modele["sujet"], contexte).strip()
            corps = render_modele_email(modele["corps"], contexte)

            erreur_initiale = None if p["email"] else "Aucune adresse email détectée dans le PDF"

            conn_lot.execute("""
                INSERT INTO factures_envois
                (lot_id, nom, email, pages, sujet, corps, mail_erreur, mail_modele_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (lot_id, p["nom"], p["email"], ",".join(str(n) for n in p["pages"]),
                  sujet, corps, erreur_initiale, modele_id))

        conn_lot.commit()
        conn_lot.close()

        flash(f"📊 {len(pages)} facture(s) détectée(s) — vérifiez la liste puis cliquez « Envoyer les mails » quand vous êtes prêt.", "info")

        return redirect(url_for("tresorerie.factures_resultats", lot_id=lot_id))

    return render_template(
        "tresorerie/factures_upload.html",
        mail_mode=mail_mode,
        mail_test_to=mail_test_to,
        mail_sender=mail_sender,
        modeles_facture=modeles_facture,
        periode=periode,
        annee=annee,
        trimestre=trimestre
    )


@tresorerie_bp.route('/factures_modele_save', methods=['POST'])
@login_required
@require_access("tresorerie", "ecriture")
def factures_modele_save():

    modele_id = request.form.get("id")
    code_modele = request.form.get("code_modele", "").strip()
    sujet = request.form.get("sujet", "").strip()
    corps = request.form.get("corps", "").strip()

    if not code_modele or not sujet or not corps:
        flash("❌ Code, sujet et corps sont obligatoires.", "danger")
        return redirect(url_for("tresorerie.factures_pdf"))

    conn = sqlite3.connect(get_db_path())

    if modele_id:
        conn.execute("""
            UPDATE modeles_emails
            SET code_modele = ?, sujet = ?, corps = ?, date_modification = ?
            WHERE id = ? AND type_periode = 'facture'
        """, (code_modele, sujet, corps, datetime.now().isoformat(), modele_id))
        flash("✅ Modèle mis à jour.", "success")
    else:
        conn.execute("""
            INSERT INTO modeles_emails (code_modele, sujet, corps, date_modification, type_periode)
            VALUES (?, ?, ?, ?, 'facture')
        """, (code_modele, sujet, corps, datetime.now().isoformat()))
        flash("✅ Modèle créé.", "success")

    conn.commit()
    conn.close()

    return redirect(url_for("tresorerie.factures_pdf"))


@tresorerie_bp.route('/factures_modele_delete', methods=['POST'])
@login_required
@require_access("tresorerie", "ecriture")
def factures_modele_delete():

    modele_id = request.form.get("id")

    conn = sqlite3.connect(get_db_path())
    conn.execute(
        "DELETE FROM modeles_emails WHERE id = ? AND type_periode = 'facture'",
        (modele_id,)
    )
    conn.commit()
    conn.close()

    flash("🗑️ Modèle supprimé.", "warning")
    return redirect(url_for("tresorerie.factures_pdf"))


@tresorerie_bp.route('/factures_toggle_mode', methods=['POST'])
@login_required
@require_access("tresorerie", "ecriture")
def factures_toggle_mode():

    current = session.get(
        "MAIL_MODE",
        os.getenv("MAIL_MODE", "TEST").upper()
    )

    if current == "TEST":
        session["MAIL_MODE"] = "PROD"
        flash("🚀 Mode PROD activé", "success")
    else:
        session["MAIL_MODE"] = "TEST"
        flash("🧪 Mode TEST activé", "warning")

    # Ce toggle est partagé par les 2 écrans d'envoi de factures participation
    # (ancien flux upload-PDF + V2) — retour sur l'écran d'où le clic vient.
    if request.referrer and request.host_url.rstrip("/") in request.referrer:
        return redirect(request.referrer)

    return redirect(url_for("tresorerie.factures_pdf"))


# ============================
# 📊 ÉCRAN RÉSULTATS D'UN LOT DE FACTURES
# ============================
@tresorerie_bp.route('/factures/resultats/<int:lot_id>')
@login_required
@require_access("tresorerie", "ecriture")
def factures_resultats(lot_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    lot = conn.execute("SELECT * FROM factures_lots WHERE id = ?", (lot_id,)).fetchone()

    if not lot:
        conn.close()
        flash("❌ Lot introuvable", "danger")
        return redirect(url_for("tresorerie.factures_pdf"))

    envois = conn.execute("""
        SELECT * FROM factures_envois
        WHERE lot_id = ?
        ORDER BY nom
    """, (lot_id,)).fetchall()

    conn.close()

    reste_a_envoyer = any(e["email"] and not e["mail_envoye_le"] for e in envois)
    mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())

    return render_template(
        "tresorerie/factures_resultats.html",
        lot=lot,
        envois=envois,
        reste_a_envoyer=reste_a_envoyer,
        mail_mode=mail_mode
    )


# ============================
# 📧 ENVOYER LES MAILS D'UN LOT (depuis l'écran Résultats)
# ============================
@tresorerie_bp.route('/factures/envoyer/<int:lot_id>', methods=['POST'])
@login_required
@require_access("tresorerie", "ecriture")
def factures_envoyer(lot_id):

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    lot = conn.execute("SELECT * FROM factures_lots WHERE id = ?", (lot_id,)).fetchone()

    if not lot:
        conn.close()
        flash("❌ Lot introuvable", "danger")
        return redirect(url_for("tresorerie.factures_pdf"))

    if not lot["pdf_path"] or not os.path.exists(lot["pdf_path"]):
        conn.close()
        flash("❌ Le PDF source de ce lot n'existe plus sur le serveur.", "danger")
        return redirect(url_for("tresorerie.factures_resultats", lot_id=lot_id))

    a_envoyer = conn.execute("""
        SELECT id, email, pages, sujet, corps
        FROM factures_envois
        WHERE lot_id = ? AND email IS NOT NULL AND mail_envoye_le IS NULL
    """, (lot_id,)).fetchall()

    conn.close()

    if not a_envoyer:
        flash("ℹ️ Rien à envoyer : toutes les factures de ce lot ont déjà été traitées.", "warning")
        return redirect(url_for("tresorerie.factures_resultats", lot_id=lot_id))

    mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")

    items = [{
        "envoi_id": r["id"],
        "pages": [int(n) for n in r["pages"].split(",") if n],
        "email": r["email"],
        "sujet": r["sujet"],
        "corps": r["corps"],
    } for r in a_envoyer]

    app_reel = current_app._get_current_object()
    current_user_email = current_user.email

    Thread(
        target=envoyer_factures_background,
        args=(app_reel, db_path, lot_id, items, lot["pdf_path"],
              mail_mode, mail_test_to, lot["sender"], current_user_email)
    ).start()

    if mail_mode == "TEST":
        flash("🧪 Envoi TEST lancé en arrière-plan (2 mails max vers l'adresse de test).", "warning")
    else:
        flash(f"🚀 Envoi réel lancé en arrière-plan pour {len(items)} facture(s) (expéditeur : {lot['sender']}).", "info")

    return redirect(url_for("tresorerie.factures_resultats", lot_id=lot_id))


# ============================
# 👁️ VOIR LE PDF D'UNE FACTURE (régénéré à la demande)
# ============================
@tresorerie_bp.route('/factures/voir_pdf/<int:envoi_id>')
@login_required
@require_access("tresorerie", "ecriture")
def factures_voir_pdf(envoi_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    envoi = conn.execute("SELECT * FROM factures_envois WHERE id = ?", (envoi_id,)).fetchone()

    if not envoi:
        conn.close()
        return "Ligne introuvable", 404

    lot = conn.execute("SELECT * FROM factures_lots WHERE id = ?", (envoi["lot_id"],)).fetchone()
    conn.close()

    if not lot or not lot["pdf_path"] or not os.path.exists(lot["pdf_path"]):
        return "PDF source introuvable sur le serveur", 404

    pages = [int(n) for n in envoi["pages"].split(",") if n]
    reader = PdfReader(lot["pdf_path"])
    fichier = build_pdf(reader, pages)

    return send_file(fichier, mimetype="application/pdf")


# ============================
# 🔄 VÉRIFIER STATUT MAILJET (FACTURES)
# ============================
@tresorerie_bp.route('/factures/verifier_statut_mailjet/<int:lot_id>', methods=['POST'])
@login_required
@require_access("tresorerie", "ecriture")
def factures_verifier_statut_mailjet(lot_id):
    from utils import mailjet_get_message_status

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    lignes = conn.execute("""
        SELECT id, mail_mailjet_message_ids
        FROM factures_envois
        WHERE lot_id = ?
          AND mail_mailjet_message_ids IS NOT NULL
          AND mail_mailjet_message_ids != ''
    """, (lot_id,)).fetchall()

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
            UPDATE factures_envois
            SET mail_statut_final = ?, mail_statut_verifie_le = ?
            WHERE id = ?
        """, (statut, datetime.now().isoformat(timespec="seconds"), ligne["id"]))

    conn.commit()
    conn.close()

    if verifies:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        flash(f"🔄 Statut Mailjet vérifié pour {verifies} mail(s) : {detail}", "info")
    else:
        flash("ℹ️ Aucun mail avec un identifiant Mailjet à vérifier pour ce lot.", "warning")

    return redirect(url_for("tresorerie.factures_resultats", lot_id=lot_id))


# ============================
# 🔁 RENVOYER VIA GMAIL (FACTURES)
# ============================
@tresorerie_bp.route('/factures/renvoyer_gmail/<int:envoi_id>', methods=['POST'])
@login_required
@require_access("tresorerie", "ecriture")
def factures_renvoyer_gmail(envoi_id):
    from utils_gmail_send import envoyer_mail_gmail, GmailSendError

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    envoi = conn.execute("SELECT * FROM factures_envois WHERE id = ?", (envoi_id,)).fetchone()

    if not envoi:
        conn.close()
        flash("❌ Ligne introuvable", "danger")
        return redirect(url_for("tresorerie.factures_pdf"))

    lot = conn.execute("SELECT * FROM factures_lots WHERE id = ?", (envoi["lot_id"],)).fetchone()

    if not envoi["email"]:
        conn.close()
        flash(f"❌ Aucune adresse email pour {envoi['nom']}", "danger")
        return redirect(url_for("tresorerie.factures_resultats", lot_id=envoi["lot_id"]))

    if envoi["mail_mode_test"]:
        conn.close()
        flash("⛔ Le dernier envoi pour cette ligne était en Mode TEST — un renvoi Gmail partirait, lui, pour de vrai. Refaites d'abord un envoi réel.", "danger")
        return redirect(url_for("tresorerie.factures_resultats", lot_id=envoi["lot_id"]))

    if not lot["pdf_path"] or not os.path.exists(lot["pdf_path"]):
        conn.close()
        flash("❌ Le PDF source de ce lot n'existe plus sur le serveur, impossible de régénérer la facture.", "danger")
        return redirect(url_for("tresorerie.factures_resultats", lot_id=envoi["lot_id"]))

    pages = [int(n) for n in envoi["pages"].split(",") if n]

    try:
        reader = PdfReader(lot["pdf_path"])
        fichier = build_pdf(reader, pages)

        envoyer_mail_gmail(
            sujet=envoi["sujet"],
            destinataires=[envoi["email"]],
            texte=envoi["corps"],
            attachment_path=fichier
        )

        conn.execute("""
            UPDATE factures_envois
            SET mail_renvoi_gmail_le = ?
            WHERE id = ?
        """, (datetime.now().isoformat(timespec="seconds"), envoi_id))
        conn.commit()

        flash(f"📧 Facture renvoyée via Gmail à {envoi['email']} pour {envoi['nom']}", "success")

        if os.path.exists(fichier):
            os.remove(fichier)

    except GmailSendError as e:
        write_log(f"❌ Erreur renvoi Gmail facture pour {envoi['nom']} : {e}")
        flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")

    conn.close()
    return redirect(url_for("tresorerie.factures_resultats", lot_id=envoi["lot_id"]))


@tresorerie_bp.route("/telecharger_factures")
@login_required
@require_access("tresorerie", "ecriture")
def telecharger_factures():

    zip_path = session.get("zip_path")

    if not zip_path or not os.path.exists(zip_path):
        flash("❌ Fichier introuvable", "danger")
        return redirect(url_for("tresorerie.factures_decoupage"))

    return render_template(
        "tresorerie/telechargement_factures.html",
        zip_path=zip_path
    )
