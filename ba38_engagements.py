# ba38_engagements.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, has_access, write_log
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime

import sqlite3

engagements_bp = Blueprint("engagements", __name__)


# ============================================================
# PAGE PRINCIPALE MODULE ENGAGEMENTS
# ============================================================

@engagements_bp.route("/engagements")
@login_required
def engagements_main():

    if not has_access("engagements", "lecture"):
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT
                e.id,
                e.cree_le,
                e.statut,
                e.demandeur_nom,
                p.nom_affiche AS pole,
                d.objet,
                d.montant_total
            FROM engagements e
            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id
            LEFT JOIN poles p
                ON p.id = e.pole_id
            ORDER BY e.cree_le DESC;
        """).fetchall()
    return render_template(
        "engagements/main.html",
        demandes=rows
    )

# ============================================================
# PAGE PRINCIPALE PARAMETRAGE ENGAGEMENTS
# ============================================================

@engagements_bp.route("/engagements/parametres")
@login_required
def engagements_parametres_main():

    if not has_access("engagement_parametres", "lecture"):
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    return render_template("engagements/parametres.html")


# ============================================================
# GESTION DES POLES
# ============================================================

@engagements_bp.route("/engagements/parametres/poles", methods=["GET", "POST"])
@login_required
def gestion_poles():

    if not has_access("engagement_parametres", "lecture"):
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("index"))

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        if request.method == "POST":

            if not has_access("engagement_parametres", "ecriture"):
                flash("⛔ Modification non autorisée.", "danger")
                return redirect(url_for("engagements.gestion_poles"))

            pole_id = request.form.get("pole_id")
            nom_affiche = request.form.get("nom_affiche")
            responsable_id = request.form.get("responsable_id") or None
            supp1_id = request.form.get("suppleant1_id") or None
            supp2_id = request.form.get("suppleant2_id") or None

            conn.execute("""
                UPDATE poles
                SET nom_affiche = ?,
                    responsable_id = ?,
                    suppleant1_id = ?,
                    suppleant2_id = ?
                WHERE id = ?
            """, (
                nom_affiche,
                responsable_id,
                supp1_id,
                supp2_id,
                pole_id
            ))

            conn.commit()
            flash("✅ Pôle mis à jour.", "success")
            return redirect(url_for("engagements.gestion_poles"))

        poles = conn.execute("""
            SELECT p.*,
                   u1.email AS responsable_email,
                   u2.email AS supp1_email,
                   u3.email AS supp2_email
            FROM poles p
            LEFT JOIN users u1 ON u1.id = p.responsable_id
            LEFT JOIN users u2 ON u2.id = p.suppleant1_id
            LEFT JOIN users u3 ON u3.id = p.suppleant2_id
            ORDER BY p.nom_affiche
        """).fetchall()

        users = conn.execute("""
            SELECT id, email
            FROM users
            ORDER BY email
        """).fetchall()

    return render_template(
        "engagements/gestion_poles.html",
        poles=poles,
        users=users
    )


# ============================================================
# NOUVELLE DEMANDE D'ENGAGEMENT
# ============================================================

@engagements_bp.route("/engagements/nouvelle")
@login_required
def nouvelle_demande():

    if not has_access("engagements", "ecriture"):
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("engagements.engagements_main"))

    return render_template("engagements/nouvelle_demande.html")


@engagements_bp.route("/engagements/deplacement/nouveau")
@login_required
def nouveau_deplacement():
    return render_template("engagements/nouveau_deplacement.html")


@engagements_bp.route("/engagements/note-frais/nouvelle")
@login_required
def nouvelle_note_frais():
    return render_template("engagements/nouvelle_note_frais.html")


@engagements_bp.route("/engagements/depense/nouvelle", methods=["GET", "POST"])
@login_required
def nouvelle_depense():

    if not has_access("engagements", "ecriture"):
        flash("⛔ Accès interdit.", "danger")
        return redirect(url_for("engagements.engagements_main"))

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # 🔹 Charger les pôles pour le formulaire
        poles = conn.execute("""
            SELECT id, nom_affiche
            FROM poles
            WHERE actif = 1
            ORDER BY nom_affiche
        """).fetchall()

        if request.method == "POST":

            pole_id = request.form.get("pole_id")
            objet = request.form.get("objet")
            description = request.form.get("description")
            rubrique = request.form.get("rubrique")
            precision_rubrique = request.form.get("precision_rubrique")
            montant_total = request.form.get("montant_total")

            attestation = 1 if request.form.get("attestation_comparaison") else 0
            signature = request.form.get("signature")

            if not pole_id or not objet or not montant_total:
                flash("⚠️ Merci de remplir les champs obligatoires.", "warning")
                return render_template("engagements/nouvelle_depense.html", poles=poles)

            if not signature:
                flash("⚠️ Vous devez confirmer la signature.", "warning")
                return render_template("engagements/nouvelle_depense.html", poles=poles)

            montant = float(montant_total)
            devis_necessaire = 1 if montant > 500 else 0
            nb_devis = int(request.form.get("nb_devis") or 0)
            commentaire_devis = request.form.get("commentaire_devis")

            # 🔹 Règle métier 200-500 €
            if 200 < montant <= 500 and not attestation:
                flash("⚠️ Attestation obligatoire entre 200 € et 500 €.", "warning")
                return render_template("engagements/nouvelle_depense.html", poles=poles)

            # 🔹 Règle métier > 500 €
            if montant > 500 and nb_devis < 3:
                flash("⚠️ Minimum 3 devis requis au-delà de 500 €.", "danger")
                return render_template("engagements/nouvelle_depense.html", poles=poles)

            # ============================
            # 1️⃣ INSERT TABLE MÈRE
            # ============================

            cur = conn.execute("""
                INSERT INTO engagements (
                    type,
                    demandeur_id,
                    demandeur_nom,
                    demandeur_email,
                    pole_id,
                    statut
                )
                VALUES (?, ?, ?, ?, ?, 'demande')
            """, (
                "depense",
                current_user.id,
                current_user.username,
                current_user.email,
                pole_id
            ))

            engagement_id = cur.lastrowid

            # ============================
            # 2️⃣ INSERT TABLE SPÉCIFIQUE
            # ============================

            conn.execute("""
                INSERT INTO engagements_depenses (
                    engagement_id,
                    objet,
                    description,
                    rubrique,
                    precision_rubrique,
                    montant_total,
                    attestation_comparaison,
                    devis_necessaire,
                    nb_devis,
                    commentaire_devis
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                engagement_id,
                objet,
                description,
                rubrique,
                precision_rubrique,
                montant,
                attestation,
                devis_necessaire,
                nb_devis,
                commentaire_devis
            ))

            conn.commit()

            flash("✅ Demande d'engagement enregistrée.", "success")
            return redirect(url_for("engagements.engagements_main"))

    # 🔹 GET
    return render_template(
        "engagements/nouvelle_depense.html",
        poles=poles
    )
