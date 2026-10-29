# ============================================================
# 📤 Envoi à la BAI des réceptions livrées directement à la cuisine
#     Fournisseurs concernés : case « Livraison Directe Cuisine » de la
#     fiche fournisseur (fournisseurs.livraison_directe_cuisine = 'oui').
#     Un envoi = les réceptions d'UN jour (celui affiché sur la page
#     Réceptions, aujourd'hui par défaut) pas encore envoyées.
#     Chaque réception n'est transmise qu'une fois : envoi_bai_le /
#     envoi_bai_par sur cuisine_receptions (même horodatage pour toutes
#     les réceptions d'un envoi = identifiant du lot, sert à l'historique).
#     Destinataires : paramètre cuisine_mail_envoi_bai (Paramètres cuisine).
# ============================================================

import io
import os
import sqlite3
import tempfile
from pathlib import Path

from flask import current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from ba38_utilitaires.core import envoyer_mail, require_access, split_emails, upload_database, write_log
from ba38_utilitaires.organisation import get_organisation
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import _connect, now_paris_str, today_paris

PARAM_DESTINATAIRES_BAI = "cuisine_mail_envoi_bai"

# Code VIF : celui du produit dans le référentiel ingrédients carnés (vide
# pour un produit saisi hors référentiel ou sans code renseigné).
SQL_RECEPTIONS_DIRECTES = """
    SELECT r.*, f.nom AS fournisseur_nom, ic.code_vif
    FROM cuisine_receptions r
    JOIN fournisseurs f ON f.id = r.fournisseur_id
    LEFT JOIN cuisine_ingredients_carnes ic
           ON ic.groupe = r.ingredient_groupe AND ic.produit = r.ingredient_produit
    WHERE r.actif = 1 AND f.livraison_directe_cuisine = 'oui'
"""
ORDRE = " ORDER BY r.date_reception, r.heure_arrivee, f.nom COLLATE NOCASE, r.id"


def destinataires_bai(conn):
    """Adresses du paramètre (une par valeur, « ; » toléré dans une valeur)."""
    adresses = []
    for (valeur,) in conn.execute(
        "SELECT param_value FROM parametres WHERE param_name = ? ORDER BY id", (PARAM_DESTINATAIRES_BAI,)
    ).fetchall():
        adresses += [a for a in split_emails(valeur) if a not in adresses]
    return adresses


def receptions_a_envoyer(conn, jour):
    return conn.execute(
        SQL_RECEPTIONS_DIRECTES + " AND r.envoi_bai_le IS NULL AND r.date_reception = ?" + ORDRE, (jour,)
    ).fetchall()


def nb_receptions_a_envoyer(conn, jour):
    return conn.execute(
        """SELECT COUNT(*) FROM cuisine_receptions r JOIN fournisseurs f ON f.id = r.fournisseur_id
           WHERE r.actif = 1 AND f.livraison_directe_cuisine = 'oui' AND r.envoi_bai_le IS NULL
             AND r.date_reception = ?""",
        (jour,),
    ).fetchone()[0]


def _jour():
    jour = (request.values.get("date") or "").strip()
    return jour if len(jour) == 10 and jour[4] == "-" and jour[7] == "-" else today_paris()


def _ids_choisis(conn, jour):
    """Réceptions cochées, limitées à celles du jour encore à envoyer (un
    double clic ou un onglet resté ouvert ne renvoie rien deux fois)."""
    ids = {int(v) for v in request.values.getlist("reception_id") if v.isdigit()}
    return [r for r in receptions_a_envoyer(conn, jour) if r["id"] in ids]


def _date_fr(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso and len(iso) >= 10 else ""


def _periode(receptions):
    dates = sorted(r["date_reception"] for r in receptions)
    if not dates:
        return ""
    if dates[0] == dates[-1]:
        return f"du {_date_fr(dates[0])}"
    return f"du {_date_fr(dates[0])} au {_date_fr(dates[-1])}"


def _totaux_par_fournisseur(receptions):
    totaux = {}
    for r in receptions:
        nb, poids = totaux.get(r["fournisseur_nom"], (0, 0.0))
        totaux[r["fournisseur_nom"]] = (nb + 1, poids + (r["poids_kg"] or 0))
    return dict(sorted(totaux.items(), key=lambda t: t[0].casefold()))


def _totaux_par_code_vif(receptions):
    totaux = {}
    for r in receptions:
        nb, poids = totaux.get(r["code_vif"] or "", (0, 0.0))
        totaux[r["code_vif"] or ""] = (nb + 1, poids + (r["poids_kg"] or 0))
    return dict(sorted(totaux.items(), key=lambda t: _cle_code_vif(t[0])))


def _cle_code_vif(code):
    """Tri : codes VIF croissants, « sans code » en dernier."""
    return (not code, code or "")


def _par_code_vif(receptions_fournisseur):
    """{code VIF ou "": [réceptions]} pour un fournisseur, codes triés."""
    groupes = {}
    for r in receptions_fournisseur:
        groupes.setdefault(r["code_vif"] or "", []).append(r)
    return dict(sorted(groupes.items(), key=lambda t: _cle_code_vif(t[0])))


def _kg(valeur):
    return f"{valeur:.3f}".rstrip("0").rstrip(".").replace(".", ",") if valeur is not None else ""


def generer_pdf_envoi_bai(receptions, envoye_le=None):
    """Bordereau A4 portrait : une ligne par produit réceptionné, groupé par
    fournisseur puis par code VIF (sous-total par code, total fournisseur)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    org = get_organisation()
    styles = getSampleStyleSheet()
    petit = styles["Normal"].clone("petit", fontSize=8, leading=10)
    titre_section = styles["Normal"].clone("section", fontSize=10.5, leading=13, fontName="Helvetica-Bold")
    edite_le = envoye_le or now_paris_str()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=14 * mm,
        title="Réceptions cuisine livrées en direct",
    )

    def pied(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(12 * mm, 8 * mm, f"Réceptions cuisine livrées en direct - édité le {_date_fr(edite_le)} {edite_le[11:16]}")
        canvas.drawRightString(A4[0] - 12 * mm, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    elements = []
    logo = None
    logo_path = Path(current_app.root_path) / (org.get("logo_path") or "")
    if org.get("logo_path") and logo_path.exists():
        logo = Image(str(logo_path), width=20 * mm, height=20 * mm, kind="proportional")
    titre = Paragraph(
        "<font size=15><b>RÉCEPTIONS CUISINE LIVRÉES EN DIRECT</b></font><br/>"
        f"<font size=10>{escape(org['nom'])} - réceptions {_periode(receptions)}</font><br/>"
        f"<font size=9>{len(receptions)} réception{'s' if len(receptions) > 1 else ''}"
        f"{' - transmises le ' + _date_fr(edite_le) + ' ' + edite_le[11:16] if envoye_le else ''}</font>",
        styles["Normal"],
    )
    entete = Table([[logo or "", titre]], colWidths=[26 * mm, None])
    entete.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elements += [entete, Spacer(1, 5 * mm)]

    style_tableau = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (7, 1), (8, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e6e6e6")),
    ]
    largeurs = [17 * mm, 11 * mm, 17 * mm, None, 23 * mm, 17 * mm, 17 * mm, 16 * mm, 12 * mm]
    totaux = _totaux_par_fournisseur(receptions)
    for fournisseur, (nb, poids_total) in totaux.items():
        lignes = [["Date", "Heure", "Code VIF", "Produit", "N° lot", "DLC", "DDM", "Poids (kg)", "T°"]]
        styles_sous_totaux = []
        for code, lignes_code in _par_code_vif([x for x in receptions if x["fournisseur_nom"] == fournisseur]).items():
            for r in lignes_code:
                produit = r["ingredient_produit"] or r["libelle_produit"] or "-"
                if r["ingredient_groupe"]:
                    produit = f"{r['ingredient_groupe']} - {produit}"
                lignes.append([
                    _date_fr(r["date_reception"]), (r["heure_arrivee"] or "")[:5], r["code_vif"] or "",
                    Paragraph(escape(produit), petit), Paragraph(escape(r["numero_lot"] or ""), petit),
                    _date_fr(r["dlc_ddm"]), _date_fr(r["ddm"]), _kg(r["poids_kg"]),
                    "" if r["temperature_mesuree"] is None else f"{r['temperature_mesuree']:g} °C",
                ])
            # Sous-total du code VIF dans ce fournisseur
            n = len(lignes)
            lignes.append(["", "", code or "Sans code", f"Sous-total code VIF {code or '(sans code)'}", "", "", "",
                           _kg(sum(r["poids_kg"] or 0 for r in lignes_code)), ""])
            styles_sous_totaux += [
                ("FONTNAME", (0, n), (-1, n), "Helvetica-BoldOblique"),
                ("BACKGROUND", (0, n), (-1, n), colors.HexColor("#f7f7f7")),
            ]
        lignes.append(["", "", "", f"Total {fournisseur}", "", "", "", _kg(poids_total), ""])
        t = Table(lignes, colWidths=largeurs, repeatRows=1)
        t.setStyle(TableStyle(style_tableau + styles_sous_totaux))
        elements += [Paragraph(f"{escape(fournisseur)} ({nb} réception{'s' if nb > 1 else ''})", titre_section),
                     Spacer(1, 1.5 * mm), t, Spacer(1, 5 * mm)]

    recap = [["Fournisseur", "Réceptions", "Poids (kg)"]]
    recap += [[f, str(nb), _kg(p)] for f, (nb, p) in totaux.items()]
    recap.append(["Total", str(len(receptions)), _kg(sum(p for _, p in totaux.values()))])
    t = Table(recap, colWidths=[70 * mm, 25 * mm, 30 * mm], hAlign="RIGHT")
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]))
    elements += [KeepTogether([Paragraph("Récapitulatif par fournisseur", titre_section), Spacer(1, 1.5 * mm), t]), Spacer(1, 5 * mm)]

    par_code = [["Code VIF", "Réceptions", "Poids (kg)"]]
    par_code += [[c or "Sans code VIF", str(nb), _kg(p)] for c, (nb, p) in _totaux_par_code_vif(receptions).items()]
    t = Table(par_code, colWidths=[70 * mm, 25 * mm, 30 * mm], hAlign="RIGHT")
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]))
    elements.append(KeepTogether([Paragraph("Récapitulatif par code VIF", titre_section), Spacer(1, 1.5 * mm), t]))

    doc.build(elements, onFirstPage=pied, onLaterPages=pied)
    buffer.seek(0)
    return buffer


def _historique(conn, limite=15):
    return conn.execute(
        """SELECT r.envoi_bai_le, MAX(r.envoi_bai_par) AS envoi_bai_par, COUNT(*) AS nb,
                  SUM(r.poids_kg) AS poids, MIN(r.date_reception) AS du, MAX(r.date_reception) AS au
           FROM cuisine_receptions r
           WHERE r.envoi_bai_le IS NOT NULL
           GROUP BY r.envoi_bai_le ORDER BY r.envoi_bai_le DESC LIMIT ?""",
        (limite,),
    ).fetchall()


@production_cuisine_bp.route("/receptions/envoi-bai")
@login_required
@require_access("production_cuisine", "ecriture")
def envoi_receptions_bai():
    jour = _jour()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        receptions = receptions_a_envoyer(conn, jour)
        destinataires = destinataires_bai(conn)
        fournisseurs = [r["nom"] for r in conn.execute(
            "SELECT nom FROM fournisseurs WHERE livraison_directe_cuisine = 'oui' ORDER BY nom COLLATE NOCASE"
        ).fetchall()]
        historique = _historique(conn)
    return render_template(
        "production_cuisine/receptions_envoi_bai.html",
        receptions=receptions, destinataires=destinataires, fournisseurs=fournisseurs,
        historique=historique, totaux=_totaux_par_fournisseur(receptions), kg=_kg, jour=jour,
    )


@production_cuisine_bp.route("/receptions/envoi-bai/apercu.pdf")
@login_required
@require_access("production_cuisine", "ecriture")
def apercu_envoi_bai_pdf():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        receptions = _ids_choisis(conn, _jour())
    if not receptions:
        flash("⚠️ Aucune réception cochée.", "warning")
        return redirect(url_for("production_cuisine.envoi_receptions_bai", date=_jour()))
    return send_file(generer_pdf_envoi_bai(receptions), mimetype="application/pdf",
                     download_name="receptions_cuisine_apercu.pdf")


@production_cuisine_bp.route("/receptions/envoi-bai/<envoi_le>.pdf")
@login_required
@require_access("production_cuisine", "lecture")
def pdf_envoi_bai(envoi_le):
    """Bordereau d'un envoi passé (toutes les réceptions de cet horodatage)."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        receptions = conn.execute(
            SQL_RECEPTIONS_DIRECTES.replace(" AND f.livraison_directe_cuisine = 'oui'", "")
            + " AND r.envoi_bai_le = ?" + ORDRE,
            (envoi_le,),
        ).fetchall()
    if not receptions:
        flash("⛔ Envoi introuvable.", "danger")
        return redirect(url_for("production_cuisine.envoi_receptions_bai"))
    return send_file(generer_pdf_envoi_bai(receptions, envoye_le=envoi_le), mimetype="application/pdf",
                     download_name=f"receptions_cuisine_{envoi_le[:10]}.pdf")


@production_cuisine_bp.route("/receptions/envoi-bai", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def envoyer_receptions_bai():
    jour = _jour()
    retour = url_for("production_cuisine.envoi_receptions_bai", date=jour)
    utilisateur = getattr(current_user, "email", None)
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        receptions = _ids_choisis(conn, jour)
        if not receptions:
            flash("⚠️ Aucune réception cochée (ou déjà transmise entre-temps).", "warning")
            return redirect(retour)
        ids = [r["id"] for r in receptions]
        horodatage = now_paris_str()

        destinataires = destinataires_bai(conn)
        if not destinataires:
            flash("⚠️ Aucun destinataire : renseignez l'adresse de la BAI dans ⚙️ Paramètres cuisine.", "warning")
            return redirect(retour)
        totaux = _totaux_par_fournisseur(receptions)
        periode = _periode(receptions)
        sujet = f"Réceptions cuisine livrées en direct {periode} ({len(receptions)} réceptions)"
        texte = (
            "Bonjour,\n\n"
            f"Veuillez trouver ci-joint les réceptions livrées directement à la cuisine {periode} :\n\n"
            + "\n\n".join(
                f"- {f} : {nb} réception{'s' if nb > 1 else ''}, {_kg(p)} kg\n"
                + "\n".join(
                    f"    · code VIF {c or '(sans code)'} : {_kg(sum(r['poids_kg'] or 0 for r in lc))} kg"
                    for c, lc in _par_code_vif([r for r in receptions if r["fournisseur_nom"] == f]).items()
                )
                for f, (nb, p) in totaux.items()
            )
            + "\n\nPar code VIF :\n"
            + "\n".join(f"- {c or 'sans code VIF'} : {_kg(p)} kg" for c, (nb, p) in _totaux_par_code_vif(receptions).items())
            + f"\n\nTotal : {len(receptions)} réceptions, {_kg(sum(p for _, p in totaux.values()))} kg.\n\n"
            "Cordialement,\nLa cuisine"
        )
        chemin = os.path.join(tempfile.gettempdir(), f"receptions_cuisine_bai_{horodatage[:10]}_{os.getpid()}.pdf")
        try:
            with open(chemin, "wb") as fichier:
                fichier.write(generer_pdf_envoi_bai(receptions, envoye_le=horodatage).getvalue())
            resultat = envoyer_mail(
                sujet=sujet, destinataires=destinataires, texte=texte, attachment_path=chemin,
                attachment_filename=f"receptions_cuisine_{horodatage[:10]}.pdf",
                sender_name="Cuisine BA38", reply_to=utilisateur, current_user_email=utilisateur,
            )
            statut = (resultat or {}).get("Messages", [{}])[0].get("Status")
            if statut != "success":
                raise RuntimeError(f"Mailjet : statut {statut or 'inconnu'}")
        except Exception as e:
            write_log(f"❌ Envoi réceptions cuisine à la BAI : {e}")
            flash("❌ Le mail n'est pas parti, rien n'a été marqué comme envoyé. Réessayez plus tard.", "danger")
            return redirect(retour)
        finally:
            if os.path.exists(chemin):
                os.remove(chemin)
        write_log(f"📤 Réceptions cuisine du {jour} envoyées à la BAI ({', '.join(destinataires)}) : {len(ids)} réceptions par {utilisateur}")

        marques = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE cuisine_receptions SET envoi_bai_le = ?, envoi_bai_par = ? WHERE id IN ({marques}) AND envoi_bai_le IS NULL",
            [horodatage, utilisateur, *ids],
        )
        conn.commit()
    upload_database()
    flash(f"✅ {len(ids)} réception{'s' if len(ids) > 1 else ''} envoyée{'s' if len(ids) > 1 else ''} à la BAI.", "success")
    return redirect(retour)
