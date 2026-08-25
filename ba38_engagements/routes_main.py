# ba38_engagements.py

from flask import Blueprint, render_template, send_file, request, jsonify, session, redirect, url_for, flash, abort
from flask import current_app
from flask_login import login_required, current_user
from utils import get_db_path, get_db_connection, has_access, write_log, require_access
from utils import get_real_ip
from utils import envoyer_mail, is_valid_iban
from utils import generer_token_validation_pole
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime
from werkzeug.utils import secure_filename
from decimal import Decimal
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

            montant = Decimal(montant_total)

            nb_devis = int(
                request.form.get("nb_devis") or 0
            )

            commentaire_devis = request.form.get(
                "commentaire_devis"
            )

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

            if un_devis_obligatoire and nb_fichiers_devis < 1:

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


            if deux_devis_obligatoires:

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
                        texte=texte
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
                    texte=texte
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

                    ?, ?, ?, ?,

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

                nom_beneficiaire = (
                    fournisseur_nom
                    if type_engagement == "fournisseur"
                    else None
                )

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
    return render_template(
        "engagements/detail_engagement.html",

        engagement=engagement,

        workflow=workflow,
        commentaires=commentaires,
        fichiers=fichiers,

        peut_valider_pole=peut_valider_pole,
        peut_valider_presidence=peut_valider_presidence
    )
