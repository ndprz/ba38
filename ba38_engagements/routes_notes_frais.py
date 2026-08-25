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
d'un engagement remboursé à un bénévole (vous-même ou autre
bénévole), qu'il s'agisse d'un achat ou d'un déplacement.
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

from ba38_engagements import engagements_bp

import sqlite3
import os
import uuid


# =====================================================
# CORRESPONDANCE RUBRIQUE -> CASE A COCHER DU PDF
# =====================================================
# Chaque rubrique correspond à une case à cocher et à
# son champ "Montant" associé sur la même ligne du PDF.

RUBRIQUE_CASE_PDF = {
    "fournitures_entretien":
        ("6063 Fournitures entretien  petit matériel", "Montant1"),
    "fournitures_administratives":
        ("6064 Fournitures administratives", "Montant2"),
    "deplacements_administratifs":
        ("62511 Déplacements administratifs", "Montant3"),
    "deplacements_institutions":
        ("62504 Déplacement relations avec institutions", "Montant4"),
    "vie_associative":
        ("62571 Vie associative denrées repas location salle", "Montant5"),
    "deplacements":
        ("62561 Frais déplacement visite Association", "Montant6"),
    "formation":
        ("62572 Frais de formation frais formateur repas salle", "Montant7"),
    "collecte_annuelle":
        ("62381 Collecte annuelle précisez", "Montant8"),
    "autres":
        ("Autres précisez", "Montant9"),
}

# Case cochée par défaut si la rubrique demandée ne correspond
# à aucune case du PDF (ex : "achats_denrees", qui n'a pas de
# ligne dédiée sur la note de frais).
RUBRIQUE_CASE_PDF_DEFAUT = ("Autres précisez", "Montant9")


# =====================================================
# GENERATION AUTOMATIQUE NOTE DE FRAIS
# =====================================================

def generer_note_frais_auto(
    conn,
    engagement_id,
    objet,
    montant_total,
    date_frais=None,
    kms=0,
    peages=0,
    repas=0,
    rubrique=None,
    precision_rubrique=None,
    commentaire="",
    nom_beneficiaire=None
):
    """
    Génère automatiquement une note de frais PDF.

    Paramètres :
        conn                : connexion SQLite active
        engagement_id       : identifiant engagement
        objet               : objet de la dépense
        montant_total       : montant à rembourser
        date_frais          : date du déplacement (achat / non concerné : None)
        kms                 : kilomètres parcourus (déplacement uniquement)
        peages              : montant péages (déplacement uniquement)
        repas               : montant repas (déplacement uniquement)
        rubrique            : rubrique sélectionnée (case à cocher du PDF)
        precision_rubrique  : précision libre (case "Autres")
        commentaire         : commentaire libre
        nom_beneficiaire    : nom figurant sur la note (fournisseur ou demandeur)
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

    total = Decimal(
        str(montant_total or 0)
    ).quantize(Decimal("0.01"))

    template_pdf = os.path.join(
        current_app.root_path,
        "templates",
        "pdf",
        "note_de_frais.pdf"
    )

    if not os.path.exists(template_pdf):
        raise FileNotFoundError(
            f"Template PDF introuvable : {template_pdf}"
        )

    case_pdf, montant_champ_pdf = RUBRIQUE_CASE_PDF.get(
        rubrique,
        RUBRIQUE_CASE_PDF_DEFAUT
    )

    reader = PdfReader(template_pdf)

    writer = PdfWriter()
    writer.append(reader)
    writer.set_need_appearances_writer()

    champs = {
        "Frais engagés par Nom Prénom":
            nom_beneficiaire or engagement["demandeur_nom"],

        "Objet":
            objet,

        case_pdf:
            "/On",

        montant_champ_pdf:
            str(total),

        "Total à Rembourser":
            str(total),

        "date":
            date_frais or datetime.now().strftime("%d/%m/%Y"),

        "Signature_ba380":
            f"Document généré électroniquement dans BA380\n"
            f"Par : {engagement['demandeur_nom']}\n"
            f"Courriel : {engagement['demandeur_email']}\n"
            f"Date : {datetime.now().strftime('%d/%m/%Y à %H:%M')}"
    }

    # Détail kilométrique : uniquement pertinent pour un déplacement
    if kms:
        champs["kilometres"] = str(kms)
        champs["Mt /km"] = str(tarif_km)
        champs["Montant IK"] = str(montant_ik)

    if peages or repas:
        champs["Montant Frais"] = str(peages + repas)

    if case_pdf == "Autres précisez" and precision_rubrique:
        champs["Autres à préciser"] = precision_rubrique

    writer.update_page_form_field_values(
        writer.pages[0],
        champs
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

        if engagement["statut"] in ("reglee", "comptabilise", "termine"):

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
                d.sous_type_depense,
                d.type_engagement,
                d.rubrique,
                d.precision_rubrique,
                d.date_frais,
                d.fournisseur_nom
            FROM engagements e
            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id
            WHERE e.id = ?
            """,
            (engagement_id,)
        ).fetchone()

        if not engagement:
            abort(404)

        type_eng = engagement["type_engagement"]
        sous_type = engagement["sous_type_depense"]

        note_autorisee = (
            type_eng in ("benevole_self", "benevole_other")
            or (type_eng == "fournisseur" and sous_type == "deplacement")
        )

        if not note_autorisee:

            flash(
                "⚠️ La note de frais n'est disponible que pour un "
                "remboursement bénévole ou un déplacement fournisseur.",
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

            commentaire = request.form.get(
                "commentaire",
                ""
            ).strip()

            if engagement["sous_type_depense"] == "deplacement":

                kms = Decimal(
                    request.form.get("kms") or "0"
                )

                peages = Decimal(
                    request.form.get("peages") or "0"
                )

                repas = Decimal(
                    request.form.get("repas") or "0"
                )

                tarif_km = Decimal("0.529")

                montant_ik = (
                    kms * tarif_km
                ).quantize(Decimal("0.01"))

                total = (
                    montant_ik + peages + repas
                ).quantize(Decimal("0.01"))

            else:

                kms = Decimal("0")
                peages = Decimal("0")
                repas = Decimal("0")

                total = Decimal(
                    request.form.get("montant") or "0"
                ).quantize(Decimal("0.01"))

            nom_beneficiaire = (
                engagement["fournisseur_nom"]
                if engagement["type_engagement"] == "fournisseur"
                else None
            )

            generer_note_frais_auto(
                conn=conn,
                engagement_id=engagement_id,
                objet=objet,
                montant_total=total,
                date_frais=date_frais,
                kms=kms,
                peages=peages,
                repas=repas,
                rubrique=engagement["rubrique"],
                precision_rubrique=engagement["precision_rubrique"],
                commentaire=commentaire,
                nom_beneficiaire=nom_beneficiaire
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
