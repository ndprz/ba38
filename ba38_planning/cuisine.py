# ba38_planning_cuisine.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from ba38_utilitaires.core import get_db_connection, write_log, upload_database, require_access
from ba38_planning.utils import get_lundi_de_la_semaine, parse_id, get_nom, get_type_benevole_options

planning_cuisine_bp = Blueprint('planning_cuisine', __name__)

# 🍽️ Créneaux du planning cuisine (jours en colonnes, pas de postes différenciés)
CRENEAUX = [
    ("salaries", "Salariés"),
    ("matin", "10h00 - 13h30"),
    ("apres_midi", "13h30 - 17h30"),
]
SLOTS_PAR_CRENEAU = {"salaries": 4, "matin": 12, "apres_midi": 12}
MAX_SLOTS = 12  # nombre de colonnes cuiXX en base


def get_benevoles_cuisine(cursor):
    """Bénévoles + salariés utilisables sur le planning cuisine.

    - equipe : tous les bénévoles, ceux marqués trois_etoiles='oui' en tête
      (créneaux matin / après-midi)
    - salaries : uniquement les salariés (type_benevole='salarie'), ceux
      marqués trois_etoiles='oui' en tête (créneau Salariés)
    """
    benevoles = cursor.execute("""
        SELECT id, nom, prenom, type_benevole, trois_etoiles
        FROM benevoles
        ORDER BY (LOWER(COALESCE(trois_etoiles,'')) != 'oui'), nom, prenom
    """).fetchall()
    benevoles = [dict(b) for b in benevoles]

    equipe = benevoles
    salaries = [b for b in benevoles if (b.get("type_benevole") or "").lower() == "salarie"]

    return equipe, salaries


def _jours_actifs(cursor):
    param = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'").fetchone()
    travail_vendredi = param and param[0].strip().lower() == "oui"
    jours = ["lundi", "mardi", "mercredi", "jeudi"]
    if travail_vendredi:
        jours.append("vendredi")
    return jours, travail_vendredi


@planning_cuisine_bp.route("/creation_planning_cuisine", methods=["GET", "POST"])
@login_required
@require_access("planning", "ecriture")
def creation_planning_cuisine():
    semaine_iso = request.form.get("semaine") or request.args.get("semaine")
    if not semaine_iso:
        return render_template("planning/cuisine/creation_planning_cuisine.html", semaine="")

    try:
        annee, numero_semaine = map(int, semaine_iso.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return render_template("planning/cuisine/creation_planning_cuisine.html", semaine=semaine_iso)

    action = request.form.get("action")
    lundi = get_lundi_de_la_semaine(semaine_iso)

    conn = get_db_connection()
    cursor = conn.cursor()

    jours, _ = _jours_actifs(cursor)
    jours_dates = {j: lundi + timedelta(i) for i, j in enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi"]) if j in jours}

    planning_existe = cursor.execute(
        "SELECT COUNT(*) FROM plannings_cuisine WHERE annee = ? AND semaine = ?", (annee, numero_semaine)
    ).fetchone()[0] > 0

    if planning_existe and action != "forcer_generation":
        conn.close()
        return render_template("planning/cuisine/creation_planning_cuisine.html",
                               semaine=semaine_iso,
                               planning_existe=True,
                               planning=[])

    if planning_existe:
        write_log(
            f"🔄 Régénération planning cuisine "
            f"S{numero_semaine}/{annee} par {current_user.username}"
        )
        cursor.execute(
            "DELETE FROM plannings_cuisine WHERE annee = ? AND semaine = ?",
            (annee, numero_semaine)
        )
    else:
        write_log(
            f"🆕 Création planning cuisine "
            f"S{numero_semaine}/{annee} par {current_user.username}"
        )

    modeles = cursor.execute("SELECT * FROM planning_standard_cuisine_ids").fetchall()
    if not modeles:
        conn.close()
        flash("⚠️ Aucun modèle de planning trouvé. Veuillez d’abord définir le modèle avant de générer un planning.", "warning")
        return render_template("planning/cuisine/creation_planning_cuisine.html", semaine=semaine_iso, planning_existe=False)

    absences = cursor.execute("SELECT benevole_id, date_debut, date_fin FROM absences").fetchall()
    absents_par_jour = {j: set() for j in jours_dates}
    for a in absences:
        try:
            debut = datetime.strptime(a["date_debut"], "%d/%m/%Y").date()
            fin = datetime.strptime(a["date_fin"], "%d/%m/%Y").date()
            for jour, date_obj in jours_dates.items():
                if debut <= date_obj <= fin:
                    absents_par_jour[jour].add(a["benevole_id"])
        except Exception:
            continue

    planning = []
    for modele in modeles:
        jour = modele["jour"].lower()
        if jour not in jours:
            continue
        creneau = modele["creneau"]

        ligne = {"jour": jour, "creneau": creneau, "semaine": numero_semaine}
        for i in range(1, MAX_SLOTS + 1):
            champ = f"cui{str(i).zfill(2)}"
            champ_id = f"{champ}_id"
            champ_abs = f"{champ}_absent"
            champ_remp = f"{champ}_remplacant"

            bene_id = modele[champ_id]
            ligne[champ_id] = bene_id
            ligne[champ_abs] = "oui" if bene_id in absents_par_jour.get(jour, set()) else "non"
            ligne[champ_remp] = None

            titulaire = get_nom(bene_id, "benevoles", ["prenom", "nom"])
            remplacant = None
            ligne[f"{champ}_nom"] = remplacant if ligne[champ_abs] == "oui" and remplacant else titulaire
            ligne[f"{champ}_remplacant_nom"] = remplacant

        planning.append(ligne)

    for ligne in planning:
        champs_sql = ["annee", "semaine", "jour", "creneau"]
        valeurs_sql = [annee, ligne["semaine"], ligne["jour"], ligne["creneau"]]
        for i in range(1, MAX_SLOTS + 1):
            champ = f"cui{str(i).zfill(2)}"
            champs_sql += [f"{champ}_id", f"{champ}_absent", f"{champ}_remplacant"]
            valeurs_sql += [ligne[f"{champ}_id"], ligne[f"{champ}_absent"], ligne[f"{champ}_remplacant"]]

        cursor.execute(f"""
            INSERT INTO plannings_cuisine ({', '.join(champs_sql)})
            VALUES ({', '.join(['?'] * len(valeurs_sql))})
        """, valeurs_sql)

    conn.commit()
    conn.close()

    ordre_jours = {j: i for i, j in enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi"])}
    ordre_creneaux = {c: i for i, (c, _) in enumerate(CRENEAUX)}
    planning.sort(key=lambda l: (ordre_jours.get(l["jour"], 99), ordre_creneaux.get(l["creneau"], 99)))

    # 🔁 Transposition pour l'aperçu : grille[creneau][jour] = ligne
    grille = {c: {} for c, _ in CRENEAUX}
    for l in planning:
        if l["creneau"] in grille:
            grille[l["creneau"]][l["jour"]] = l

    return render_template("planning/cuisine/creation_planning_cuisine.html",
                           semaine=semaine_iso,
                           planning=planning,
                           grille=grille,
                           jours=jours,
                           jours_dates=jours_dates,
                           creneaux=CRENEAUX,
                           slots_par_creneau=SLOTS_PAR_CRENEAU,
                           planning_existe=False)


@planning_cuisine_bp.route("/apercu_planning_cuisine")
@login_required
@require_access("planning", "lecture")
def apercu_planning_cuisine():
    semaine_iso = request.args.get("semaine")
    if not semaine_iso:
        flash("❌ Semaine invalide.", "danger")
        return redirect(url_for("planning.planning_main"))

    try:
        annee, numero_semaine = map(int, semaine_iso.split("-W"))
    except Exception:
        flash("❌ Format de semaine incorrect.", "danger")
        return redirect(url_for("planning.planning_main"))

    lundi = get_lundi_de_la_semaine(semaine_iso)

    conn = get_db_connection()
    cursor = conn.cursor()

    lignes_raw = cursor.execute("""
        SELECT * FROM plannings_cuisine
        WHERE annee = ? AND semaine = ?
        ORDER BY
            CASE LOWER(jour)
                WHEN 'lundi' THEN 1
                WHEN 'mardi' THEN 2
                WHEN 'mercredi' THEN 3
                WHEN 'jeudi' THEN 4
                WHEN 'vendredi' THEN 5
                ELSE 6
            END
    """, (annee, numero_semaine)).fetchall()
    lignes = [dict(l) for l in lignes_raw]

    benevoles = cursor.execute("SELECT id, nom, prenom FROM benevoles").fetchall()
    bene_dict = {b["id"]: f"{b['nom']} {b['prenom']}" for b in benevoles}

    for ligne in lignes:
        for i in range(1, MAX_SLOTS + 1):
            champ = f"cui{str(i).zfill(2)}"
            titulaire_id = ligne.get(f"{champ}_id")
            remplacant_id = ligne.get(f"{champ}_remplacant")
            ligne[f"{champ}_nom"] = bene_dict.get(titulaire_id, "") if titulaire_id else ""
            ligne[f"{champ}_remplacant_nom"] = bene_dict.get(remplacant_id, "") if remplacant_id else ""

    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    jours_dates = {j: lundi + timedelta(i) for i, j in enumerate(jours)}
    for l in lignes:
        l["date_jour"] = jours_dates.get(l["jour"], lundi).strftime("%d/%m/%Y")

    # 🔁 Transposition : grille[creneau][jour] = ligne
    jours_presents = sorted({l["jour"] for l in lignes}, key=lambda j: jours.index(j) if j in jours else 99)
    grille = {c: {} for c, _ in CRENEAUX}
    for l in lignes:
        if l["creneau"] in grille:
            grille[l["creneau"]][l["jour"]] = l

    conn.close()
    return render_template("planning/cuisine/apercu_planning_cuisine.html",
                           semaine=semaine_iso,
                           jours=jours_presents,
                           jours_dates=jours_dates,
                           creneaux=CRENEAUX,
                           slots_par_creneau=SLOTS_PAR_CRENEAU,
                           grille=grille)


@planning_cuisine_bp.route("/maj_modele_planning_cuisine", methods=["GET", "POST"])
@login_required
@require_access("planning", "ecriture")
def maj_modele_planning_cuisine():
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]

    conn = get_db_connection()
    cursor = conn.cursor()

    row = cursor.execute("""
        SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'
    """).fetchone()
    travail_vendredi = row["param_value"].strip().lower() if row else "oui"

    equipe, salaries = get_benevoles_cuisine(cursor)

    model = cursor.execute("SELECT * FROM planning_standard_cuisine_ids").fetchall()
    model = [dict(m) for m in model]

    if request.method == "POST":
        try:
            cursor.execute("DELETE FROM planning_standard_cuisine_ids")

            for jour in jours:
                if jour == "vendredi" and travail_vendredi == "non":
                    continue

                for creneau, _ in CRENEAUX:
                    champs_cui = [f"cui{str(i).zfill(2)}_id" for i in range(1, MAX_SLOTS + 1)]
                    valeurs_cui = [parse_id(request.form.get(f"{c}_{jour}_{creneau}")) for c in champs_cui]

                    cursor.execute(f"""
                        INSERT INTO planning_standard_cuisine_ids (
                            jour, creneau, {', '.join(champs_cui)}
                        ) VALUES (
                            ?, ?, {', '.join(['?'] * MAX_SLOTS)}
                        )
                    """, (jour, creneau, *valeurs_cui))

            conn.commit()
            upload_database()
            flash("✅ Modèle de planning cuisine mis à jour.", "success")
            return redirect(url_for("planning_cuisine.maj_modele_planning_cuisine"))

        except Exception as e:
            flash(f"❌ Erreur lors de la mise à jour : {e}", "danger")

    conn.close()

    model_par_jour_creneau = {(m["jour"], m["creneau"]): m for m in model}

    return render_template(
        "planning/cuisine/maj_modele_planning_cuisine.html",
        model_par_jour_creneau=model_par_jour_creneau,
        equipe=equipe,
        salaries=salaries,
        jours=jours,
        creneaux=CRENEAUX,
        slots_par_creneau=SLOTS_PAR_CRENEAU,
        travail_vendredi=travail_vendredi,
    )


@planning_cuisine_bp.route("/apercu_modele_planning_cuisine")
@login_required
@require_access("planning", "lecture")
def apercu_modele_planning_cuisine():
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]

    conn = get_db_connection()
    cursor = conn.cursor()

    row = cursor.execute("""
        SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'
    """).fetchone()
    travail_vendredi = row["param_value"].strip().lower() if row else "oui"

    model = cursor.execute("SELECT * FROM planning_standard_cuisine_ids").fetchall()
    model_par_jour_creneau = {(m["jour"], m["creneau"]): m for m in model}

    benevoles = cursor.execute("SELECT id, nom || ' ' || prenom AS nom FROM benevoles").fetchall()
    bene_dict = {b["id"]: b["nom"] for b in benevoles}

    conn.close()

    return render_template(
        "planning/cuisine/apercu_modele_planning_cuisine.html",
        model_par_jour_creneau=model_par_jour_creneau,
        bene_dict=bene_dict,
        jours=jours,
        creneaux=CRENEAUX,
        slots_par_creneau=SLOTS_PAR_CRENEAU,
        travail_vendredi=travail_vendredi,
    )


@planning_cuisine_bp.route("/gestion_planning_cuisine", methods=["GET", "POST"])
@login_required
@require_access("planning", "ecriture")
def gestion_planning_cuisine():
    semaine_iso = request.args.get("semaine")
    if not semaine_iso:
        flash("❌ Semaine manquante", "danger")
        return redirect(url_for("planning_cuisine.creation_planning_cuisine"))

    try:
        annee, numero_semaine = map(int, semaine_iso.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_cuisine.creation_planning_cuisine"))

    lundi = get_lundi_de_la_semaine(semaine_iso)
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]

    conn = get_db_connection()
    cursor = conn.cursor()

    equipe, salaries = get_benevoles_cuisine(cursor)

    lignes = cursor.execute("""
        SELECT * FROM plannings_cuisine
        WHERE annee = ? AND semaine = ?
        ORDER BY
            CASE LOWER(jour)
                WHEN 'lundi' THEN 1
                WHEN 'mardi' THEN 2
                WHEN 'mercredi' THEN 3
                WHEN 'jeudi' THEN 4
                WHEN 'vendredi' THEN 5
                ELSE 6
            END,
            id ASC
    """, (annee, numero_semaine)).fetchall()
    lignes = [dict(l) for l in lignes]

    absences = cursor.execute("""
        SELECT benevole_id, date_debut, date_fin
        FROM absences
    """).fetchall()

    absences = [
        {
            "benevole_id": a["benevole_id"],
            "debut": datetime.strptime(a["date_debut"], "%d/%m/%Y").date(),
            "fin": datetime.strptime(a["date_fin"], "%d/%m/%Y").date(),
        }
        for a in absences
    ]

    planning_auto_modified = False

    for ligne in lignes:
        jour = ligne["jour"].lower()
        if jour not in jours:
            continue
        date_jour = lundi + timedelta(jours.index(jour))
        max_slots = SLOTS_PAR_CRENEAU.get(ligne["creneau"], MAX_SLOTS)

        sql_updates = []
        sql_params = []

        for i in range(1, max_slots + 1):
            base = f"cui{str(i).zfill(2)}"
            champ_id = f"{base}_id"
            champ_abs = f"{base}_absent"
            champ_remp = f"{base}_remplacant"

            bene_id = ligne.get(champ_id)
            remp_id = ligne.get(champ_remp)

            if not bene_id:
                continue

            est_absent = any(
                a["benevole_id"] == bene_id and a["debut"] <= date_jour <= a["fin"]
                for a in absences
            )

            db_absent = (ligne.get(champ_abs) or "non").lower() == "oui"

            if remp_id:
                continue

            if est_absent != db_absent:
                ligne[champ_abs] = "oui" if est_absent else "non"
                sql_updates.append(f"{champ_abs} = ?")
                sql_params.append(ligne[champ_abs])
                planning_auto_modified = True

        if sql_updates:
            cursor.execute(
                f"UPDATE plannings_cuisine SET {', '.join(sql_updates)} WHERE id = ?",
                (*sql_params, ligne["id"])
            )

    if planning_auto_modified:
        conn.commit()
        upload_database()

    for ligne in lignes:
        for i in range(1, MAX_SLOTS + 1):
            for suffixe in ("_id", "_remplacant"):
                champ = f"cui{str(i).zfill(2)}{suffixe}"
                if ligne.get(champ) == "None":
                    ligne[champ] = None

    if request.method == "POST":
        try:
            for ligne in lignes:
                ligne_id = ligne["id"]
                max_slots = SLOTS_PAR_CRENEAU.get(ligne["creneau"], MAX_SLOTS)
                updates = []
                params = []

                for i in range(1, max_slots + 1):
                    base = f"cui{str(i).zfill(2)}"

                    field_id = f"{base}_id_{ligne_id}"
                    field_remp = f"{base}_remplacant_{ligne_id}"

                    val_id = request.form.get(field_id, "").strip()
                    val_remp = request.form.get(field_remp, "").strip()

                    def to_int(v):
                        return int(v) if v.isdigit() else None

                    nouveau_id = to_int(val_id)
                    nouveau_remp = to_int(val_remp)

                    ancien_id = ligne.get(f"{base}_id")
                    est_absent = (ligne.get(f"{base}_absent") or "").lower() == "oui"

                    if nouveau_id is None:
                        id_final = None
                        remp_final = None
                    elif est_absent and (ancien_id is None or nouveau_id != ancien_id):
                        id_final = ancien_id
                        remp_final = nouveau_id
                    else:
                        id_final = nouveau_id
                        remp_final = None

                    updates.extend([f"{base}_id = ?", f"{base}_remplacant = ?"])
                    params.extend([id_final, remp_final])

                params.append(ligne_id)
                cursor.execute(
                    f"UPDATE plannings_cuisine SET {', '.join(updates)} WHERE id = ?",
                    params
                )

            conn.commit()
            upload_database()
            flash("✅ Planning cuisine mis à jour.", "success")
            return redirect(
                url_for("planning_cuisine.gestion_planning_cuisine", semaine=semaine_iso)
            )

        except Exception as e:
            conn.rollback()
            flash(f"❌ Erreur enregistrement : {e}", "danger")

    # 🔁 Transposition pour l'affichage : grille[creneau][jour] = ligne
    grille = {c: {} for c, _ in CRENEAUX}
    for l in lignes:
        if l["creneau"] in grille:
            grille[l["creneau"]][l["jour"]] = l

    jours_presents = sorted({l["jour"] for l in lignes}, key=lambda j: jours.index(j) if j in jours else 99)

    type_benevole_options = get_type_benevole_options(conn)
    civilite_options = ["M", "Mme", "Mlle"]

    conn.close()

    return render_template(
        "planning/cuisine/gestion_planning_cuisine.html",
        semaine=semaine_iso,
        jours=jours_presents,
        jours_dates={j: lundi + timedelta(i) for i, j in enumerate(jours)},
        creneaux=CRENEAUX,
        slots_par_creneau=SLOTS_PAR_CRENEAU,
        grille=grille,
        equipe=equipe,
        salaries=salaries,
        planning_auto_modified=planning_auto_modified,
        type_benevole_options=type_benevole_options,
        civilite_options=civilite_options,
    )
