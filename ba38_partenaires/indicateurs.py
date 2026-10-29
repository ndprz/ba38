# =========================================
# 📊 Module Indicateurs
# =========================================

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_login import login_required
from ba38_utilitaires.core import get_db_connection, write_log, require_access, get_db_path
from ba38_partenaires.indicateurs_coherence import (
    controle_coherence, calculer_propositions, generer_pdf_propositions,
    lignes_incoherentes, generer_pdf_incoherences,
)
import os
import pandas as pd
import sqlite3
from datetime import datetime
from werkzeug.utils import secure_filename

indicateurs_bp = Blueprint("indicateurs", __name__)

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
# 📦 Import tonnage VIF (Kg Brut par association)
# =========================================
def parser_tonnage_vif(contenu):
    """
    Parse un export VIF de tonnages (cp1252, tabulations) : après la ligne
    d'en-tête "Client\tLivré à (Libellé)\tKg Net\tKg Brut", une ligne par
    association "code_VIF\tnom\tkg_net\tkg_brut", jusqu'à une ligne vide ou
    "TOTAL GENERAL". Contrairement au CSV AMS (statut), le code ici est déjà
    au format exact de associations.code_VIF (zéros de tête inclus) — pas de
    lstrip("0"). Si plusieurs sections "Etat du BL" contiennent le même
    code (ex. "A facturer" + "Facturé"), les montants sont additionnés.
    On retient le Kg BRUT (choix BA, 2026-10-29) : colonne repérée par son
    titre dans l'en-tête, 4e colonne à défaut. Stocké dans tonnage_kg_net
    (nom de colonne historique, conservé pour éviter une migration).
    Retourne {code_vif_normalisé: kg_brut (float)}.
    """
    tonnages = {}
    dans_section = False
    idx_kg = 3

    for ligne in contenu.splitlines():
        cols = ligne.split("\t")
        premiere_col = cols[0].strip()

        if premiere_col == "Client":
            dans_section = True
            titres = [c.strip().lower() for c in cols]
            idx_kg = titres.index("kg brut") if "kg brut" in titres else 3
            continue

        if not dans_section:
            continue

        if not ligne.strip() or premiere_col.upper().startswith("TOTAL GENERAL"):
            dans_section = False
            continue

        if len(cols) <= idx_kg:
            continue

        code_vif = normalize_code(premiere_col)
        kg_str = cols[idx_kg].strip().replace(" ", "").replace(",", ".")

        try:
            kg = float(kg_str)
        except ValueError:
            continue

        tonnages[code_vif] = tonnages.get(code_vif, 0) + kg

    return tonnages



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
# 📈 Valeurs AMS de la période (retenues BA)
# =========================================
# indicateur AMS → colonne indicateurs_suivi ; on prend la valeur
# « retenue BA » (valeur définitive : les valeurs calculées/retenues Asso
# restent à 0 quand c'est la BA qui saisit pour l'association).
AMS_INDICATEURS = [
    ("volumes distribué en kg net", "ams_volume_kg", float),
    ("nb foyers", "ams_nb_foyers", int),
    ("nb bénéficiaires", "ams_nb_personnes", int),
    ("nb passages", "ams_nb_passages", int),
]
AMS_MOTIFS = [
    ("motif écart asso", "ams_motif_ecart_asso"),
    ("motif écart ba", "ams_motif_ecart_ba"),
]


def _norm_col(col):
    return " ".join(str(col).replace("-", " - ").split()).lower()


def _nombre_ams(valeur, conv):
    if valeur is None or pd.isna(valeur):
        return None
    texte = str(valeur).replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not texte:
        return None
    try:
        return conv(float(texte))
    except ValueError:
        return None


def build_valeurs_ams(df, periode):
    """code VIF normalisé → {colonne indicateurs_suivi: valeur} pour la période."""
    prefixe = _norm_col(periode) + " - "
    colonnes = {}
    for col in df.columns:
        n = _norm_col(col)
        if not n.startswith(prefixe):
            continue
        reste = n[len(prefixe):]
        for libelle, cible, _ in AMS_INDICATEURS:
            if reste.startswith(libelle + " - ") and reste.endswith("valeurs retenues ba"):
                colonnes[cible] = col
        for libelle, cible in AMS_MOTIFS:
            if reste == libelle:
                colonnes[cible] = col

    write_log(f"📊 Colonnes AMS retenues pour {periode} : {colonnes}")

    conversions = {cible: conv for _, cible, conv in AMS_INDICATEURS}
    index = {}
    for _, row in df.iterrows():
        code = normalize_code(row.get("Code Association", "")).lstrip("0")
        if not code:
            continue
        valeurs = {}
        for cible, col in colonnes.items():
            v = row.get(col)
            if cible in conversions:
                valeurs[cible] = _nombre_ams(v, conversions[cible])
            else:
                valeurs[cible] = None if v is None or pd.isna(v) or not str(v).strip() else str(v).strip()
        index[code] = valeurs
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

            if date_limite:
                cur.execute("""
                    UPDATE indicateurs_campagnes
                    SET date_limite = ?
                    WHERE id = ?
                """, (date_limite, campagne_id))

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
        valeurs_ams = build_valeurs_ams(df, periode)

        associations = cur.execute("""
            SELECT id, code_VIF, exclusion_mails_indicateurs
            FROM associations
            WHERE validite = 'oui'
        """).fetchall()

        codes_db = {normalize_code(a["code_VIF"]).lstrip("0") for a in associations}

        count_insert = 0

        for assoc in associations:

            code_vif = normalize_code(assoc["code_VIF"]).lstrip("0")

            statut = index_csv.get(code_vif, "")
            statut = statut.strip()

            present = 1 if statut != "" else 0

            if not statut:
                write_log(f"⚠️ Code absent du CSV : {code_vif}")

            # Exclusion par défaut : reprise du réglage permanent de la fiche
            # partenaire (associations.exclusion_mails_indicateurs), utilisé
            # uniquement à la création de la ligne de suivi (nouvelle campagne).
            exclu_par_defaut = 1 if assoc["exclusion_mails_indicateurs"] == "oui" else 0
            ams = valeurs_ams.get(code_vif, {})

            # UPSERT : si une ligne existe déjà pour cette campagne+association
            # (rechargement d'un CSV plus récent), on ne touche qu'au statut/
            # présence issus du CSV — on préserve exclure_envoi_mail et tout
            # l'historique d'envoi (mail_envoye_le, statut Mailjet, renvois
            # Gmail...), qui ne doivent jamais être perdus par un rechargement.
            cur.execute("""
                INSERT INTO indicateurs_suivi
                (campagne_id, association_id, statut_csv, present_csv, date_import, exclure_envoi_mail,
                 ams_volume_kg, ams_nb_foyers, ams_nb_personnes, ams_nb_passages,
                 ams_motif_ecart_asso, ams_motif_ecart_ba)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campagne_id, association_id) DO UPDATE SET
                    statut_csv = excluded.statut_csv,
                    present_csv = excluded.present_csv,
                    date_import = excluded.date_import,
                    ams_volume_kg = excluded.ams_volume_kg,
                    ams_nb_foyers = excluded.ams_nb_foyers,
                    ams_nb_personnes = excluded.ams_nb_personnes,
                    ams_nb_passages = excluded.ams_nb_passages,
                    ams_motif_ecart_asso = excluded.ams_motif_ecart_asso,
                    ams_motif_ecart_ba = excluded.ams_motif_ecart_ba
            """, (
                campagne_id,
                assoc["id"],
                statut,
                present,
                datetime.now(),
                exclu_par_defaut,
                ams.get("ams_volume_kg"),
                ams.get("ams_nb_foyers"),
                ams.get("ams_nb_personnes"),
                ams.get("ams_nb_passages"),
                ams.get("ams_motif_ecart_asso"),
                ams.get("ams_motif_ecart_ba"),
            ))

            count_insert += 1

        # Codes présents dans le CSV mais absents de notre base
        codes_csv_inconnus = sorted(set(index_csv.keys()) - codes_db)
        if codes_csv_inconnus:
            msg = "⚠️ Codes VIF du CSV non trouvés dans notre base : " + ", ".join(codes_csv_inconnus)
            write_log(msg)
            flash(msg, "warning")

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
            a.code_VIF AS code_vif,
            a.courriel_resp_IE1,
            a.courriel_resp_IE2,
            a.courriel_association,
            a.nbre_beneficiaires_vif,
            s.id AS suivi_id,
            s.statut_csv,
            s.present_csv,
            s.exclure_envoi_mail,
            s.mail_envoye_le,
            s.mail_mode_test,
            s.mail_erreur,
            s.mail_statut_final,
            s.mail_statut_verifie_le,
            s.mail_modele_id,
            s.mail_renvoi_gmail_le,
            s.tonnage_kg_net,
            s.ams_volume_kg,
            s.ams_nb_foyers,
            s.ams_nb_personnes,
            s.ams_nb_passages,
            s.ams_motif_ecart_asso,
            s.ams_motif_ecart_ba
        FROM associations a
        LEFT JOIN indicateurs_suivi s
            ON s.association_id = a.id
            AND s.campagne_id = ?
        WHERE a.validite = 'oui'
        ORDER BY a.nom_association
    """, (campagne_id,)).fetchall()

    # Pastille de cohérence des valeurs AMS (vert/orange/rouge + détail au survol)
    lignes_dicts = []
    for l in lignes:
        d = dict(l)
        d["coherence"], d["coherence_messages"] = controle_coherence(d, campagne["periode"])
        lignes_dicts.append(d)
    lignes = lignes_dicts

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

    type_periode = "annuel" if campagne["periode"].lower().startswith("année") else "trimestriel"
    modeles = cur.execute("""
        SELECT * FROM modeles_emails WHERE type_periode = ? ORDER BY TRIM(code_modele) COLLATE NOCASE
    """, (type_periode,)).fetchall()

    return render_template(
        "indicateurs/resultats.html",
        campagne=campagne,
        lignes=lignes,
        total=total,
        repondu=repondu,
        non_repondu=non_repondu,
        modeles=modeles
    )


@indicateurs_bp.route("/indicateurs/proposition_non_repondus/<int:campagne_id>.pdf")
@login_required
@require_access("indicateurs", "lecture")
def proposition_non_repondus_pdf(campagne_id):
    """État PDF des indicateurs proposés (calculés depuis le tonnage VIF) pour
    les associations n'ayant pas répondu, à saisir manuellement dans AMS."""
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM indicateurs_campagnes WHERE id = ?", (campagne_id,)
        ).fetchone()
        if not campagne:
            flash("❌ Campagne introuvable", "danger")
            return redirect(url_for("indicateurs.index"))
        propositions, ratio_defaut = calculer_propositions(conn, campagne_id, campagne["periode"])

    pdf = generer_pdf_propositions(campagne, propositions, ratio_defaut)
    nom = f"Proposition_indicateurs_{campagne['periode'].replace(' ', '_')}_non_repondus.pdf"
    return send_file(pdf, mimetype="application/pdf", download_name=nom, as_attachment=False)


@indicateurs_bp.route("/indicateurs/incoherences/<int:campagne_id>.pdf")
@login_required
@require_access("indicateurs", "lecture")
def incoherences_pdf(campagne_id):
    """État PDF des associations dont les indicateurs AMS sont incohérents (pastille orange ou rouge)."""
    with get_db_connection() as conn:
        campagne = conn.execute(
            "SELECT * FROM indicateurs_campagnes WHERE id = ?", (campagne_id,)
        ).fetchone()
        if not campagne:
            flash("❌ Campagne introuvable", "danger")
            return redirect(url_for("indicateurs.index"))
        lignes = lignes_incoherentes(conn, campagne_id, campagne["periode"])

    pdf = generer_pdf_incoherences(campagne, lignes)
    nom = f"Incoherences_indicateurs_{campagne['periode'].replace(' ', '_')}.pdf"
    return send_file(pdf, mimetype="application/pdf", download_name=nom, as_attachment=False)


@indicateurs_bp.route("/indicateurs/importer_tonnage/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("indicateurs", "ecriture")
def importer_tonnage(campagne_id):
    """
    Importe un export VIF de tonnages (Kg Brut par association) pour cette
    campagne — colonne "Tonnage Trimestre" du tableau Résultats. N'écrase
    que tonnage_kg_net, préserve tout le reste (statut, historique d'envoi).
    """
    fichier = request.files.get("tonnage_file")
    if not fichier or not fichier.filename:
        flash("⛔ Fichier tonnage manquant", "danger")
        return redirect(url_for("indicateurs.resultats", campagne_id=campagne_id))

    filename = secure_filename(fichier.filename)
    filepath = os.path.join(UPLOAD_DIR, filename)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fichier.save(filepath)

    with open(filepath, "rb") as f:
        contenu_bytes = f.read()
    try:
        contenu = contenu_bytes.decode("utf-8")
    except UnicodeDecodeError:
        contenu = contenu_bytes.decode("cp1252")

    tonnages = parser_tonnage_vif(contenu)

    if not tonnages:
        flash("⛔ Aucune ligne de tonnage détectée dans le fichier — vérifiez le format.", "danger")
        return redirect(url_for("indicateurs.resultats", campagne_id=campagne_id))

    with get_db_connection() as conn:
        associations = conn.execute(
            "SELECT id, code_VIF FROM associations WHERE validite = 'oui'"
        ).fetchall()

        codes_db = {}
        nb_maj = 0
        for assoc in associations:
            code_vif = normalize_code(assoc["code_VIF"])
            codes_db[code_vif] = True
            if code_vif not in tonnages:
                continue
            conn.execute("""
                INSERT INTO indicateurs_suivi (campagne_id, association_id, tonnage_kg_net)
                VALUES (?, ?, ?)
                ON CONFLICT(campagne_id, association_id) DO UPDATE SET
                    tonnage_kg_net = excluded.tonnage_kg_net
            """, (campagne_id, assoc["id"], tonnages[code_vif]))
            nb_maj += 1

        conn.commit()

    codes_inconnus = sorted(set(tonnages.keys()) - set(codes_db.keys()))
    if codes_inconnus:
        flash(
            "⚠️ Codes VIF du fichier tonnage non trouvés dans notre base : " + ", ".join(codes_inconnus),
            "warning"
        )

    flash(f"📦 Tonnage importé pour {nb_maj} association(s).", "success")
    return redirect(url_for("indicateurs.resultats", campagne_id=campagne_id))


@indicateurs_bp.route("/indicateurs/voir_pdf/<int:suivi_id>")
@login_required
@require_access("indicateurs", "lecture")
def voir_pdf(suivi_id):
    """
    Régénère à la volée et affiche le PDF indicateurs d'une association
    (même PDF que celui joint au mail envoyé/à envoyer) — pas de fichier
    persisté, recalculé depuis la base à chaque consultation.
    """
    from ba38_utilitaires.core import get_templates_pdf_dir
    from ba38_utilitaires.pdf_form import remplir_pdf_indicateurs

    with get_db_connection() as conn:
        suivi = conn.execute("""
            SELECT s.*, a.nom_association, a.code_VIF AS code_vif,
                   a.responsable_IE, a.tel_resp_IE, a.CAR
            FROM indicateurs_suivi s
            JOIN associations a ON s.association_id = a.id
            WHERE s.id = ?
        """, (suivi_id,)).fetchone()

        if not suivi:
            return "Ligne introuvable", 404

        campagne = conn.execute(
            "SELECT * FROM indicateurs_campagnes WHERE id = ?", (suivi["campagne_id"],)
        ).fetchone()

    type_periode = "annuel" if campagne["periode"].lower().startswith("année") else "trimestriel"
    template = os.path.join(
        get_templates_pdf_dir(),
        "indicateurs_annuels.pdf" if type_periode == "annuel" else "indicateurs_trimestriels.pdf"
    )

    pdf_path = f"/tmp/indicateurs_voir_{suivi_id}.pdf"
    remplir_pdf_indicateurs(template, pdf_path, dict(suivi), campagne)

    return send_file(pdf_path, mimetype="application/pdf")




def get_mois_trimestre(trimestre):
    mapping = {
        "T1": ("janvier", "février", "mars"),
        "T2": ("avril", "mai", "juin"),
        "T3": ("juillet", "août", "septembre"),
        "T4": ("octobre", "novembre", "décembre"),
    }
    return mapping.get(trimestre, ("", "", ""))




@indicateurs_bp.route("/indicateurs/modifier_date_limite/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("indicateurs", "ecriture")
def modifier_date_limite(campagne_id):
    date_limite = request.form.get("date_limite", "").strip()
    date_limite_ams = request.form.get("date_limite_ams", "").strip()

    with get_db_connection() as conn:
        conn.execute("""
            UPDATE indicateurs_campagnes
            SET date_limite = ?, date_limite_ams = ?
            WHERE id = ?
        """, (date_limite, date_limite_ams or None, campagne_id))
        conn.commit()

    flash(f"📅 Dates limites mises à jour : retour assos {date_limite or '—'}, saisie AMS {date_limite_ams or '—'}", "success")
    return redirect(url_for("indicateurs.resultats", campagne_id=campagne_id))


@indicateurs_bp.route("/indicateurs/toggle_exclusion/<int:suivi_id>", methods=["POST"])
@login_required
@require_access("indicateurs", "ecriture")
def toggle_exclusion(suivi_id):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE indicateurs_suivi
            SET exclure_envoi_mail = CASE WHEN exclure_envoi_mail = 1 THEN 0 ELSE 1 END
            WHERE id = ?
        """, (suivi_id,))
        conn.commit()
        row = cur.execute(
            "SELECT exclure_envoi_mail FROM indicateurs_suivi WHERE id = ?", (suivi_id,)
        ).fetchone()
    return jsonify({"exclure": row["exclure_envoi_mail"]})


@indicateurs_bp.route("/indicateurs/verifier_statut_mailjet/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("indicateurs", "ecriture")
def verifier_statut_mailjet(campagne_id):
    from ba38_utilitaires.core import mailjet_get_message_status

    with get_db_connection() as conn:
        cur = conn.cursor()

        lignes = cur.execute("""
            SELECT id, mail_mailjet_message_ids
            FROM indicateurs_suivi
            WHERE campagne_id = ?
              AND mail_mailjet_message_ids IS NOT NULL
              AND mail_mailjet_message_ids != ''
        """, (campagne_id,)).fetchall()

        counts = {}
        verifies = 0

        for ligne in lignes:
            premier_id = ligne["mail_mailjet_message_ids"].split(",")[0]
            statut = mailjet_get_message_status(premier_id)

            if not statut:
                continue

            verifies += 1
            counts[statut] = counts.get(statut, 0) + 1

            cur.execute("""
                UPDATE indicateurs_suivi
                SET mail_statut_final = ?, mail_statut_verifie_le = ?
                WHERE id = ?
            """, (statut, datetime.now().isoformat(timespec="seconds"), ligne["id"]))

        conn.commit()

    if verifies:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        flash(f"🔄 Statut Mailjet vérifié pour {verifies} mail(s) : {detail}", "info")
    else:
        flash("ℹ️ Aucun mail avec un identifiant Mailjet à vérifier pour cette campagne.", "warning")

    return redirect(url_for("indicateurs.resultats", campagne_id=campagne_id))


@indicateurs_bp.route("/indicateurs/renvoyer_gmail/<int:suivi_id>", methods=["POST"])
@login_required
@require_access("indicateurs", "ecriture")
def renvoyer_gmail(suivi_id):
    """
    Envoie (ou renvoie) le mail indicateurs d'une association précise
    directement via l'API Gmail (compte ba380@banquealimentaire.org) — soit
    en contournement des rebonds temporaires Microsoft/mail.ru liés à
    l'absence d'authentification SPF/DKIM du domaine côté Mailjet (renvoi
    d'un mail déjà parti réellement, réutilise alors le même modèle que
    l'envoi initial), soit pour envoyer individuellement à une association
    qui n'a jamais reçu de mail réel pour cette campagne (modèle choisi
    explicitement dans le formulaire, via le paramètre modele_id).
    """
    from ba38_utilitaires.core import get_templates_pdf_dir, render_modele_email
    from ba38_utilitaires.pdf_form import remplir_pdf_indicateurs
    from ba38_utilitaires.gmail_send import envoyer_mail_gmail, GmailSendError

    with get_db_connection() as conn:
        suivi = conn.execute("""
            SELECT s.*, a.nom_association, a.code_VIF AS code_vif,
                   a.courriel_resp_IE1, a.courriel_resp_IE2, a.courriel_association,
                   a.responsable_IE, a.tel_resp_IE, a.CAR
            FROM indicateurs_suivi s
            JOIN associations a ON s.association_id = a.id
            WHERE s.id = ?
        """, (suivi_id,)).fetchone()

        if not suivi:
            flash("❌ Ligne introuvable", "danger")
            return redirect(url_for("indicateurs.index"))

        campagne = conn.execute(
            "SELECT * FROM indicateurs_campagnes WHERE id = ?", (suivi["campagne_id"],)
        ).fetchone()

        deja_envoye_reel = suivi["mail_envoye_le"] and not suivi["mail_mode_test"]
        modele_id_choisi = request.form.get("modele_id")
        modele_id = suivi["mail_modele_id"] if deja_envoye_reel else (
            int(modele_id_choisi) if modele_id_choisi else None
        )

        if not modele_id:
            flash("⛔ Choisissez un modèle de mail pour cet envoi.", "danger")
            return redirect(url_for("indicateurs.resultats", campagne_id=suivi["campagne_id"]))

        modele = conn.execute(
            "SELECT * FROM modeles_emails WHERE id = ?", (modele_id,)
        ).fetchone()

        if not modele:
            flash("❌ Modèle introuvable", "danger")
            return redirect(url_for("indicateurs.resultats", campagne_id=suivi["campagne_id"]))

        from ba38_utilitaires.core import emails_indicateurs, REPLY_TO_INDICATEURS
        emails = emails_indicateurs(suivi)
        if not emails:
            flash(f"AUCUN MAIL ENVOYÉ — aucune adresse email (ni resp. IE1/IE2, ni boîte générale) pour {suivi['nom_association']}", "danger")
            return redirect(url_for("indicateurs.resultats", campagne_id=suivi["campagne_id"]))

        periode = campagne["periode"]
        type_periode = "annuel" if periode.lower().startswith("année") else "trimestriel"

        pdf_path = f"/tmp/indicateurs_gmail_{suivi['association_id']}.pdf"
        template = os.path.join(
            get_templates_pdf_dir(),
            "indicateurs_annuels.pdf" if type_periode == "annuel" else "indicateurs_trimestriels.pdf"
        )
        remplir_pdf_indicateurs(template, pdf_path, dict(suivi), campagne)

        date_limite = campagne["date_limite"]
        contexte = {
            "nom_association": suivi["nom_association"],
            "periode": periode,
            "trimestre": periode.split()[0],
            "annee": periode.split()[1],
            "date_limite": date_limite,
        }

        sujet = render_modele_email(modele["sujet"], contexte).strip()
        corps = render_modele_email(modele["corps"], contexte)

        try:
            envoyer_mail_gmail(sujet=sujet, destinataires=emails, texte=corps, attachment_path=pdf_path,
                               reply_to=REPLY_TO_INDICATEURS)

            now_iso = datetime.now().isoformat(timespec="seconds")

            if deja_envoye_reel:
                # Renvoi d'un mail déjà réellement parti : on ne touche pas
                # à mail_envoye_le/mail_modele_id, on garde la trace de
                # l'envoi d'origine.
                conn.execute("""
                    UPDATE indicateurs_suivi
                    SET mail_renvoi_gmail_le = ?
                    WHERE id = ?
                """, (now_iso, suivi_id))
            else:
                # Premier envoi réel (jamais envoyé, ou envoyé seulement en
                # mode TEST jusqu'ici) : cet envoi devient le vrai.
                conn.execute("""
                    UPDATE indicateurs_suivi
                    SET mail_renvoi_gmail_le = ?,
                        mail_envoye_le = ?,
                        mail_mode_test = 0,
                        mail_modele_id = ?,
                        mail_erreur = NULL
                    WHERE id = ?
                """, (now_iso, now_iso, modele_id, suivi_id))
            conn.commit()

            verbe = "renvoyé" if deja_envoye_reel else "envoyé"
            flash(f"📧 Mail {verbe} via Gmail à {', '.join(emails)} pour {suivi['nom_association']}", "success")

        except GmailSendError as e:
            write_log(f"❌ Erreur renvoi Gmail pour {suivi['nom_association']} : {e}")
            flash(f"❌ Échec du renvoi via Gmail : {e}", "danger")

        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    return redirect(url_for("indicateurs.resultats", campagne_id=suivi["campagne_id"]))


@indicateurs_bp.route("/check_campagne")
@login_required
def check_campagne():

    periode = request.args.get("periode")

    db_path = get_db_path()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        campagne = cur.execute("""
            SELECT id, date_limite
            FROM indicateurs_campagnes
            WHERE periode = ?
        """, (periode,)).fetchone()

    return jsonify({
        "exists": campagne is not None,
        "id": campagne["id"] if campagne else None,
        "date_limite": campagne["date_limite"] if campagne else None
    })