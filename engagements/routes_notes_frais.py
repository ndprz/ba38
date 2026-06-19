"""
routes_notes_frais.py
=====================

Gestion des notes de frais du module Engagements.

Fonctionnalités :
- Génération automatique d'une note de frais PDF.
- Remplacement automatique de l'ancienne note de frais.
- Historisation dans engagements_workflow.
- Consultation et régénération depuis un engagement.

Ce module est utilisé automatiquement lors de la création
d'un engagement de type déplacement.
"""

from flask import (
    render_template,
    request,
    redirect,
    url_for,
    flash,
    abort,
    current_app,
)

from flask_login import login_required, current_user

from decimal import Decimal
from pypdf import PdfReader, PdfWriter

from utils import (
    get_db_path,
    write_log,
    require_access,
)

from datetime import datetime

from engagements import engagements_bp

import sqlite3
import os
import uuid


# =====================================================
# GENERATION AUTOMATIQUE NOTE DE FRAIS
# =====================================================

def generer_note_frais_auto(
    conn,
    engagement_id,
    objet,
    date_frais,
    kms,
    peages,
    repas,
    commentaire=""
):
    """
    Génère automatiquement une note de frais PDF.

    Paramètres :
        conn            : connexion SQLite active
        engagement_id   : identifiant engagement
        objet           : objet du déplacement
        date_frais      : date du déplacement
        kms             : kilomètres parcourus
        peages          : montant péages
        repas           : montant repas
        commentaire     : commentaire libre
    """

    conn.row_factory = sqlite3.Row

    engagement = conn.execute(
        """
        SELECT *
        FROM engagements
        WHERE id = ?
        """,
        (engagement_id,)
    ).fetchone()

    if not engagement:
        write_log(
            f"[NOTE FRAIS] Engagement introuvable : {engagement_id}"
        )
        return

    kms = Decimal(str(kms or 0))
    peages = Decimal(str(peages or 0))
    repas = Decimal(str(repas or 0))

    tarif_km = Decimal("0.529")

    montant_ik = (
        kms * tarif_km
    ).quantize(Decimal("0.01"))

    total = (
        montant_ik + peages + repas
    ).quantize(Decimal("0.01"))

    template_pdf = os.path.join(
        current_app.root_path,
        "templates",
        "pdf",
        "note de frais_voyage.pdf"
    )

    if not os.path.exists(template_pdf):
        raise FileNotFoundError(
            f"Template PDF introuvable : {template_pdf}"
        )

    reader = PdfReader(template_pdf)

    writer = PdfWriter()
    writer.append(reader)
    writer.set_need_appearances_writer()

    writer.update_page_form_field_values(
        writer.pages[0],
        {
            "Frais engagés par Nom Prénom":
                engagement["demandeur_nom"],

            "Objet":
                objet,

            "62561 Frais déplacement visite Association":
                "/On",

            "kilometres":
                str(kms),

            "date":
                date_frais,

            "Montant IK":
                str(montant_ik),

            "Montant Frais":
                str(peages + repas),

            "Total à Rembourser":
                str(total),

            "Mt /km":
                str(tarif_km),

            "Signature_ba380":
                f"Document généré électroniquement dans BA380\n"
                f"Par : {engagement['demandeur_nom']}\n"
                f"Courriel : {engagement['demandeur_email']}\n"
                f"Date : {datetime.now().strftime('%d/%m/%Y à %H:%M')}"

        }
    )

    upload_dir = os.path.join(
        current_app.root_path,
        "uploads",
        "engagements",
        str(engagement_id)
    )

    os.makedirs(upload_dir, exist_ok=True)

    nom_original = f"note_frais_{engagement_id}.pdf"
    nom_stockage = f"{uuid.uuid4().hex}.pdf"

    chemin_pdf = os.path.join(
        upload_dir,
        nom_stockage
    )

    with open(chemin_pdf, "wb") as output_stream:
        writer.write(output_stream)

    anciens_fichiers = conn.execute(
        """
        SELECT chemin_fichier
        FROM engagements_fichiers
        WHERE engagement_id = ?
          AND type_fichier = 'note_frais'
        """,
        (engagement_id,)
    ).fetchall()

    for fichier in anciens_fichiers:

        ancien_chemin = fichier["chemin_fichier"]

        if ancien_chemin and os.path.exists(ancien_chemin):

            try:
                os.remove(ancien_chemin)

            except Exception as exc:

                write_log(
                    f"[NOTE FRAIS] "
                    f"Suppression impossible : {exc}"
                )

    conn.execute(
        """
        DELETE FROM engagements_fichiers
        WHERE engagement_id = ?
          AND type_fichier = 'note_frais'
        """,
        (engagement_id,)
    )

    conn.execute(
        """
        INSERT INTO engagements_fichiers (
            engagement_id,
            nom_original,
            nom_stockage,
            chemin_fichier,
            type_fichier,
            uploaded_by,
            uploaded_le
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            engagement_id,
            nom_original,
            nom_stockage,
            chemin_pdf,
            "note_frais",
            current_user.email
        )
    )

    conn.execute(
        """
        INSERT INTO engagements_workflow (
            engagement_id,
            action,
            commentaire,
            user_id,
            user_email
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            engagement_id,
            "note_frais",
            f"Note de frais générée automatiquement ({total} €)",
            current_user.id,
            current_user.email
        )
    )


# =====================================================
# NOTE DE FRAIS
# =====================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/note-frais",
    methods=["GET", "POST"]
)
@login_required
@require_access("engagements", "ecriture")
def note_frais_engagement(engagement_id):
    """
    Affichage et régénération manuelle
    d'une note de frais.
    """


    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT statut
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if engagement["statut"] == "reglee":

            flash(
                "⚠️ Cet engagement est déjà réglé.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )


        engagement = conn.execute(
            """
            SELECT
                e.*,
                d.objet,
                d.description,
                d.montant_total,
                d.sous_type_depense
            FROM engagements e
            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id
            WHERE e.id = ?
            """,
            (engagement_id,)
        ).fetchone()

        if not engagement:
            abort(404)

        if engagement["sous_type_depense"] != "deplacement":

            flash(
                "⚠️ Cet engagement n'est pas un déplacement.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        if request.method == "POST":

            objet = request.form.get(
                "objet",
                ""
            ).strip()

            date_frais = request.form.get(
                "date_frais"
            )

            kms = Decimal(
                request.form.get("kms") or "0"
            )

            peages = Decimal(
                request.form.get("peages") or "0"
            )

            repas = Decimal(
                request.form.get("repas") or "0"
            )

            commentaire = request.form.get(
                "commentaire",
                ""
            ).strip()

            tarif_km = Decimal("0.529")

            montant_ik = (
                kms * tarif_km
            ).quantize(Decimal("0.01"))

            total = (
                montant_ik + peages + repas
            ).quantize(Decimal("0.01"))

            generer_note_frais_auto(
                conn=conn,
                engagement_id=engagement_id,
                objet=objet,
                date_frais=date_frais,
                kms=kms,
                peages=peages,
                repas=repas,
                commentaire=commentaire
            )

            # ====================================================
            # MISE A JOUR DEPENSE
            # ====================================================

            conn.execute("""
                UPDATE engagements_depenses
                SET
                    objet = ?,
                    date_frais = ?,
                    kms = ?,
                    peages = ?,
                    repas = ?,
                    commentaire_frais = ?,
                    montant_total = ?
                WHERE engagement_id = ?
            """, (
                objet,
                date_frais,
                float(kms),
                float(peages),
                float(repas),
                commentaire,
                float(total),
                engagement_id
            ))

            conn.execute("""
                INSERT INTO engagements_workflow (
                    engagement_id,
                    action,
                    commentaire,
                    user_id,
                    user_email
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                engagement_id,
                "regeneration_note_frais",
                f"Note de frais régénérée - montant recalculé : {total:.2f} €",
                current_user.id,
                current_user.email
            ))


            conn.commit()

            flash(
                f"✅ Note de frais générée ({total} €)",
                "success"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

    return render_template(
        "engagements/note_frais.html",
        engagement=engagement
    )
