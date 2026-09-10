# ba38_engagements.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash, abort
from flask import current_app
from flask_login import login_required, current_user
from ba38_utilitaires.core import get_db_path, get_db_connection, has_access, write_log, require_access
from ba38_utilitaires.core import get_real_ip
from ba38_utilitaires.core import envoyer_mail, is_valid_iban
from ba38_utilitaires.core import generer_token_validation_pole
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
from ba38_engagements.utils_financement import calculer_montant_utilise

# ============================================================
# PAGE PRINCIPALE MODULE ENGAGEMENTS
# ============================================================

@engagements_bp.route("/main")
@login_required
@require_access("engagements", "lecture")
def engagements_main():

    db_path = get_db_path()

    # =========================================================
    # GESTION SESSION AFFICHAGE ARCHIVES
    # =========================================================

    if "show_deleted" in request.args:

        session["engagements_show_deleted"] = (
            request.args.get("show_deleted") == "1"
        )

    show_deleted = session.get(
        "engagements_show_deleted",
        False
    )

    # =========================================================
    # DROIT DE VOIR TOUS LES ENGAGEMENTS
    # =========================================================

    voir_tous_engagements = has_access(
        "engagements_admin",
        "ecriture"
    )

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        # =====================================================
        # REQUETE PRINCIPALE
        # =====================================================

        query = """
            SELECT
                e.id,
                e.cree_le,
                e.statut,
                e.demandeur_nom,

                e.deleted,
                e.deleted_le,

                e.est_modele_abonnement,
                e.abonnement_parent_id,

                p.nom_affiche AS pole,

                d.objet,
                d.montant_total,

                d.type_engagement,
                d.fournisseur_nom,
                d.beneficiaire_nom,

                b.nom AS benevole_nom,
                b.prenom AS benevole_prenom

            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            LEFT JOIN engagement_poles p
                ON p.id = e.pole_id

            LEFT JOIN benevoles b
                ON b.id = d.beneficiaire_benevole_id
        """

        where_clauses = []
        params = []

        # =====================================================
        # FILTRE ARCHIVES
        # =====================================================

        if not show_deleted:

            where_clauses.append(
                "COALESCE(e.deleted, 0) = 0"
            )

        # =====================================================
        # UTILISATEUR STANDARD
        # =====================================================

        if not voir_tous_engagements:

            where_clauses.append(
                "e.demandeur_id = ?"
            )

            params.append(
                current_user.id
            )

        # =====================================================
        # CONSTRUCTION WHERE
        # =====================================================

        if where_clauses:

            query += "\nWHERE " + "\nAND ".join(where_clauses)

        # =====================================================
        # TRI
        # =====================================================

        query += """
            ORDER BY e.cree_le DESC
        """

        rows = conn.execute(
            query,
            params
        ).fetchall()


        # =====================================================
        # TABLEAU DE BORD - STATISTIQUES GLOBALES
        # =====================================================

        stats_where = [
            "COALESCE(e.deleted, 0) = 0"
        ]

        stats_params = []

        if not voir_tous_engagements:

            stats_where.append(
                "e.demandeur_id = ?"
            )

            stats_params.append(
                current_user.id
            )

        stats_sql = f"""

            SELECT

                COUNT(*) AS nb_total,

                SUM(
                    COALESCE(d.montant_total, 0)
                ) AS montant_total,

                SUM(
                    CASE
                        WHEN e.statut = 'validation_pole'
                        THEN 1
                        ELSE 0
                    END
                ) AS nb_validation_pole,

                SUM(
                    CASE
                        WHEN e.statut = 'validation_presidence'
                        THEN 1
                        ELSE 0
                    END
                ) AS nb_validation_presidence,

                SUM(
                    CASE
                        WHEN e.statut = 'valide'
                        THEN 1
                        ELSE 0
                    END
                ) AS nb_valides,

                SUM(
                    CASE
                        WHEN e.statut = 'reglee'
                        THEN 1
                        ELSE 0
                    END
                ) AS nb_payes,

                SUM(
                    CASE
                        WHEN e.statut = 'reglee'
                        THEN 1
                        ELSE 0
                    END
                ) AS nb_rembourses,

                SUM(
                    CASE
                        WHEN e.statut = 'termine'
                        THEN 1
                        ELSE 0
                    END
                ) AS nb_termines,

                SUM(
                    CASE
                        WHEN e.statut = 'refuse'
                        THEN 1
                        ELSE 0
                    END
                ) AS nb_refuses,

                SUM(
                    CASE
                        WHEN e.statut = 'valide'
                        AND d.type_engagement = 'fournisseur'
                        THEN COALESCE(d.montant_total, 0)
                        ELSE 0
                    END
                ) AS montant_a_payer,

                SUM(
                    CASE
                        WHEN e.statut = 'valide'
                        AND d.type_engagement != 'fournisseur'
                        THEN COALESCE(d.montant_total, 0)
                        ELSE 0
                    END
                ) AS montant_a_rembourser

            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            WHERE {" AND ".join(stats_where)}

        """

        stats = conn.execute(
            stats_sql,
            stats_params
        ).fetchone()

        # =====================================================
        # CONVERSION DICT
        # =====================================================

        demandes = [dict(r) for r in rows]

        # =====================================================
        # HISTORIQUE WORKFLOW (tous les badges par engagement)
        # =====================================================

        historique_par_engagement = {}

        if demandes:

            ids = [d["id"] for d in demandes]

            placeholders = ",".join("?" * len(ids))

            historique_rows = conn.execute(f"""
                SELECT
                    engagement_id,
                    nouveau_statut
                FROM engagements_workflow
                WHERE engagement_id IN ({placeholders})
                AND nouveau_statut IS NOT NULL
                AND nouveau_statut != 'supprime'
                ORDER BY engagement_id, date_action ASC, id ASC
            """, ids).fetchall()

            for r in historique_rows:

                badges = historique_par_engagement.setdefault(
                    r["engagement_id"], []
                )

                if not badges or badges[-1] != r["nouveau_statut"]:

                    badges.append(r["nouveau_statut"])

        for d in demandes:

            d["workflow_badges"] = historique_par_engagement.get(
                d["id"], [d["statut"]]
            )

    # =========================================================
    # AFFICHAGE
    # =========================================================

    return render_template(
        "engagements/main.html",
        demandes=demandes,
        show_deleted=show_deleted,
        stats=stats
    )




@engagements_bp.route("/engagements/depense/nouvelle", methods=["GET", "POST"])
@require_access("engagements", "ecriture")
@login_required
def nouvelle_depense():

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # 🔹 Charger les pôles pour le formulaire
        poles = conn.execute("""
            SELECT id, nom_affiche
            FROM engagement_poles
            WHERE actif = 1
            ORDER BY nom_affiche
        """).fetchall()

        paliers_rows = conn.execute("""
            SELECT *
            FROM engagements_parametres
            WHERE actif = 1
            ORDER BY montant_max
        """).fetchall()

        benevoles = conn.execute("""
            SELECT id, nom, prenom
            FROM benevoles
            ORDER BY nom, prenom
        """).fetchall()

        subventions = conn.execute("""
            SELECT *
            FROM engagement_subventions
            ORDER BY nom_subvention
        """).fetchall()

        fournisseurs = [
            dict(f) for f in conn.execute("""
                SELECT id, nom, adresse, adresse2, cp, ville, tel, mail, iban
                FROM fournisseurs
                WHERE actif IS NULL OR actif != 'non'
                ORDER BY nom COLLATE NOCASE
            """).fetchall()
        ]

        paliers = [dict(p) for p in paliers_rows]

        if request.method == "POST":

            pole_id = request.form.get("pole_id")

            objet = request.form.get("objet")

            description = request.form.get("description")

            rubrique = request.form.get("rubrique")

            precision_rubrique = request.form.get(
                "precision_rubrique"
            )

            subvention_id = request.form.get(
                "subvention_id"
            ) or None

            montant_total = request.form.get(
                "montant_total"
            )

            if montant_total:
                montant_total = montant_total.replace(",", ".")

            # =====================================================
            # TYPE ENGAGEMENT
            # =====================================================

            type_engagement = request.form.get(
                "type_engagement"
            )

            sous_type_depense = request.form.get(
                "sous_type_depense",
                "achat"
            )

            # =====================================================
            # NOTE DE FRAIS
            # =====================================================

            date_frais = None
            kms = None
            peages = None
            repas = None
            commentaire_frais = None


            beneficiaire_benevole_id = request.form.get(
                "beneficiaire_benevole_id"
            )
            date_frais = request.form.get(
                "date_frais"
            )

            kms = request.form.get(
                "kms"
            ) or 0

            peages = request.form.get(
                "peages"
            ) or 0

            repas = request.form.get(
                "repas"
            ) or 0

            commentaire_frais = request.form.get(
                "commentaire_frais"
            )

            fournisseur_id = request.form.get(
                "fournisseur_id"
            ) or None

            fournisseur_nom = request.form.get(
                "fournisseur_nom"
            )

            fournisseur_adresse = request.form.get(
                "fournisseur_adresse"
            )

            fournisseur_telephone = request.form.get(
                "fournisseur_telephone"
            )

            fournisseur_email = request.form.get(
                "fournisseur_email"
            )

            fournisseur_iban = request.form.get(
                "fournisseur_iban"
            )

            fournisseur_sans_coordonnees = bool(
                request.form.get("fournisseur_sans_coordonnees")
            )

            engagement_recurrent = bool(
                request.form.get("engagement_recurrent")
            )

            abonnement_jour_mois = request.form.get(
                "abonnement_jour_mois", type=int
            )

            attestation = 1 if request.form.get("attestation_comparaison") else 0
            signature = request.form.get("signature")

            signature_le = datetime.now().isoformat()

            signature_ip = get_real_ip()

            signature_user_agent = request.headers.get("User-Agent")

            if (
                sous_type_depense == "achat"
                and type_engagement in (
                    "benevole_self",
                    "benevole_other"
                )
                and rubrique == "achats_denrees"
            ):

                flash(
                    "⚠️ Achat denrées réservé aux achats "
                    "avec paiement fournisseur.",
                    "warning"
                )

                return render_template(
                    "engagements/nouvelle_depense.html",
                    poles=poles,
                    paliers=paliers,
                    benevoles=benevoles,
                    subventions=subventions,
                    fournisseurs=fournisseurs
                )

            if not pole_id or not objet or not montant_total:
                flash("⚠️ Merci de remplir les champs obligatoires.", "warning")
                return render_template(
                    "engagements/nouvelle_depense.html",
                    poles=poles,
                    paliers=paliers,
                    benevoles=benevoles,
                    subventions=subventions,
                    fournisseurs=fournisseurs
                )

            if not signature:

                flash(
                    "⚠️ Vous devez confirmer la signature.",
                    "warning"
                )

                return render_template(
                    "engagements/nouvelle_depense.html",
                    poles=poles,
                    paliers=paliers,
                    benevoles=benevoles,
                    subventions=subventions,
                    fournisseurs=fournisseurs
                )

            # =====================================================
            # MONTANT / DEVIS / FICHIERS
            # =====================================================

            try:
                montant = Decimal(montant_total)
            except InvalidOperation:

                flash(
                    "⚠️ Le montant saisi est invalide.",
                    "warning"
                )

                return render_template(
                    "engagements/nouvelle_depense.html",
                    poles=poles,
                    paliers=paliers,
                    benevoles=benevoles,
                    subventions=subventions,
                    fournisseurs=fournisseurs
                )

            nb_devis = int(
                request.form.get("nb_devis") or 0
            )

            commentaire_devis = request.form.get(
                "commentaire_devis"
            )

            devis_derogation = 1 if request.form.get(
                "devis_derogation"
            ) else 0

            files = []

            devis1 = request.files.get("devis_file_1")
            devis2 = request.files.get("devis_file_2")

            documents_complementaires = (
                request.files.getlist(
                    "documents_complementaires"
                )
            )

            write_log("===================================")
            write_log(
                f"Nb documents complémentaires = "
                f"{len(documents_complementaires)}"
            )

            for doc in documents_complementaires:

                write_log(
                    f"Document reçu : {doc.filename}"
                )

            write_log("===================================")

            if devis1 and devis1.filename:
                files.append(devis1)

            if devis2 and devis2.filename:
                files.append(devis2)

            write_log(
                f"Nombre de fichiers reçus = {len(files)}"
            )

            for f in files:
                write_log(
                    f"Fichier reçu : {f.filename}"
                )


            fichiers_devis = [
                f for f in files
                if f and f.filename
            ]

            nb_fichiers_devis = len(fichiers_devis)

            # =====================================================
            # RECHERCHE PALIER
            # =====================================================

            palier = conn.execute("""
                SELECT *
                FROM engagements_parametres
                WHERE actif = 1
                AND montant_max >= ?
                ORDER BY montant_max
                LIMIT 1
            """, (str(montant),)).fetchone()

            # write_log("===================================")

            # write_log("[ENGAGEMENTS] DEBUG WORKFLOW")

            # write_log(f"Montant = {montant}")

            if not palier:

                # write_log("Palier = None")

                # write_log("===================================")

                flash(
                    "⚠️ Aucun palier de validation configuré.",
                    "danger"
                )

                return render_template(
                    "engagements/nouvelle_depense.html",
                    poles=poles,
                    paliers=paliers,
                    benevoles=benevoles,
                    subventions=subventions,
                    fournisseurs=fournisseurs
                )

            # write_log(f"Palier = {dict(palier)}")

            # write_log(
            #     f"un_devis = {palier['un_devis']}"
            # )

            # write_log(
            #     f"deux_devis = {palier['deux_devis']}"
            # )

            # write_log(
            #     f"accord_presidence = {palier['accord_presidence']}"
            # )

            # write_log("===================================")

            # =====================================================
            # REGLES DEVIS
            # =====================================================

            un_devis_obligatoire = (
                palier["un_devis"] == "o"
            )

            deux_devis_obligatoires = (
                palier["deux_devis"] == "o"
            )

            # =====================================================
            # EXCEPTION DEPLACEMENT
            # =====================================================

            if sous_type_depense == "deplacement":

                un_devis_obligatoire = False
                deux_devis_obligatoires = False

            devis_necessaire = 1 if (
                un_devis_obligatoire
                or deux_devis_obligatoires
            ) else 0

            # =====================================================
            # CONTROLES METIER
            # =====================================================

            if (
                un_devis_obligatoire
                and nb_fichiers_devis < 1
                and not devis_derogation
            ):

                flash(
                    "⚠️ Au moins 1 devis est obligatoire.",
                    "warning"
                )

                return render_template(
                    "engagements/nouvelle_depense.html",
                    poles=poles,
                    paliers=paliers,
                    benevoles=benevoles,
                    subventions=subventions,
                    fournisseurs=fournisseurs
                )


            if deux_devis_obligatoires and not devis_derogation:

                if not devis1 or not devis1.filename \
                or not devis2 or not devis2.filename:

                    flash(
                        "⚠️ Les deux devis PDF sont obligatoires.",
                        "warning"
                    )

                    return render_template(
                        "engagements/nouvelle_depense.html",
                        poles=poles,
                        paliers=paliers,
                        benevoles=benevoles,
                        subventions=subventions,
                        fournisseurs=fournisseurs
                    )

            # =====================================================
            # WORKFLOW
            # =====================================================

            if palier["accord_resp_pole"] == "o":

                statut = "validation_pole"

            elif palier["accord_presidence"] == "o":

                statut = "validation_presidence"

            else:

                statut = "valide"

            write_log(f"Statut final = {statut}")


            if type_engagement == "fournisseur":

                if fournisseur_iban:

                    if not is_valid_iban(fournisseur_iban):

                        flash(
                            "⚠️ IBAN invalide.",
                            "warning"
                        )

                        return render_template(
                            "engagements/nouvelle_depense.html",
                            poles=poles,
                            paliers=paliers,
                            benevoles=benevoles,
                            subventions=subventions,
                            fournisseurs=fournisseurs
                        )

                champs_manquants = []

                if not fournisseur_nom:
                    champs_manquants.append("nom")

                if not fournisseur_adresse:
                    champs_manquants.append("adresse")

                if not fournisseur_sans_coordonnees:

                    if not fournisseur_email:
                        champs_manquants.append("email")

                    if not fournisseur_iban:
                        champs_manquants.append("IBAN")

                if champs_manquants:

                    flash(
                        "⚠️ Champs fournisseur manquants : "
                        + ", ".join(champs_manquants),
                        "warning"
                    )

                    return render_template(
                        "engagements/nouvelle_depense.html",
                        poles=poles,
                        paliers=paliers,
                        benevoles=benevoles,
                        subventions=subventions,
                        fournisseurs=fournisseurs
                    )

                if engagement_recurrent:

                    if not abonnement_jour_mois or not (1 <= abonnement_jour_mois <= 28):

                        flash(
                            "⚠️ Le jour du mois (engagement récurrent) "
                            "doit être compris entre 1 et 28.",
                            "warning"
                        )

                        return render_template(
                            "engagements/nouvelle_depense.html",
                            poles=poles,
                            paliers=paliers,
                            benevoles=benevoles,
                            subventions=subventions,
                            fournisseurs=fournisseurs
                        )

            else:

                engagement_recurrent = False

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
                    statut,

                    signature_le,
                    signature_ip,
                    signature_user_agent
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "depense",
                current_user.id,
                current_user.username,
                current_user.email,
                pole_id,

                statut,

                signature_le,
                signature_ip,
                signature_user_agent
            ))
            engagement_id = cur.lastrowid

            if engagement_recurrent:

                conn.execute("""
                    UPDATE engagements
                    SET
                        est_modele_abonnement = 1,
                        abonnement_actif = 1,
                        abonnement_jour_mois = ?
                    WHERE id = ?
                """, (abonnement_jour_mois, engagement_id))

                write_log(
                    f"[ABONNEMENTS] Engagement #{engagement_id} créé "
                    f"directement comme abonnement (jour {abonnement_jour_mois} du mois)"
                )

            # =====================================================
            # STOCKAGE DEVIS PDF
            # =====================================================

            upload_dir = os.path.join(
                current_app.root_path,
                "uploads",
                "engagements",
                str(engagement_id)
            )

            os.makedirs(upload_dir, exist_ok=True)

            for file in files:

                if not file or not file.filename:
                    continue

                filename = secure_filename(file.filename)

                if not filename.lower().endswith(".pdf"):

                    flash(
                        "⚠️ Seuls les fichiers PDF sont autorisés.",
                        "warning"
                    )

                    return render_template(
                        "engagements/nouvelle_depense.html",
                        poles=poles,
                        paliers=paliers,
                        benevoles=benevoles,
                        subventions=subventions,
                        fournisseurs=fournisseurs
                    )

                unique_name = (
                    f"{uuid.uuid4()}_{filename}"
                )

                filepath = os.path.join(
                    upload_dir,
                    unique_name
                )

                file.save(filepath)

                conn.execute("""
                    INSERT INTO engagements_fichiers (
                        engagement_id,
                        type_fichier,
                        nom_original,
                        nom_stockage,
                        chemin_fichier,
                        taille,
                        mime_type,
                        uploaded_by,
                        uploaded_le
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    engagement_id,
                    "devis",
                    filename,
                    unique_name,
                    filepath,
                    os.path.getsize(filepath),
                    file.mimetype,
                    current_user.id,
                    datetime.now().isoformat()
                ))

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
                    "upload_fichier",
                    None,
                    None,
                    f"Ajout fichier : {filename}",
                    current_user.id,
                    current_user.email
                ))

            # =====================================================
            # STOCKAGE DOCUMENTS COMPLEMENTAIRES
            # =====================================================

            for file in documents_complementaires:

                if not file or not file.filename:
                    continue

                filename = secure_filename(
                    file.filename
                )

                extension = os.path.splitext(
                    filename
                )[1].lower()

                if extension not in [
                    ".pdf",
                    ".jpg",
                    ".jpeg",
                    ".png"
                ]:
                    continue

                unique_name = (
                    f"{uuid.uuid4()}_{filename}"
                )

                filepath = os.path.join(
                    upload_dir,
                    unique_name
                )

                file.save(filepath)

                conn.execute("""
                    INSERT INTO engagements_fichiers (
                        engagement_id,
                        type_fichier,
                        nom_original,
                        nom_stockage,
                        chemin_fichier,
                        taille,
                        mime_type,
                        uploaded_by,
                        uploaded_le
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    engagement_id,
                    "justificatif",
                    filename,
                    unique_name,
                    filepath,
                    os.path.getsize(filepath),
                    file.mimetype,
                    current_user.id,
                    datetime.now().isoformat()
                ))

            # ============================
            # RECHERCHE POLE
            # ============================
            pole = conn.execute("""
                SELECT
                    p.nom_affiche,

                    p.responsable_id,
                    p.suppleant1_id,
                    p.suppleant2_id,

                    u1.email AS responsable_email,
                    u2.email AS supp1_email,
                    u3.email AS supp2_email

                FROM engagement_poles p

                LEFT JOIN users u1
                    ON u1.id = p.responsable_id

                LEFT JOIN users u2
                    ON u2.id = p.suppleant1_id

                LEFT JOIN users u3
                    ON u3.id = p.suppleant2_id

                WHERE p.id = ?
            """, (pole_id,)).fetchone()

            sujet = f"Nouvelle demande d'engagement #{engagement_id}"

            lien = url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id,
                _external=True
            )

            if statut == "validation_pole":

                # ============================
                # ENVOI MAIL PERSONNALISE
                # (lien de validation sécurisé, sans connexion)
                # ============================

                # Si le demandeur est le responsable de pôle,
                # seul le suppléant 1 valide (conflit d'intérêt).
                # Le suppléant 1 est enregistré comme secours pour ce
                # seul cas et n'est donc pas sollicité en temps normal.
                if current_user.id == pole["responsable_id"]:
                    destinataires_pole = list(filter(None, [
                        (pole["suppleant1_id"], pole["supp1_email"])
                        if pole["suppleant1_id"] and pole["supp1_email"] else None,
                    ]))
                else:
                    destinataires_pole = list(filter(None, [
                        (pole["responsable_id"], pole["responsable_email"])
                        if pole["responsable_id"] and pole["responsable_email"] else None,
                    ]))

                for user_id, user_email in destinataires_pole:

                    token = generer_token_validation_pole(
                        engagement_id,
                        user_id
                    )

                    lien_validation = url_for(
                        "engagements.valider_engagement_pole_lien",
                        engagement_id=engagement_id,
                        token=token,
                        _external=True
                    )

                    texte = f"""
            Bonjour,

            Une nouvelle demande d'engagement nécessite votre validation.

            Pôle :
            {pole["nom_affiche"]}

            Demandeur :
            {current_user.username}

            Objet :
            {objet}

            Montant :
            {montant:.2f} €

            Valider directement, sans vous connecter :
            {lien_validation}

            Ou en vous connectant à l'application :
            {lien}

            ---
            BA38
            """

                    envoyer_mail(
                        sujet=sujet,
                        destinataires=[user_email],
                        texte=texte,
                        sender_override="ba380@banquealimentaire.org",
                        sender_name=current_user.username,
                        reply_to=current_user.email
                    )

            else:

                # ============================
                # ENVOI MAIL NOTIFICATION (générique)
                # ============================

                destinataires = []

                if pole["responsable_email"]:
                    destinataires.append(pole["responsable_email"])

                if pole["supp1_email"]:
                    destinataires.append(pole["supp1_email"])

                if pole["supp2_email"]:
                    destinataires.append(pole["supp2_email"])

                # suppression doublons
                destinataires = list(set(destinataires))

                texte = f"""
            Bonjour,

            Une nouvelle demande d'engagement nécessite votre validation.

            Pôle :
            {pole["nom_affiche"]}

            Demandeur :
            {current_user.username}

            Objet :
            {objet}

            Montant :
            {montant:.2f} €

            Accéder à la demande :
            {lien}

            ---
            BA38
            """

                envoyer_mail(
                    sujet=sujet,
                    destinataires=destinataires,
                    texte=texte,
                    sender_override="ba380@banquealimentaire.org",
                    sender_name=current_user.username,
                    reply_to=current_user.email
                )

            # ============================
            # 2️⃣ INSERT ENGAGEMENT WORKFLOW
            # ============================


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
                "signature",
                None,
                "validation_pole",
                "Validation électronique de la demande",
                current_user.id,
                current_user.email
            ))

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
                    subvention_id,

                    montant_total,

                    date_frais,
                    kms,
                    peages,
                    repas,
                    commentaire_frais,

                    attestation_comparaison,

                    devis_necessaire,
                    nb_devis,
                    commentaire_devis,
                    devis_derogation,

                    type_engagement,

                    beneficiaire_user_id,
                    beneficiaire_benevole_id,
                    beneficiaire_nom,

                    fournisseur_id,
                    fournisseur_nom,
                    fournisseur_adresse,
                    fournisseur_telephone,
                    fournisseur_email,
                    fournisseur_iban,

                    sous_type_depense

                )
                VALUES (

                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,

                    ?, ?, ?, ?, ?,

                    ?, ?, ?, ?, ?,

                    ?, ?, ?, ?, ?, ?,

                    ?

                )
            """, (

                engagement_id,

                objet,
                description,
                rubrique,
                precision_rubrique,
                subvention_id,

                str(montant),

                date_frais,
                kms,
                peages,
                repas,
                commentaire_frais,

                attestation,

                devis_necessaire,
                nb_devis,
                commentaire_devis,
                devis_derogation,

                type_engagement,

                current_user.id
                    if type_engagement == "benevole_self"
                    else None,

                beneficiaire_benevole_id
                    if type_engagement == "benevole_other"
                    else None,

                current_user.username
                    if type_engagement == "benevole_self"
                    else None,

                fournisseur_id,
                fournisseur_nom,
                fournisseur_adresse,
                fournisseur_telephone,
                fournisseur_email,
                fournisseur_iban,

                sous_type_depense

            ))

            # =====================================================
            # GENERATION AUTO NOTE DE FRAIS
            # =====================================================
            # Note de frais générée pour tout remboursement à un
            # bénévole (vous-même ou autre bénévole), que ce soit
            # un achat ou un déplacement. Également pour un déplacement
            # avec paiement fournisseur (au nom du fournisseur).

            generer_frais = (
                type_engagement in ("benevole_self", "benevole_other")
                or (
                    type_engagement == "fournisseur"
                    and sous_type_depense == "deplacement"
                )
            )

            if generer_frais:

                from .routes_notes_frais import generer_note_frais_auto

                if type_engagement == "fournisseur":
                    nom_beneficiaire = fournisseur_nom
                elif type_engagement == "benevole_other" and beneficiaire_benevole_id:
                    benevole = conn.execute(
                        "SELECT nom, prenom FROM benevoles WHERE id = ?",
                        (beneficiaire_benevole_id,)
                    ).fetchone()
                    nom_beneficiaire = (
                        f"{benevole['prenom']} {benevole['nom']}"
                        if benevole else None
                    )
                else:
                    nom_beneficiaire = None

                generer_note_frais_auto(
                    conn=conn,
                    engagement_id=engagement_id,
                    objet=objet,
                    montant_total=montant,
                    date_frais=date_frais,
                    kms=kms,
                    peages=peages,
                    repas=repas,
                    rubrique=rubrique,
                    precision_rubrique=precision_rubrique,
                    commentaire="",
                    nom_beneficiaire=nom_beneficiaire
                )

            conn.commit()

            flash("✅ Demande d'engagement enregistrée.", "success")
            return redirect(url_for("engagements.engagements_main"))

    # 🔹 GET

    return render_template(
        "engagements/nouvelle_depense.html",
        poles=poles,
        paliers=paliers,
        benevoles=benevoles,
        subventions=subventions,
        fournisseurs=fournisseurs
    )


# ============================================================
# API : CREATION RAPIDE D'UN FOURNISSEUR
# ============================================================

@engagements_bp.route(
    "/engagements/api/quick_create_fournisseur",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def api_quick_create_fournisseur():

    try:
        data = request.get_json(force=True)

        nom = (data.get("nom") or "").strip()
        adresse = (data.get("adresse") or "").strip()
        adresse2 = (data.get("adresse2") or "").strip()
        cp = (data.get("cp") or "").strip()
        ville = (data.get("ville") or "").strip()
        tel = (data.get("tel") or "").strip()
        mail = (data.get("mail") or "").strip()
        iban = (data.get("iban") or "").strip()

        if not nom:
            return jsonify(
                success=False,
                error="Le nom du fournisseur est obligatoire."
            )

        if iban and not is_valid_iban(iban):
            return jsonify(
                success=False,
                error="IBAN invalide."
            )

        db_path = get_db_path()

        with sqlite3.connect(db_path) as conn:

            cur = conn.execute("""
                INSERT INTO fournisseurs (
                    nom, adresse, adresse2, cp, ville, tel, mail, iban,
                    actif, date_creation, user_modif
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'oui', ?, ?)
            """, (
                nom,
                adresse or None,
                adresse2 or None,
                cp or None,
                ville or None,
                tel or None,
                mail or None,
                iban or None,
                datetime.now().isoformat(),
                current_user.email
            ))

            fournisseur_id = cur.lastrowid
            conn.commit()

        write_log(
            f"➕ Fournisseur créé rapidement depuis engagements : "
            f"#{fournisseur_id} {nom} (par {current_user.email})"
        )

        return jsonify(
            success=True,
            id=fournisseur_id,
            nom=nom,
            adresse=adresse,
            adresse2=adresse2,
            cp=cp,
            ville=ville,
            tel=tel,
            mail=mail,
            iban=iban
        )

    except Exception as e:
        current_app.logger.exception(
            "❌ Exception api_quick_create_fournisseur"
        )
        write_log(f"❌ Erreur création fournisseur rapide : {e}")
        return jsonify(success=False, error="Erreur serveur")


# ============================================================
# DETAIL D'UN ENGAGEMENT
# ============================================================

@engagements_bp.route("/detail_engagement/<int:engagement_id>")
@login_required
@require_access("engagements", "lecture")
def detail_engagement(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        # =====================================================
        # ENGAGEMENT PRINCIPAL
        # =====================================================

        engagement = conn.execute("""
            SELECT
                e.*,

                b.nom AS benevole_nom,
                b.prenom AS benevole_prenom,

                p.nom_affiche AS pole_nom,

                d.objet,
                d.description,
                d.rubrique,
                d.precision_rubrique,
                d.montant_total,
                d.attestation_comparaison,
                d.devis_necessaire,
                d.nb_devis,
                d.commentaire_devis,
                d.devis_derogation,

                d.type_engagement,
                d.sous_type_depense,

                d.beneficiaire_user_id,
                d.beneficiaire_benevole_id,
                d.beneficiaire_nom,

                d.fournisseur_nom,
                d.fournisseur_adresse,
                d.fournisseur_telephone,
                d.fournisseur_email,
                d.fournisseur_iban,

                d.fournisseur_connu_tresorerie,

                d.subvention_id,

                s.nom_subvention,
                s.commentaire AS subvention_commentaire,
                s.nom_organisme,
                s.accord_le,
                s.utilisation_date_debut,
                s.utilisation_date_fin,
                s.montant_prevu,
                s.montant_recu,
                s.date_montant_recu,
                s.montant_utilise,
                s.montant_restant

            FROM engagements e

            LEFT JOIN engagement_poles p
                ON p.id = e.pole_id

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            LEFT JOIN engagement_subventions s
                ON s.id = d.subvention_id

            LEFT JOIN benevoles b
                ON b.id = d.beneficiaire_benevole_id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        engagement = dict(engagement)

        if engagement["subvention_id"]:

            montant_utilise = float(
                calculer_montant_utilise(
                    conn, engagement["subvention_id"]
                )
            )

            engagement["montant_utilise"] = montant_utilise

            # Base de calcul : le reçu si dispo, sinon le prévu
            base_restant = (
                engagement["montant_recu"]
                or engagement["montant_prevu"]
                or 0
            )

            engagement["montant_restant"] = (
                base_restant - montant_utilise
            )

        # =====================================================
        # WORKFLOW / HISTORIQUE
        # =====================================================

        workflow = conn.execute("""
            SELECT
                *
            FROM engagements_workflow
            WHERE engagement_id = ?
            ORDER BY date_action DESC
        """, (engagement_id,)).fetchall()

        # =====================================================
        # COMMENTAIRES
        # =====================================================

        commentaires = conn.execute("""
            SELECT
                *
            FROM engagements_commentaires
            WHERE engagement_id = ?
            ORDER BY cree_le DESC
        """, (engagement_id,)).fetchall()

        # =====================================================
        # PIECES JOINTES
        # =====================================================

        fichiers = conn.execute("""
            SELECT
                *
            FROM engagements_fichiers
            WHERE engagement_id = ?
            ORDER BY uploaded_le DESC
        """, (engagement_id,)).fetchall()

        # =====================================================
        # AUTORISATIONS WORKFLOW
        # =====================================================

        peut_valider_pole = False
        peut_valider_presidence = False

        pole = conn.execute("""
            SELECT
                responsable_id,
                suppleant1_id,
                suppleant2_id,
                validation_presidence_email
            FROM engagement_poles
            WHERE id = ?
        """, (engagement["pole_id"],)).fetchone()

        if pole:

            ids_autorises = [
                pole["responsable_id"],
                pole["suppleant1_id"],
                pole["suppleant2_id"]
            ]

            peut_valider_pole = current_user.id in ids_autorises

            # =====================================================
            # VALIDATION presidence
            # =====================================================

            if engagement["statut"] == "validation_presidence":

                emails_presidence = []

                if pole["validation_presidence_email"]:

                    emails_presidence = [

                        x.strip().lower()

                        for x in pole[
                            "validation_presidence_email"
                        ].split(";")

                        if x.strip()

                    ]

                peut_valider_presidence = (

                    current_user.email.lower()
                    in emails_presidence

                )

        est_responsable_pole = bool(
            pole and current_user.id == pole["responsable_id"]
        )

        # =====================================================
        # MODIFICATION (Pôle/Objet/Description/Montant/Nature/
        # Rubrique/Précision/Commentaire devis)
        # =====================================================
        # Un modèle d'abonnement reste modifiable en permanence
        # (c'est un gabarit vivant pour les prochaines générations).
        # Un engagement normal ne l'est que tant qu'aucune validation
        # (pôle ou présidence) n'a encore eu lieu.

        peut_modifier = bool(engagement["est_modele_abonnement"]) or (
            engagement["statut"] in (
                "validation_pole",
                "validation_presidence"
            )
        )

        poles = conn.execute("""
            SELECT id, nom_affiche
            FROM engagement_poles
            WHERE actif = 1
            ORDER BY nom_affiche
        """).fetchall()

    return render_template(
        "engagements/detail_engagement.html",

        engagement=engagement,

        workflow=workflow,
        commentaires=commentaires,
        fichiers=fichiers,

        peut_valider_pole=peut_valider_pole,
        peut_valider_presidence=peut_valider_presidence,
        est_responsable_pole=est_responsable_pole,

        peut_modifier=peut_modifier,
        poles=poles
    )


# ============================================================
# MODIFICATION D'UN ENGAGEMENT (avant validation, ou modèle
# d'abonnement à tout moment)
# ============================================================
# Page dédiée (et non plus une modale) car elle réutilise le
# widget fournisseur/bénévole complet (select2 + AJAX) partagé
# avec la page de création via le partial
# engagements/_bloc_paiement_beneficiaire.html.
# ============================================================

@engagements_bp.route(
    "/detail_engagement/<int:engagement_id>/modifier-page"
)
@login_required
@require_access("engagements", "ecriture")
def modifier_depense_page(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT
                e.id,
                e.statut,
                e.est_modele_abonnement,
                e.pole_id,

                d.objet,
                d.description,
                d.montant_total,
                d.sous_type_depense,
                d.rubrique,
                d.precision_rubrique,
                d.commentaire_devis,

                d.subvention_id,
                d.type_engagement,

                d.beneficiaire_benevole_id,

                d.fournisseur_id,
                d.fournisseur_nom,
                d.fournisseur_adresse,
                d.fournisseur_telephone,
                d.fournisseur_email,
                d.fournisseur_iban,

                d.date_frais,
                d.kms,
                d.peages,
                d.repas

            FROM engagements e

            LEFT JOIN engagements_depenses d
                ON d.engagement_id = e.id

            WHERE e.id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        peut_modifier = bool(engagement["est_modele_abonnement"]) or (
            engagement["statut"] in (
                "validation_pole",
                "validation_presidence"
            )
        )

        if not peut_modifier:

            flash(
                "⚠️ Cet engagement ne peut plus être modifié "
                "(déjà validé).",
                "warning"
            )

            return redirect(url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id
            ))

        engagement = dict(engagement)

        # =====================================================
        # VALEURS POUR LE PARTIAL PARTAGE AVEC LA CREATION
        # =====================================================
        # Le partial _bloc_paiement_beneficiaire.html attend un
        # objet exposant .get(cle, defaut), comme request.form en
        # création. On lui fournit ici les valeurs actuelles de
        # engagements_depenses, avec les identifiants castés en
        # chaînes (comme le ferait request.form) pour que les
        # comparaisons "== x|string" du template fonctionnent.

        def _str_or_vide(valeur):
            return str(valeur) if valeur is not None else ""

        valeurs = {

            "sous_type_depense": engagement["sous_type_depense"] or "achat",
            "type_engagement": engagement["type_engagement"] or "benevole_self",

            "beneficiaire_benevole_id": _str_or_vide(
                engagement["beneficiaire_benevole_id"]
            ),

            "fournisseur_id": _str_or_vide(engagement["fournisseur_id"]),
            "fournisseur_nom": engagement["fournisseur_nom"] or "",
            "fournisseur_adresse": engagement["fournisseur_adresse"] or "",
            "fournisseur_telephone": engagement["fournisseur_telephone"] or "",
            "fournisseur_email": engagement["fournisseur_email"] or "",
            "fournisseur_iban": engagement["fournisseur_iban"] or "",

            # Non persisté en base (cf. nouvelle_depense) : décoché
            # par défaut à l'ouverture de la page de modification.
            "fournisseur_sans_coordonnees": "",

            "subvention_id": _str_or_vide(engagement["subvention_id"]),

            "rubrique": engagement["rubrique"] or "",
            "precision_rubrique": engagement["precision_rubrique"] or "",

            "date_frais": engagement["date_frais"] or "",
            "kms": engagement["kms"] if engagement["kms"] is not None else 0,
            "peages": engagement["peages"] if engagement["peages"] is not None else 0,
            "repas": engagement["repas"] if engagement["repas"] is not None else 0,

        }

        poles = conn.execute("""
            SELECT id, nom_affiche
            FROM engagement_poles
            WHERE actif = 1
            ORDER BY nom_affiche
        """).fetchall()

        benevoles = conn.execute("""
            SELECT id, nom, prenom
            FROM benevoles
            ORDER BY nom, prenom
        """).fetchall()

        subventions = conn.execute("""
            SELECT *
            FROM engagement_subventions
            ORDER BY nom_subvention
        """).fetchall()

        fournisseurs = [
            dict(f) for f in conn.execute("""
                SELECT id, nom, adresse, adresse2, cp, ville, tel, mail, iban
                FROM fournisseurs
                WHERE actif IS NULL OR actif != 'non'
                ORDER BY nom COLLATE NOCASE
            """).fetchall()
        ]

    return render_template(
        "engagements/modifier_depense.html",

        engagement=engagement,
        valeurs=valeurs,
        mode="modification",

        poles=poles,
        benevoles=benevoles,
        subventions=subventions,
        fournisseurs=fournisseurs
    )


@engagements_bp.route(
    "/detail_engagement/<int:engagement_id>/modifier",
    methods=["POST"]
)
@login_required
@require_access("engagements", "ecriture")
def modifier_engagement_configuration(engagement_id):

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:

        conn.row_factory = sqlite3.Row

        engagement = conn.execute("""
            SELECT id, statut, est_modele_abonnement,
                   demandeur_id, demandeur_nom
            FROM engagements
            WHERE id = ?
        """, (engagement_id,)).fetchone()

        if not engagement:
            abort(404)

        peut_modifier = bool(engagement["est_modele_abonnement"]) or (
            engagement["statut"] in (
                "validation_pole",
                "validation_presidence"
            )
        )

        if not peut_modifier:

            flash(
                "⚠️ Cet engagement ne peut plus être modifié "
                "(déjà validé).",
                "warning"
            )

            return redirect(url_for(
                "engagements.detail_engagement",
                engagement_id=engagement_id
            ))

        # =====================================================
        # ETAT AVANT MODIFICATION
        # (nécessaire pour décider si la note de frais générée
        # automatiquement doit être régénérée, cf. plus bas)
        # =====================================================

        depense_avant = conn.execute("""
            SELECT
                type_engagement,
                beneficiaire_benevole_id,
                objet,
                montant_total,
                sous_type_depense
            FROM engagements_depenses
            WHERE engagement_id = ?
        """, (engagement_id,)).fetchone()

        # =====================================================
        # CHAMPS "DE BASE" (déjà modifiables avant cette évolution)
        # =====================================================

        pole_id = request.form.get("pole_id", type=int)
        objet = request.form.get("objet", "").strip()
        description = request.form.get("description", "").strip()
        sous_type_depense = request.form.get(
            "sous_type_depense", ""
        ).strip()
        rubrique = request.form.get("rubrique", "").strip()
        precision_rubrique = request.form.get(
            "precision_rubrique", ""
        ).strip()
        commentaire_devis = request.form.get(
            "commentaire_devis", ""
        ).strip()

        if not pole_id or not objet:

            flash(
                "⚠️ Pôle et objet sont obligatoires.",
                "warning"
            )

            return redirect(url_for(
                "engagements.modifier_depense_page",
                engagement_id=engagement_id
            ))

        try:
            montant_total = Decimal(
                request.form.get("montant_total", "").replace(",", ".")
            )
        except (InvalidOperation, ValueError):
            montant_total = None

        if montant_total is None or montant_total <= 0:

            flash(
                "⚠️ Le montant saisi est invalide.",
                "warning"
            )

            return redirect(url_for(
                "engagements.modifier_depense_page",
                engagement_id=engagement_id
            ))

        # =====================================================
        # SUBVENTION + MODE DE PAIEMENT + BENEFICIAIRE
        # (champs ajoutés par cette évolution)
        # =====================================================

        subvention_id = request.form.get(
            "subvention_id"
        ) or None

        type_engagement = request.form.get(
            "type_engagement", ""
        ).strip()

        beneficiaire_benevole_id = request.form.get(
            "beneficiaire_benevole_id"
        ) or None

        fournisseur_id = request.form.get(
            "fournisseur_id"
        ) or None

        fournisseur_nom = request.form.get(
            "fournisseur_nom", ""
        ).strip()

        fournisseur_adresse = request.form.get(
            "fournisseur_adresse", ""
        ).strip()

        fournisseur_telephone = request.form.get(
            "fournisseur_telephone", ""
        ).strip()

        fournisseur_email = request.form.get(
            "fournisseur_email", ""
        ).strip()

        fournisseur_iban = request.form.get(
            "fournisseur_iban", ""
        ).strip()

        fournisseur_sans_coordonnees = bool(
            request.form.get("fournisseur_sans_coordonnees")
        )

        date_frais = request.form.get("date_frais") or None
        kms = request.form.get("kms") or 0
        peages = request.form.get("peages") or 0
        repas = request.form.get("repas") or 0

        if type_engagement not in (
            "benevole_self", "benevole_other", "fournisseur"
        ):

            flash(
                "⚠️ Mode de paiement invalide.",
                "warning"
            )

            return redirect(url_for(
                "engagements.modifier_depense_page",
                engagement_id=engagement_id
            ))

        if type_engagement == "benevole_other" and not beneficiaire_benevole_id:

            flash(
                "⚠️ Merci de sélectionner le bénévole bénéficiaire.",
                "warning"
            )

            return redirect(url_for(
                "engagements.modifier_depense_page",
                engagement_id=engagement_id
            ))

        if type_engagement == "fournisseur":

            if fournisseur_iban:

                if not is_valid_iban(fournisseur_iban):

                    flash(
                        "⚠️ IBAN invalide.",
                        "warning"
                    )

                    return redirect(url_for(
                        "engagements.modifier_depense_page",
                        engagement_id=engagement_id
                    ))

            champs_manquants = []

            if not fournisseur_nom:
                champs_manquants.append("nom")

            if not fournisseur_adresse:
                champs_manquants.append("adresse")

            if not fournisseur_sans_coordonnees:

                if not fournisseur_email:
                    champs_manquants.append("email")

                if not fournisseur_iban:
                    champs_manquants.append("IBAN")

            if champs_manquants:

                flash(
                    "⚠️ Champs fournisseur manquants : "
                    + ", ".join(champs_manquants),
                    "warning"
                )

                return redirect(url_for(
                    "engagements.modifier_depense_page",
                    engagement_id=engagement_id
                ))

        # =====================================================
        # VALEURS FINALES SELON LE TYPE D'ENGAGEMENT
        # =====================================================
        # Par sécurité, on annule explicitement (met à NULL) les
        # champs bénévole/fournisseur/déplacement qui ne
        # correspondent pas au type sélectionné : contrairement au
        # formulaire de création (toujours vierge au départ), le
        # formulaire de modification est pré-rempli avec les
        # anciennes valeurs, qui resteraient sinon présentes en base
        # même après un changement de mode de paiement ou de nature.

        beneficiaire_user_id_final = (
            engagement["demandeur_id"]
            if type_engagement == "benevole_self"
            else None
        )

        beneficiaire_nom_final = (
            engagement["demandeur_nom"]
            if type_engagement == "benevole_self"
            else None
        )

        beneficiaire_benevole_id_final = (
            beneficiaire_benevole_id
            if type_engagement == "benevole_other"
            else None
        )

        if type_engagement == "fournisseur":
            fournisseur_id_final = fournisseur_id
            fournisseur_nom_final = fournisseur_nom
            fournisseur_adresse_final = fournisseur_adresse
            fournisseur_telephone_final = fournisseur_telephone
            fournisseur_email_final = fournisseur_email
            fournisseur_iban_final = fournisseur_iban
        else:
            fournisseur_id_final = None
            fournisseur_nom_final = None
            fournisseur_adresse_final = None
            fournisseur_telephone_final = None
            fournisseur_email_final = None
            fournisseur_iban_final = None

        if sous_type_depense == "deplacement":
            date_frais_final = date_frais
            kms_final = kms
            peages_final = peages
            repas_final = repas
        else:
            date_frais_final = None
            kms_final = 0
            peages_final = 0
            repas_final = 0

        conn.execute("""
            UPDATE engagements
            SET pole_id = ?
            WHERE id = ?
        """, (pole_id, engagement_id))

        conn.execute("""
            UPDATE engagements_depenses
            SET
                objet = ?,
                description = ?,
                montant_total = ?,
                sous_type_depense = ?,
                rubrique = ?,
                precision_rubrique = ?,
                commentaire_devis = ?,

                subvention_id = ?,
                type_engagement = ?,

                beneficiaire_user_id = ?,
                beneficiaire_benevole_id = ?,
                beneficiaire_nom = ?,

                fournisseur_id = ?,
                fournisseur_nom = ?,
                fournisseur_adresse = ?,
                fournisseur_telephone = ?,
                fournisseur_email = ?,
                fournisseur_iban = ?,

                date_frais = ?,
                kms = ?,
                peages = ?,
                repas = ?
            WHERE engagement_id = ?
        """, (
            objet,
            description,
            float(montant_total),
            sous_type_depense,
            rubrique,
            precision_rubrique,
            commentaire_devis,

            subvention_id,
            type_engagement,

            beneficiaire_user_id_final,
            beneficiaire_benevole_id_final,
            beneficiaire_nom_final,

            fournisseur_id_final,
            fournisseur_nom_final,
            fournisseur_adresse_final,
            fournisseur_telephone_final,
            fournisseur_email_final,
            fournisseur_iban_final,

            date_frais_final,
            kms_final,
            peages_final,
            repas_final,

            engagement_id
        ))

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
            "modification_configuration",
            engagement["statut"],
            engagement["statut"],
            "Modification des informations de l'engagement "
            "(pôle/objet/description/montant/nature/rubrique/"
            "précision/commentaire devis/subvention/mode de "
            "paiement/bénéficiaire/fournisseur)",
            current_user.id,
            current_user.email
        ))

        # =====================================================
        # REGENERATION DE LA NOTE DE FRAIS SI NECESSAIRE
        # =====================================================
        # Une note de frais auto-générée existante peut devenir
        # incohérente (nom du bénéficiaire, montant, objet) si la
        # modification change le mode de paiement, le bénéficiaire,
        # l'objet ou le montant. On la régénère dans ce cas, avec
        # la même règle "generer_frais" que la création
        # (routes_main.py, nouvelle_depense()).

        generer_frais = (
            type_engagement in ("benevole_self", "benevole_other")
            or (
                type_engagement == "fournisseur"
                and sous_type_depense == "deplacement"
            )
        )

        ancien_type_engagement = (
            depense_avant["type_engagement"] if depense_avant else None
        )

        ancien_beneficiaire_benevole_id = (
            depense_avant["beneficiaire_benevole_id"] if depense_avant else None
        )

        ancien_objet = (
            depense_avant["objet"] if depense_avant else None
        )

        ancien_montant_total = (
            depense_avant["montant_total"] if depense_avant else None
        )

        champs_pertinents_modifies = (
            depense_avant is None
            or type_engagement != (ancien_type_engagement or "")
            or str(beneficiaire_benevole_id_final or "")
                != str(ancien_beneficiaire_benevole_id or "")
            or objet != (ancien_objet or "")
            or float(montant_total) != float(ancien_montant_total or 0)
        )

        if generer_frais and champs_pertinents_modifies:

            from .routes_notes_frais import generer_note_frais_auto

            if type_engagement == "fournisseur":
                nom_beneficiaire = fournisseur_nom_final
            elif (
                type_engagement == "benevole_other"
                and beneficiaire_benevole_id_final
            ):
                benevole = conn.execute(
                    "SELECT nom, prenom FROM benevoles WHERE id = ?",
                    (beneficiaire_benevole_id_final,)
                ).fetchone()
                nom_beneficiaire = (
                    f"{benevole['prenom']} {benevole['nom']}"
                    if benevole else None
                )
            else:
                nom_beneficiaire = None

            generer_note_frais_auto(
                conn=conn,
                engagement_id=engagement_id,
                objet=objet,
                montant_total=montant_total,
                date_frais=date_frais_final,
                kms=kms_final,
                peages=peages_final,
                repas=repas_final,
                rubrique=rubrique,
                precision_rubrique=precision_rubrique,
                commentaire="",
                nom_beneficiaire=nom_beneficiaire
            )

            write_log(
                f"[ENGAGEMENTS] Note de frais régénérée pour "
                f"l'engagement #{engagement_id} suite à modification "
                f"(mode de paiement/bénéficiaire/objet/montant)"
            )

        elif not generer_frais and depense_avant and (
            ancien_type_engagement in ("benevole_self", "benevole_other")
            or (
                ancien_type_engagement == "fournisseur"
                and (depense_avant["sous_type_depense"] == "deplacement")
            )
        ):

            # ATTENTION : cet engagement avait une note de frais
            # auto-générée, et le nouveau mode de paiement/nature ne
            # nécessite plus de note de frais (ex: bascule vers
            # fournisseur + achat classique). L'ancien PDF de note de
            # frais n'est PAS supprimé automatiquement ici (cas jugé
            # trop rare pour être traité dans ce correctif) : il reste
            # visible dans les pièces jointes de l'engagement et doit
            # être retiré manuellement si besoin.

            write_log(
                f"[ENGAGEMENTS] Engagement #{engagement_id} : le "
                f"nouveau mode de paiement ne nécessite plus de note "
                f"de frais, mais l'ancienne note (si elle existe) "
                f"n'a pas été supprimée automatiquement."
            )

        conn.commit()

        write_log(
            f"[ENGAGEMENTS] Engagement #{engagement_id} modifié "
            f"par {current_user.email}"
        )

    flash(
        "✅ Engagement modifié.",
        "success"
    )

    return redirect(url_for(
        "engagements.detail_engagement",
        engagement_id=engagement_id
    ))
