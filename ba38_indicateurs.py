# =========================================
# 📊 Module Indicateurs
# =========================================

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required
from utils import get_db_connection, write_log, require_access, get_db_path
import os
import pandas as pd
import sqlite3
from datetime import datetime
from werkzeug.utils import secure_filename

indicateurs_bp = Blueprint(
    "indicateurs",
    __name__,
    template_folder="templates/indicateurs"
)

UPLOAD_DIR = os.getenv("UPLOAD_DIR_INDICATEURS", "/srv/ba38/uploads/indicateurs")


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
    # 🔥 DB + CSV EN UNE SEULE TRANSACTION (FIX LOCK SQLITE)
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

        # =====================================================
        # 📊 TRAITEMENT CSV
        # =====================================================
        df = load_indicateurs_csv(filepath)

        col_statut = find_statut_column(df, periode)

        if not col_statut:
            flash("⛔ Colonne statut introuvable dans le CSV", "danger")
            return redirect(url_for("indicateurs.index"))

        index_csv = build_csv_index(df, col_statut)

        # 🔥 sécurité anti doublons
        cur.execute("""
            DELETE FROM indicateurs_suivi
            WHERE campagne_id = ?
        """, (campagne_id,))

        associations = cur.execute("""
            SELECT id, code_VIF
            FROM associations
            WHERE validite = 'oui'
        """).fetchall()

        count_insert = 0

        for assoc in associations:

            code_vif = normalize_code(assoc["code_VIF"]).lstrip("0")

            statut = index_csv.get(code_vif, "")
            statut = statut.strip()

            present = 1 if statut != "" else 0

            if not statut:
                write_log(f"⚠️ Code absent du CSV : {code_vif}")

            cur.execute("""
                INSERT INTO indicateurs_suivi
                (campagne_id, association_id, statut_csv, present_csv, date_import)
                VALUES (?, ?, ?, ?, ?)
            """, (
                campagne_id,
                assoc["id"],
                statut,
                present,
                datetime.now()
            ))

            count_insert += 1

        write_log(f"📊 {count_insert} lignes insérées dans indicateurs_suivi")

        # =====================================================
        # 🔥 UPDATE CAMPAGNE
        # =====================================================
        cur.execute("""
            UPDATE indicateurs_campagnes
            SET fichier_csv = ?, date_limite = ?, date_creation = ?, periode = ?
            WHERE id = ?
        """, (filepath, date_limite, datetime.now(), periode, campagne_id))

        conn.commit()

        write_log(f"📊 {count_insert} lignes insérées dans indicateurs_suivi")

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






def get_mois_trimestre(trimestre):
    mapping = {
        "T1": ("janvier", "février", "mars"),
        "T2": ("avril", "mai", "juin"),
        "T3": ("juillet", "août", "septembre"),
        "T4": ("octobre", "novembre", "décembre"),
    }
    return mapping.get(trimestre, ("", "", ""))




@indicateurs_bp.route("/check_campagne")
@login_required
def check_campagne():

    periode = request.args.get("periode")

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        campagne = cur.execute("""
            SELECT id 
            FROM indicateurs_campagnes
            WHERE periode = ?
        """, (periode,)).fetchone()

    return jsonify({
        "exists": campagne is not None,
        "id": campagne["id"] if campagne else None
    })