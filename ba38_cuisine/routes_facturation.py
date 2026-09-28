# ============================================================
# 💶 Facturation mensuelle cuisine
#     - Une facture par client et par mois : elle regroupe ses bons de
#       livraison validés du mois non encore facturés, qui passent au
#       statut 'facture' (ce qui bloque leur annulation).
#     - Numéro FC-AAAA-NNNN (année d'émission), séquence continue : une
#       facture annulée garde son numéro (pas de trou), ses BL redeviennent
#       facturables.
#     - Écran unique sur le modèle de Cotisations V2 (ba38_tresorerie) :
#       envoi Mailjet en arrière-plan, contrôle du statut Mailjet,
#       règlements (partiels possibles, cuisine_factures_reglements : la
#       facture est soldée — date_paiement — quand leur total atteint son
#       montant), relances (routes_facturation_relance.py), renvoi Gmail.
#     - PDF régénéré à la demande depuis la base (aucun fichier conservé).
#     - Accessible depuis le module cuisine ET la trésorerie : droit
#       "production_cuisine" OU "tresorerie".
# ============================================================

import io
import os
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for,
)
from flask_login import current_user, login_required

from ba38_utilitaires.core import (
    get_db_path, has_access, mailjet_get_message_status, render_modele_email, split_emails,
    upload_database, write_log, envoyer_mail,
)
from ba38_utilitaires.organisation import get_organisation
from ba38_utilitaires.taches_fond import lancer_tache_fond
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import _connect, now_paris_str, today_paris

MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"]
CATEGORIES = {"carne": "Carné", "legumes": "Légumes"}
# Règlement à réception de facture : échéance = date de la facture.
DELAI_ECHEANCE_JOURS = 0
MAIL_SENDER = "ba380.comptable@banquealimentaire.org"
CODE_MODELE_ENVOI = "CUISINE Facture"
MODES_REGLEMENT = ["Virement", "Chèque", "Espèces", "Autre"]


def require_access_facturation(niveau):
    """Facturation cuisine : ouverte aux droits cuisine OU trésorerie."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not (has_access("production_cuisine", niveau) or has_access("tresorerie", niveau)):
                write_log(f"⛔ Accès refusé (facturation cuisine) : user={session.get('user_email')}")
                flash("⛔ Accès refusé", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def periode_label(annee, mois):
    return f"{MOIS_FR[int(mois) - 1]} {annee}"


def euros(valeur):
    """1234.5 → '1 234,50' (format français)."""
    return f"{valeur or 0:,.2f}".replace(",", " ").replace(".", ",")


def date_fr(valeur):
    try:
        return date.fromisoformat((valeur or "")[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return valeur or ""


def mail_mode_courant():
    return session.get("MAIL_MODE", os.getenv("MAIL_MODE", "TEST").upper())


def _prochain_numero_facture(conn, annee):
    prefixe = f"FC-{annee}-"
    dernier = conn.execute(
        "SELECT MAX(CAST(SUBSTR(numero, ?) AS INTEGER)) FROM cuisine_factures WHERE numero LIKE ?",
        (len(prefixe) + 1, prefixe + "%"),
    ).fetchone()[0]
    return f"{prefixe}{(dernier or 0) + 1:04d}"


def _bornes_mois(annee, mois):
    debut = date(annee, mois, 1)
    fin = date(annee + (mois == 12), mois % 12 + 1, 1)
    return debut.isoformat(), fin.isoformat()


def bl_a_facturer(conn, annee, mois):
    """BL validés (non annulés, non encore facturés) livrés dans le mois."""
    debut, fin = _bornes_mois(annee, mois)
    return conn.execute(
        """SELECT * FROM cuisine_bons_livraison
           WHERE statut = 'valide' AND date_livraison >= ? AND date_livraison < ?
           ORDER BY nom_association COLLATE NOCASE, date_livraison, numero""",
        (debut, fin),
    ).fetchall()


def charger_facture(conn, facture_id):
    """(facture, bons, lignes_par_bon, association) — tout ce qu'il faut
    pour le PDF, relu depuis la base."""
    facture = conn.execute("SELECT * FROM cuisine_factures WHERE id = ?", (facture_id,)).fetchone()
    if not facture:
        return None, [], {}, None
    bons = conn.execute(
        """SELECT b.* FROM cuisine_bons_livraison b
           JOIN cuisine_factures_bl fb ON fb.bon_id = b.id
           WHERE fb.facture_id = ?
           ORDER BY b.date_livraison, b.numero""",
        (facture_id,),
    ).fetchall()
    lignes_par_bon = {}
    for b in bons:
        lignes_par_bon[b["id"]] = conn.execute(
            """SELECT * FROM cuisine_bons_livraison_lignes WHERE bon_id = ?
               ORDER BY CASE categorie_produit WHEN 'carne' THEN 0 ELSE 1 END,
                        libelle_recette COLLATE NOCASE, nb_portions_barquette DESC""",
            (b["id"],),
        ).fetchall()
    association = conn.execute(
        "SELECT adresse_association_1, adresse_association_2, CP, COMMUNE FROM associations WHERE id = ?",
        (facture["association_id"],),
    ).fetchone()
    return facture, bons, lignes_par_bon, association


# ------------------------------------------------------------
# 📄 PDF
# ------------------------------------------------------------
def generer_facture_cuisine_pdf(facture, bons, lignes_par_bon, association, periode):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    org = get_organisation()
    styles = getSampleStyleSheet()
    petit = styles["Normal"].clone("petit", fontSize=9, leading=11)
    detail = styles["Normal"].clone("detail", fontSize=8, leading=10, leftIndent=8, textColor=colors.HexColor("#444444"))
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm, bottomMargin=15 * mm,
        title=f"Facture {facture['numero']}",
    )
    elements = []

    logo = None
    logo_path = Path(current_app.root_path) / (org.get("logo_path") or "")
    if org.get("logo_path") and logo_path.exists():
        logo = Image(str(logo_path), width=22 * mm, height=22 * mm, kind="proportional")
    titre = Paragraph(
        f"<font size=18><b>FACTURE</b></font><br/><font size=11>{facture['numero']} — repas livrés en {periode}</font>",
        styles["Normal"],
    )
    entete = Table([[logo or "", titre]], colWidths=[28 * mm, None])
    entete.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elements += [entete, Spacer(1, 6 * mm)]

    bloc_org = "<b>{}</b><br/>{}<br/>{}{}".format(
        org["nom"], (org.get("adresse") or "").replace("\n", "<br/>"),
        f"Tél : {org['tel']}<br/>" if org.get("tel") else "",
        f"E-mail : {org['email']}" if org.get("email") else "",
    )
    adresse_client = ""
    if association:
        adresse_client = "<br/>".join(filter(None, [
            association["adresse_association_1"], association["adresse_association_2"],
            " ".join(filter(None, [association["CP"], association["COMMUNE"]])),
        ]))
    bloc_client = f"<b>Facturé à :</b><br/><b>{facture['nom_association']}</b><br/>{adresse_client}"
    infos = Table([[Paragraph(bloc_org, petit), Paragraph(bloc_client, petit)]], colWidths=[90 * mm, None])
    infos.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (1, 0), (1, 0), 0.5, colors.grey),
        ("LEFTPADDING", (1, 0), (1, 0), 6),
    ]))
    elements += [infos, Spacer(1, 5 * mm)]

    cartouche = Table(
        [["N° facture", "Date", "Échéance", "Période"],
         [facture["numero"], date_fr(facture["date_facture"]),
          date_fr(facture["date_echeance"]), periode]],
        colWidths=[40 * mm, 35 * mm, 35 * mm, 45 * mm],
    )
    cartouche.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    elements += [cartouche, Spacer(1, 6 * mm)]

    # Une ligne par BL (montant), détail des recettes livrées dessous.
    donnees = [["Désignation", "Barquette", "Qté", "Portions", "Prix / portion", "Montant"]]
    style = [
        ("GRID", (0, 0), (-1, 0), 0.5, colors.grey),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 1),
    ]
    for b in bons:
        ligne_bl = len(donnees)
        prix = b["prix_portion_carne"]
        donnees.append([
            Paragraph(f"<b>Bon de livraison {b['numero']} — livraison du {date_fr(b['date_livraison'])}</b>", petit),
            "", "", "", "", "",
        ])
        style += [
            ("SPAN", (0, ligne_bl), (-1, ligne_bl)),
            ("BACKGROUND", (0, ligne_bl), (-1, ligne_bl), colors.HexColor("#f7f3ee")),
            ("LINEABOVE", (0, ligne_bl), (-1, ligne_bl), 0.5, colors.grey),
            ("TOPPADDING", (0, ligne_bl), (-1, ligne_bl), 3),
        ]
        for l in lignes_par_bon.get(b["id"], []):
            donnees.append([
                Paragraph(f"{CATEGORIES.get(l['categorie_produit'], '')} — {l['libelle_recette']}"
                          + (f" (DLC {date_fr(l['dlc'])})" if l["dlc"] else ""), detail),
                f"{l['taille']} ({l['nb_portions_barquette']}p)",
                str(l["quantite"]),
                str(l["quantite"] * l["nb_portions_barquette"]),
                "", "",
            ])
        ligne_total = len(donnees)
        donnees.append([
            Paragraph(f"Portions carnées facturées <font size=7>(légumes inclus : {b['portions_legumes'] or 0} portions)</font>", petit),
            "", "", str(b["portions_carne"] or 0),
            f"{euros(prix)} €" if prix is not None else "—",
            f"{euros(b['montant'])} €",
        ])
        style += [
            ("FONTNAME", (3, ligne_total), (-1, ligne_total), "Helvetica-Bold"),
            ("LINEABOVE", (3, ligne_total), (-1, ligne_total), 0.3, colors.grey),
            ("BOTTOMPADDING", (0, ligne_total), (-1, ligne_total), 4),
        ]
    table = Table(donnees, colWidths=[None, 24 * mm, 12 * mm, 18 * mm, 24 * mm, 26 * mm], repeatRows=1)
    table.setStyle(TableStyle(style))
    elements += [table, Spacer(1, 5 * mm)]

    recap = [
        ["Bons de livraison", str(len(bons))],
        ["Portions carnées", str(facture["portions_carne"] or 0)],
        ["Portions légumes (incluses)", str(facture["portions_legumes"] or 0)],
        ["Net à payer", f"{euros(facture['montant'])} €"],
    ]
    t_recap = Table(recap, colWidths=[55 * mm, 32 * mm], hAlign="RIGHT")
    t_recap.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 11),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eeeeee")),
    ]))
    elements += [t_recap, Spacer(1, 8 * mm)]

    mentions = [
        "TVA non applicable, art. 293B du CGI.",
        f"Règlement à réception de facture, par virement en rappelant le n° de facture {facture['numero']}.",
        f"IBAN : {org.get('iban') or ''} — BIC : {org.get('bic') or ''}",
        f"SIRET : {org.get('siret') or org.get('siren') or ''} — NAF : {org.get('naf') or ''}",
    ]
    for m in mentions:
        elements.append(Paragraph(m, petit))

    if facture["statut"] == "annulee":
        elements += [Spacer(1, 6 * mm), Paragraph(
            f"<font color='red' size=14><b>FACTURE ANNULÉE le {date_fr(facture['date_annulation'])}</b></font>",
            styles["Normal"])]

    doc.build(elements)
    buffer.seek(0)
    return buffer


def pdf_facture_vers_fichier(conn, facture_id, chemin):
    """Écrit le PDF d'une facture dans `chemin` (pièce jointe des mails)."""
    facture, bons, lignes_par_bon, association = charger_facture(conn, facture_id)
    campagne = conn.execute("SELECT annee, mois FROM cuisine_factures_campagnes WHERE id = ?",
                            (facture["campagne_id"],)).fetchone()
    pdf = generer_facture_cuisine_pdf(facture, bons, lignes_par_bon, association,
                                      periode_label(campagne["annee"], campagne["mois"]))
    with open(chemin, "wb") as f:
        f.write(pdf.read())


# ------------------------------------------------------------
# 📅 Sélection du mois + campagnes existantes
# ------------------------------------------------------------
@production_cuisine_bp.route("/facturation")
@login_required
@require_access_facturation("lecture")
def facturation_selection():
    aujourd_hui = date.fromisoformat(today_paris())
    # Par défaut : le mois précédent (facturation en début de mois).
    precedent = (aujourd_hui.replace(day=1) - timedelta(days=1))
    with _connect() as conn:
        campagnes = conn.execute(
            """SELECT c.*,
                      COUNT(f.id) FILTER (WHERE f.statut = 'emise') AS nb_factures,
                      COALESCE(SUM(f.montant) FILTER (WHERE f.statut = 'emise'), 0) AS montant_total,
                      COUNT(f.id) FILTER (WHERE f.statut = 'emise' AND f.date_paiement IS NOT NULL) AS nb_payees,
                      COUNT(f.id) FILTER (WHERE f.statut = 'emise' AND f.mail_envoye_le IS NOT NULL AND f.mail_mode_test = 0) AS nb_envoyees
               FROM cuisine_factures_campagnes c
               LEFT JOIN cuisine_factures f ON f.campagne_id = c.id
               GROUP BY c.id ORDER BY c.annee DESC, c.mois DESC"""
        ).fetchall()
        # BL restant à facturer, par mois (tous mois confondus).
        en_attente = conn.execute(
            """SELECT substr(date_livraison, 1, 7) AS mois, COUNT(*) AS nb, COALESCE(SUM(montant), 0) AS montant
               FROM cuisine_bons_livraison WHERE statut = 'valide'
               GROUP BY mois ORDER BY mois DESC"""
        ).fetchall()
        nb_preparation = conn.execute(
            "SELECT COUNT(*) FROM cuisine_bons_livraison WHERE statut = 'preparation'"
        ).fetchone()[0]
    return render_template(
        "production_cuisine/facturation_selection.html",
        campagnes=campagnes, en_attente=en_attente, nb_preparation=nb_preparation,
        mois_defaut=f"{precedent.year:04d}-{precedent.month:02d}",
        periode_label=periode_label, euros=euros,
    )


@production_cuisine_bp.route("/facturation/generer", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_generer():
    try:
        annee, mois = (int(x) for x in (request.form.get("mois") or "").split("-"))
        date(annee, mois, 1)
    except ValueError:
        flash("⚠️ Mois invalide.", "warning")
        return redirect(url_for("production_cuisine.facturation_selection"))

    utilisateur = getattr(current_user, "email", None) or getattr(current_user, "username", None)
    maintenant = now_paris_str()
    date_facture = today_paris()
    echeance = (date.fromisoformat(date_facture) + timedelta(days=DELAI_ECHEANCE_JOURS)).isoformat()

    conn = _connect()
    try:
        # Verrou d'écriture : deux clics / deux postes ne facturent pas deux
        # fois les mêmes BL et ne prennent pas le même numéro.
        conn.execute("BEGIN IMMEDIATE")
        bons = bl_a_facturer(conn, annee, mois)
        campagne = conn.execute(
            "SELECT id FROM cuisine_factures_campagnes WHERE annee = ? AND mois = ?", (annee, mois)
        ).fetchone()
        if not bons:
            conn.rollback()
            flash(f"ℹ️ Aucun bon de livraison validé à facturer pour {periode_label(annee, mois)}.", "warning")
            if campagne:
                return redirect(url_for("production_cuisine.facturation_resultats", campagne_id=campagne["id"]))
            return redirect(url_for("production_cuisine.facturation_selection"))

        if campagne:
            campagne_id = campagne["id"]
        else:
            campagne_id = conn.execute(
                "INSERT INTO cuisine_factures_campagnes (annee, mois, date_creation, cree_par) VALUES (?, ?, ?, ?)",
                (annee, mois, maintenant, utilisateur),
            ).lastrowid

        par_client = {}
        for b in bons:
            par_client.setdefault(b["association_id"], []).append(b)

        creees = []
        for association_id, bons_client in sorted(par_client.items(), key=lambda kv: kv[1][0]["nom_association"].lower()):
            assoc = conn.execute(
                "SELECT nom_association, courriel_resp_tresorerie, courriel_association FROM associations WHERE id = ?",
                (association_id,),
            ).fetchone()
            email = ((assoc["courriel_resp_tresorerie"] or assoc["courriel_association"]) if assoc else None) or None
            numero = _prochain_numero_facture(conn, date_facture[:4])
            facture_id = conn.execute(
                """INSERT INTO cuisine_factures
                   (campagne_id, numero, association_id, nom_association, date_facture, date_echeance,
                    portions_carne, portions_legumes, montant, statut, date_creation, user_creation, email)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'emise', ?, ?, ?)""",
                (campagne_id, numero, association_id, bons_client[0]["nom_association"], date_facture, echeance,
                 sum(b["portions_carne"] or 0 for b in bons_client),
                 sum(b["portions_legumes"] or 0 for b in bons_client),
                 round(sum(b["montant"] or 0 for b in bons_client), 2),
                 maintenant, utilisateur, email),
            ).lastrowid
            # Rien à payer : soldée d'emblée (jamais relancée).
            if round(sum(b["montant"] or 0 for b in bons_client), 2) <= 0:
                conn.execute("UPDATE cuisine_factures SET date_paiement = ? WHERE id = ?", (date_facture, facture_id))
            for b in bons_client:
                conn.execute("INSERT INTO cuisine_factures_bl (facture_id, bon_id) VALUES (?, ?)", (facture_id, b["id"]))
                cur = conn.execute(
                    "UPDATE cuisine_bons_livraison SET statut = 'facture', date_facturation = ? WHERE id = ? AND statut = 'valide'",
                    (maintenant, b["id"]),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"BL {b['numero']} modifié entre-temps")
            creees.append(numero)
        conn.commit()
    except Exception as e:
        conn.rollback()
        write_log(f"❌ Erreur génération factures cuisine {annee}-{mois:02d} : {e}")
        flash("❌ Erreur lors de la génération des factures — rien n'a été enregistré.", "danger")
        return redirect(url_for("production_cuisine.facturation_selection"))
    finally:
        conn.close()

    upload_database()
    flash(f"✅ {len(creees)} facture(s) créée(s) pour {periode_label(annee, mois)} ({len(bons)} BL) : "
          f"{creees[0]}{' → ' + creees[-1] if len(creees) > 1 else ''}.", "success")
    return redirect(url_for("production_cuisine.facturation_resultats", campagne_id=campagne_id))


# ------------------------------------------------------------
# 📊 Écran résultats d'un mois
# ------------------------------------------------------------
@production_cuisine_bp.route("/facturation/<int:campagne_id>")
@login_required
@require_access_facturation("lecture")
def facturation_resultats(campagne_id):
    with _connect() as conn:
        campagne = conn.execute("SELECT * FROM cuisine_factures_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        if not campagne:
            flash("❌ Mois de facturation introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        factures = conn.execute(
            """SELECT f.*, (SELECT COUNT(*) FROM cuisine_factures_bl fb WHERE fb.facture_id = f.id) AS nb_bl,
                      (SELECT GROUP_CONCAT(b.numero, ', ') FROM cuisine_factures_bl fb
                         JOIN cuisine_bons_livraison b ON b.id = fb.bon_id WHERE fb.facture_id = f.id) AS numeros_bl,
                      (SELECT COALESCE(SUM(r.montant), 0) FROM cuisine_factures_reglements r WHERE r.facture_id = f.id) AS total_regle
               FROM cuisine_factures f WHERE f.campagne_id = ?
               ORDER BY f.statut = 'annulee', f.numero""",
            (campagne_id,),
        ).fetchall()
        reglements = {}
        for r in conn.execute(
            """SELECT r.* FROM cuisine_factures_reglements r JOIN cuisine_factures f ON f.id = r.facture_id
               WHERE f.campagne_id = ? ORDER BY r.date_reglement, r.id""",
            (campagne_id,),
        ).fetchall():
            reglements.setdefault(r["facture_id"], []).append(r)
        modeles = conn.execute(
            """SELECT * FROM modeles_emails WHERE type_periode = 'facture_cuisine' OR code_modele = ?
               ORDER BY TRIM(code_modele) COLLATE NOCASE""",
            (CODE_MODELE_ENVOI,),
        ).fetchall()
        restants = bl_a_facturer(conn, campagne["annee"], campagne["mois"])

    emises = [f for f in factures if f["statut"] == "emise"]
    return render_template(
        "production_cuisine/facturation_resultats.html",
        campagne=campagne, factures=factures, modeles=modeles, restants=restants,
        periode=periode_label(campagne["annee"], campagne["mois"]),
        montant_total=sum(f["montant"] or 0 for f in emises),
        montant_paye=sum(f["total_regle"] or 0 for f in emises),
        reglements=reglements, modes_reglement=MODES_REGLEMENT, today=today_paris(),
        reste_a_envoyer=any(f["email"] and (not f["mail_envoye_le"] or f["mail_mode_test"] or f["mail_erreur"])
                            for f in emises),
        mail_mode=mail_mode_courant(),
        mail_test_to=os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org"),
        euros=euros, date_fr=date_fr,
    )


@production_cuisine_bp.route("/facturation/facture/<int:facture_id>/pdf")
@login_required
@require_access_facturation("lecture")
def facturation_pdf(facture_id):
    with _connect() as conn:
        facture, bons, lignes_par_bon, association = charger_facture(conn, facture_id)
        if not facture:
            flash("❌ Facture introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        campagne = conn.execute("SELECT annee, mois FROM cuisine_factures_campagnes WHERE id = ?",
                                (facture["campagne_id"],)).fetchone()
    try:
        pdf = generer_facture_cuisine_pdf(facture, bons, lignes_par_bon, association,
                                          periode_label(campagne["annee"], campagne["mois"]))
    except Exception as e:
        write_log(f"❌ Erreur PDF facture cuisine {facture_id} : {e}")
        flash("❌ Erreur lors de la génération du PDF.", "danger")
        return redirect(url_for("production_cuisine.facturation_resultats", campagne_id=facture["campagne_id"]))
    return send_file(pdf, mimetype="application/pdf", download_name=f"{facture['numero']}.pdf")


@production_cuisine_bp.route("/facturation/facture/<int:facture_id>/annuler", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_annuler(facture_id):
    """Annule une facture pas encore réellement envoyée : elle garde son
    numéro (séquence sans trou), ses BL redeviennent facturables."""
    motif = (request.form.get("motif") or "").strip() or None
    utilisateur = getattr(current_user, "email", None) or getattr(current_user, "username", None)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        f = conn.execute("SELECT * FROM cuisine_factures WHERE id = ?", (facture_id,)).fetchone()
        if not f:
            conn.rollback()
            flash("❌ Facture introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        retour = redirect(url_for("production_cuisine.facturation_resultats", campagne_id=f["campagne_id"]))
        if f["statut"] != "emise":
            conn.rollback()
            flash(f"⛔ La facture {f['numero']} est déjà annulée.", "warning")
            return retour
        if f["mail_envoye_le"] and not f["mail_mode_test"] and not f["mail_erreur"]:
            conn.rollback()
            flash(f"⛔ La facture {f['numero']} a déjà été envoyée au client : annulation impossible ici "
                  "(il faudrait un avoir).", "danger")
            return retour
        if conn.execute("SELECT 1 FROM cuisine_factures_reglements WHERE facture_id = ?", (facture_id,)).fetchone():
            conn.rollback()
            flash(f"⛔ Un règlement est saisi sur la facture {f['numero']} : supprimez-le avant d'annuler.", "danger")
            return retour
        conn.execute(
            """UPDATE cuisine_factures SET statut = 'annulee', date_annulation = ?, user_annulation = ?, motif_annulation = ?
               WHERE id = ?""",
            (now_paris_str(), utilisateur, motif, facture_id),
        )
        conn.execute(
            """UPDATE cuisine_bons_livraison SET statut = 'valide', date_facturation = NULL
               WHERE statut = 'facture' AND id IN (SELECT bon_id FROM cuisine_factures_bl WHERE facture_id = ?)""",
            (facture_id,),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        write_log(f"❌ Erreur annulation facture cuisine {facture_id} : {e}")
        flash("❌ Erreur lors de l'annulation — rien n'a été modifié.", "danger")
        return redirect(url_for("production_cuisine.facturation_selection"))
    finally:
        conn.close()
    upload_database()
    flash(f"↩️ Facture {f['numero']} annulée — ses bons de livraison sont de nouveau facturables.", "success")
    return retour


def recalculer_solde(conn, facture_id):
    """Facture soldée (date_paiement = date du dernier règlement) quand le
    total des règlements atteint son montant ; sinon date_paiement NULL
    (elle reste relançable)."""
    f = conn.execute("SELECT montant FROM cuisine_factures WHERE id = ?", (facture_id,)).fetchone()
    total, derniere = conn.execute(
        "SELECT COALESCE(SUM(montant), 0), MAX(date_reglement) FROM cuisine_factures_reglements WHERE facture_id = ?",
        (facture_id,),
    ).fetchone()
    montant = round(f["montant"] or 0, 2)
    if montant <= 0:
        return  # facture à 0 € : soldée dès sa création
    solde = derniere if round(total, 2) >= montant else None
    conn.execute("UPDATE cuisine_factures SET date_paiement = ? WHERE id = ?", (solde, facture_id))


def _montant_saisi(valeur):
    try:
        return round(float((valeur or "").replace(",", ".").replace(" ", "").replace("\u202f", "")), 2)
    except ValueError:
        return None


@production_cuisine_bp.route("/facturation/facture/<int:facture_id>/reglement", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_ajouter_reglement(facture_id):
    montant = _montant_saisi(request.form.get("montant"))
    date_reglement = (request.form.get("date_reglement") or "").strip()
    mode = request.form.get("mode") if request.form.get("mode") in MODES_REGLEMENT else None
    reference = (request.form.get("reference") or "").strip() or None
    commentaire = (request.form.get("commentaire") or "").strip() or None
    utilisateur = getattr(current_user, "email", None) or getattr(current_user, "username", None)
    with _connect() as conn:
        f = conn.execute("SELECT * FROM cuisine_factures WHERE id = ?", (facture_id,)).fetchone()
        if not f:
            flash("❌ Facture introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        retour = redirect(url_for("production_cuisine.facturation_resultats", campagne_id=f["campagne_id"]))
        if f["statut"] != "emise":
            flash(f"⛔ La facture {f['numero']} est annulée.", "danger")
            return retour
        try:
            date.fromisoformat(date_reglement)
        except ValueError:
            flash("⚠️ Date de règlement invalide.", "warning")
            return retour
        if date_reglement > today_paris():
            flash("⚠️ La date de règlement ne peut pas être dans le futur.", "warning")
            return retour
        deja = conn.execute("SELECT COALESCE(SUM(montant), 0) FROM cuisine_factures_reglements WHERE facture_id = ?",
                            (facture_id,)).fetchone()[0]
        reste = round((f["montant"] or 0) - deja, 2)
        if montant is None or montant <= 0:
            flash("⚠️ Montant du règlement invalide.", "warning")
            return retour
        if montant > reste:
            flash(f"⛔ Montant {euros(montant)} € supérieur au reste dû de {f['numero']} ({euros(reste)} €).", "danger")
            return retour
        conn.execute(
            """INSERT INTO cuisine_factures_reglements
               (facture_id, date_reglement, montant, mode, reference, commentaire, date_saisie, user_saisie)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (facture_id, date_reglement, montant, mode, reference, commentaire, now_paris_str(), utilisateur),
        )
        recalculer_solde(conn, facture_id)
        conn.commit()
    upload_database()
    reste -= montant
    flash(f"💳 Règlement de {euros(montant)} € enregistré sur {f['numero']} — "
          + ("facture soldée ✅" if reste <= 0.005 else f"reste dû {euros(reste)} €"), "success")
    return retour


@production_cuisine_bp.route("/facturation/reglement/<int:reglement_id>/supprimer", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_supprimer_reglement(reglement_id):
    with _connect() as conn:
        r = conn.execute(
            """SELECT r.*, f.numero, f.campagne_id FROM cuisine_factures_reglements r
               JOIN cuisine_factures f ON f.id = r.facture_id WHERE r.id = ?""",
            (reglement_id,),
        ).fetchone()
        if not r:
            flash("❌ Règlement introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        conn.execute("DELETE FROM cuisine_factures_reglements WHERE id = ?", (reglement_id,))
        recalculer_solde(conn, r["facture_id"])
        conn.commit()
    upload_database()
    write_log(f"🗑️ Règlement supprimé sur facture cuisine {r['numero']} : {r['montant']} € du {r['date_reglement']}")
    flash(f"🗑️ Règlement de {euros(r['montant'])} € du {date_fr(r['date_reglement'])} supprimé de {r['numero']}.", "success")
    return redirect(url_for("production_cuisine.facturation_resultats", campagne_id=r["campagne_id"]))


@production_cuisine_bp.route("/facturation/toggle_mode", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_toggle_mode():
    """Même bascule TEST/PROD (session MAIL_MODE) que la trésorerie."""
    if mail_mode_courant() == "PROD":
        session["MAIL_MODE"] = "TEST"
        flash("🧪 Mode TEST activé (mails redirigés)", "warning")
    else:
        session["MAIL_MODE"] = "PROD"
        flash("✅ Mode PROD réactivé", "success")
    return redirect(request.referrer or url_for("production_cuisine.facturation_selection"))


# ------------------------------------------------------------
# 📧 Envoi des factures (arrière-plan)
# ------------------------------------------------------------
def envoyer_factures_cuisine_background(app, db_path, campagne_id, items, mail_mode, mail_test_to, current_user_email):
    with app.app_context():
        envoyes, nb_erreurs, count_test = 0, 0, 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        for item in items:
            sujet_envoi = item["sujet"]
            if mail_mode == "TEST":
                if count_test >= 2:
                    break
                count_test += 1
                destinataires = [mail_test_to]
                sujet_envoi = f"🧪 [TEST] {sujet_envoi}"
            else:
                destinataires = split_emails(item["email"])
            pdf_path = f"/tmp/facture_cuisine_{item['facture_id']}.pdf"
            try:
                if not destinataires:
                    raise ValueError(f"Aucune adresse email valide pour {item['nom_association']}")
                pdf_facture_vers_fichier(conn, item["facture_id"], pdf_path)
                write_log(f"📧 Envoi facture cuisine {item['numero']} à {', '.join(destinataires)}")
                resultat = envoyer_mail(
                    sujet=sujet_envoi, destinataires=destinataires, texte=item["corps"],
                    sender_override=MAIL_SENDER, attachment_path=pdf_path, bcc=[MAIL_SENDER],
                    current_user_email=current_user_email,
                )
                mj_status, mj_ids = None, None
                if resultat and resultat.get("Messages"):
                    mj_message = resultat["Messages"][0]
                    mj_status = mj_message.get("Status")
                    mj_ids = ",".join(str(t["MessageID"]) for t in mj_message.get("To", []) if "MessageID" in t) or None
                conn.execute(
                    """UPDATE cuisine_factures
                       SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = NULL,
                           mail_mailjet_status = ?, mail_mailjet_message_ids = ?,
                           mail_statut_final = NULL, mail_statut_verifie_le = NULL,
                           mail_modele_id = ?, sujet = ?, corps = ?
                       WHERE id = ?""",
                    (now_iso, 1 if mail_mode == "TEST" else 0, mj_status, mj_ids,
                     item["modele_id"], sujet_envoi, item["corps"], item["facture_id"]),
                )
                conn.commit()
                envoyes += 1
            except Exception as e:
                write_log(f"❌ Erreur envoi facture cuisine {item['numero']} : {e}")
                nb_erreurs += 1
                conn.execute(
                    """UPDATE cuisine_factures SET mail_envoye_le = ?, mail_mode_test = ?, mail_erreur = ?,
                              mail_modele_id = ?, sujet = ?, corps = ? WHERE id = ?""",
                    (now_iso, 1 if mail_mode == "TEST" else 0, str(e), item["modele_id"],
                     sujet_envoi, item["corps"], item["facture_id"]),
                )
                conn.commit()
            finally:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)

        conn.execute(
            """UPDATE cuisine_factures_campagnes
               SET dernier_envoi_le = ?, dernier_envoi_par = ?, dernier_envoi_mode_test = ?,
                   dernier_envoi_nb_ok = ?, dernier_envoi_nb_erreur = ?
               WHERE id = ?""",
            (now_iso, current_user_email, 1 if mail_mode == "TEST" else 0, envoyes, nb_erreurs, campagne_id),
        )
        conn.commit()
        conn.close()
        upload_database()
        write_log(f"📤 Envoi factures cuisine (arrière-plan) terminé : {envoyes} envoyée(s), {nb_erreurs} erreur(s)")


def contexte_mail(facture, campagne, nb_bl):
    return {
        "nom_association": facture["nom_association"],
        "numero": facture["numero"],
        "periode": periode_label(campagne["annee"], campagne["mois"]),
        "montant": euros(facture["montant"]),
        "echeance": date_fr(facture["date_echeance"]),
        "nb_bl": nb_bl,
    }


@production_cuisine_bp.route("/facturation/<int:campagne_id>/envoyer", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_envoyer(campagne_id):
    retour = redirect(url_for("production_cuisine.facturation_resultats", campagne_id=campagne_id))
    modele_id = request.form.get("modele_id")
    ids_choisis = {int(x) for x in request.form.getlist("facture_ids") if x.isdigit()}
    with _connect() as conn:
        campagne = conn.execute("SELECT * FROM cuisine_factures_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        modele = conn.execute("SELECT * FROM modeles_emails WHERE id = ?", (modele_id,)).fetchone() if modele_id else None
        if not campagne or not modele:
            flash("❌ Mois ou modèle de mail introuvable.", "danger")
            return retour
        a_envoyer = conn.execute(
            """SELECT f.*, (SELECT COUNT(*) FROM cuisine_factures_bl fb WHERE fb.facture_id = f.id) AS nb_bl
               FROM cuisine_factures f
               WHERE f.campagne_id = ? AND f.statut = 'emise' AND f.email IS NOT NULL AND f.email != ''
                 AND (f.mail_envoye_le IS NULL OR f.mail_mode_test = 1 OR f.mail_erreur IS NOT NULL)
               ORDER BY f.numero""",
            (campagne_id,),
        ).fetchall()
    if not ids_choisis:
        flash("⚠️ Cochez au moins une facture à envoyer.", "warning")
        return retour
    a_envoyer = [f for f in a_envoyer if f["id"] in ids_choisis]
    if not a_envoyer:
        flash("ℹ️ Rien à envoyer : toutes les factures de ce mois ont déjà été envoyées (ou n'ont pas d'email).", "warning")
        return retour

    items = []
    for f in a_envoyer:
        ctx = contexte_mail(f, campagne, f["nb_bl"])
        items.append({
            "facture_id": f["id"], "numero": f["numero"], "email": f["email"], "nom_association": f["nom_association"],
            "sujet": render_modele_email(modele["sujet"], ctx).strip(),
            "corps": render_modele_email(modele["corps"], ctx),
            "modele_id": modele["id"],
        })
    mail_mode = mail_mode_courant()
    mail_test_to = os.getenv("MAIL_TEST_TO", "ba380.informatique2@banquealimentaire.org")
    current_user_email = current_user.email
    lancer_tache_fond(
        target=envoyer_factures_cuisine_background,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, items, mail_mode, mail_test_to,
              current_user_email),
        nom="Envoi factures cuisine",
        utilisateur=current_user_email,
    )
    if mail_mode == "TEST":
        flash("🧪 Envoi TEST lancé en arrière-plan (2 mails max vers l'adresse de test).", "warning")
    else:
        flash(f"🚀 Envoi réel lancé en arrière-plan pour {len(items)} facture(s) — actualisez la page dans quelques instants.", "info")
    return retour


@production_cuisine_bp.route("/facturation/<int:campagne_id>/verifier_statut_mailjet", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_verifier_statut_mailjet(campagne_id):
    counts, verifies = {}, 0
    with _connect() as conn:
        lignes = conn.execute(
            """SELECT id, mail_mailjet_message_ids FROM cuisine_factures
               WHERE campagne_id = ? AND mail_mailjet_message_ids IS NOT NULL AND mail_mailjet_message_ids != ''""",
            (campagne_id,),
        ).fetchall()
        for ligne in lignes:
            statut = mailjet_get_message_status(ligne["mail_mailjet_message_ids"].split(",")[0])
            if not statut:
                continue
            verifies += 1
            counts[statut] = counts.get(statut, 0) + 1
            conn.execute(
                "UPDATE cuisine_factures SET mail_statut_final = ?, mail_statut_verifie_le = ? WHERE id = ?",
                (statut, datetime.now().isoformat(timespec="seconds"), ligne["id"]),
            )
        conn.commit()
    if verifies:
        upload_database()
        flash(f"🔄 Statut Mailjet vérifié pour {verifies} mail(s) : "
              + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())), "info")
    else:
        flash("ℹ️ Aucun mail avec un identifiant Mailjet à vérifier pour ce mois.", "warning")
    return redirect(url_for("production_cuisine.facturation_resultats", campagne_id=campagne_id))


@production_cuisine_bp.route("/facturation/facture/<int:facture_id>/renvoyer_gmail", methods=["POST"])
@login_required
@require_access_facturation("ecriture")
def facturation_renvoyer_gmail(facture_id):
    from ba38_utilitaires.gmail_send import envoyer_mail_gmail, GmailSendError

    with _connect() as conn:
        f = conn.execute("SELECT * FROM cuisine_factures WHERE id = ?", (facture_id,)).fetchone()
        if not f:
            flash("❌ Facture introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        retour = redirect(url_for("production_cuisine.facturation_resultats", campagne_id=f["campagne_id"]))
        destinataires = split_emails(f["email"])
        if not destinataires:
            flash(f"❌ Aucune adresse email valide pour {f['nom_association']}.", "danger")
            return retour
        if not f["sujet"] or not f["corps"]:
            flash(f"⛔ Aucun envoi précédent connu pour {f['nom_association']} — utilisez d'abord « Envoyer ».", "danger")
            return retour
        if f["mail_mode_test"]:
            flash(f"⛔ Le dernier envoi pour {f['nom_association']} était en Mode TEST — un renvoi Gmail partirait, "
                  "lui, pour de vrai. Refaites d'abord un envoi réel.", "danger")
            return retour
        pdf_path = f"/tmp/facture_cuisine_gmail_{facture_id}.pdf"
        pdf_facture_vers_fichier(conn, facture_id, pdf_path)
    try:
        envoyer_mail_gmail(sujet=f["sujet"], destinataires=destinataires, texte=f["corps"], attachment_path=pdf_path)
        with _connect() as conn:
            conn.execute("UPDATE cuisine_factures SET mail_renvoi_gmail_le = ? WHERE id = ?",
                         (datetime.now().isoformat(timespec="seconds"), facture_id))
            conn.commit()
        upload_database()
        flash(f"📧 Facture {f['numero']} renvoyée via Gmail à {f['email']}.", "success")
    except GmailSendError as e:
        write_log(f"❌ Erreur renvoi Gmail facture cuisine {f['numero']} : {e}")
        flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
    return retour


@production_cuisine_bp.route("/facturation/<int:campagne_id>/export_excel")
@login_required
@require_access_facturation("lecture")
def facturation_export_excel(campagne_id):
    import pandas as pd

    with _connect() as conn:
        campagne = conn.execute("SELECT * FROM cuisine_factures_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        if not campagne:
            flash("❌ Mois de facturation introuvable.", "danger")
            return redirect(url_for("production_cuisine.facturation_selection"))
        df = pd.read_sql_query(
            """SELECT f.numero AS "N° facture", f.nom_association AS "Client", f.email AS "Email",
                      f.date_facture AS "Date", f.date_echeance AS "Échéance",
                      (SELECT GROUP_CONCAT(b.numero, ', ') FROM cuisine_factures_bl fb
                         JOIN cuisine_bons_livraison b ON b.id = fb.bon_id WHERE fb.facture_id = f.id) AS "BL",
                      f.portions_carne AS "Portions carnées", f.portions_legumes AS "Portions légumes",
                      f.montant AS "Montant",
                      (SELECT COALESCE(SUM(r.montant), 0) FROM cuisine_factures_reglements r WHERE r.facture_id = f.id) AS "Réglé",
                      CASE WHEN f.statut = 'annulee' THEN 0 ELSE ROUND(f.montant - (SELECT COALESCE(SUM(r.montant), 0)
                           FROM cuisine_factures_reglements r WHERE r.facture_id = f.id), 2) END AS "Reste dû",
                      CASE WHEN f.statut = 'annulee' THEN 'Annulée'
                           WHEN f.date_paiement IS NOT NULL THEN 'Soldée'
                           WHEN EXISTS (SELECT 1 FROM cuisine_factures_reglements r WHERE r.facture_id = f.id) THEN 'Partiel'
                           ELSE 'Impayée' END AS "Statut",
                      f.date_paiement AS "Date solde",
                      (SELECT GROUP_CONCAT(r.date_reglement || ' ' || r.montant || ' € ' || COALESCE(r.mode, '')
                              || COALESCE(' ' || r.reference, ''), ' | ')
                         FROM cuisine_factures_reglements r WHERE r.facture_id = f.id) AS "Règlements", f.mail_envoye_le AS "Mail envoyé le",
                      f.mail_statut_final AS "Statut Mailjet", f.relance_niveau AS "Niveau de relance"
               FROM cuisine_factures f WHERE f.campagne_id = ? ORDER BY f.numero""",
            conn, params=(campagne_id,),
        )
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)
    return send_file(
        output, as_attachment=True,
        download_name=f"factures_cuisine_{campagne['annee']}_{campagne['mois']:02d}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
