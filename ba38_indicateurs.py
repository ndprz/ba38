# =========================================
# 📊 Module Indicateurs
# =========================================

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from utils import get_db_connection, write_log, require_access
import os
import pandas as pd
from datetime import datetime
from werkzeug.utils import secure_filename

indicateurs_bp = Blueprint(
    "indicateurs",
    __name__,
    template_folder="templates/indicateurs"
)

UPLOAD_DIR = "/srv/ba38/dev/uploads/indicateurs"


# =========================================
# 🔍 Détection ligne d’en-tête CSV
# =========================================
def detect_header_line(filepath):
    with open(filepath, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if line.startswith("Nom Association;"):
                return i
    return 0



# =========================================
# 🔧 Normalisation des codes VIF
# =========================================
def normalize_code(code):
    """
    Nettoie un code pour assurer un matching fiable :
    - supprime espaces, retours ligne
    - supprime guillemets
    - gère les formats pandas (ex: 123.0)
    """

    code = str(code)

    code = code.strip()
    code = code.replace("\xa0", "")  # espace insécable
    code = code.replace(" ", "")
    code = code.replace('"', '')
    code = code.replace("\n", "")
    code = code.replace("\r", "")

    if code.endswith(".0"):
        code = code[:-2]

    return code



# =========================================
# 📊 Chargement CSV robuste
# =========================================
def load_indicateurs_csv(filepath):

    header_line = detect_header_line(filepath)

    df = pd.read_csv(
        filepath,
        sep=";",
        skiprows=header_line,
        encoding="utf-8",
        engine="python",
        dtype={"Code Association": str}
    )

    df.columns = [c.strip() for c in df.columns]

    write_log(f"📊 Colonnes détectées : {df.columns.tolist()}")

    return df


# =========================================
# 🔍 Trouver colonne statut dynamiquement
# =========================================
def find_statut_column(df, periode):

    periode_clean = periode.strip().lower()

    for col in df.columns:
        col_clean = col.strip().lower()

        if col_clean.endswith("- statut") and periode_clean in col_clean:
            return col

    return None


# =========================================
# 🧠 Construire index CSV (clé du système)
# =========================================
def build_csv_index(df, colonne_statut):

    index = {}

    for _, row in df.iterrows():

        code_raw = row.get("Code Association", "")
        code = normalize_code(code_raw).lstrip("0")

        # write_log(f"CSV code brut: {code_raw} → normalisé: {code}")

        # 🔥 IGNORER les lignes invalides
        if not code:
            continue

        statut = str(row.get(colonne_statut, "")).strip()

        index[code] = statut

    write_log(f"📊 Index CSV construit : {len(index)} entrées")

    return index


# =========================================
# 📌 Écran 1 : Création campagne
# =========================================
@indicateurs_bp.route("/indicateurs", methods=["GET", "POST"])
@login_required
@require_access("indicateurs", "lecture")
def index():

    from datetime import datetime

    annee_now = datetime.now().year

    periodes = [
        f"T1 {annee_now}",
        f"T2 {annee_now}",
        f"T3 {annee_now}",
    ]

    # =========================================================
    # 🔹 GET
    # =========================================================
    if request.method == "GET":
        return render_template(
            "indicateurs/index.html",
            periodes=periodes
        )

    # =========================================================
    # 🔹 POST
    # =========================================================
    periode = request.form.get("periode")
    date_limite = request.form.get("date_limite")
    fichier = request.files.get("csv_file")
    action = request.form.get("action")  # reload / use / None

    if not periode:
        flash("⛔ Période obligatoire", "danger")
        return redirect(url_for("indicateurs.index"))

    # =========================================================
    # 🔥 EXTRACTION ANNEE / TRIMESTRE
    # =========================================================
    try:
        if periode.startswith("T"):
            trimestre = int(periode[1])
            annee = int(periode.split()[1])

        elif periode.lower().startswith("année"):
            trimestre = 4
            annee = int(periode.split()[1])

        else:
            raise ValueError("Format inconnu")

    except Exception:
        flash("⛔ Format période invalide", "danger")
        return redirect(url_for("indicateurs.index"))

    # =========================================================
    # 🔥 DB LOGIQUE PRINCIPALE
    # =========================================================
    with get_db_connection() as conn:
        cur = conn.cursor()

        # 🔎 recherche existant
        existing = cur.execute("""
            SELECT id FROM indicateurs_campagnes
            WHERE annee = ? AND trimestre = ?
        """, (annee, trimestre)).fetchone()

        # =====================================================
        # 🔥 CAS 1 : EXISTE → afficher modale
        # =====================================================
        if existing and not action:

            campagne = cur.execute("""
                SELECT * FROM indicateurs_campagnes
                WHERE id = ?
            """, (existing["id"],)).fetchone()

            return render_template(
                "indicateurs/index.html",
                periodes=periodes,
                show_modal=True,
                existing_id=existing["id"],
                periode=periode,
                date_limite=date_limite,
                campagne=campagne
            )

        # =====================================================
        # 🔥 CAS 2 : UTILISER EXISTANT
        # =====================================================
        if existing and action == "use":
            return redirect(url_for(
                "indicateurs.resultats",
                campagne_id=existing["id"]
            ))

        # =====================================================
        # 🔥 CAS 3 : RELOAD ou CREATION
        # =====================================================
        if existing and action == "reload":
            campagne_id = existing["id"]

            cur.execute("""
                DELETE FROM indicateurs_suivi
                WHERE campagne_id = ?
            """, (campagne_id,))

            write_log(f"♻️ Rechargement campagne {campagne_id}")

        else:
            cur.execute("""
                INSERT INTO indicateurs_campagnes
                (annee, trimestre, periode, date_limite, fichier_csv, date_creation)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (annee, trimestre, periode, date_limite, "", datetime.now()))

            campagne_id = cur.lastrowid

        # =====================================================
        # 🔥 UPLOAD CSV
        # =====================================================
        if not fichier:
            flash("⛔ Fichier CSV obligatoire", "danger")
            return redirect(url_for("indicateurs.index"))

        filename = secure_filename(fichier.filename)
        filepath = os.path.join(UPLOAD_DIR, filename)

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        fichier.save(filepath)

        # update campagne
        cur.execute("""
            UPDATE indicateurs_campagnes
            SET fichier_csv = ?, date_limite = ?, date_creation = ?, periode = ?
            WHERE id = ?
        """, (filepath, date_limite, datetime.now(), periode, campagne_id))

        conn.commit()

    # =========================================================
    # 🔥 FIN → TOUJOURS REDIRECT
    # =========================================================
    return redirect(url_for(
        "indicateurs.resultats",
        campagne_id=campagne_id
    ))

# =========================================
# 📊 Écran 2 : Résultats
# =========================================
@indicateurs_bp.route("/indicateurs/<int:campagne_id>")
@login_required
@require_access("indicateurs", "lecture")
def resultats(campagne_id):

    conn = get_db_connection()
    cur = conn.cursor()

    campagne = cur.execute("""
        SELECT * FROM indicateurs_campagnes WHERE id = ?
    """, (campagne_id,)).fetchone()

    lignes = cur.execute("""
        SELECT
            a.nom_association,
            a.code_VIF,
            s.statut_csv,
            s.present_csv
        FROM associations a
        LEFT JOIN indicateurs_suivi s
            ON s.association_id = a.id
            AND s.campagne_id = ?
        WHERE a.validite = 'oui'
        ORDER BY a.nom_association
    """, (campagne_id,)).fetchall()

    total = len(lignes)

    repondu = sum(
        1 for l in lignes
        if l["present_csv"] == 1 and l["statut_csv"] and l["statut_csv"].lower() == "validé"
    )

    non_repondu = sum(
        1 for l in lignes
        if l["present_csv"] == 1 and (not l["statut_csv"] or l["statut_csv"].lower() != "validé")
    )

    absent_ams = sum(
        1 for l in lignes
        if l["present_csv"] == 0
    )

    return render_template(
        "indicateurs/resultats.html",
        campagne=campagne,
        lignes=lignes,
        total=total,
        repondu=repondu,
        non_repondu=non_repondu
    )


from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from datetime import datetime
import os

def generate_pdf_indicateurs_trim(association, campagne, output_path):

    styles = getSampleStyleSheet()

    doc = SimpleDocTemplate(output_path, pagesize=A4)

    elements = []

    # 🧾 Titre
    elements.append(Paragraph(
        f"<b>Indicateurs d’Etat {campagne['periode']}</b>",
        styles["Title"]
    ))

    elements.append(Spacer(1, 20))

    # 📅 Date limite
    elements.append(Paragraph(
        f"Retour avant le <b>{campagne['date_limite']}</b>",
        styles["Normal"]
    ))

    elements.append(Spacer(1, 20))

    # 🏢 Association
    elements.append(Paragraph(
        f"<b>Association :</b> {association['nom_association']}",
        styles["Normal"]
    ))

    elements.append(Paragraph(
        f"<b>Code VIF :</b> {association['code_vif']}",
        styles["Normal"]
    ))

    elements.append(Spacer(1, 20))

    # 📩 Contact
    elements.append(Paragraph(
        f"<b>Email :</b> {association.get('courriel_resp_IE1', '')}",
        styles["Normal"]
    ))

    elements.append(Spacer(1, 40))

    # 📊 Champs à remplir
    elements.append(Paragraph("Nombre de foyers inscrits :", styles["Normal"]))
    elements.append(Spacer(1, 15))

    elements.append(Paragraph("Nombre de bénéficiaires :", styles["Normal"]))
    elements.append(Spacer(1, 15))

    elements.append(Paragraph("Nombre de passages :", styles["Normal"]))

    doc.build(elements)

    return output_path

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from datetime import datetime


def get_mois_trimestre(trimestre):
    mapping = {
        "T1": ("janvier", "février", "mars"),
        "T2": ("avril", "mai", "juin"),
        "T3": ("juillet", "août", "septembre"),
        "T4": ("octobre", "novembre", "décembre"),
    }
    return mapping.get(trimestre, ("", "", ""))


def generate_pdf_indicateurs_annuel(association, campagne, output_path):

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(output_path, pagesize=A4)

    elements = []

    # 🔍 Extraction période
    periode = campagne["periode"]  # ex: T1 2026
    parts = periode.split()
    trimestre = parts[0]
    annee = parts[1]

    mois1, mois2, mois3 = get_mois_trimestre(trimestre)

    # =========================================================
    # 🧾 TITRE
    # =========================================================
    elements.append(Paragraph(
        f"<b>Indicateurs d’Etat {trimestre} {annee}</b>",
        styles["Title"]
    ))

    elements.append(Spacer(1, 10))

    elements.append(Paragraph(
        f"Retour avant le <b>{campagne['date_limite']}</b>",
        styles["Normal"]
    ))

    elements.append(Spacer(1, 15))

    # =========================================================
    # 🏢 ASSOCIATION
    # =========================================================
    elements.append(Paragraph(
        f"<b>Association :</b> {association['nom_association']}",
        styles["Normal"]
    ))

    elements.append(Paragraph(
        f"<b>Code VIF :</b> {association['code_vif']}",
        styles["Normal"]
    ))

    elements.append(Paragraph(
        f"<b>Responsable IE :</b> {association.get('responsable_IE', '')}",
        styles["Normal"]
    ))

    elements.append(Paragraph(
        f"<b>Téléphone :</b> {association.get('tel_resp_IE', '')}",
        styles["Normal"]
    ))

    elements.append(Paragraph(
        f"<b>Email :</b> {association.get('courriel_resp_IE1', '')} ; {association.get('courriel_resp_IE2', '')}",
        styles["Normal"]
    ))

    elements.append(Spacer(1, 15))

    # =========================================================
    # 📊 PARTIE TRIMESTRIELLE
    # =========================================================
    elements.append(Paragraph("<b>TRIMESTRE</b>", styles["Heading2"]))

    elements.append(Paragraph(
        f"Calcul foyers : {mois1} + nouveaux {mois2} + nouveaux {mois3}",
        styles["Normal"]
    ))

    elements.append(Paragraph("Nombre de foyers inscrits : ____________", styles["Normal"]))
    elements.append(Paragraph("Nombre de bénéficiaires : ____________", styles["Normal"]))
    elements.append(Paragraph("Nombre de passages : ____________", styles["Normal"]))

    elements.append(Spacer(1, 15))

    # =========================================================
    # 📊 PARTIE ANNUELLE
    # =========================================================
    elements.append(Paragraph("<b>ANNUEL</b>", styles["Heading2"]))

    elements.append(Paragraph(
        "Foyers annuels (ne pas additionner les trimestres)",
        styles["Normal"]
    ))

    elements.append(Paragraph("Nombre de foyers annuels : ____________", styles["Normal"]))

    elements.append(Paragraph(
        "Bénéficiaires annuels (ne pas additionner les trimestres)",
        styles["Normal"]
    ))

    elements.append(Paragraph("Nombre de bénéficiaires annuels : ____________", styles["Normal"]))

    elements.append(Paragraph(
        "Total passages = somme des 4 trimestres",
        styles["Normal"]
    ))

    elements.append(Paragraph("Total passages annuels : ____________", styles["Normal"]))

    elements.append(Spacer(1, 20))

    # =========================================================
    # 📞 CONTACT
    # =========================================================
    elements.append(Paragraph(
        "Contact : ba380@banquealimentaire.org - 04 76 85 92 50",
        styles["Normal"]
    ))

    elements.append(Paragraph(
        "11 allée de la Pinéa - 38600 FONTAINE",
        styles["Normal"]
    ))

    doc.build(elements)

    return output_path
