import os
import json
import sqlite3
from datetime import datetime
from threading import Thread

from flask import request, render_template, flash, redirect, url_for, session, current_app
from flask_login import login_required

from ba38_utilitaires.core import get_db_path, write_log, envoyer_mail, split_emails, require_access, mailjet_get_message_status
from ba38_utilitaires.gmail_send import envoyer_mail_gmail, GmailSendError

from ba38_tresorerie import tresorerie_bp
from ba38_tresorerie.cotisations_v2 import generer_facture_cotisation_v2_pdf


def _resoudre_lignes_email(rows):
    """
    Convertit les lignes SQL (jointes à associations) en dicts, et complète
    l'email de la facture — capturé une fois au traitement PARSOL2L, donc
    potentiellement obsolète — par l'adresse actuelle de l'association si
    absent (même mécanisme que Participation V2).
    """
    lignes = []
    for row in rows:
        d = dict(row)
        assoc_email = d.pop("_assoc_tresorerie", None) or d.pop("_assoc_association", None)
        if not d.get("email"):
            d["email"] = assoc_email
        lignes.append(d)
    return lignes


def envoyer_relances_cotisations_v2_background(app, db_path, items, sujet_modele, corps_modele,
                                                numero_relance, annee, mail_sender, mail_mode,
                                                mail_test_to):
    """
    Envoi des relances de cotisations V2 en arrière-plan (Thread), sur le
    modèle de envoyer_relances_participation_background : le PDF est
    régénéré à la demande et joint en pièce attachée (pas de lien Drive).
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

            pdf_path = f"/tmp/cotisations_v2_relance_{item['facture_id']}.pdf"

            try:
                sujet = sujet_modele.format(numero_relance=numero_relance + 1, annee=annee)

                texte_mail = corps_modele.format(
                    numero_relance=numero_relance + 1,
                    annee=annee,
                    nom_association=item["nom_association"],
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
                    "annee": annee,
                    "numero_facture": item["numero_facture"],
                    "code_vif": item["code_vif"],
                    "commentaire_regroupement": detail.get("commentaire_regroupement"),
                }

                generer_facture_cotisation_v2_pdf(data_pdf, pdf_path)

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
                    UPDATE cotisations_v2_factures
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
                write_log(f"❌ Erreur relance cotisation V2 (facture_id={item['facture_id']}) : {e}")
                nb_erreurs += 1
                conn.execute("""
                    UPDATE cotisations_v2_factures
                    SET relance_mail_erreur = ?
                    WHERE id = ?
                """, (str(e), item["facture_id"]))
                conn.commit()

            finally:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)

        conn.close()

        write_log(
            f"📤 Relances cotisations V2 {annee} (arrière-plan) terminées : "
            f"{nb_mails} envoyée(s), {nb_erreurs} erreur(s)."
        )


@tresorerie_bp.route("/cotisations_v2/relance/<int:campagne_id>", methods=["GET"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_relance_start(campagne_id):

    mail_mode = session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")
    mail_sender = request.args.get("mail_sender", "ba380.comptable@banquealimentaire.org")
    numero_relance = int(request.args.get("numero_relance", 0))

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    campagne = conn.execute(
        "SELECT * FROM cotisations_v2_campagnes WHERE id = ?", (campagne_id,)
    ).fetchone()

    if not campagne:
        conn.close()
        flash("❌ Campagne introuvable", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_selection"))

    lignes = conn.execute("""
        SELECT pf.*,
               a.courriel_resp_tresorerie AS _assoc_tresorerie,
               a.courriel_association AS _assoc_association
        FROM cotisations_v2_factures pf
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
        "tresorerie/cotisations_v2/relance.html",
        campagne=campagne,
        mail_mode=mail_mode,
        mail_test_to=mail_test_to,
        mail_sender=mail_sender,
        numero_relance=numero_relance,
        lignes=lignes,
        preview=False
    )


@tresorerie_bp.route("/cotisations_v2/relance/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_relance(campagne_id):

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
            "SELECT * FROM cotisations_v2_campagnes WHERE id = ?", (campagne_id,)
        ).fetchone()

        if not campagne:
            conn.close()
            flash("❌ Campagne introuvable", "danger")
            return redirect(url_for("tresorerie.cotisations_v2_selection"))

        code_modele = f"COTISATIONS V2 Relance {numero_relance + 1}"

        modele = conn.execute("""
            SELECT sujet, corps FROM modeles_emails WHERE code_modele = ? LIMIT 1
        """, (code_modele,)).fetchone()

        if not modele:
            conn.close()
            flash(f"❌ Modèle '{code_modele}' introuvable.", "danger")
            return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=campagne_id))

        sujet_modele = modele["sujet"]
        corps_modele = modele["corps"]

        lignes = conn.execute("""
            SELECT pf.*,
                   a.courriel_resp_tresorerie AS _assoc_tresorerie,
                   a.courriel_association AS _assoc_association
            FROM cotisations_v2_factures pf
            LEFT JOIN associations a ON a.Id = pf.association_id
            WHERE pf.campagne_id = ?
              AND pf.mail_envoye_le IS NOT NULL
              AND pf.date_paiement IS NULL
            ORDER BY pf.numero_facture
        """, (campagne_id,)).fetchall()

        lignes = _resoudre_lignes_email(lignes)

        a_relancer = [l for l in lignes if (l["relance_niveau"] or 0) == numero_relance]

        total_relances = sum(float(l["montant"] or 0) for l in a_relancer)

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
                "tresorerie/cotisations_v2/relance.html",
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
                "tresorerie/cotisations_v2/relance.html",
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
            return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=campagne_id))

        conn.close()

        items = [
            {
                "facture_id": l["id"],
                "association_id": l["association_id"],
                "nom_association": l["nom_association"],
                "numero_facture": l["numero_facture"],
                "code_vif": l["code_vif"],
                "montant": l["montant"],
                "detail_json": l["detail_json"],
                "email": l["email"],
            }
            for l in a_relancer
            if l["email"]
        ]

        if not items:
            flash("❌ Aucune association avec une adresse email valide à relancer.", "danger")
            return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=campagne_id))

        app_reel = current_app._get_current_object()
        db_path = get_db_path()

        Thread(
            target=envoyer_relances_cotisations_v2_background,
            args=(app_reel, db_path, items, sujet_modele, corps_modele,
                  numero_relance, campagne["annee"], mail_sender, mail_mode, mail_test_to)
        ).start()

        if mail_mode == "TEST":
            flash("🧪 Envoi TEST des relances lancé en arrière-plan (2 mails max vers l'adresse de test).", "warning")
        else:
            flash(f"🚀 Envoi des relances lancé en arrière-plan pour {len(items)} association(s).", "info")

        return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=campagne_id))

    except Exception:
        current_app.logger.exception("Erreur relance cotisations V2")
        flash("Erreur lors des relances.", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=campagne_id))


@tresorerie_bp.route("/cotisations_v2/relance/<int:campagne_id>/verifier_statut_mailjet", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_relance_verifier_statut_mailjet(campagne_id):

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    lignes = conn.execute("""
        SELECT id, relance_mailjet_message_ids
        FROM cotisations_v2_factures
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
            UPDATE cotisations_v2_factures
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

    return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=campagne_id))


@tresorerie_bp.route("/cotisations_v2/relance/renvoyer_gmail/<int:facture_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def cotisations_v2_relance_renvoyer_gmail(facture_id):

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
        return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=f["campagne_id"]))

    if not f["relance_sujet"] or not f["relance_corps"]:
        conn.close()
        flash(f"⛔ Aucune relance précédente connue pour {f['nom_association']} — envoyez d'abord une relance.", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=f["campagne_id"]))

    if f["mode_test_relance"]:
        conn.close()
        flash(f"⛔ La dernière relance pour {f['nom_association']} était en Mode TEST — un renvoi Gmail partirait, lui, pour de vrai. Refaites d'abord une relance réelle.", "danger")
        return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=f["campagne_id"]))

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

    pdf_path = f"/tmp/cotisations_v2_relance_gmail_{facture_id}.pdf"
    generer_facture_cotisation_v2_pdf(data_pdf, pdf_path)

    conn = sqlite3.connect(get_db_path())

    try:
        envoyer_mail_gmail(
            sujet=f["relance_sujet"],
            destinataires=destinataires,
            texte=f["relance_corps"],
            attachment_path=pdf_path
        )

        conn.execute("""
            UPDATE cotisations_v2_factures SET relance_renvoi_gmail_le = ? WHERE id = ?
        """, (datetime.now().isoformat(timespec="seconds"), facture_id))
        conn.commit()

        flash(f"📧 Relance renvoyée via Gmail à {f['email']} pour {f['nom_association']}", "success")

    except GmailSendError as e:
        write_log(f"❌ Erreur renvoi Gmail relance cotisation V2 pour {f['nom_association']} : {e}")
        flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")

    finally:
        conn.close()
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

    return redirect(url_for("tresorerie.cotisations_v2_relance_start", campagne_id=f["campagne_id"]))
