from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from utils import get_db_connection, write_log, upload_database
from ba38_planning_utils import get_lundi_de_la_semaine, get_nom, get_absents_par_jour
from datetime import timedelta
from datetime import datetime, timedelta



planning_vif_bp = Blueprint("planning_vif", __name__)



@planning_vif_bp.route("/maj_modele_planning_vif", methods=["GET", "POST"])
@login_required
def maj_modele_planning_vif():
    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        action = request.form.get("action")

        # 🔥 SUPPRESSION
        if action and action.startswith("supprimer_"):
            id_ligne = action.replace("supprimer_", "")
            cursor.execute("DELETE FROM planning_standard_vif_ids WHERE id = ?", (id_ligne,))

        # ➕ AJOUT
        elif action == "ajouter":
            jour = request.form.get("jour")
            vif01 = request.form.get("vif01") or None
            vif02 = request.form.get("vif02") or None
            duree_raw = request.form.get("duree")
            duree = int(duree_raw) if duree_raw and duree_raw.isdigit() else None

            cursor.execute("""
                INSERT INTO planning_standard_vif_ids (jour, vif01_id, vif02_id, duree)
                VALUES (?, ?, ?, ?)
            """, (jour, vif01, vif02, duree))

        # 💾 ENREGISTREMENT GLOBAL
        else:
            ligne_ids = request.form.getlist("ligne_ids[]")
            jours = request.form.getlist("jour[]")
            vif01_list = request.form.getlist("vif01[]")
            vif02_list = request.form.getlist("vif02[]")
            durees = request.form.getlist("duree[]")

            for idx, id_ligne in enumerate(ligne_ids):
                jour = jours[idx]
                vif01 = vif01_list[idx] or None
                vif02 = vif02_list[idx] or None

                duree_raw = durees[idx]
                duree = int(duree_raw) if duree_raw and duree_raw.isdigit() else None

                cursor.execute("""
                    UPDATE planning_standard_vif_ids
                    SET jour = ?, vif01_id = ?, vif02_id = ?, duree = ?
                    WHERE id = ?
                """, (jour, vif01, vif02, duree, id_ligne))

        conn.commit()
        flash("✅ Modèle VIF mis à jour avec succès.", "success")
        return redirect(url_for("planning_vif.maj_modele_planning_vif"))

    # GET — affichage
    lignes = cursor.execute("SELECT * FROM planning_standard_vif_ids").fetchall()

    jours_ordre = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    lignes = sorted(lignes, key=lambda l: jours_ordre.index(l["jour"].lower()))

    benevoles = cursor.execute("""
        SELECT
            id,
            nom,
            prenom,
            saisie_vif
        FROM benevoles
        ORDER BY
            CASE
                WHEN LOWER(TRIM(COALESCE(saisie_vif,''))) = 'oui' THEN 0
                ELSE 1
            END,
            nom,
            prenom
    """).fetchall()

    benevoles = [dict(b) for b in benevoles]

    jours_semaine = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]

    # pour cacher la ligne d'ajout quand une journée existe déjà
    jours_saisis = {l["jour"].lower() for l in lignes}
    row = cursor.execute("SELECT param_value FROM parametres WHERE param_name='travail_vendredi'").fetchone()
    travail_vendredi = row["param_value"] if row else "oui"

    jours_actifs = ["lundi", "mardi", "mercredi", "jeudi"]
    if travail_vendredi == "oui":
        jours_actifs.append("vendredi")

    afficher_ligne_ajout = any(j not in jours_saisis for j in jours_actifs)

    conn.close()

    return render_template(
        "maj_modele_planning_vif.html",
        lignes=lignes,
        benevoles=benevoles,
        jours_semaine=jours_semaine,
        afficher_ligne_ajout=afficher_ligne_ajout,
    )


@planning_vif_bp.route("/creation_planning_vif", methods=["GET", "POST"])
@login_required
def creation_planning_vif():
    semaine = request.form.get("semaine") or request.args.get("semaine")
    action = request.form.get("action")
    if not semaine:
        return render_template("creation_planning_vif.html", semaine="")

    try:
        annee, numero_semaine = map(int, semaine.split("-W"))
        numero_semaine = str(numero_semaine)
        
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_vif.creation_planning_vif"))
    lundi = get_lundi_de_la_semaine(semaine)
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    jours_dates = {j: lundi + timedelta(days=i) for i, j in enumerate(jours)}


    conn = get_db_connection()
    cursor = conn.cursor()

    existing = cursor.execute("SELECT COUNT(*) as n FROM plannings_vif WHERE annee = ? AND semaine = ?", (annee, numero_semaine)).fetchone()
    if existing["n"] > 0 and action != "forcer_generation":
        conn.close()
        return render_template("creation_planning_vif.html", semaine=semaine, planning_existe=True)

    travail_vendredi = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'").fetchone()
    if travail_vendredi and travail_vendredi["param_value"] == "non":
        jours.remove("vendredi")

    benevoles = {b["id"]: b for b in cursor.execute("SELECT id, nom, prenom, prep_pesee FROM benevoles").fetchall()}
    absences = cursor.execute("""
        SELECT benevole_id, date_debut, date_fin
        FROM absences
    """).fetchall()

    absences = [
        {
            "benevole_id": a["benevole_id"],
            "debut": datetime.strptime(a["date_debut"], "%d/%m/%Y").date(),
            "fin":   datetime.strptime(a["date_fin"], "%d/%m/%Y").date(),
        }
        for a in absences
        if a["benevole_id"]
    ]

    model = cursor.execute("SELECT * FROM planning_standard_vif_ids").fetchall()
    champs = ["vif01", "vif02"]

    planning = []
    for ligne in model:
        jour = ligne["jour"]
        if jour not in jours:
            continue
        bloc = {"jour": jour, "date_jour": jours_dates[jour].strftime("%d/%m/%Y")}
        for champ in champs:
            bene_id = ligne[f"{champ}_id"]
            bloc[f"{champ}_id"] = bene_id
            date_jour = jours_dates[jour]

            est_absent = any(
                a["benevole_id"] == bene_id
                and a["debut"] <= date_jour <= a["fin"]
                for a in absences
            )

            titulaire = get_nom(bene_id, "benevoles", ["prenom", "nom"]) if bene_id else ""
            bloc[f"{champ}_absent"] = "oui" if est_absent else "non"
            bloc[f"{champ}_remplacant"] = None

            # Affichage : titulaire même s'il est absent
            bloc[f"{champ}_nom"] = titulaire
            bloc[f"{champ}_remplacant_nom"] = ""
        planning.append(bloc)

    if existing["n"] > 0:
        cursor.execute("DELETE FROM plannings_vif WHERE annee = ? AND semaine = ?", (annee, numero_semaine))

    for bloc in planning:
        values = [annee, numero_semaine, bloc["jour"]]
        for champ in champs:
            values.append(bloc.get(f"{champ}_id"))
        for champ in champs:
            values.append(bloc.get(f"{champ}_absent", "non"))
        for champ in champs:
            values.append(bloc.get(f"{champ}_remplacant"))
        cursor.execute(f"""
            INSERT INTO plannings_vif (
                annee, semaine, jour,
                vif01_id, vif02_id,
                vif01_absent, vif02_absent,
                vif01_remplacant, vif02_remplacant
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)

    conn.commit()
    upload_database()

    flash("✅ Planning VIF généré avec succès.", "success")
    lignes = cursor.execute(
        "SELECT * FROM plannings_vif WHERE annee = ? AND semaine = ?",
        (annee, numero_semaine)
    ).fetchall()

    lignes = [dict(l) for l in lignes]

    # -------------------------------------------------
    # Enrichissement des lignes pour affichage (OBLIGATOIRE)
    # -------------------------------------------------
    for l in lignes:
        for i in range(1, 3):  # VIF01 / VIF02 uniquement
            tid = l.get(f"vif{i:02d}_id")
            rid = l.get(f"vif{i:02d}_remplacant")

            if tid in ("", "None"):
                tid = None
            if rid in ("", "None"):
                rid = None

            l[f"vif{i:02d}_nom"] = (
                get_nom(tid, "benevoles", ["prenom", "nom"]) if tid else ""
            )
            l[f"vif{i:02d}_remplacant_nom"] = (
                get_nom(rid, "benevoles", ["prenom", "nom"]) if rid else ""
            )


    conn.close()

    return render_template(
        "creation_planning_vif.html",
        semaine=semaine,
        planning=lignes,
        planning_existe=False,
    )


@planning_vif_bp.route("/gestion_planning_vif", methods=["GET", "POST"])
@login_required
def gestion_planning_vif():
    """
    Gestion du planning VIF.

    Règles métier :
    - Relecture systématique des absences à chaque affichage
    - Forçage automatique vifXX_absent='oui' si absence détectée
    - Ne jamais écraser un poste ayant déjà un remplaçant
    - Calcul d’un effectif réel (titulaire / remplaçant)
    - Toute modification automatique active le bouton 💾
    """

    # -------------------------------------------------
    # Imports locaux nécessaires
    # -------------------------------------------------
    from datetime import datetime, timedelta

    # -------------------------------------------------
    # Utilitaire de conversion ID
    # -------------------------------------------------
    def to_int_or_none(v):
        try:
            if v in (None, "", "None"):
                return None
            return int(v)
        except Exception:
            return None

    # -------------------------------------------------
    # Paramètre semaine
    # -------------------------------------------------
    semaine = request.args.get("semaine") or request.form.get("semaine")
    if not semaine:
        flash("❌ Semaine manquante", "danger")
        return redirect(url_for("planning_vif.creation_planning_vif"))

    try:
        annee, num_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_vif.creation_planning_vif"))

    lundi = get_lundi_de_la_semaine(semaine)
    if not lundi:
        flash("❌ Semaine invalide", "danger")
        return redirect(url_for("planning_vif.creation_planning_vif"))

    conn = get_db_connection()
    cursor = conn.cursor()

    # -------------------------------------------------
    # Chargement des lignes du planning VIF
    # -------------------------------------------------
    lignes = cursor.execute("""
        SELECT * FROM plannings_vif
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
            id
    """, (annee, num_semaine)).fetchall()

    lignes = [dict(l) for l in lignes]

    # -------------------------------------------------
    # Dictionnaire dates par jour
    # -------------------------------------------------
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    jours_dates = {j: lundi + timedelta(i) for i, j in enumerate(jours)}

    # -------------------------------------------------
    # Préchargement des noms (titulaire / remplaçant)
    # -------------------------------------------------
    for l in lignes:
        for i in range(1, 3):
            tid = to_int_or_none(l.get(f"vif{i:02d}_id"))
            rid = to_int_or_none(l.get(f"vif{i:02d}_remplacant"))

            l[f"vif{i:02d}_nom"] = get_nom(tid, "benevoles", ["prenom", "nom"]) if tid else ""
            l[f"vif{i:02d}_remplacant_nom"] = get_nom(rid, "benevoles", ["prenom", "nom"]) if rid else ""

    # -------------------------------------------------
    # Relecture des absences (LOGIQUE PALETTES)
    # -------------------------------------------------
    absences = cursor.execute("""
        SELECT benevole_id, date_debut, date_fin
        FROM absences
    """).fetchall()

    # 🔁 construction absents_par_jour (SOURCE DE VÉRITÉ)
    absents_par_jour = get_absents_par_jour(absences, jours_dates)

    planning_auto_modified = False

    # -------------------------------------------------
    # Auto-flag des absents (mise à jour DB si nécessaire)
    # -------------------------------------------------
    for l in lignes:
        jour = (l.get("jour") or "").lower()
        if jour not in absents_par_jour:
            continue

        for i in range(1, 3):
            champ_id  = f"vif{i:02d}_id"
            champ_abs = f"vif{i:02d}_absent"

            titulaire = to_int_or_none(l.get(champ_id))
            if not titulaire:
                continue

            est_absent = titulaire in absents_par_jour[jour]
            flag_db   = (l.get(champ_abs) or "non") == "oui"

            # 🔄 synchronisation stricte avec la table absences
            if est_absent != flag_db:
                l[champ_abs] = "oui" if est_absent else "non"
                cursor.execute(
                    f"UPDATE plannings_vif SET {champ_abs} = ? WHERE id = ?",
                    (l[champ_abs], l["id"])
                )
                planning_auto_modified = True


    if planning_auto_modified:
        conn.commit()
        upload_database()

    # -------------------------------------------------
    # Normalisation + calcul effectif réel
    # -------------------------------------------------
    for l in lignes:
        for i in range(1, 3):
            tid = to_int_or_none(l.get(f"vif{i:02d}_id"))
            rid = to_int_or_none(l.get(f"vif{i:02d}_remplacant"))
            absent = (l.get(f"vif{i:02d}_absent") or "non") == "oui"

            l[f"vif{i:02d}_id"] = tid
            l[f"vif{i:02d}_remplacant"] = rid
            l[f"vif{i:02d}_effectif"] = rid if absent and rid else tid

    # -------------------------------------------------
    # Liste bénévoles (saisie_vif='oui' en tête)
    # -------------------------------------------------
    benevoles = [
        dict(b) for b in cursor.execute("""
            SELECT id, nom, prenom, saisie_vif
            FROM benevoles
            ORDER BY
                CASE
                    WHEN LOWER(TRIM(COALESCE(saisie_vif,''))) = 'oui' THEN 0
                    ELSE 1
                END,
                nom,
                prenom
        """).fetchall()
    ]

    # -------------------------------------------------
    # POST : Enregistrement manuel
    # -------------------------------------------------
    if request.method == "POST":
        try:
            ligne_ids = request.form.getlist("ligne_ids[]")
            champs = ["vif01", "vif02"]
            ids_form = {c: request.form.getlist(f"{c}_ids[]") for c in champs}

            for idx, ligne_id in enumerate(ligne_ids):
                ancienne = cursor.execute(
                    "SELECT * FROM plannings_vif WHERE id = ?", (ligne_id,)
                ).fetchone()
                if not ancienne:
                    continue

                sets, params = [], []

                for c in champs:
                    ancien_id = to_int_or_none(ancienne[f"{c}_id"])
                    absent = (ancienne[f"{c}_absent"] or "non") == "oui"
                    nouveau_id = to_int_or_none(ids_form[c][idx])

                    if absent and nouveau_id and nouveau_id != ancien_id:
                        id_final = ancien_id
                        remp_final = nouveau_id
                    else:
                        id_final = nouveau_id
                        remp_final = None

                    sets += [f"{c}_id = ?", f"{c}_remplacant = ?"]
                    params += [id_final, remp_final]

                params.append(ligne_id)
                cursor.execute(
                    f"UPDATE plannings_vif SET {', '.join(sets)} WHERE id = ?",
                    params
                )

            conn.commit()
            upload_database()
            flash("✅ Planning VIF enregistré.", "success")
            return redirect(url_for("planning_vif.gestion_planning_vif", semaine=semaine))

        except Exception as e:
            conn.rollback()
            flash(f"❌ Erreur enregistrement : {e}", "danger")

    conn.close()
    return render_template(
        "gestion_planning_vif.html",
        semaine=semaine,
        lignes=lignes,
        benevoles=benevoles,
        jours_dates=jours_dates,
        planning_auto_modified=planning_auto_modified,
    )


@planning_vif_bp.route("/apercu_planning_vif")
@login_required
def apercu_planning_vif():
    semaine = request.args.get("semaine")
    try:
        annee, num_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_vif.creation_planning_vif"))
    lundi = get_lundi_de_la_semaine(semaine)

    conn = get_db_connection()
    cursor = conn.cursor()
    lignes = cursor.execute("SELECT * FROM plannings_vif WHERE annee = ? AND semaine = ? ORDER BY jour, id", (annee, num_semaine    )).fetchall()
    lignes = [dict(l) for l in lignes]

    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    jours_dates = {j: lundi + timedelta(days=i) for i, j in enumerate(jours)}

    for l in lignes:
        jour = l["jour"].lower()
        delta = jours.index(jour)
        l["date_jour"] = (lundi + timedelta(days=delta)).strftime("%d/%m/%Y")
        for champ in ["vif01", "vif02"]:
            id_val = l.get(f"{champ}_id")
            remp_val = l.get(f"{champ}_remplacant")
            if id_val in ["", "None", None]:
                id_val = None
            if remp_val in ["", "None", None]:
                remp_val = None
            l[f"{champ}_nom"] = get_nom(id_val, "benevoles", ["nom", "prenom"]) if id_val else ""
            l[f"{champ}_remplacant_nom"] = get_nom(remp_val, "benevoles", ["nom", "prenom"]) if remp_val else ""

    conn.close()
    lignes.sort(key=lambda l: jours.index(l["jour"].lower()))
    return render_template("apercu_planning_vif.html", semaine=semaine, planning=lignes)
