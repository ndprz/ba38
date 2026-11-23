# ============================================================
# 📤 Mécénat — export du plan pluriannuel d'investissements (PPI), Excel
#   Feuille « PPI » : un dossier par ligne, montants TTC répartis par année
#   (année de la date prévue), sous-totaux par famille, total général
#   (formules Excel), subventions obtenues et reste à financer.
#   Feuille « Financement » : détail mécène par mécène.
# ============================================================

import io
from datetime import date, datetime

from flask import flash, redirect, request, send_file, url_for
from flask_login import login_required
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ba38_tresorerie import tresorerie_bp
from ba38_tresorerie.mecenat import TYPES_DOSSIER, _connect, acces

EUR = '#,##0.00" €"'
GRIS = PatternFill("solid", fgColor="D9D9D9")
ENTETE = PatternFill("solid", fgColor="1F4E78")


def _annee(valeur):
    try:
        return int((valeur or "")[:4])
    except ValueError:
        return None


@tresorerie_bp.route("/tresorerie/mecenat/export_ppi")
@login_required
@acces("lecture")
def mecenat_export_ppi():
    debut = request.args.get("annee", type=int) or date.today().year
    nb = request.args.get("nb", type=int) or 4
    if not (2000 <= debut <= 2100) or not (1 <= nb <= 10):
        flash("❌ Année de départ ou nombre d'années invalide (1 à 10 ans).", "danger")
        return redirect(url_for("tresorerie.mecenat_dossiers"))
    non_dates = request.args.get("non_dates") == "1"
    # Un PPI porte sur les investissements ; les achats de denrées sont exportés sur demande.
    type_filtre = request.args.get("type", "investissement")
    if type_filtre not in TYPES_DOSSIER:
        type_filtre = ""   # « tous les types »
    annees = list(range(debut, debut + nb))
    pole_filtre = request.args.get("pole", type=int) or 0

    with _connect() as conn:
        dossiers = conn.execute(
            """SELECT d.*, TRIM(COALESCE(p1.prenom,'') || ' ' || COALESCE(p1.nom,'')) AS pilote_projet,
                      TRIM(COALESCE(p2.prenom,'') || ' ' || COALESCE(p2.nom,'')) AS pilote_mecenat,
                      (SELECT nom_affiche FROM engagement_poles WHERE id = d.pole_id) AS pole
               FROM mecenat_dossiers d
               LEFT JOIN benevoles p1 ON p1.id = d.pilote_projet_id
               LEFT JOIN benevoles p2 ON p2.id = d.pilote_mecenat_id
               WHERE (? = '' OR d.type_dossier = ?) AND (? = 0 OR d.pole_id = ?)
               ORDER BY d.type_dossier, LOWER(COALESCE(d.famille,'')), COALESCE(NULLIF(d.date_prevue,''), '9999'), d.id""", (type_filtre, type_filtre, pole_filtre, pole_filtre)).fetchall()
        demandes = conn.execute(
            """SELECT dm.*, m.structure FROM mecenat_demandes dm JOIN mecenat m ON m.id = dm.mecene_id
               ORDER BY m.structure COLLATE NOCASE""").fetchall()

    retenus = []
    for d in dossiers:
        a = _annee(d["date_prevue"])
        if a in annees or (a is None and non_dates):
            retenus.append((d, a))

    wb = Workbook()
    ws = wb.active
    ws.title = "PPI"
    ws["A1"] = f"Plan pluriannuel d'investissements {annees[0]}–{annees[-1]} (montants TTC)"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"] = ws["A1"].value + (f" — {TYPES_DOSSIER[type_filtre].lower()}" if type_filtre else " — tous types")
    ws["A2"] = f"Édité le {datetime.now():%d/%m/%Y} — TVA non récupérée ; montants placés dans l'année de la date prévue."

    cols_annees = [str(a) for a in annees] + (["Non daté"] if non_dates else [])
    entetes = ["Type", "Pôle", "Famille", "Objet", "Date prévue", "Pilote projet", "Pilote recherche mécénat"] + cols_annees + \
              ["Total TTC", "Subventions obtenues", "Reste à financer"]
    HL = 4
    for i, h in enumerate(entetes, 1):
        c = ws.cell(HL, i, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = ENTETE
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    c0 = 8                          # première colonne d'année
    c_tot = c0 + len(cols_annees)   # Total TTC
    c_sub, c_reste = c_tot + 1, c_tot + 2
    L = lambda n: get_column_letter(n)

    def ligne_total(r, libelle, debut_r, fin_r):
        ws.cell(r, 1, libelle).font = Font(bold=True)
        for col in range(c0, c_sub + 1):
            ws.cell(r, col, f"=SUM({L(col)}{debut_r}:{L(col)}{fin_r})")
        ws.cell(r, c_reste, f"={L(c_tot)}{r}-{L(c_sub)}{r}")
        for col in range(1, c_reste + 1):
            ws.cell(r, col).fill = GRIS
            ws.cell(r, col).font = Font(bold=True)
            if col >= c0:
                ws.cell(r, col).number_format = EUR

    r = HL + 1
    sous_totaux, famille_courante, debut_famille = [], None, r
    for d, a in retenus:
        fam = (d["famille"] or "").strip() or "(sans famille)"
        if not type_filtre:
            fam = f"{TYPES_DOSSIER[d['type_dossier']]} — {fam}"
        if famille_courante is not None and fam != famille_courante:
            ligne_total(r, f"Sous-total {famille_courante}", debut_famille, r - 1)
            sous_totaux.append(r)
            r += 1
            debut_famille = r
        famille_courante = fam
        ws.cell(r, 1, TYPES_DOSSIER[d["type_dossier"]])
        ws.cell(r, 2, d["pole"] or "")
        ws.cell(r, 3, (d["famille"] or "").strip())
        ws.cell(r, 4, d["objet"])
        ws.cell(r, 5, datetime.strptime(d["date_prevue"][:10], "%Y-%m-%d") if d["date_prevue"] else None)
        ws.cell(r, 5).number_format = "DD/MM/YYYY"
        ws.cell(r, 6, d["pilote_projet"])
        ws.cell(r, 7, d["pilote_mecenat"])
        idx = c0 + (annees.index(a) if a in annees else len(annees))
        ws.cell(r, idx, d["montant_ttc"] or 0)
        ws.cell(r, c_tot, f"=SUM({L(c0)}{r}:{L(c_tot - 1)}{r})")
        ws.cell(r, c_sub, d["total_subvention_obtenues"] or 0)
        ws.cell(r, c_reste, f"={L(c_tot)}{r}-{L(c_sub)}{r}")
        for col in range(c0, c_reste + 1):
            ws.cell(r, col).number_format = EUR
        r += 1
    if famille_courante is not None:
        ligne_total(r, f"Sous-total {famille_courante}", debut_famille, r - 1)
        sous_totaux.append(r)
        r += 1
        ligne_total(r, "TOTAL GÉNÉRAL", HL + 1, r - 1)
        # le total général ne doit pas compter les sous-totaux deux fois : somme des seuls sous-totaux
        for col in range(c0, c_sub + 1):
            ws.cell(r, col, "=" + "+".join(f"{L(col)}{s}" for s in sous_totaux))
    else:
        ws.cell(HL + 1, 1, "Aucun dossier sur cette période.")

    for col, w in {1: 16, 2: 22, 3: 22, 4: 42, 5: 13, 6: 22, 7: 24}.items():
        ws.column_dimensions[L(col)].width = w
    for col in range(c0, c_reste + 1):
        ws.column_dimensions[L(col)].width = 16
    ws.row_dimensions[HL].height = 32
    ws.freeze_panes = ws.cell(HL + 1, 5)

    # ---- Feuille détail du financement ----
    w2 = wb.create_sheet("Financement")
    ids = {d["id"]: d for d, _ in retenus}
    for i, h in enumerate(["Type", "Pôle", "Famille", "Objet", "Mécène", "Montant demandé", "Montant obtenu", "Date de versement", "Notes"], 1):
        c = w2.cell(1, i, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = ENTETE
    n = 2
    for d, _ in retenus:
        for dm in (x for x in demandes if x["dossier_id"] == d["id"]):
            w2.cell(n, 1, TYPES_DOSSIER[d["type_dossier"]])
            w2.cell(n, 2, d["pole"] or "")
            w2.cell(n, 3, d["famille"] or "")
            w2.cell(n, 4, d["objet"])
            w2.cell(n, 5, dm["structure"])
            w2.cell(n, 6, dm["montant_demande"] or 0).number_format = EUR
            w2.cell(n, 7, dm["montant_obtenu"] or 0).number_format = EUR
            if dm["date_versement"]:
                w2.cell(n, 8, datetime.strptime(dm["date_versement"][:10], "%Y-%m-%d")).number_format = "DD/MM/YYYY"
            w2.cell(n, 9, dm["notes"] or "")
            n += 1
    for col, w in {1: 16, 2: 22, 3: 22, 4: 42, 5: 28, 6: 17, 7: 17, 8: 17, 9: 50}.items():
        w2.column_dimensions[L(col)].width = w
    w2.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"PPI_mecenat_{annees[0]}-{annees[-1]}{'_' + (type_filtre or 'tous') if type_filtre != 'investissement' else ''}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
