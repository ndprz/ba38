# ba38_engagements.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash, abort
from flask import current_app
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, has_access, write_log, require_access
from utils import get_real_ip
from utils import envoyer_mail, is_valid_iban
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime
from werkzeug.utils import secure_filename
from decimal import Decimal, InvalidOperation
from pypdf import PdfReader, PdfWriter

import sqlite3
import os
import uuid


from ba38_engagements import engagements_bp





# ============================================================
# TRANSMISSION TRESORERIE
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/transmettre-tresorerie",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def transmettre_tresorerie(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT
                e.*,
                d.objet,
                d.montant_total,
                d.type_engagement,
                p.nom_affiche,
                u.email AS tresorier_email

            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            LEFT JOIN engagement_poles p
                ON p.id = e.pole_id

            LEFT JOIN users u
                ON u.id = p.tresorier_user_id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        ancien_statut = engagement["statut"]

        conn.execute("""
            UPDATE engagements
            SET statut = 'a_payer'
            WHERE id = ?
        """, (engagement_id,))

        conn.execute("""
            INSERT INTO engagements_workflow (
                engagement_id,
                action,
                ancien_statut,
                nouveau_statut,
                commentaire,
                user_id,
                user_email
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,
            "transmission_tresorerie",
            ancien_statut,
            "a_payer",
            "Transmission à la trésorerie",
            current_user.id,
            current_user.email
        ))

        # =====================================================
        # MAIL TRESORERIE
        # =====================================================

        if engagement["tresorier_email"]:

            lien = url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id,
                _external=True
            )

            sujet = (
                f"Engagement prêt pour règlement "
                f"#{engagement_id}"
            )

            texte = f"""
    Bonjour,

    Un engagement est prêt pour règlement.

    Engagement :
    #{engagement_id}

    Objet :
    {engagement["objet"]}

    Montant :
    {engagement["montant_total"]:.2f} €

    Lien :
    {lien}

    ---
    BA38
    """

            envoyer_mail(
                sujet=sujet,
                destinataires=[
                    engagement["tresorier_email"]
                ],
                texte=texte
            )

        conn.commit()

    flash(
        "✅ Demande transmise à la trésorerie.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )




# ============================================================
# TRESORERIE - VERIFICATION DOUBLON NUMERO ECRITURE EBP
# ============================================================

@engagements_bp.route(
    "/engagements/api/verifier-numero-ebp"
)
@login_required
@require_access("engagements", "ecriture")
def api_verifier_numero_ebp():

    numero_ecriture_ebp = request.args.get(
        "numero_ecriture_ebp", ""
    ).strip()

    engagement_id = request.args.get(
        "engagement_id",
        type=int
    )

    if not numero_ecriture_ebp:
        return jsonify({"doublon": False, "engagements": []})

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        doublons = conn.execute("""
            SELECT
                e.id,
                d.objet
            FROM engagements e
            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id
            WHERE e.numero_ecriture_ebp = ?
              AND e.id != ?
        """, (numero_ecriture_ebp, engagement_id or 0)).fetchall()

    return jsonify({
        "doublon": len(doublons) > 0,
        "engagements": [
            {"id": d["id"], "objet": d["objet"]}
            for d in doublons
        ]
    })


# ============================================================
# TRESORERIE - PAIEMENT / REMBOURSEMENT
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/reglee",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def marquer_reglee(engagement_id):
    """
    Phase 1 du règlement : le fournisseur a été payé ou le
    bénévole remboursé. Le numéro d'écriture EBP n'est pas
    encore connu à ce stade — il sera saisi plus tard, à la
    comptabilisation (cf. marquer_comptabilise), à réception
    du relevé bancaire.
    """

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT
                e.*,
                d.type_engagement
            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        ancien_statut = engagement["statut"]

        nouveau_statut = "reglee"

        # =====================================================
        # UPDATE ENGAGEMENT
        # =====================================================

        conn.execute("""
            UPDATE engagements
            SET
                statut = ?,
                paye_le = CURRENT_TIMESTAMP,
                paye_par = ?
            WHERE id = ?
        """, (
            nouveau_statut,
            current_user.id,
            engagement_id
        ))

        # =====================================================
        # WORKFLOW
        # =====================================================

        conn.execute("""
            INSERT INTO engagements_workflow (
                engagement_id,
                action,
                ancien_statut,
                nouveau_statut,
                commentaire,
                user_id,
                user_email
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,

            "reglement",

            ancien_statut,
            nouveau_statut,

            "Règlement effectué (paiement fournisseur ou "
            "remboursement bénévole)",

            current_user.id,
            current_user.email
        ))

        conn.commit()

        # =====================================================
        # LOG
        # =====================================================

        write_log(
            f"[ENGAGEMENTS] Paiement engagement "
            f"#{engagement_id} -> {nouveau_statut}"
        )

    flash(
        "✅ Engagement réglé.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )


# ============================================================
# TRESORERIE - COMPTABILISATION (SAISIE ECRITURE EBP)
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/comptabiliser",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def marquer_comptabilise(engagement_id):
    """
    Phase 2 du règlement : à réception du relevé bancaire,
    la comptable rapproche le paiement et saisit le numéro
    d'écriture EBP correspondant.
    """

    numero_ecriture_ebp = request.form.get(
        "numero_ecriture_ebp", ""
    ).strip()

    confirmer_doublon = request.form.get(
        "confirmer_doublon"
    ) == "1"

    appliquer_futurs = request.form.get(
        "appliquer_futurs"
    ) == "1"

    if not numero_ecriture_ebp:

        flash(
            "⚠️ Le numéro d'écriture EBP est obligatoire "
            "pour marquer l'engagement comptabilisé.",
            "warning"
        )

        return redirect(
            url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id
            )
        )

    try:
        nouveau_montant = Decimal(
            request.form.get("montant_total", "")
        )
    except (InvalidOperation, ValueError):
        nouveau_montant = None

    if nouveau_montant is None or nouveau_montant <= 0:

        flash(
            "⚠️ Le montant saisi est invalide.",
            "warning"
        )

        return redirect(
            url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id
            )
        )

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT *
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        depense = conn.execute("""
            SELECT montant_total
            FROM engagements_depenses
            WHERE engagement_id = ?
        """, (engagement_id,)).fetchone()

        ancien_montant = Decimal(str(depense["montant_total"]))

        if engagement["statut"] != "reglee":

            flash(
                "⚠️ Cet engagement n'est pas en attente de "
                "comptabilisation.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        # =====================================================
        # CONTROLE DOUBLON NUMERO ECRITURE EBP
        # =====================================================

        doublons = conn.execute("""
            SELECT id
            FROM engagements
            WHERE numero_ecriture_ebp = ?
              AND id != ?
        """, (numero_ecriture_ebp, engagement_id)).fetchall()

        if doublons and not confirmer_doublon:

            numeros = ", ".join(
                f"#{d['id']}" for d in doublons
            )

            flash(
                f"⚠️ Le numéro d'écriture EBP « {numero_ecriture_ebp} » "
                f"est déjà utilisé par l'engagement {numeros}. "
                f"Confirmez si vous souhaitez réutiliser ce même numéro.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        ancien_statut = engagement["statut"]
        nouveau_statut = "comptabilise"

        # =====================================================
        # UPDATE ENGAGEMENT
        # =====================================================

        conn.execute("""
            UPDATE engagements
            SET
                statut = ?,
                comptabilise_le = CURRENT_TIMESTAMP,
                comptabilise_par = ?,
                numero_ecriture_ebp = ?
            WHERE id = ?
        """, (
            nouveau_statut,
            current_user.id,
            numero_ecriture_ebp,
            engagement_id
        ))

        # =====================================================
        # WORKFLOW
        # =====================================================

        conn.execute("""
            INSERT INTO engagements_workflow (
                engagement_id,
                action,
                ancien_statut,
                nouveau_statut,
                commentaire,
                user_id,
                user_email
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,

            "comptabilisation",

            ancien_statut,
            nouveau_statut,

            f"Comptabilisation effectuée (écriture EBP n° "
            f"{numero_ecriture_ebp})",

            current_user.id,
            current_user.email
        ))

        # =====================================================
        # CORRECTION DU MONTANT (rapprochement bancaire)
        # =====================================================

        montant_modifie = nouveau_montant != ancien_montant

        if montant_modifie:

            conn.execute("""
                UPDATE engagements_depenses
                SET montant_total = ?
                WHERE engagement_id = ?
            """, (float(nouveau_montant), engagement_id))

            conn.execute("""
                INSERT INTO engagements_workflow (
                    engagement_id,
                    action,
                    ancien_statut,
                    nouveau_statut,
                    commentaire,
                    user_id,
                    user_email
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                engagement_id,

                "modification_montant",

                nouveau_statut,
                nouveau_statut,

                f"Montant corrigé de {ancien_montant}€ à "
                f"{nouveau_montant}€ lors de la comptabilisation",

                current_user.id,
                current_user.email
            ))

        # =====================================================
        # REPERCUSSION SUR LES PROCHAINS PRELEVEMENTS DE
        # L'ABONNEMENT (si l'engagement est une occurrence
        # générée depuis un modèle d'abonnement récurrent)
        # =====================================================

        if (
            montant_modifie
            and appliquer_futurs
            and engagement["abonnement_parent_id"]
        ):

            modele_id = engagement["abonnement_parent_id"]

            depense_modele = conn.execute("""
                SELECT montant_total
                FROM engagements_depenses
                WHERE engagement_id = ?
            """, (modele_id,)).fetchone()

            if depense_modele:

                ancien_montant_modele = Decimal(
                    str(depense_modele["montant_total"])
                )

                conn.execute("""
                    UPDATE engagements_depenses
                    SET montant_total = ?
                    WHERE engagement_id = ?
                """, (float(nouveau_montant), modele_id))

                conn.execute("""
                    INSERT INTO engagements_workflow (
                        engagement_id,
                        action,
                        ancien_statut,
                        nouveau_statut,
                        commentaire,
                        user_id,
                        user_email
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    modele_id,

                    "modification_montant_abonnement",

                    None,
                    None,

                    f"Montant de l'abonnement ajusté de "
                    f"{ancien_montant_modele}€ à {nouveau_montant}€ "
                    f"pour les prochaines échéances (suite à "
                    f"comptabilisation de l'engagement "
                    f"#{engagement_id})",

                    current_user.id,
                    current_user.email
                ))

        conn.commit()

        # =====================================================
        # LOG
        # =====================================================

        write_log(
            f"[ENGAGEMENTS] Comptabilisation engagement "
            f"#{engagement_id} -> {nouveau_statut}"
        )

    if montant_modifie:

        flash(
            f"✅ Engagement comptabilisé, montant corrigé à "
            f"{nouveau_montant}€.",
            "success"
        )

    else:

        flash(
            "✅ Engagement comptabilisé.",
            "success"
        )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )

# ============================================================
# RETRANSMISSION APRES REFUS
# ============================================================

@engagements_bp.route(
    "/engagements/<int:engagement_id>/retransmettre",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def retransmettre_engagement(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT
                e.*,
                d.objet,
                d.montant_total,
                d.type_engagement,
                d.sous_type_depense,
                p.nom_affiche,
                u.email AS tresorier_email

            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            LEFT JOIN engagement_poles p
                ON p.id = e.pole_id

            LEFT JOIN users u
                ON u.id = p.tresorier_user_id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        # ====================================================
        # Seul le demandeur peut retransmettre
        # ====================================================

        if engagement["demandeur_id"] != current_user.id:

            abort(403)

        if engagement["statut"] != "refuse":

            flash(
                "⚠️ Cette demande n'est pas refusée.",
                "warning"
            )

            return redirect(
                url_for(
                    "engagements.detail_engagement",
                    engagement_id=engagement_id
                )
            )

        # ====================================================
        # Détermination du nouveau statut
        # ====================================================


        nouveau_statut = "a_payer"


        ancien_statut = engagement["statut"]

        # ====================================================
        # Mise à jour engagement
        # ====================================================

        conn.execute("""
            UPDATE engagements
            SET statut = ?
            WHERE id = ?
        """, (
            nouveau_statut,
            engagement_id
        ))

        # ====================================================
        # Workflow
        # ====================================================

        conn.execute("""
            INSERT INTO engagements_workflow (
                engagement_id,
                action,
                ancien_statut,
                nouveau_statut,
                commentaire,
                user_id,
                user_email
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            engagement_id,
            "retransmission",
            ancien_statut,
            nouveau_statut,
            "Dossier corrigé puis retransmis à la trésorerie",
            current_user.id,
            current_user.email
        ))

        # =====================================================
        # MAIL TRESORERIE
        # =====================================================

        if engagement["tresorier_email"]:

            lien = url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id,
                _external=True
            )

            sujet = (
                f"Engagement prêt pour règlement "
                f"#{engagement_id}"
            )

            texte = f"""
            Bonjour,

            Un engagement préalablement refusé est à nouveau
            prêt pour règlement.

            Engagement :
            #{engagement_id}

            Objet :
            {engagement["objet"]}

            Montant :
            {engagement["montant_total"]:.2f} €

            Lien :
            {lien}

            ---
            BA38
            """

            envoyer_mail(
                sujet=sujet,
                destinataires=[
                    engagement["tresorier_email"]
                ],
                texte=texte
            )

        conn.commit()

    flash(
        "✅ Demande retransmise.",
        "success"
    )

    return redirect(
        url_for(
            "engagements.detail_engagement",
            engagement_id=engagement_id
        )
    )
