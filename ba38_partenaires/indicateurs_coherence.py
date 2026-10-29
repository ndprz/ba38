"""
Indicateurs État : contrôle de cohérence des valeurs AMS (pastille vert/orange/rouge)
et état PDF de proposition d'indicateurs pour les associations non répondues.

Les coefficients et seuils sont regroupés ici pour être ajustés facilement ;
une nouvelle règle = une fonction ajoutée à REGLES.
"""
import io
from xml.sax.saxutils import escape
import statistics
from datetime import datetime
from pathlib import Path

from flask import current_app

from ba38_utilitaires.organisation import get_organisation

# ---------------------------------------------------------------------------
# Paramètres
# ---------------------------------------------------------------------------
# Référence nationale FFBA : 47 kg par bénéficiaire et par an, soit 11,75 kg par trimestre
KG_PAR_BENEFICIAIRE_AN = 47
PASSAGES_PAR_BENEFICIAIRE_TRIMESTRE = 12  # proposition PDF : 1 passage par semaine environ (×4 sur l'année)
PERSONNES_PAR_FOYER_DEFAUT = 2.0         # si aucun ratio connu (ni asso, ni campagne)

# Écart relatif tonnage AMS / VIF : ≤ ORANGE → vert, ≤ ROUGE → orange, au-delà → rouge
ECART_TONNAGE_ORANGE = 0.10
ECART_TONNAGE_ROUGE = 0.25

# Ratio personnes AMS / bénéficiaires théoriques : dans [VERT_MIN, VERT_MAX] → vert,
# dans [ORANGE_MIN, ORANGE_MAX] → orange, au-delà → rouge
RATIO_BENEF_VERT = (0.5, 2.0)
RATIO_BENEF_ORANGE = (0.25, 4.0)

VERT, ORANGE, ROUGE = "vert", "orange", "rouge"
_RANG = {VERT: 0, ORANGE: 1, ROUGE: 2}


def kg_par_beneficiaire(periode):
    """47 kg pour une campagne annuelle, 47 / 4 = 11,75 kg pour un trimestre."""
    if str(periode or "").strip().lower().startswith("année"):
        return KG_PAR_BENEFICIAIRE_AN
    return KG_PAR_BENEFICIAIRE_AN / 4


def passages_par_beneficiaire(periode):
    """12 passages par trimestre (≈ 1 par semaine), 48 pour une campagne annuelle."""
    if str(periode or "").strip().lower().startswith("année"):
        return PASSAGES_PAR_BENEFICIAIRE_TRIMESTRE * 4
    return PASSAGES_PAR_BENEFICIAIRE_TRIMESTRE


def _fmt(n, dec=0):
    return f"{n:,.{dec}f}".replace(",", " ").replace(".", ",")


# ---------------------------------------------------------------------------
# Règles de cohérence : chaque règle renvoie None (rien à signaler / non
# applicable) ou (niveau, message).
# ---------------------------------------------------------------------------
def regle_tonnage_absent(l):
    if not l.get("ams_volume_kg"):
        return ROUGE, "Pas de tonnage dans AMS"


def regle_foyers_absents(l):
    if not l.get("ams_nb_foyers"):
        return ROUGE, "Pas de foyers dans AMS"


def regle_personnes_absentes(l):
    if not l.get("ams_nb_personnes"):
        return ROUGE, "Pas de personnes dans AMS"


def regle_passages_absents(l):
    if not l.get("ams_nb_passages"):
        return ROUGE, "Pas de passages dans AMS"


def regle_ecart_tonnage(l):
    vif, ams = l.get("tonnage_kg_net"), l.get("ams_volume_kg")
    if vif is None or not ams:
        return None
    if vif == 0:
        return ORANGE, f"Tonnage AMS {_fmt(ams)} kg mais 0 kg au VIF"
    ecart = abs(ams - vif) / vif
    msg = f"Tonnage AMS {_fmt(ams)} kg / VIF {_fmt(vif)} kg : écart {_fmt(ecart * 100)} %"
    if ecart > ECART_TONNAGE_ROUGE:
        return ROUGE, msg
    if ecart > ECART_TONNAGE_ORANGE:
        return ORANGE, msg
    return VERT, msg


def regle_beneficiaires_theoriques(l):
    vif, pers = l.get("tonnage_kg_net"), l.get("ams_nb_personnes")
    if not vif or not pers:
        return None
    coef = kg_par_beneficiaire(l.get("periode"))
    theo = vif / coef
    ratio = pers / theo
    msg = (f"Personnes AMS {_fmt(pers)} / théoriques {_fmt(theo)} "
           f"(VIF ÷ {_fmt(coef, 2)}) : {_fmt(ratio * 100)} %")
    if RATIO_BENEF_VERT[0] <= ratio <= RATIO_BENEF_VERT[1]:
        return VERT, msg
    if RATIO_BENEF_ORANGE[0] <= ratio <= RATIO_BENEF_ORANGE[1]:
        return ORANGE, msg
    return ROUGE, msg


REGLES = [
    regle_tonnage_absent,
    regle_foyers_absents,
    regle_personnes_absentes,
    regle_passages_absents,
    regle_ecart_tonnage,
    regle_beneficiaires_theoriques,
]


def controle_coherence(ligne, periode):
    """Renvoie (niveau, [messages avec pastille emoji]) pour l'infobulle de la page."""
    niveau, details = controle_coherence_detail(ligne, periode)
    pastilles = {ROUGE: "🔴 ", ORANGE: "🟠 ", VERT: "🟢 "}
    return niveau, [pastilles[n] + msg for n, msg in details]


def controle_coherence_detail(ligne, periode):
    """Renvoie (niveau, [(niveau_regle, message)]) ; niveau None si rien à contrôler
    (association n'ayant pas validé et sans aucune valeur AMS)."""
    l = dict(ligne)
    l["periode"] = periode
    valeurs = [l.get(k) for k in ("ams_volume_kg", "ams_nb_foyers", "ams_nb_personnes", "ams_nb_passages")]
    if (l.get("statut_csv") or "").strip().lower() != "validé" and not any(valeurs):
        return None, []

    niveau, details = VERT, []
    for regle in REGLES:
        res = regle(l)
        if not res:
            continue
        details.append(res)
        if _RANG[res[0]] > _RANG[niveau]:
            niveau = res[0]
    return niveau, details


# ---------------------------------------------------------------------------
# Proposition d'indicateurs pour les non répondus
# ---------------------------------------------------------------------------
def _ratio_personnes_par_foyer(conn, campagne_id):
    """Ratio personnes/foyer : celui de l'association sur sa dernière campagne
    renseignée (autre que la campagne courante), à défaut la médiane des
    associations validées de la campagne courante."""
    par_asso = {}
    for r in conn.execute("""
        SELECT association_id, ams_nb_personnes, ams_nb_foyers
        FROM indicateurs_suivi
        WHERE campagne_id != ? AND ams_nb_personnes > 0 AND ams_nb_foyers > 0
        ORDER BY campagne_id
    """, (campagne_id,)):
        par_asso[r["association_id"]] = r["ams_nb_personnes"] / r["ams_nb_foyers"]

    ratios = [r["ams_nb_personnes"] / r["ams_nb_foyers"] for r in conn.execute("""
        SELECT ams_nb_personnes, ams_nb_foyers FROM indicateurs_suivi
        WHERE campagne_id = ? AND LOWER(TRIM(statut_csv)) = 'validé'
          AND ams_nb_personnes > 0 AND ams_nb_foyers > 0
    """, (campagne_id,))]
    defaut = statistics.median(ratios) if ratios else PERSONNES_PAR_FOYER_DEFAUT
    return par_asso, defaut


def calculer_propositions(conn, campagne_id, periode):
    """Lignes de proposition pour les associations « Non reçu » de la campagne."""
    coef = kg_par_beneficiaire(periode)
    nb_passages = passages_par_beneficiaire(periode)
    par_asso, defaut = _ratio_personnes_par_foyer(conn, campagne_id)
    lignes = conn.execute("""
        SELECT s.association_id, s.tonnage_kg_net, a.nom_association, a.code_VIF, a.nbre_beneficiaires_vif
        FROM indicateurs_suivi s
        JOIN associations a ON a.id = s.association_id
        WHERE s.campagne_id = ? AND s.present_csv = 1
          AND LOWER(TRIM(s.statut_csv)) = 'non reçu'
        ORDER BY a.nom_association COLLATE NOCASE
    """, (campagne_id,)).fetchall()

    propositions = []
    for l in lignes:
        vif = l["tonnage_kg_net"]
        p = {"nom": l["nom_association"], "code_vif": l["code_VIF"], "vif_kg": vif,
             "benef_vif": l["nbre_beneficiaires_vif"]}
        if vif:
            benef = round(vif / coef)
            ratio = par_asso.get(l["association_id"])
            p["ratio_source"] = "asso" if ratio else "moyenne"
            ratio = ratio or defaut
            p.update(
                tonne_net=vif / 1000,
                beneficiaires=benef,
                foyers=max(1, round(benef / ratio)) if benef else 0,
                passages=benef * nb_passages,
                ratio=ratio,
            )
        propositions.append(p)
    return propositions, defaut


def generer_pdf_propositions(campagne, propositions, ratio_defaut):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    org = get_organisation()
    styles = getSampleStyleSheet()
    petit = styles["Normal"].clone("petit", fontSize=8.5, leading=10.5)
    titre_style = styles["Normal"].clone("titre", fontSize=14, leading=17, fontName="Helvetica-Bold")
    page = landscape(A4)
    marge = 12 * mm

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=page, leftMargin=marge, rightMargin=marge, topMargin=marge, bottomMargin=14 * mm,
        title=f"Proposition indicateurs État - {campagne['periode']}",
    )
    edite_le = datetime.now().strftime("%d/%m/%Y %H:%M")

    def pied(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(marge, 8 * mm, f"Proposition indicateurs État {campagne['periode']} - non répondus - édité le {edite_le}")
        canvas.drawRightString(page[0] - marge, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    elements = []
    logo = None
    logo_path = Path(current_app.root_path) / (org.get("logo_path") or "")
    if org.get("logo_path") and logo_path.exists():
        logo = Image(str(logo_path), width=18 * mm, height=18 * mm, kind="proportional")
    titre = Paragraph(f"Proposition d'indicateurs État - {campagne['periode']}<br/>"
                      f"<font size='10'>Associations n'ayant pas répondu (statut AMS « Non reçu ») - "
                      f"{len(propositions)} association(s)</font>", titre_style)
    entete = Table([[logo or "", titre]], colWidths=[24 * mm, None])
    entete.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elements += [entete, Spacer(1, 3 * mm)]

    elements.append(Paragraph(
        f"Calcul à partir du tonnage VIF du trimestre (kg brut) : "
        f"<b>bénéficiaires</b> = tonnage ÷ {_fmt(kg_par_beneficiaire(campagne['periode']), 2)} "
        f"(référence FFBA {KG_PAR_BENEFICIAIRE_AN} kg par bénéficiaire et par an) ; "
        f"<b>passages</b> = bénéficiaires × {passages_par_beneficiaire(campagne['periode'])} (environ 1 passage par semaine) ; "
        f"<b>foyers</b> = bénéficiaires ÷ nombre de personnes par foyer de l'association lors de sa dernière "
        f"déclaration AMS (à défaut, médiane des associations validées de la campagne : {_fmt(ratio_defaut, 2)}, "
        f"marquée *). Valeurs à saisir manuellement dans AMS avant la clôture.", petit))
    elements.append(Spacer(1, 3 * mm))

    entetes = ["Association", "Code VIF", "Tonnage VIF\n(kg brut)", "Volume à saisir\n(T net)",
               "Benef Vif", "Bénéficiaires", "Pers./foyer", "Foyers", "Passages", "Saisi\nAMS"]
    donnees = [entetes]
    for p in propositions:
        if p.get("vif_kg"):
            donnees.append([
                Paragraph(escape(p["nom"] or ""), petit), p["code_vif"] or "",
                _fmt(p["vif_kg"]), _fmt(p["tonne_net"], 3),
                _fmt(p["benef_vif"]) if p.get("benef_vif") else "—", _fmt(p["beneficiaires"]),
                _fmt(p["ratio"], 2) + ("" if p["ratio_source"] == "asso" else " *"),
                _fmt(p["foyers"]), _fmt(p["passages"]), "",
            ])
        else:
            donnees.append([Paragraph(escape(p["nom"] or ""), petit), p["code_vif"] or "",
                            "—", Paragraph("<i>Pas de tonnage VIF importé : pas de proposition</i>", petit),
                            _fmt(p["benef_vif"]) if p.get("benef_vif") else "—", "", "", "", "", ""])

    avec = [p for p in propositions if p.get("vif_kg")]
    if avec:
        donnees.append([Paragraph("<b>Total</b>", petit), "",
                        _fmt(sum(p["vif_kg"] for p in avec)), _fmt(sum(p["tonne_net"] for p in avec), 3),
                        _fmt(sum(p.get("benef_vif") or 0 for p in avec)),
                        _fmt(sum(p["beneficiaires"] for p in avec)), "",
                        _fmt(sum(p["foyers"] for p in avec)), _fmt(sum(p["passages"] for p in avec)), ""])

    largeurs = [66 * mm, 22 * mm, 26 * mm, 28 * mm, 18 * mm, 24 * mm, 20 * mm, 20 * mm, 22 * mm, 20 * mm]
    t = Table(donnees, colWidths=largeurs, repeatRows=1)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 1), (-2, -1), "RIGHT"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
    ]
    if avec:
        style += [("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f5f5f5")),
                  ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    elements.append(t)

    doc.build(elements, onFirstPage=pied, onLaterPages=pied)
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------------------
# État des incohérences (orange et rouge)
# ---------------------------------------------------------------------------
def lignes_incoherentes(conn, campagne_id, periode):
    """Associations dont la pastille est orange ou rouge, rouges d'abord."""
    lignes = conn.execute("""
        SELECT s.*, a.nom_association, a.code_VIF, a.nbre_beneficiaires_vif
        FROM indicateurs_suivi s
        JOIN associations a ON a.id = s.association_id
        WHERE s.campagne_id = ?
        ORDER BY a.nom_association COLLATE NOCASE
    """, (campagne_id,)).fetchall()

    resultat = []
    for l in lignes:
        niveau, details = controle_coherence_detail(l, periode)
        if niveau in (ORANGE, ROUGE):
            d = dict(l)
            d["niveau"] = niveau
            d["anomalies"] = [(n, m) for n, m in details if n != VERT]
            resultat.append(d)
    resultat.sort(key=lambda d: -_RANG[d["niveau"]])
    return resultat


def generer_pdf_incoherences(campagne, lignes):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    org = get_organisation()
    styles = getSampleStyleSheet()
    petit = styles["Normal"].clone("petit", fontSize=8, leading=10)
    titre_style = styles["Normal"].clone("titre", fontSize=14, leading=17, fontName="Helvetica-Bold")
    page = landscape(A4)
    marge = 10 * mm
    couleurs = {ROUGE: colors.HexColor("#dc3545"), ORANGE: colors.HexColor("#fd7e14")}
    hex_couleurs = {ROUGE: "#dc3545", ORANGE: "#e8590c"}

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=page, leftMargin=marge, rightMargin=marge, topMargin=marge, bottomMargin=14 * mm,
        title=f"Incohérences indicateurs État - {campagne['periode']}",
    )
    edite_le = datetime.now().strftime("%d/%m/%Y %H:%M")

    def pied(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(marge, 8 * mm, f"Incohérences indicateurs État {campagne['periode']} - édité le {edite_le}")
        canvas.drawRightString(page[0] - marge, 8 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    nb_rouge = sum(1 for l in lignes if l["niveau"] == ROUGE)
    nb_orange = len(lignes) - nb_rouge

    elements = []
    logo = None
    logo_path = Path(current_app.root_path) / (org.get("logo_path") or "")
    if org.get("logo_path") and logo_path.exists():
        logo = Image(str(logo_path), width=18 * mm, height=18 * mm, kind="proportional")
    titre = Paragraph(
        f"Incohérences des indicateurs État - {campagne['periode']}<br/>"
        f"<font size='10'><font color='{hex_couleurs[ROUGE]}'>{nb_rouge} rouge(s)</font> - "
        f"<font color='{hex_couleurs[ORANGE]}'>{nb_orange} orange(s)</font></font>", titre_style)
    entete = Table([[logo or "", titre]], colWidths=[24 * mm, None])
    entete.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elements += [entete, Spacer(1, 2 * mm)]

    elements.append(Paragraph(
        f"Valeurs AMS retenues BA comparées au tonnage VIF importé. Règles : tonnage, foyers, personnes ou "
        f"passages absents (rouge) ; écart de tonnage AMS / VIF au-delà de {_fmt(ECART_TONNAGE_ORANGE * 100)} % "
        f"(orange) ou {_fmt(ECART_TONNAGE_ROUGE * 100)} % (rouge) ; personnes AMS comparées aux bénéficiaires "
        f"théoriques (VIF ÷ {_fmt(kg_par_beneficiaire(campagne['periode']), 2)}, référence FFBA "
        f"{KG_PAR_BENEFICIAIRE_AN} kg par bénéficiaire et par an) : orange hors "
        f"{_fmt(RATIO_BENEF_VERT[0] * 100)}-{_fmt(RATIO_BENEF_VERT[1] * 100)} %, rouge hors "
        f"{_fmt(RATIO_BENEF_ORANGE[0] * 100)}-{_fmt(RATIO_BENEF_ORANGE[1] * 100)} %.", petit))
    elements.append(Spacer(1, 3 * mm))

    def nombre(v, dec=0):
        return _fmt(v, dec) if v else "—"

    donnees = [["", "Association", "Code VIF", "Statut", "VIF\n(kg brut)", "Benef\nVif", "AMS\n(T net)", "Foyers",
                "Personnes", "Passages", "Anomalies", "Motifs d'écart (Asso / BA)"]]
    for l in lignes:
        anomalies = "<br/>".join(
            f"<font color='{hex_couleurs[n]}'>•</font> {escape(m)}" for n, m in l["anomalies"])
        motifs = " / ".join(escape(x) for x in (l.get("ams_motif_ecart_asso"), l.get("ams_motif_ecart_ba")) if x)
        donnees.append([
            "", Paragraph(escape(l["nom_association"] or ""), petit), l["code_VIF"] or "",
            Paragraph(escape(l.get("statut_csv") or ""), petit),
            _fmt(l["tonnage_kg_net"]) if l.get("tonnage_kg_net") is not None else "—",
            nombre(l.get("nbre_beneficiaires_vif")),
            nombre((l.get("ams_volume_kg") or 0) / 1000, 2),
            nombre(l.get("ams_nb_foyers")), nombre(l.get("ams_nb_personnes")), nombre(l.get("ams_nb_passages")),
            Paragraph(anomalies, petit), Paragraph(motifs, petit),
        ])

    largeurs = [5 * mm, 44 * mm, 18 * mm, 17 * mm, 17 * mm, 13 * mm, 15 * mm, 14 * mm, 17 * mm, 16 * mm, 64 * mm, None]
    largeurs[-1] = page[0] - 2 * marge - sum(largeurs[:-1])
    t = Table(donnees, colWidths=largeurs, repeatRows=1)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (4, 1), (9, -1), "RIGHT"),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
    ]
    for i, l in enumerate(lignes, start=1):
        style.append(("BACKGROUND", (0, i), (0, i), couleurs[l["niveau"]]))
    t.setStyle(TableStyle(style))
    elements.append(t if lignes else Paragraph("Aucune incohérence détectée.", petit))

    doc.build(elements, onFirstPage=pied, onLaterPages=pied)
    buffer.seek(0)
    return buffer
