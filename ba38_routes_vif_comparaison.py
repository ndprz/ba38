# routes_vif_comparaison.py
#
# Route Flask : comparaison planning VIF vs base association
#
# Fichiers VIF attendus dans VIF_FILES_DIR (configurable) :
#   - planning_partenaires.txt  (jours, fréquence, heure)
#   - planning_par_date.txt     (nbre bénéf, menu sec, menu frais)
#
# La DB contient une table `associations` avec les champs :
#   code_vif, nom_association,
#   besoins_particuliers, jour_de_passage_a_la_BAI, heure_de_passage,
#   emplacement, menu_sec, menu_frais, frequence, nbre_beneficiaires_vif

import os
import sqlite3
from datetime import datetime
from flask import Blueprint, render_template, current_app, request, jsonify
from flask_login import login_required

vif_bp = Blueprint("vif", __name__)

# Colonnes DB autorisées pour la mise à jour depuis VIF (whitelist anti-injection)
UPDATABLE_FIELDS = {
    "jour_de_passage_a_la_BAI": str,
    "menu_sec":                 str,
    "menu_frais":               str,
    "frequence":                str,
    "nbre_beneficiaires_vif":   int,
}

# ─────────────────────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────────────────────

# Préfixes qui identifient un menu « frais »
FRAIS_PREFIXES = ("FRAIS", "FR-", "FESOPE", "VIAND")


def _est_frais(menu: str) -> bool:
    return any(menu.startswith(p) for p in FRAIS_PREFIXES)


# ─────────────────────────────────────────────────────────────
# PARSEURS
# ─────────────────────────────────────────────────────────────

def parser_partenaires(chemin: str) -> dict:
    """
    Lit planning_partenaires.txt (encodage ISO-8859-1).

    Retourne un dict  code -> {name, jours, frequence, heure}

    Structure du fichier :
        Client :\\t01380001\\tAccueil SDF
        Utilise un planning depuis le …
        Heure\\tJour de commande\\tPériodicité\\tMode de passation\\tTélévendeur
        0\\tMardi\\t\\t7\\tAppel télévente\\tP\\tPlanning
        0\\t\\t\\t7\\t…
        …
    """
    clients = {}
    current = None
    header_found = False

    with open(chemin, encoding="iso-8859-1") as f:
        for line in f:
            ls = line.rstrip("\n")
            parts = ls.split("\t")

            if "Client :" in ls and len(parts) >= 3:
                code = parts[1].strip()
                name = parts[2].strip()
                current = code
                clients[code] = {"name": name, "rows": []}
                header_found = False

            elif current and "Heure" in ls and "Jour" in ls:
                header_found = True

            elif (
                current
                and header_found
                and ls.strip()
                and "Client :" not in ls
                and "Utilise" not in ls
            ):
                clients[current]["rows"].append(parts)

    results = {}
    for code, data in clients.items():
        jours = []
        periodicites = set()
        heure = None

        for row in data["rows"]:
            h     = row[0].strip() if len(row) > 0 else ""
            jour  = row[1].strip() if len(row) > 1 else ""
            perio = row[3].strip() if len(row) > 3 else ""

            if jour and jour not in jours:
                jours.append(jour)
            if perio and perio.isdigit():
                periodicites.add(int(perio))
            if heure is None and h.isdigit():
                heure = int(h)

        # Fréquence : toutes les périodicités sont 7 (hebdo)
        nb = len(jours)
        if 7 in periodicites:
            if nb == 1:
                frequence = "Hebdomadaire"
            elif nb == 2:
                frequence = "2x/semaine"
            elif nb >= 3:
                frequence = f"{nb}x/semaine"
            else:
                frequence = "Hebdomadaire"
        else:
            frequence = "/".join(str(p) for p in sorted(periodicites)) + "j" if periodicites else "—"

        results[code] = {
            "name": data["name"],
            "jours": jours,
            "frequence": frequence,
            "heure": heure,
        }

    return results


def parser_par_date(chemin: str) -> dict:
    """
    Lit planning_par_date.txt (encodage ISO-8859-1).

    Retourne un dict  code -> {nbre_benef, menus_sec, menus_frais}

    Structure :
        Partenaire : \\t01380001 - Accueil SDF
        Nbre Bénéf : \\t70
        Date d'enlèvement\\tOrdre\\tMenu type
        07/07/2026\\t0\\tSEC-BB
        \\t1\\tFRAIS
    """
    with open(chemin, encoding="iso-8859-1") as f:
        content = f.read()

    results = {}
    blocks = content.strip().split("\n\n")

    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue

        code = None
        nbre = None
        menus_sec = []
        menus_frais = []
        prochaine_date = None

        for line in lines:
            parts = line.split("\t")

            if line.startswith("Partenaire :") and len(parts) >= 2:
                rest = parts[1].strip()
                dash = rest.find(" - ")
                if dash >= 0:
                    code = rest[:dash].strip()

            elif line.startswith("Nbre Bénéf :") and len(parts) >= 2:
                nbre = parts[1].strip()

            elif (line[0].isdigit() or line[0] == "\t") and len(parts) >= 3:
                if line[0].isdigit() and prochaine_date is None:
                    try:
                        prochaine_date = datetime.strptime(parts[0].strip(), "%d/%m/%Y")
                    except ValueError:
                        pass
                menu = parts[2].strip()
                if menu and menu != "Menu type":
                    if _est_frais(menu):
                        if menu not in menus_frais:
                            menus_frais.append(menu)
                    else:
                        if menu not in menus_sec:
                            menus_sec.append(menu)

        if code:
            results[code] = {
                "nbre_benef": int(nbre) if (nbre and nbre.isdigit()) else None,
                "menus_sec": ", ".join(menus_sec),
                "menus_frais": ", ".join(menus_frais),
                "prochaine_date": prochaine_date,
            }

    return results


# ─────────────────────────────────────────────────────────────
# ROUTE
# ─────────────────────────────────────────────────────────────

@vif_bp.route("/vif/comparaison")
@login_required
def comparaison_vif():
    """
    Affiche le tableau de comparaison VIF / base association.

    Les fichiers VIF sont lus depuis le répertoire configuré dans
    current_app.config["VIF_FILES_DIR"]   (ex: /srv/uploads/vif/)
    ou, par défaut, dans le dossier instance/ de l'application.
    """

    # ── 1. Lire les fichiers VIF ──────────────────────────────
    vif_dir = current_app.config.get(
        "VIF_FILES_DIR",
        os.path.join(current_app.root_path, "uploads", "comparaison_vif")
    )
    chemin_partenaires = os.path.join(vif_dir, "planning_partenaires.txt")
    chemin_par_date    = os.path.join(vif_dir, "planning_par_date.txt")

    def _date_fichier(chemin):
        if os.path.exists(chemin):
            return datetime.fromtimestamp(os.path.getmtime(chemin)).strftime("%-d/%m/%Y")
        return None

    date_partenaires = _date_fichier(chemin_partenaires)
    date_par_date    = _date_fichier(chemin_par_date)

    vif_partenaires = {}
    vif_dates = {}
    erreur_vif = None
    fichiers_vif_absents = not (
        os.path.exists(chemin_partenaires) and os.path.exists(chemin_par_date)
    )

    try:
        if os.path.exists(chemin_partenaires):
            vif_partenaires = parser_partenaires(chemin_partenaires)
        if os.path.exists(chemin_par_date):
            vif_dates = parser_par_date(chemin_par_date)
    except Exception as e:
        erreur_vif = str(e)

    # ── 2. Lire la base association ───────────────────────────
    #
    # Table attendue : associations
    # Colonnes : code_vif, nom_association,
    #            besoins_particuliers, jour_de_passage_a_la_BAI,
    #            heure_de_passage, emplacement,
    #            menu_sec, menu_frais, frequence, nbre_beneficiaires_vif
    #
    # Adaptez get_db_path() / la requête à votre schéma réel.
    # ─────────────────────────────────────────────────────────
    from utils import get_db_path  # ou votre propre helper
    db_path = get_db_path()

    rows_db = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows_db = conn.execute("""
            SELECT
                Id,
                code_vif,
                nom_association,
                besoins_particuliers,
                jour_de_passage_a_la_BAI,
                heure_de_passage,
                emplacement,
                menu_sec,
                menu_frais,
                frequence,
                nbre_beneficiaires_vif,
                ignorer_differences_planif_vif
            FROM associations
            WHERE validite = "oui"
            ORDER BY code_vif
        """).fetchall()

    # ── 3. Fusionner ─────────────────────────────────────────
    rows = []
    for db in rows_db:
        code = db["code_vif"]

        vp = vif_partenaires.get(code, {})   # données planning partenaires
        vd = vif_dates.get(code, {})          # données planning par date

        absent_vif   = (code not in vif_partenaires and code not in vif_dates)
        en_vif_dates = code in vif_dates   # présent dans planning_par_date

        # ── Détection des différences ──
        base_jour      = db["jour_de_passage_a_la_BAI"]
        vif_jours      = vp.get("jours", [])
        base_jours_set = set(j.strip() for j in base_jour.split(",")) if base_jour else set()
        vif_jours_set  = set(vif_jours)
        diff_jour      = bool(vif_jours_set and base_jours_set != vif_jours_set)

        base_heure  = db["heure_de_passage"]
        vif_heure   = vp.get("heure")
        diff_heure  = (
            vif_heure is not None
            and (base_heure is None or str(base_heure) != str(vif_heure))
        )

        base_msec   = db["menu_sec"]
        vif_msec    = vd.get("menus_sec", "")
        # diff si l'asso est dans VIF dates ET les menus diffèrent (y compris quand VIF est vide)
        diff_msec   = bool(
            en_vif_dates
            and (vif_msec or base_msec)
            and vif_msec != (base_msec or "")
        )

        base_mfrais = db["menu_frais"]
        vif_mfrais  = vd.get("menus_frais", "")
        diff_mfrais = bool(
            en_vif_dates
            and (vif_mfrais or base_mfrais)
            and vif_mfrais != (base_mfrais or "")
        )

        base_freq   = db["frequence"]
        vif_freq    = vp.get("frequence", "")
        if vif_freq == "14j":
            prochaine = vd.get("prochaine_date")
            if prochaine:
                semaine = prochaine.isocalendar()[1]
                vif_freq = "14j pair" if semaine % 2 == 0 else "14j impair"
        diff_freq   = bool(vif_freq and (not base_freq or base_freq != vif_freq))

        base_benef  = db["nbre_beneficiaires_vif"]
        vif_benef   = vd.get("nbre_benef")
        diff_benef  = (
            vif_benef is not None
            and (base_benef is None or str(base_benef) != str(vif_benef))
        )

        has_diff = any([
            diff_jour, diff_msec,
            diff_mfrais, diff_freq, diff_benef,
        ])

        vif_nom  = vp.get("name", "")
        diff_nom = bool(
            vif_nom
            and not absent_vif
            and vif_nom.strip().lower() != (db["nom_association"] or "").strip().lower()
        )

        rows.append({
            # Identité
            "id_assoc": db["Id"],
            "code":    code,
            "nom_association": db["nom_association"],
            "vif_nom":  vif_nom,
            "diff_nom": diff_nom,
            # Base asso
            "base_besoins_particuliers": db["besoins_particuliers"],
            "base_jour_passage":         base_jour,
            "base_heure_passage":        base_heure,
            "base_emplacement":          db["emplacement"],
            "base_menu_sec":             base_msec,
            "base_menu_frais":           base_mfrais,
            "base_frequence":            base_freq,
            "base_nbre_benef":           base_benef,
            # VIF
            "vif_jours":     ", ".join(vif_jours),
            "vif_heure":     vif_heure,
            "vif_menu_sec":  vif_msec,
            "vif_menu_frais": vif_mfrais,
            "vif_frequence": vif_freq,
            "vif_nbre_benef": vif_benef,
            # Flags
            "absent_vif":    absent_vif,
            "has_diff":      has_diff,
            "diff_jour":     diff_jour,
            "diff_heure":    diff_heure,
            "diff_msec":     diff_msec,
            "diff_mfrais":   diff_mfrais,
            "diff_freq":     diff_freq,
            "diff_benef":    diff_benef,
            "ignorer_vif":   (db["ignorer_differences_planif_vif"] or "").lower() == "oui",
        })

    return render_template(
        "comparaison_vif.html",
        rows=rows,
        erreur_vif=erreur_vif,
        fichiers_vif_absents=fichiers_vif_absents,
        vif_dir=vif_dir,
        date_partenaires=date_partenaires,
        date_par_date=date_par_date,
    )


@vif_bp.route("/vif/upload_fichiers", methods=["POST"])
@login_required
def upload_vif_fichiers():
    from utils import has_access
    if not has_access("associations", "ecriture"):
        return jsonify({"success": False, "error": "Accès refusé"}), 403

    vif_dir = current_app.config.get(
        "VIF_FILES_DIR",
        os.path.join(current_app.root_path, "uploads", "comparaison_vif")
    )
    os.makedirs(vif_dir, exist_ok=True)

    FICHIERS_ATTENDUS = {
        "planning_partenaires": "planning_partenaires.txt",
        "planning_par_date":    "planning_par_date.txt",
    }

    sauvegardes = []
    for champ, nom_cible in FICHIERS_ATTENDUS.items():
        f = request.files.get(champ)
        if not f or not f.filename:
            continue
        if not f.filename.lower().endswith(".txt"):
            return jsonify({"success": False,
                            "error": f"Fichier « {f.filename} » refusé : seuls les .txt sont acceptés"}), 400
        f.save(os.path.join(vif_dir, nom_cible))
        sauvegardes.append(nom_cible)

    if not sauvegardes:
        return jsonify({"success": False, "error": "Aucun fichier reçu"}), 400

    return jsonify({"success": True, "sauvegardes": sauvegardes})


@vif_bp.route("/vif/update_field", methods=["POST"])
@login_required
def update_vif_field():
    from utils import get_db_path, has_access
    if not has_access("associations", "ecriture"):
        return jsonify({"success": False, "error": "Accès refusé"}), 403

    data = request.get_json(force=True) or {}
    assoc_id  = data.get("assoc_id")
    db_column = data.get("db_column")
    value     = data.get("value")

    if db_column not in UPDATABLE_FIELDS:
        return jsonify({"success": False, "error": "Colonne non autorisée"}), 400

    if not isinstance(assoc_id, int):
        return jsonify({"success": False, "error": "Id invalide"}), 400

    if value is not None:
        try:
            value = UPDATABLE_FIELDS[db_column](value)
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "Valeur invalide"}), 400

    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE associations SET {db_column} = ? WHERE Id = ?",
            (value, assoc_id),
        )
        conn.commit()

    return jsonify({"success": True})
