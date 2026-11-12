"""Suivi des cotisations bénévoles : synthèse + liste par année, export Excel.

Données : table `benevoles_cotisations` (voir ba38_benevoles/cotisations.py).
"""
import io
from collections import Counter
from datetime import date

from datetime import datetime

from flask import jsonify, render_template, request, send_file
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from ba38_utilitaires.core import get_db_connection, require_access
from ba38_benevoles.routes import benevoles_bp, _date_cotis
from ba38_benevoles import cotisations as cotis

STATUTS = {
    "tous": "Tous",
    "a_jour": "À jour",
    "non": "Pas à jour",
    "relancer": "À relancer (à jour l'an dernier, pas cette année)",
}
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _fr(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if iso and len(iso) >= 10 else ""


def _parametres():
    with get_db_connection() as conn:
        annees = cotis.annees(conn)
    try:
        annee = int(request.values.get("annee") or cotis.annee_affichee())
    except ValueError:
        annee = cotis.annee_affichee()
    if annee not in annees:
        annee = annees[-1]
    statut = request.values.get("statut", "tous")
    if statut not in STATUTS:
        statut = "tous"
    salaries = request.values.get("salaries") == "1"
    return annees, annee, statut, salaries


def donnees(conn, annee, statut, salaries):
    """(synthèse, lignes filtrées) pour l'année."""
    payees = {r[0]: (r[1] or "") for r in conn.execute(
        "SELECT id_benevole, date_paiement FROM benevoles_cotisations WHERE annee = ?", (annee,))}
    precedentes = cotis.ids_a_jour(conn, annee - 1)

    lignes = []
    for b in conn.execute("SELECT id, nom, prenom, email, type_benevole FROM benevoles "
                          "ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE"):
        salarie = (b["type_benevole"] or "").strip().lower() == "salarie"
        if salarie and not salaries:
            continue
        a_jour = b["id"] in payees
        lignes.append({
            "id": b["id"], "nom": b["nom"] or "", "prenom": b["prenom"] or "",
            "email": b["email"] or "", "type": b["type_benevole"] or "",
            "a_jour": a_jour, "date": payees.get(b["id"], ""),
            "relancer": (not a_jour) and b["id"] in precedentes,
        })

    total = len(lignes)
    nb_a_jour = sum(1 for l in lignes if l["a_jour"])
    par_mois = Counter(l["date"][5:7] for l in lignes if l["a_jour"] and len(l["date"]) >= 7)
    synthese = {
        "total": total,
        "a_jour": nb_a_jour,
        "non": total - nb_a_jour,
        "taux": round(100 * nb_a_jour / total) if total else 0,
        "relancer": sum(1 for l in lignes if l["relancer"]),
        "sans_date": sum(1 for l in lignes if l["a_jour"] and not l["date"]),
        "precedente": sum(1 for l in lignes if l["id"] in precedentes),
        "par_mois": [(MOIS[m - 1], par_mois.get(f"{m:02d}", 0)) for m in range(1, 13)],
    }
    synthese["max_mois"] = max([n for _, n in synthese["par_mois"]] + [1])

    if statut == "a_jour":
        lignes = [l for l in lignes if l["a_jour"]]
    elif statut == "non":
        lignes = [l for l in lignes if not l["a_jour"]]
    elif statut == "relancer":
        lignes = [l for l in lignes if l["relancer"]]
    return synthese, lignes


@benevoles_bp.route("/benevoles/cotisations")
@login_required
@require_access("benevoles", "lecture")
def suivi_cotisations():
    annees, annee, statut, salaries = _parametres()
    with get_db_connection() as conn:
        synthese, lignes = donnees(conn, annee, statut, salaries)
    return render_template(
        "benevoles/suivi_cotisations.html",
        annees=list(reversed(annees)), annee=annee, statut=statut, statuts=STATUTS,
        salaries=salaries, synthese=synthese, lignes=lignes, fr=_fr,
    )


@benevoles_bp.route("/benevoles/cotisations/excel")
@login_required
@require_access("benevoles", "lecture")
def suivi_cotisations_excel():
    annees, annee, statut, salaries = _parametres()
    with get_db_connection() as conn:
        synthese, lignes = donnees(conn, annee, statut, salaries)

    wb = Workbook()
    ws = wb.active
    ws.title = f"Cotisations {annee}"
    entetes = ["Id", "Nom", "Prénom", "E-mail", "Type", f"Cotisation {annee}", "Date de paiement", "À relancer"]
    ws.append(entetes)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDDDDD")
    for l in lignes:
        ws.append([l["id"], l["nom"], l["prenom"], l["email"], l["type"],
                   "À jour" if l["a_jour"] else "Pas à jour",
                   _fr(l["date"]), "oui" if l["relancer"] else ""])
    for i, largeur in enumerate([8, 24, 20, 34, 14, 16, 18, 12], 1):
        ws.column_dimensions[get_column_letter(i)].width = largeur
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    ws2 = wb.create_sheet("Synthèse")
    for libelle, valeur in [
        ("Année", annee),
        ("Filtre de la liste", STATUTS[statut] + ("" if salaries else " (hors salariés)")),
        ("Bénévoles", synthese["total"]),
        ("À jour", synthese["a_jour"]),
        ("Pas à jour", synthese["non"]),
        ("Taux à jour (%)", synthese["taux"]),
        (f"À jour en {annee - 1}", synthese["precedente"]),
        ("À relancer", synthese["relancer"]),
        ("À jour sans date de paiement", synthese["sans_date"]),
        ("", ""),
        ("Paiements par mois", ""),
    ] + [(m, n) for m, n in synthese["par_mois"]]:
        ws2.append([libelle, valeur])
    ws2.column_dimensions["A"].width = 38
    ws2.column_dimensions["B"].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"suivi_cotisations_{annee}_{date.today():%Y%m%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@benevoles_bp.route("/api/benevole_cotisation", methods=["POST"])
@login_required
@require_access("benevoles", "ecriture")
def api_benevole_cotisation():
    """Coche/décoche une année de cotisation depuis la liste (JSON : id, annee, a_jour, date).
    Le jeton CSRF doit être envoyé dans l'en-tête X-CSRFToken."""
    data = request.get_json(silent=True) or {}
    try:
        bene_id, annee = int(data.get("id")), int(data.get("annee"))
    except (TypeError, ValueError):
        return jsonify(ok=False, erreur="Paramètres invalides"), 400
    if not (2000 <= annee <= 2100):
        return jsonify(ok=False, erreur="Année invalide"), 400
    a_jour = bool(data.get("a_jour"))
    date_p = _date_cotis(data.get("date")) if a_jour else None
    if a_jour and data.get("date") and not date_p:
        return jsonify(ok=False, erreur="Date invalide"), 400

    with get_db_connection() as conn:
        if not conn.execute("SELECT 1 FROM benevoles WHERE id = ?", (bene_id,)).fetchone():
            return jsonify(ok=False, erreur="Bénévole introuvable"), 404
        cotis.fixer(conn, bene_id, annee, a_jour, current_user.username, date_p)
        now = datetime.now()
        conn.execute("UPDATE benevoles SET date_modif = ?, heure_modif = ?, user_modif = ? WHERE id = ?",
                     (now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), current_user.username, bene_id))
        conn.commit()
        date_finale = cotis.dates_par_annee(conn, bene_id).get(annee, "") if a_jour else ""
    return jsonify(ok=True, a_jour=a_jour, date=date_finale)
