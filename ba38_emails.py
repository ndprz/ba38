from flask import Blueprint, render_template, request, redirect, url_for, flash
import sqlite3
import os
from utils import get_db_path, require_access, write_log, envoyer_mail, render_modele_email
from ba38_indicateurs import generate_pdf_indicateurs_trim, generate_pdf_indicateurs_annuel
from utils_pdf_form import remplir_pdf_indicateurs

emails_bp = Blueprint("emails", __name__, template_folder="templates")

# ==========================================
# 📄 Liste des modèles
# ==========================================
@emails_bp.route("/emails")
def liste_modeles():

    with sqlite3.connect(get_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        modeles = conn.execute("""
            SELECT * FROM modeles_emails
            ORDER BY code_modele
        """).fetchall()

    return render_template(
        "emails/liste_modeles.html",
        modeles=modeles
    )


# ==========================================
# ➕ Ajouter / Modifier
# ==========================================
@emails_bp.route("/emails/edit/<int:id>", methods=["GET", "POST"])
@emails_bp.route("/emails/new", methods=["GET", "POST"])
def edit_modele(id=None):

    with sqlite3.connect(get_db_path()) as conn:
        conn.row_factory = sqlite3.Row

        if id:
            modele = conn.execute("SELECT * FROM modeles_emails WHERE id = ?", (id,)).fetchone()
        else:
            modele = None

        if request.method == "POST":
            code = request.form.get("code_modele")
            sujet = request.form.get("sujet")
            corps = request.form.get("corps")

            if id:
                conn.execute("""
                    UPDATE modeles_emails
                    SET code_modele=?, sujet=?, corps=?, date_modification=datetime('now')
                    WHERE id=?
                """, (code, sujet, corps, id))
                flash("Modèle mis à jour", "success")

            else:
                conn.execute("""
                    INSERT INTO modeles_emails (code_modele, sujet, corps, date_modification)
                    VALUES (?, ?, ?, datetime('now'))
                """, (code, sujet, corps))
                flash("Modèle créé", "success")

            conn.commit()
            return redirect(url_for("emails.liste_modeles"))

    return render_template("emails/edit_modele.html", modele=modele)


# ==========================================
# 🗑️ Suppression
# ==========================================
@emails_bp.route("/emails/delete/<int:id>")
def delete_modele(id):
    with sqlite3.connect(get_db_path()) as conn:
        conn.execute("DELETE FROM modeles_emails WHERE id = ?", (id,))
        conn.commit()

    flash("Modèle supprimé", "warning")
    return redirect(url_for("emails.liste_modeles"))


# ============================================================================
# 📧 ENVOI DES MAILS INDICATEURS
# ============================================================================

@emails_bp.route("/emails/envoi/<int:campagne_id>", methods=["GET", "POST"])
def envoyer_mails(campagne_id):

    db_path = get_db_path()

    # ============================================================================
    # 📥 RÉCUPÉRATION DES DONNÉES
    # ============================================================================
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        modeles = conn.execute("""
            SELECT * FROM modeles_emails
            ORDER BY code_modele
        """).fetchall()

        campagne = conn.execute("""
            SELECT * FROM indicateurs_campagnes
            WHERE id = ?
        """, (campagne_id,)).fetchone()

        write_log(f"📧 campagne_id utilisé = {campagne_id}")

        if not campagne:
            raise ValueError(f"❌ Campagne {campagne_id} introuvable")

        lignes = conn.execute("""
            SELECT
                i.*,
                a.nom_association,
                a.code_VIF AS code_vif,
                a.courriel_resp_IE1,
                a.courriel_resp_IE2,
                a.responsable_IE,
                a.tel_resp_IE,
                a.CAR
            FROM indicateurs_suivi i
            JOIN associations a ON i.association_id = a.id
            WHERE i.campagne_id = ?
            AND LOWER(TRIM(i.statut_csv)) LIKE 'non%'
        """, (campagne_id,)).fetchall()

    # ============================================================================
    # 🚀 TRAITEMENT ENVOI
    # ============================================================================
    if request.method == "POST":

        modele_id = request.form.get("modele_id")
        sender = request.form.get("sender") or "ba380@banquealimentaire.org"
        mode_test_local = request.form.get("mode_test") == "on"

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            modele = conn.execute("""
                SELECT * FROM modeles_emails
                WHERE id = ?
            """, (modele_id,)).fetchone()

        if not modele:
            flash("❌ Modèle introuvable", "danger")
            return redirect(url_for("emails.envoyer_mails", campagne_id=campagne_id))

        # ============================================================================
        # ❌ VALIDATION EMAILS
        # ============================================================================
        erreurs = []

        for l in lignes:
            if not (l["courriel_resp_IE1"] or l["courriel_resp_IE2"]):
                erreurs.append(l["nom_association"])

        if erreurs:
            msg = "❌ Aucune adresse email pour :<br>" + "<br>".join(erreurs)
            flash(msg, "danger")
            return redirect(url_for("emails.envoyer_mails", campagne_id=campagne_id))

        # ============================================================================
        # 📤 ENVOI
        # ============================================================================
        envoyes = 0
        periode = campagne["periode"]

        if periode.lower().startswith("année"):
            type_periode = "annuel"
        else:
            type_periode = "trimestriel"

        for i, l in enumerate(lignes):

            # 🔥 OPTION 2 → conversion en dict
            association = dict(l)

            # 📄 génération PDF
            pdf_path = f"/tmp/indicateurs_{association['association_id']}.pdf"

            # 🔥 choix du template
            if type_periode == "annuel":
                template = "/srv/ba38/dev/templates/pdf/indicateurs_annuels.pdf"
            else:
                template = "/srv/ba38/dev/templates/pdf/indicateurs_trimestriels.pdf"

            remplir_pdf_indicateurs(
                template,
                pdf_path,
                association,
                campagne
            )

            # ============================================================================
            # 📧 CONTEXTE MAIL
            # ============================================================================
            from datetime import datetime

            date_limite = campagne["date_limite"]

            if date_limite:
                try:
                    date_limite = datetime.strptime(date_limite, "%Y-%m-%d").strftime("%d/%m/%Y")
                except:
                    pass

            contexte = {
                "nom_association": association["nom_association"],
                "periode": periode,
                "trimestre": periode.split()[0],
                "annee": periode.split()[1],
                "date_limite": date_limite,
            }

            write_log(f"CONTEXTE: {contexte}")

            sujet = render_modele_email(modele["sujet"], contexte).strip()
            corps = render_modele_email(modele["corps"], contexte)

            # ============================================================================
            # 📬 DESTINATAIRES
            # ============================================================================
            emails = []

            if association["courriel_resp_IE1"]:
                emails.append(association["courriel_resp_IE1"])

            if association["courriel_resp_IE2"]:
                emails.append(association["courriel_resp_IE2"])

            emails = list(set(emails))

            # ============================================================================
            # 🧪 MODE TEST
            # ============================================================================
            if mode_test_local:
                emails = ["ba380.informatique2@banquealimentaire.org"]

                if i >= 2:
                    break

            # ============================================================================
            # 📤 ENVOI
            # ============================================================================
            try:
                write_log(f"📧 Envoi à {emails} | {association['nom_association']}")

                envoyer_mail(
                    sujet=sujet,
                    destinataires=emails,
                    texte=corps,
                    sender_override=sender,
                    attachment_path=pdf_path
                )

                envoyes += 1

                # 🔥 nettoyage fichier
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)

            except Exception as e:
                write_log(f"❌ Erreur envoi mail pour {association['nom_association']} : {e}")

        # ============================================================================
        # 📢 MESSAGE FINAL
        # ============================================================================
        if mode_test_local:
            flash(f"🧪 TEST : {envoyes} mails envoyés (max 2)", "warning")
        else:
            flash(f"📤 {envoyes} mails envoyés", "success")

        return redirect(url_for("indicateurs.resultats", campagne_id=campagne_id))

    # ============================================================================
    # 📄 AFFICHAGE PAGE
    # ============================================================================
    return render_template(
        "indicateurs/indicateurs_envoi_mails.html",
        modeles=modeles,
        campagne=campagne,
        nb=len(lignes)
    )
