import os
import sqlite3
from datetime import datetime
from threading import Thread

from flask import request, render_template, flash, redirect, url_for, session, current_app
from flask_login import login_required

from ba38_utilitaires.core import get_db_path, write_log, envoyer_mail, split_emails, require_access, get_google_services

from ba38_tresorerie import tresorerie_bp
from ba38_tresorerie.drive_utils import get_pdf_by_code_vif


@tresorerie_bp.route("/cotisations/saisie-paiements", methods=["GET", "POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_saisie_paiements():
    """
    Saisie et modification des paiements des cotisations.

    Logique :
    - Le statut est calculé dynamiquement :
        * date_paiement -> "paye"
        * date_envoi_facture -> "envoyee"
        * sinon -> "calcule"
    - POST :
        * Si date renseignée -> enregistrement paiement
        * Si date vidée -> annulation paiement
    """

    from datetime import datetime

    annee = request.args.get("annee") or request.form.get("annee")
    impayes_only = request.args.get("impayes") == "1"

    if not annee:
        annee = datetime.now().year
    else:
        annee = int(annee)

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # ==========================================================
    # POST → ENREGISTREMENT
    # ==========================================================
    if request.method == "POST":

        for key, value in request.form.items():

            if key.startswith("date_paiement_"):

                cotisation_id = key.replace("date_paiement_", "")
                value = value.strip()

                if value:
                    try:
                        date_obj = datetime.strptime(value, "%d/%m/%Y")
                        date_sql = date_obj.strftime("%Y-%m-%d")

                        cursor.execute("""
                            UPDATE cotisations
                            SET date_paiement = ?
                            WHERE id = ?
                        """, (date_sql, cotisation_id))

                    except ValueError:
                        flash(f"Date invalide pour ID {cotisation_id}", "danger")

                else:
                    cursor.execute("""
                        UPDATE cotisations
                        SET date_paiement = NULL
                        WHERE id = ?
                    """, (cotisation_id,))

        conn.commit()
        flash("Modifications enregistrées.", "success")

    else:
        # ==========================================================
        # GET → GARDE : campagne inexistante pour cette année
        # ==========================================================
        cursor.execute(
            "SELECT 1 FROM cotisations WHERE annee = ? LIMIT 1",
            (annee,)
        )
        if not cursor.fetchone():
            conn.close()
            flash(
                f"⚠️ Aucune campagne de cotisations pour {annee} — "
                "importez d'abord le fichier PARSOL2L annuel.",
                "warning"
            )
            return redirect(
                url_for("tresorerie.cotisations", annee=annee, manque=1)
            )

    # ==========================================================
    # GET → AFFICHAGE
    # ==========================================================
    sql = """
        SELECT
            c.*,
            a.nom_association,
            a.compte_comptable
        FROM cotisations c
        JOIN associations a
            ON a.Id = c.id_association
        WHERE c.annee = ?
    """

    params = [annee]

    if impayes_only:
        sql += " AND c.date_paiement IS NULL"

    sql += " ORDER BY c.numero_facture"

    cursor.execute(sql, params)
    lignes = cursor.fetchall()

    total_facture = 0
    total_paye = 0
    resultats = []

    for l in lignes:

        montant = l["montant"] or 0
        total_facture += montant

        # ==========================
        # Statut calculé dynamiquement
        # ==========================
        if l["date_paiement"]:
            statut_calcule = "paye"
            total_paye += montant
        elif l["date_envoi_facture"]:
            statut_calcule = "envoyee"
        else:
            statut_calcule = "calcule"

        ligne_dict = dict(l)
        ligne_dict["statut_calcule"] = statut_calcule

        resultats.append(ligne_dict)

    total_restant = total_facture - total_paye
    taux_recouvrement = (
        round((total_paye / total_facture) * 100, 2)
        if total_facture > 0
        else 0
    )

    conn.close()

    return render_template(
        "tresorerie/cotisations_saisie_paiements.html",
        resultats=resultats,
        annee=annee,
        total_facture=total_facture,
        total_paye=total_paye,
        total_restant=total_restant,
        taux_recouvrement=taux_recouvrement,
        impayes_only=impayes_only
    )


@tresorerie_bp.route("/parametres/modele-relance", methods=["GET", "POST"])
@login_required
@require_access("tresorerie", "ecriture")
def modele_relance():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if request.method == "POST":
        sujet = request.form.get("sujet")
        corps = request.form.get("corps")

        cursor.execute("""
            UPDATE modeles_emails
            SET sujet = ?, corps = ?, date_modification = ?
            WHERE code_modele = 'relance_cotisation'
        """, (sujet, corps, datetime.now().isoformat()))
        conn.commit()
        flash("Modèle mis à jour.", "success")

    cursor.execute("""
        SELECT sujet, corps
        FROM modeles_emails
        WHERE code_modele = 'relance_cotisation'
    """)
    modele = cursor.fetchone()
    conn.close()

    return render_template(
        "tresorerie/modele_relance.html",
        modele=modele
    )


@tresorerie_bp.route("/cotisations/modele/<code_modele>", methods=["GET", "POST"])
@login_required
@require_access("tresorerie", "ecriture")
def edit_modele_email(code_modele):
    """
    Edition d'un modèle email stocké dans modeles_emails.
    """

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # ===========================
    # POST → Enregistrement
    # ===========================
    if request.method == "POST":

        sujet = request.form.get("sujet", "").strip()
        corps = request.form.get("corps", "").strip()

        cursor.execute("""
            UPDATE modeles_emails
            SET sujet = ?,
                corps = ?,
                date_modification = ?
            WHERE code_modele = ?
        """, (
            sujet,
            corps,
            datetime.now().isoformat(),
            code_modele
        ))

        conn.commit()
        conn.close()

        flash("✅ Modèle mis à jour.", "success")

        return redirect(
            url_for("tresorerie.edit_modele_email",
                    code_modele=code_modele)
        )

    # ===========================
    # GET → Affichage
    # ===========================
    cursor.execute("""
        SELECT *
        FROM modeles_emails
        WHERE code_modele = ?
        LIMIT 1
    """, (code_modele,))

    modele = cursor.fetchone()

    conn.close()

    if not modele:
        flash("❌ Modèle introuvable.", "danger")
        return redirect(url_for("tresorerie.tresorerie"))

    return render_template(
        "tresorerie/edit_modele_email.html",
        modele=modele
    )


# ============================
# RELANCES
# ============================

@tresorerie_bp.route("/cotisations/relance", methods=["GET"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_relance_start():

    mail_mode = session.get(
        "MAIL_MODE",
        os.getenv("MAIL_MODE", "PROD").upper()
    )

    mail_test_to = os.getenv(
        "MAIL_TEST_TO",
        "ba380.informatique2@banquealimentaire.org"
    )

    mail_sender = request.args.get(
        "mail_sender",
        "ba380.comptable@banquealimentaire.org"
    )

    from datetime import datetime

    annee = request.args.get("annee")

    write_log(f"🔍 Année sélectionnée dans cotisations_relance_start: {annee}")

    if not annee:
        annee = datetime.now().year
    else:
        annee = int(annee)

    lignes = None

    numero_relance = 0

    if annee:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1 FROM cotisations WHERE annee = ? LIMIT 1",
            (annee,)
        )
        if not cursor.fetchone():
            conn.close()
            flash(
                f"⚠️ Aucune campagne de cotisations pour {annee} — "
                "importez d'abord le fichier PARSOL2L annuel.",
                "warning"
            )
            return redirect(
                url_for("tresorerie.cotisations", annee=annee, manque=1)
            )

        cursor.execute("""
            SELECT
                c.*,
                a.nom_association,
                a.compte_comptable
            FROM cotisations c
            JOIN associations a
                ON a.Id = c.id_association
            WHERE c.annee = ?
            AND c.date_paiement IS NULL
            AND c.date_envoi_facture IS NOT NULL
            ORDER BY c.numero_facture
        """, (annee,))

        lignes = cursor.fetchall()
        conn.close()



    return render_template(
        "tresorerie/cotisations_relance.html",
        mail_mode=mail_mode,
        mail_test_to=mail_test_to,
        mail_sender=mail_sender,
        numero_relance=numero_relance,
        annee=annee,
        lignes=lignes,
        preview=False   # ✅ AJOUT
    )


def envoyer_relances_background(app, db_path, items, sujet_modele, corps_modele,
                                 numero_relance, annee, mail_sender, mail_mode,
                                 mail_test_to, folder_id_factures):
    """
    Envoi des relances de cotisations en arrière-plan (Thread).

    Reproduit le dispositif déjà en place pour indicateurs/factures/
    participation : sort le lot du cycle requête/réponse HTTP synchrone
    (évite les timeouts nginx/gunicorn sur beaucoup d'associations) et
    persiste, ligne par ligne, le statut Mailjet (succès/échec) — un échec
    Mailjet sur une association n'arrête plus l'envoi des suivantes.
    """
    with app.app_context():
        from datetime import datetime

        client, drive_service, creds = get_google_services()

        nb_mails = 0
        nb_pdf_introuvables = 0
        nb_erreurs = 0

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        lignes_traitement = items
        if mail_mode == "TEST":
            lignes_traitement = items[:2]

        for item in lignes_traitement:

            try:
                code_vif_8 = str(item["code_vif"]).zfill(8)

                file_id, nom_pdf = get_pdf_by_code_vif(
                    drive_service,
                    folder_id_factures,
                    code_vif_8
                ) if drive_service else (None, None)

                if not file_id:
                    nb_pdf_introuvables += 1
                    continue

                lien_drive = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

                sujet = sujet_modele.format(
                    numero_relance=numero_relance + 1,
                    annee=annee
                )

                texte_mail = corps_modele.format(
                    numero_relance=numero_relance + 1,
                    annee=annee,
                    nom_association=item["nom_association"],
                    lien_drive=lien_drive,
                    montant="{:.2f}".format(item["montant"] or 0)
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

                resultat = envoyer_mail(
                    sujet=sujet_envoi,
                    destinataires=destinataire,
                    texte=texte_mail,
                    sender_override=mail_sender,
                    is_html=True,
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
                    UPDATE cotisations
                    SET relance_niveau = COALESCE(relance_niveau,0)+1,
                        date_derniere_relance = ?,
                        mode_test_relance = ?,
                        relance_sujet = ?,
                        relance_corps = ?,
                        relance_mail_erreur = NULL,
                        relance_mailjet_status = ?,
                        relance_mailjet_message_ids = ?
                    WHERE id = ?
                """, (
                    datetime.now().isoformat(timespec="seconds"),
                    1 if mail_mode == "TEST" else 0,
                    sujet_envoi,
                    texte_mail,
                    mj_status,
                    mj_ids,
                    item["id"]
                ))
                conn.commit()

                nb_mails += 1

            except Exception as e:
                write_log(f"❌ Erreur relance cotisation (id={item['id']}) : {e}")
                nb_erreurs += 1
                conn.execute("""
                    UPDATE cotisations
                    SET relance_mail_erreur = ?
                    WHERE id = ?
                """, (str(e), item["id"]))
                conn.commit()

        conn.close()

        write_log(
            f"📤 Relances cotisations {annee} (arrière-plan) terminées : "
            f"{nb_mails} envoyée(s), {nb_erreurs} erreur(s), "
            f"{nb_pdf_introuvables} PDF introuvable(s)."
        )


@tresorerie_bp.route("/cotisations/relance", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_relance():
    """
    Relance des cotisations.

    - Utilise le modèle 'relance_cotisation' dans modeles_emails
    - Injecte dynamiquement les variables via .format()
    - Ne modifie pas le système d’envoi existant
    """

    from datetime import datetime

    try:

        # ======================================================
        # Paramètres formulaire
        # ======================================================
        annee = int(request.form.get("annee"))
        numero_relance = int(request.form.get("numero_relance"))
        confirm_envoi = request.form.get("confirm_envoi")
        confirm_production = request.form.get("confirm_production")

        mail_sender = request.form.get(
            "mail_sender",
            "ba380.comptable@banquealimentaire.org"
        )

        mail_mode = session.get(
            "MAIL_MODE",
            os.getenv("MAIL_MODE", "PROD").upper()
        )

        mail_test_to = os.getenv(
            "MAIL_TEST_TO",
            "ba380.informatique2@banquealimentaire.org"
        )

        FOLDER_ID_FACTURES = os.getenv("GDRIVE_FACTURES_PDF_FOLDER_ID")

        # ======================================================
        # Connexion base
        # ======================================================
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # ======================================================
        # Chargement modèle email
        # ======================================================
        code_modele = f"relance_cotisation_{numero_relance + 1}"

        cursor.execute("""
            SELECT sujet, corps
            FROM modeles_emails
            WHERE code_modele = ?
            LIMIT 1
        """, (code_modele,))
        modele = cursor.fetchone()

        if not modele:
            conn.close()
            flash(f"❌ Modèle '{code_modele}' introuvable.", "danger")
            return redirect(
                url_for("tresorerie.cotisations_relance_start", annee=annee)
            )

        sujet_modele = modele["sujet"]
        corps_modele = modele["corps"]

        # ======================================================
        # Lecture cotisations
        # ======================================================
        cursor.execute("""
            SELECT
                c.id,
                c.numero_facture,
                c.code_vif,
                c.montant,
                c.relance_niveau,
                a.nom_association,
                a.courriel_association,
                a.courriel_resp_tresorerie
            FROM cotisations c
            JOIN associations a
                ON a.Id = c.id_association
            WHERE c.annee = ?
            AND c.date_paiement IS NULL
            AND c.date_envoi_facture IS NOT NULL
            ORDER BY c.numero_facture
        """, (annee,))

        lignes = cursor.fetchall()

        cotisations_a_relancer = [
            l for l in lignes
            if (l["relance_niveau"] or 0) == numero_relance
        ]

        total_relances = sum(
            float(l["montant"] or 0)
            for l in cotisations_a_relancer
        )

        # ======================================================
        # Aucun résultat
        # ======================================================
        if not cotisations_a_relancer:
            conn.close()
            return render_template(
                "tresorerie/cotisations_relance.html",
                mail_mode=mail_mode,
                mail_test_to=mail_test_to,
                annee=annee,
                lignes=[],
                preview=False,
                total_relances=0,
                numero_relance=numero_relance
            )

        # ======================================================
        # PREVIEW
        # ======================================================
        if not confirm_envoi:
            conn.close()
            return render_template(
                "tresorerie/cotisations_relance.html",
                mail_mode=mail_mode,
                mail_test_to=mail_test_to,
                annee=annee,
                lignes=cotisations_a_relancer,
                preview=True,
                numero_relance=numero_relance,
                total_relances=total_relances,
                mail_sender=mail_sender
            )

        # ======================================================
        # Sécurité production
        # ======================================================
        if mail_mode == "PROD" and not confirm_production:
            conn.close()
            flash("⚠ Confirmation obligatoire en PRODUCTION.", "danger")
            return redirect(
                url_for("tresorerie.cotisations_relance_start",
                        annee=annee)
            )

        # ======================================================
        # Préparation des items + envoi en arrière-plan
        # ======================================================
        conn.close()

        items = [
            {
                "id": ligne["id"],
                "code_vif": ligne["code_vif"],
                "nom_association": ligne["nom_association"],
                "montant": ligne["montant"],
                "email": ligne["courriel_resp_tresorerie"] or ligne["courriel_association"],
            }
            for ligne in cotisations_a_relancer
            if (ligne["courriel_resp_tresorerie"] or ligne["courriel_association"])
        ]

        if not items:
            flash("❌ Aucune association avec une adresse email valide à relancer.", "danger")
            return redirect(
                url_for("tresorerie.cotisations_relance_start", annee=annee)
            )

        app_reel = current_app._get_current_object()
        db_path = get_db_path()

        Thread(
            target=envoyer_relances_background,
            args=(app_reel, db_path, items, sujet_modele, corps_modele,
                  numero_relance, annee, mail_sender, mail_mode,
                  mail_test_to, FOLDER_ID_FACTURES)
        ).start()

        if mail_mode == "TEST":
            flash("🧪 Envoi TEST des relances lancé en arrière-plan (2 mails max vers l'adresse de test).", "warning")
        else:
            flash(f"🚀 Envoi des relances lancé en arrière-plan pour {len(items)} association(s).", "info")

        return redirect(
            url_for("tresorerie.cotisations_relance_start",
                    annee=annee)
        )

    except Exception:
        current_app.logger.exception("Erreur relance cotisations")
        flash("Erreur lors des relances.", "danger")
        return redirect(
            url_for("tresorerie.cotisations_relance_start",
                    annee=annee)
        )


@tresorerie_bp.route("/cotisations/relance/verifier_statut_mailjet", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_relance_verifier_statut_mailjet():
    from ba38_utilitaires.core import mailjet_get_message_status

    annee = request.form.get("annee")
    if not annee:
        flash("Année manquante", "danger")
        return redirect(url_for("tresorerie.cotisations_relance_start"))

    annee = int(annee)

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    lignes = conn.execute("""
        SELECT id, relance_mailjet_message_ids
        FROM cotisations
        WHERE annee = ?
          AND relance_mailjet_message_ids IS NOT NULL
          AND relance_mailjet_message_ids != ''
    """, (annee,)).fetchall()

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
            UPDATE cotisations
            SET relance_statut_final = ?, relance_statut_verifie_le = ?
            WHERE id = ?
        """, (statut, datetime.now().isoformat(timespec="seconds"), ligne["id"]))

    conn.commit()
    conn.close()

    if verifies:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        flash(f"🔄 Statut Mailjet vérifié pour {verifies} mail(s) : {detail}", "info")
    else:
        flash("ℹ️ Aucune relance avec un identifiant Mailjet à vérifier pour cette année.", "warning")

    return redirect(url_for("tresorerie.cotisations_relance_start", annee=annee))


@tresorerie_bp.route("/cotisations/relance/renvoyer_gmail/<int:cotisation_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_relance_renvoyer_gmail(cotisation_id):
    from ba38_utilitaires.gmail_send import envoyer_mail_gmail, GmailSendError

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    ligne = conn.execute("""
        SELECT c.*, a.nom_association, a.courriel_association, a.courriel_resp_tresorerie
        FROM cotisations c
        JOIN associations a ON a.Id = c.id_association
        WHERE c.id = ?
    """, (cotisation_id,)).fetchone()

    if not ligne:
        conn.close()
        flash("❌ Ligne introuvable", "danger")
        return redirect(url_for("tresorerie.cotisations_relance_start"))

    email = ligne["courriel_resp_tresorerie"] or ligne["courriel_association"]
    destinataires = split_emails(email)

    if not destinataires:
        conn.close()
        flash(f"❌ Aucune adresse email valide pour {ligne['nom_association']}", "danger")
        return redirect(url_for("tresorerie.cotisations_relance_start", annee=ligne["annee"]))

    if not ligne["relance_sujet"] or not ligne["relance_corps"]:
        conn.close()
        flash(
            f"⛔ Aucune relance précédente connue pour {ligne['nom_association']} — "
            "utilisez d'abord l'envoi normal des relances.",
            "danger"
        )
        return redirect(url_for("tresorerie.cotisations_relance_start", annee=ligne["annee"]))

    if ligne["mode_test_relance"]:
        conn.close()
        flash(
            f"⛔ La dernière relance pour {ligne['nom_association']} était en Mode TEST — "
            "un renvoi Gmail partirait, lui, pour de vrai. Refaites d'abord une relance réelle.",
            "danger"
        )
        return redirect(url_for("tresorerie.cotisations_relance_start", annee=ligne["annee"]))

    try:
        envoyer_mail_gmail(
            sujet=ligne["relance_sujet"],
            destinataires=destinataires,
            texte=ligne["relance_corps"]
        )

        conn.execute("""
            UPDATE cotisations SET relance_renvoi_gmail_le = ? WHERE id = ?
        """, (datetime.now().isoformat(timespec="seconds"), cotisation_id))
        conn.commit()

        flash(f"📧 Relance renvoyée via Gmail à {', '.join(destinataires)} pour {ligne['nom_association']}", "success")

    except GmailSendError as e:
        write_log(f"❌ Erreur renvoi Gmail relance cotisation pour {ligne['nom_association']} : {e}")
        flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")

    finally:
        conn.close()

    return redirect(url_for("tresorerie.cotisations_relance_start", annee=ligne["annee"]))


@tresorerie_bp.route("/cotisations/relance/reset", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_relance_reset():

    annee = request.form.get("annee")

    mail_mode = session.get(
        "MAIL_MODE",
        os.getenv("MAIL_MODE", "PROD").upper()
    )

    # 🔒 Sécurité absolue
    if mail_mode != "TEST":
        flash("⛔ Réinitialisation autorisée uniquement en MODE TEST.", "danger")
        return redirect(url_for("tresorerie.cotisations_relance_start", annee=annee))

    if not annee:
        flash("Année manquante.", "danger")
        return redirect(url_for("tresorerie.cotisations_relance_start"))

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE cotisations
        SET
            relance_niveau = 0,
            date_derniere_relance = NULL,
            mode_test_relance = 0
        WHERE annee = ?
    """, (annee,))

    nb = cursor.rowcount

    conn.commit()
    conn.close()

    flash(f"🔄 {nb} relances réinitialisées (MODE TEST).", "warning")

    return redirect(
        url_for("tresorerie.cotisations_relance_start", annee=annee)
    )
