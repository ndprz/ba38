# ba38_planning_pesee.py

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from utils import get_db_connection, upload_database, write_log
from ba38_planning_utils import get_lundi_de_la_semaine, get_benevole_infos, get_nom, get_absents_par_jour, parse_numero_semaine, get_type_benevole_options
from datetime import datetime, timedelta
from collections import defaultdict



planning_pesee_bp = Blueprint("planning_pesee", __name__)


@planning_pesee_bp.route("/maj_modele_planning_pesee", methods=["GET", "POST"])
@login_required
def maj_modele_planning_pesee():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
        row = cursor.execute("""
            SELECT param_value FROM parametres WHERE param_name='travail_vendredi'
        """).fetchone()
        travail_vendredi = row["param_value"] if row else "oui"

        # Bénévoles préparant la pesée
        benevoles_raw = cursor.execute("""
            SELECT id, nom, prenom, prep_pesee FROM benevoles
        """).fetchall()

        benevoles = sorted(
            [dict(b) for b in benevoles_raw],
            key=lambda b: (b["nom"].lower(), b["prenom"].lower())
        )

        if request.method == "POST":
            cursor.execute("DELETE FROM planning_standard_pesee_ids")

            for jour in jours:
                if jour == "vendredi" and travail_vendredi == "non":
                    continue

                champs = [
                    "pesee01_id","pesee02_id","pesee03_id","pesee04_id",
                    "pesee05_id","pesee06_id","pesee07_id","pesee08_id"
                ]

                valeurs = []
                for champ in champs:
                    v = request.form.get(f"{champ}_{jour}")
                    valeurs.append(int(v) if v and v.isdigit() else None)

                # 🔥 Gestion de la durée
                duree_raw = request.form.get(f"duree_{jour}")
                duree = int(duree_raw) if duree_raw and duree_raw.isdigit() else None

                cursor.execute(f"""
                    INSERT INTO planning_standard_pesee_ids (
                        jour,
                        {", ".join(champs)},
                        duree
                    ) VALUES (
                        ?,
                        {", ".join(['?']*8)},
                        ?
                    )
                """, (jour, *valeurs, duree))

            conn.commit()
            upload_database()
            flash("✅ Modèle de planning pesée mis à jour.", "success")
            return redirect(url_for("planning_pesee.maj_modele_planning_pesee"))

        # --- GET : lecture du modèle
        model = cursor.execute("""
            SELECT * FROM planning_standard_pesee_ids ORDER BY jour
        """).fetchall()

        conn.close()
        return render_template(
            "maj_modele_planning_pesee.html",
            model=model,
            benevoles=benevoles,
            jours=jours,
            travail_vendredi=travail_vendredi,
        )

    except Exception as e:
        write_log(f"❌ Erreur maj_modele_planning_pesee : {e}")
        raise



@planning_pesee_bp.route("/creation_planning_pesee", methods=["GET", "POST"])
@login_required
def creation_planning_pesee():
    semaine = request.form.get("semaine") or request.args.get("semaine")
    action = request.form.get("action")
    planning_existe = False

    if not semaine:
        return render_template("creation_planning_pesee.html", semaine="")

    try:
        annee, num_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_pesee.creation_planning_pesee"))
    
    lundi = get_lundi_de_la_semaine(semaine)
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]

    conn = get_db_connection()
    cursor = conn.cursor()

    existing = cursor.execute("SELECT COUNT(*) as n FROM plannings_pesee WHERE annee = ? AND semaine = ?", (annee, num_semaine  )).fetchone()
    planning_existe = existing["n"] > 0

    if planning_existe and action != "forcer_generation":
        conn.close()
        return render_template("creation_planning_pesee.html", semaine=semaine, planning_existe=True)

    row = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'").fetchone()
    travail_vendredi = row["param_value"] if row else "oui"
    if travail_vendredi == "non":
        jours = [j for j in jours if j != "vendredi"]

    benevoles_raw = cursor.execute("SELECT id, nom, prenom, prep_pesee FROM benevoles").fetchall()
    absences = cursor.execute("SELECT benevole_id, date_debut, date_fin FROM absences").fetchall()
    jours_dates = {j: lundi + timedelta(days=i) for i, j in enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi"])}
    absents_par_jour = get_absents_par_jour(absences, jours_dates)

    model = cursor.execute("SELECT * FROM planning_standard_pesee_ids").fetchall()
    poste_mapping = {f"pesee{str(i).zfill(2)}_id": f"pesee{str(i).zfill(2)}" for i in range(1, 9)}

    planning = []

    for ligne in model:
        jour = ligne["jour"]
        if jour not in jours:
            continue
        bloc = {"jour": jour, "date_jour": jours_dates[jour].strftime("%d/%m/%Y")}
        for champ_modele, champ_final in poste_mapping.items():
            bene_id = ligne[champ_modele] if champ_modele in ligne.keys() else None
            bloc[f"{champ_final}_id"] = bene_id
            bloc[f"{champ_final}_absent"] = "oui" if bene_id in absents_par_jour.get(jour, set()) else "non"
            bloc[f"{champ_final}_remplacant"] = None
            bloc[f"{champ_final}_nom"] = get_nom(bene_id, "benevoles", ["prenom", "nom"]) if bene_id else ""
        planning.append(bloc)

    if planning_existe:
        cursor.execute("DELETE FROM plannings_pesee WHERE annee = ? AND semaine = ?", (annee, num_semaine))

    for bloc in planning:
        values = [annee, num_semaine, bloc["jour"]]
        for i in range(1, 9):
            values.append(bloc.get(f"pesee{str(i).zfill(2)}_id"))
        for i in range(1, 9):
            values.append(bloc.get(f"pesee{str(i).zfill(2)}_absent", "non"))
        for i in range(1, 9):
            values.append(bloc.get(f"pesee{str(i).zfill(2)}_remplacant"))

        cursor.execute(f"""
            INSERT INTO plannings_pesee (
                annee, semaine, jour,
                pesee01_id, pesee02_id, pesee03_id, pesee04_id,
                pesee05_id, pesee06_id, pesee07_id, pesee08_id,
                pesee01_absent, pesee02_absent, pesee03_absent, pesee04_absent,
                pesee05_absent, pesee06_absent, pesee07_absent, pesee08_absent,
                pesee01_remplacant, pesee02_remplacant, pesee03_remplacant, pesee04_remplacant,
                pesee05_remplacant, pesee06_remplacant, pesee07_remplacant, pesee08_remplacant
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, values)

    conn.commit()
    conn.close()
    flash("✅ Planning pesée généré et enregistré avec succès.", "success")
    return render_template("creation_planning_pesee.html", semaine=semaine, planning=planning)



@planning_pesee_bp.route("/apercu_planning_pesee")
@login_required
def apercu_planning_pesee():
    # 🔢 Récupération de la semaine choisie (ex: '2025-W23')
    semaine = request.args.get("semaine")
    if not semaine:
        return "Semaine non spécifiée", 400

    try:
        annee, num_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_pesee.creation_planning_pesee"))
    
    lundi = get_lundi_de_la_semaine(semaine)

    conn = get_db_connection()
    cursor = conn.cursor()

    # 📥 Récupération des lignes de planning pour la semaine donnée
    lignes = cursor.execute(
        "SELECT * FROM plannings_pesee WHERE annee = ? AND semaine = ? ORDER BY jour, id",
        (annee, num_semaine),
    ).fetchall()
    lignes = [dict(l) for l in lignes]

    # 📅 Association jour → date
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    jours_dates = {j: lundi + timedelta(days=i) for i, j in enumerate(jours)}

    # 🔄 Traitement de chaque ligne de planning
    for l in lignes:
        jour = l["jour"].lower()

        # 🗓️ Ajout de la date formatée (jj/mm/aaaa)
        delta = jours.index(jour)
        l["date_jour"] = (lundi + timedelta(days=delta)).strftime("%d/%m/%Y")

        # 👥 Pour chaque poste de pesée : récupérer les noms/remplaçants
        for i in range(1, 9):
            champ = f"pesee{i:02d}"
            id_val = l.get(f"{champ}_id")
            remp_val = l.get(f"{champ}_remplacant")

            if id_val in ["", "None", None]:
                id_val = None
            if remp_val in ["", "None", None]:
                remp_val = None

            l[f"{champ}_nom"] = get_nom(id_val, "benevoles", ["nom", "prenom"]) if id_val else ""
            l[f"{champ}_remplacant_nom"] = get_nom(remp_val, "benevoles", ["nom", "prenom"]) if remp_val else ""

        write_log(f"🔍 {l['jour']} | ID: {l.get('pesee01_id')} | NOM: {l.get('pesee01_nom')}")

    conn.close()

    ordre_jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    lignes.sort(key=lambda l: ordre_jours.index(l["jour"].lower()))

    return render_template("apercu_planning_pesee.html", semaine=semaine, planning=lignes)



@planning_pesee_bp.route("/gestion_planning_pesee", methods=["GET", "POST"])
@login_required
def gestion_planning_pesee():
    """
    Gestion (modification) du planning Pesée.

    Fonctionnalités:
    - Lecture du planning (plannings_pesee) pour une semaine.
    - Relecture de la table absences à chaque affichage:
        si un titulaire est absent selon absences et que peseeXX_absent != 'oui',
        on force peseeXX_absent='oui' EN BASE (sans toucher au titulaire/remplaçant).
      => planning_auto_modified=True pour activer le bouton 💾 + warning côté JS.
    - Enregistrement:
        on ne modifie que les champs peseeXX_id et peseeXX_remplacant.
        Règle:
          * si absent='oui' et la sélection est différente du titulaire -> remplaçant
          * sinon -> titulaire = sélection, remplaçant = NULL
    """

    # -------------------------
    # Helpers
    # -------------------------
    def to_int_or_none(v):
        try:
            if v is None:
                return None
            s = str(v).strip()
            if s == "" or s.lower() == "none":
                return None
            return int(s)
        except Exception:
            return None

    semaine = request.args.get("semaine") or request.form.get("semaine")
    if not semaine:
        flash("❌ Semaine manquante", "danger")
        return redirect(url_for("planning_pesee.creation_planning_pesee"))

    try:
        annee, num_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_pesee.creation_planning_pesee"))
    if num_semaine is None:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_pesee.creation_planning_pesee"))

    lundi = get_lundi_de_la_semaine(semaine)
    if lundi is None:
        flash("❌ Erreur : semaine invalide", "danger")
        return redirect(url_for("planning_pesee.creation_planning_pesee"))

    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    jours_dates = {j: lundi + timedelta(i) for i, j in enumerate(jours)}

    conn = get_db_connection()
    cursor = conn.cursor()

    # -------------------------
    # 1) Charger les lignes planning
    # -------------------------
    lignes = cursor.execute(
        """
        SELECT * FROM plannings_pesee
        WHERE annee = ? AND semaine = ?
        ORDER BY
          CASE LOWER(jour)
            WHEN 'lundi' THEN 1
            WHEN 'mardi' THEN 2
            WHEN 'mercredi' THEN 3
            WHEN 'jeudi' THEN 4
            WHEN 'vendredi' THEN 5
            ELSE 99
          END,
          id ASC
        """,
        (annee, num_semaine),
    ).fetchall()
    lignes = [dict(l) for l in lignes]

    # -------------------------
    # 2) Relecture absences + auto-flag des nouveaux absents
    # -------------------------
    planning_auto_modified = False

    absences_rows = cursor.execute(
        "SELECT benevole_id, date_debut, date_fin FROM absences"
    ).fetchall()

    absents_par_jour = {j: set() for j in jours}
    for a in absences_rows:
        try:
            debut = datetime.strptime(a["date_debut"], "%d/%m/%Y").date()
            fin = datetime.strptime(a["date_fin"], "%d/%m/%Y").date()
            bid = to_int_or_none(a["benevole_id"])
            if not bid:
                continue
            for jour, date_obj in jours_dates.items():
                if debut <= date_obj <= fin:
                    absents_par_jour[jour].add(bid)
        except Exception:
            continue

    # Si nouvel absent détecté => on force peseeXX_absent='oui' en base
    for l in lignes:
        jour = (l.get("jour") or "").strip().lower()
        if jour not in absents_par_jour:
            continue

        updates = []
        for i in range(1, 9):
            champ_id = f"pesee{i:02d}_id"
            champ_abs = f"pesee{i:02d}_absent"
            champ_remp = f"pesee{i:02d}_remplacant"

            tid = to_int_or_none(l.get(champ_id))
            if not tid:
                continue

            remp = to_int_or_none(l.get(champ_remp))
            if remp:
                continue  # 🔒 ne pas toucher si remplaçant

            est_absent = tid in absents_par_jour[jour]
            etait_absent = str(l.get(champ_abs, "non")).lower() == "oui"

            if est_absent != etait_absent:
                l[champ_abs] = "oui" if est_absent else "non"
                updates.append(champ_abs)

        if updates:
            planning_auto_modified = True
            cursor.execute(
                f"UPDATE plannings_pesee SET {', '.join(f'{c} = ?' for c in updates)} WHERE id = ?",
                [l[c] for c in updates] + [l["id"]],
            )

    if planning_auto_modified:
        conn.commit()

    # -------------------------
    # 3) Normalisation + effectif (titulaire vs remplaçant)
    # -------------------------
    for l in lignes:
        for i in range(1, 9):
            tid = to_int_or_none(l.get(f"pesee{i:02d}_id"))
            rid = to_int_or_none(l.get(f"pesee{i:02d}_remplacant"))
            l[f"pesee{i:02d}_id"] = tid
            l[f"pesee{i:02d}_remplacant"] = rid

            absent = str(l.get(f"pesee{i:02d}_absent", "non")).strip().lower() == "oui"
            l[f"pesee{i:02d}_effectif"] = rid if (absent and rid) else tid

    # -------------------------
    # 4) Liste bénévoles
    # -------------------------
    benevoles = [dict(b) for b in cursor.execute(
        "SELECT id, nom, prenom, prep_pesee FROM benevoles ORDER BY nom, prenom"
    ).fetchall()]

    # Déjà affectés (en tenant compte des effectifs)
    collections_du_jour = defaultdict(lambda: defaultdict(set))
    for l in lignes:
        jour = (l.get("jour") or "").strip().lower()
        if jour not in jours:
            continue
        for i in range(1, 9):
            eff = l.get(f"pesee{i:02d}_effectif")
            if eff:
                collections_du_jour[f"pesee{i:02d}"][jour].add(eff)

    # -------------------------
    # 5) POST : Enregistrement
    # -------------------------
    if request.method == "POST":
        try:
            champs = [f"pesee{i:02d}" for i in range(1, 9)]
            ligne_ids = request.form.getlist("ligne_ids[]")

            # Les <select> du template doivent être: name="peseeXX_ids[]"
            donnees = {c: request.form.getlist(f"{c}_ids[]") for c in champs}

            # Sécurisation: on itère sur l'index minimal réellement disponible
            n = min(len(ligne_ids), *[len(donnees[c]) for c in champs])

            for idx in range(n):
                id_ligne = to_int_or_none(ligne_ids[idx])
                if not id_ligne:
                    continue

                # On relit la ligne en base pour avoir titulaire + absent à jour
                ancienne = cursor.execute(
                    "SELECT * FROM plannings_pesee WHERE id = ?",
                    (id_ligne,),
                ).fetchone()
                if not ancienne:
                    continue

                sets = []
                params = []

                for c in champs:
                    ancien_id = to_int_or_none(ancienne[f"{c}_id"])
                    absent = str(ancienne[f"{c}_absent"] or "non").strip().lower() == "oui"

                    sel = to_int_or_none(donnees[c][idx])

                    # Règle:
                    # - Si absent='oui' et la sélection != titulaire -> remplaçant
                    # - Sinon -> titulaire = sélection, remplaçant = NULL
                    if absent and sel and (ancien_id is None or sel != ancien_id):
                        id_final = ancien_id
                        remp_final = sel
                    else:
                        id_final = sel
                        remp_final = None

                    sets.append(f"{c}_id = ?")
                    params.append(id_final)
                    sets.append(f"{c}_remplacant = ?")
                    params.append(remp_final)

                params.append(id_ligne)
                cursor.execute(
                    f"UPDATE plannings_pesee SET {', '.join(sets)} WHERE id = ?",
                    params,
                )

            conn.commit()
            upload_database()
            flash("✅ Planning pesée enregistré.", "success")
            return redirect(url_for("planning_pesee.gestion_planning_pesee", semaine=semaine))

        except Exception as e:
            conn.rollback()
            flash(f"❌ Erreur lors de l'enregistrement : {e}", "danger")

    # Options annexes (si ton template en dépend)
    type_benevole_options = get_type_benevole_options(conn)
    civilite_options = ["M", "Mme", "Mlle"]

    conn.close()
    return render_template(
        "gestion_planning_pesee.html",
        semaine=semaine,
        lignes=lignes,
        benevoles=benevoles,
        jours_dates=jours_dates,
        collections_du_jour=collections_du_jour,
        type_benevole_options=type_benevole_options,
        civilite_options=civilite_options,
        absents_par_jour=absents_par_jour,
        planning_auto_modified=planning_auto_modified,  # ✅ pour activer warning + save btn côté JS
    )

