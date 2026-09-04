# ba38_planning_palettes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from ba38_utilitaires.core import get_db_connection, write_log, upload_database, get_db_connection, require_access
from ba38_planning.utils import get_lundi_de_la_semaine, parse_id, get_nom, get_type_benevole_options

planning_palettes_bp = Blueprint('planning_palettes', __name__)


@planning_palettes_bp.route("/creation_planning_palettes", methods=["GET", "POST"])
@login_required
@require_access("planning","ecriture")
def creation_planning_palettes():
    # 📥 Lecture de la semaine au format ISO
    semaine_iso = request.form.get("semaine") or request.args.get("semaine")
    if not semaine_iso:
        return render_template("planning/palettes/creation_planning_palettes.html", semaine="")

    # 🔢 Conversion "2025-W23" → (2025, 23)
    try:
        annee, numero_semaine = map(int, semaine_iso.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return render_template("planning/palettes/creation_planning_palettes.html", semaine=semaine_iso)

    action = request.form.get("action")
    lundi = get_lundi_de_la_semaine(semaine_iso)

    conn = get_db_connection()
    cursor = conn.cursor()

    # ⚙️ Lire le paramètre 'travail_vendredi'
    param = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'").fetchone()
    travail_vendredi = param and param[0].strip().lower() == "oui"

    jours = ["lundi", "mardi", "mercredi", "jeudi"]
    if travail_vendredi:
        jours.append("vendredi")

    jours_dates = {j: lundi + timedelta(i) for i, j in enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi"]) if j in jours}

    # 🔍 Vérifie s’il existe déjà un planning
    planning_existe = cursor.execute(
        "SELECT COUNT(*) FROM plannings_pal WHERE annee = ? AND semaine = ?", (annee, numero_semaine)
    ).fetchone()[0] > 0

    # 🚫 Si planning existe et qu’on n’a pas demandé explicitement de régénérer
    if planning_existe and action != "forcer_generation":
        conn.close()
        return render_template("planning/palettes/creation_planning_palettes.html",
                               semaine=semaine_iso,
                               planning_existe=True,
                               planning=[])

    # 🧹 Supprimer les anciennes lignes si on force la génération
    # 🔎 Log création ou régénération
    if planning_existe:
        write_log(
            f"🔄 Régénération planning palettes "
            f"S{numero_semaine}/{annee} par {current_user.username}"
        )
        cursor.execute(
            "DELETE FROM plannings_pal WHERE annee = ? AND semaine = ?",
            (annee, numero_semaine)
        )
    else:
        write_log(
            f"🆕 Création planning palettes "
            f"S{numero_semaine}/{annee} par {current_user.username}"
        )
    # 📋 Lire le modèle de planning standard
    modeles = cursor.execute("SELECT * FROM planning_standard_pal_ids").fetchall()
    if not modeles:
        conn.close()
        flash("⚠️ Aucun modèle de planning trouvé. Veuillez d’abord définir le modèle avant de générer un planning.", "warning")
        return render_template("planning/palettes/creation_planning_palettes.html", semaine=semaine_iso, planning_existe=False)


    # 📦 Lire les absences connues
    absences = cursor.execute("SELECT benevole_id, date_debut, date_fin FROM absences").fetchall()
    absents_par_jour = {j: set() for j in jours_dates}
    for a in absences:
        try:
            debut = datetime.strptime(a["date_debut"], "%d/%m/%Y").date()
            fin = datetime.strptime(a["date_fin"], "%d/%m/%Y").date()
            for jour, date_obj in jours_dates.items():
                if debut <= date_obj <= fin:
                    absents_par_jour[jour].add(a["benevole_id"])
        except:
            continue

    # 🏗️ Générer les lignes du planning
    planning = []
    for modele in modeles:
        jour = modele["jour"].lower()
        if jour not in jours:
            continue

        ligne = {"jour": jour, "semaine": numero_semaine}
        for i in range(1, 11):
            champ = f"pal{str(i).zfill(2)}"
            champ_id = f"{champ}_id"
            champ_abs = f"{champ}_absent"
            champ_remp = f"{champ}_remplacant"

            bene_id = modele[champ_id]
            ligne[champ_id] = bene_id
            ligne[champ_abs] = "oui" if bene_id in absents_par_jour.get(jour, set()) else "non"
            ligne[champ_remp] = None

            # 👤 Pour affichage immédiat
            titulaire = get_nom(bene_id, "benevoles", ["prenom", "nom"])
            remplacant = None
            ligne[f"{champ}_nom"] = remplacant if ligne[champ_abs] == "oui" and remplacant else titulaire
            ligne[f"{champ}_remplacant_nom"] = remplacant

        planning.append(ligne)

    # 📥 Enregistrement dans la base
    for ligne in planning:
        champs_sql = ["annee", "semaine", "jour"]
        valeurs_sql = [annee, ligne["semaine"], ligne["jour"]]
        for i in range(1, 11):
            champ = f"pal{str(i).zfill(2)}"
            champs_sql += [f"{champ}_id", f"{champ}_absent", f"{champ}_remplacant"]
            valeurs_sql += [ligne[f"{champ}_id"], ligne[f"{champ}_absent"], ligne[f"{champ}_remplacant"]]

        cursor.execute(f"""
            INSERT INTO plannings_pal ({', '.join(champs_sql)})
            VALUES ({', '.join(['?'] * len(valeurs_sql))})
        """, valeurs_sql)

    conn.commit()
    conn.close()

    # 🔢 Tri final pour affichage
    ordre_jours = {j: i for i, j in enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi"])}
    planning.sort(key=lambda l: ordre_jours.get(l["jour"], 99))

    return render_template("planning/palettes/creation_planning_palettes.html",
                           semaine=semaine_iso,
                           planning=planning,
                           jours=jours,
                           planning_existe=False)


@planning_palettes_bp.route("/apercu_planning_palettes")
@login_required
@require_access("planning","lecture")
def apercu_planning_palettes():
    semaine_iso = request.args.get("semaine")  # ex: "2025-W23"
    if not semaine_iso:
        flash("❌ Semaine invalide.", "danger")
        return redirect(url_for("planning_main"))

    # 🔢 Extraire le numéro de semaine (ex: 23)
    try:
        annee, numero_semaine = map(int, semaine_iso.split("-W"))
    except Exception:
        flash("❌ Format de semaine incorrect.", "danger")
        return redirect(url_for("planning_main"))

    lundi = get_lundi_de_la_semaine(semaine_iso)

    conn = get_db_connection()

    cursor = conn.cursor()

    # 📦 Lecture des lignes du planning
    lignes_raw = cursor.execute("""
        SELECT * FROM plannings_pal
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

    # 👤 Dictionnaire des noms de bénévoles
    benevoles = cursor.execute("SELECT id, nom, prenom FROM benevoles").fetchall()
    benevoles = [dict(b) for b in benevoles]
    bene_dict = {b["id"]: f"{b['nom']} {b['prenom']}" for b in benevoles}

    # 🧠 Ajout des noms formatés pour chaque poste
    for ligne in lignes:
        jour = ligne.get("jour", "")
        for i in range(1, 11):
            champ = f"pal{str(i).zfill(2)}"
            titulaire_id = ligne.get(f"{champ}_id")
            remplacant_id = ligne.get(f"{champ}_remplacant")
            ligne[f"{champ}_nom"] = bene_dict.get(titulaire_id, "") if titulaire_id else ""
            ligne[f"{champ}_remplacant_nom"] = bene_dict.get(remplacant_id, "") if remplacant_id else ""

    # 🗓️ Ajout de la date de chaque jour
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    jours_dates = {j: lundi + timedelta(i) for i, j in enumerate(jours)}
    for l in lignes:
        jour = l["jour"]
        l["date_jour"] = jours_dates.get(jour, lundi).strftime("%d/%m/%Y")

    conn.close()
    return render_template("planning/palettes/apercu_planning_palettes.html",
                           lignes=lignes,
                           semaine=semaine_iso)


@planning_palettes_bp.route("/maj_modele_planning_palettes", methods=["GET", "POST"])
@login_required
@require_access("planning","ecriture")
def maj_modele_planning_palettes():
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]

    conn = get_db_connection()
    cursor = conn.cursor()

    # Lecture paramètre travail vendredi
    row = cursor.execute("""
        SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'
    """).fetchone()
    travail_vendredi = row["param_value"].strip().lower() if row else "oui"

    # Bénévoles palettes
    benevoles = cursor.execute("""
        SELECT id, nom || ' ' || prenom AS nom_complet, prep_palette
        FROM benevoles ORDER BY nom, prenom
    """).fetchall()

    # Lire le modèle (inclut duree si colonne existante)
    model = cursor.execute("SELECT * FROM planning_standard_pal_ids ORDER BY jour").fetchall()

    if request.method == "POST":
        try:
            cursor.execute("DELETE FROM planning_standard_pal_ids")

            for jour in jours:
                if jour == "vendredi" and travail_vendredi == "non":
                    continue

                # Champs pal01_id → pal10_id
                champs_pal = [f"pal{str(i).zfill(2)}_id" for i in range(1, 11)]
                valeurs_pal = [parse_id(request.form.get(f"{c}_{jour}")) for c in champs_pal]

                # Champ durée
                duree_raw = request.form.get(f"duree_{jour}")
                duree = int(duree_raw) if duree_raw and duree_raw.isdigit() else None

                cursor.execute(f"""
                    INSERT INTO planning_standard_pal_ids (
                        jour, {', '.join(champs_pal)}, duree
                    ) VALUES (
                        ?, {', '.join(['?']*10)}, ?
                    )
                """, (jour, *valeurs_pal, duree))

            conn.commit()
            upload_database()
            flash("✅ Modèle de planning palettes mis à jour.", "success")
            return redirect(url_for("planning_palettes.maj_modele_planning_palettes"))

        except Exception as e:
            flash(f"❌ Erreur lors de la mise à jour : {e}", "danger")

    conn.close()
    return render_template(
        "planning/palettes/maj_modele_planning_palettes.html",
        model=model,
        benevoles=benevoles,
        jours=jours,
        travail_vendredi=travail_vendredi
    )


@planning_palettes_bp.route("/apercu_modele_planning_palettes")
@login_required
@require_access("planning", "lecture")
def apercu_modele_planning_palettes():
    conn = get_db_connection()
    cursor = conn.cursor()

    model = cursor.execute("""
        SELECT * FROM planning_standard_pal_ids
        ORDER BY
            CASE jour
                WHEN 'lundi' THEN 1
                WHEN 'mardi' THEN 2
                WHEN 'mercredi' THEN 3
                WHEN 'jeudi' THEN 4
                WHEN 'vendredi' THEN 5
            END
    """).fetchall()

    benevoles = cursor.execute("SELECT id, nom || ' ' || prenom AS nom FROM benevoles").fetchall()
    bene_dict = {b["id"]: b["nom"] for b in benevoles}

    row = cursor.execute("""
        SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'
    """).fetchone()
    travail_vendredi = row and row["param_value"].strip().lower() == "oui"

    conn.close()
    return render_template(
        "planning/palettes/apercu_modele_planning_palettes.html",
        model=model,
        bene_dict=bene_dict,
        travail_vendredi=travail_vendredi,
    )


@planning_palettes_bp.route("/gestion_planning_palettes", methods=["GET", "POST"])
@login_required
@require_access("planning","ecriture")
def gestion_planning_palettes():
    """
    Gestion et modification du planning palettes.

    Règles métier importantes :
    - Les absences sont relues à chaque affichage.
    - Si une nouvelle absence est détectée sur un poste déjà planifié,
      le champ palXX_absent est forcé à 'oui' EN BASE.
    - Cela permet ensuite d’identifier correctement les remplaçants.
    - Toute modification automatique déclenche l’état
      "modifications non enregistrées" côté UI.
    """

    # ------------------------------------------------------------
    # Imports locaux
    # ------------------------------------------------------------
    from ba38_utilitaires.core import get_db_connection, upload_database
    from ba38_planning.utils import get_lundi_de_la_semaine, get_type_benevole_options
    from datetime import datetime, timedelta

    # ------------------------------------------------------------
    # Paramètre semaine
    # ------------------------------------------------------------
    semaine_iso = request.args.get("semaine")
    if not semaine_iso:
        flash("❌ Semaine manquante", "danger")
        return redirect(url_for("planning_palettes.creation_planning_palettes"))

    try:
        annee, numero_semaine = map(int, semaine_iso.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for("planning_palettes.creation_planning_palettes"))

    lundi = get_lundi_de_la_semaine(semaine_iso)
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]

    # ------------------------------------------------------------
    # Connexion DB
    # ------------------------------------------------------------
    conn = get_db_connection()
    cursor = conn.cursor()

    # ------------------------------------------------------------
    # Bénévoles (prep_palette='oui' en tête)
    # ------------------------------------------------------------
    benevoles = cursor.execute("""
        SELECT id, nom, prenom, prep_palette
        FROM benevoles
        ORDER BY (LOWER(COALESCE(prep_palette,''))!='oui'), nom, prenom
    """).fetchall()

    benevoles = [dict(b) for b in benevoles]
    favoris = [b for b in benevoles if (b.get("prep_palette") or "").lower() == "oui"]
    autres   = [b for b in benevoles if (b.get("prep_palette") or "").lower() != "oui"]

    # ------------------------------------------------------------
    # Chargement des lignes de planning
    # ------------------------------------------------------------
    lignes = cursor.execute("""
        SELECT * FROM plannings_pal
        WHERE annee = ? AND semaine = ?        ORDER BY
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


    # ------------------------------------------------------------
    # 🔁 Relecture des absences
    # ------------------------------------------------------------
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
    ]

    planning_auto_modified = False  # 🔔 flag UI

    for ligne in lignes:
        jour = ligne["jour"].lower()
        date_jour = lundi + timedelta(jours.index(jour))

        sql_updates = []
        sql_params = []

        for i in range(1, 11):
            base = f"pal{str(i).zfill(2)}"
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

            # 🔒 Ne pas écraser si remplaçant
            if remp_id:
                continue

            if est_absent != db_absent:
                ligne[champ_abs] = "oui" if est_absent else "non"
                sql_updates.append(f"{champ_abs} = ?")
                sql_params.append(ligne[champ_abs])
                planning_auto_modified = True

        if sql_updates:
            cursor.execute(
                f"UPDATE plannings_pal SET {', '.join(sql_updates)} WHERE id = ?",
                (*sql_params, ligne["id"])
            )

    if planning_auto_modified:
        conn.commit()
        upload_database()

    # ------------------------------------------------------------
    # Nettoyage des "None" textuels
    # ------------------------------------------------------------
    for ligne in lignes:
        for i in range(1, 11):
            for suffixe in ("_id", "_remplacant"):
                champ = f"pal{str(i).zfill(2)}{suffixe}"
                if ligne.get(champ) == "None":
                    ligne[champ] = None

    # ------------------------------------------------------------
    # POST : Enregistrement manuel
    # ------------------------------------------------------------
    if request.method == "POST":
        try:
            for ligne in lignes:
                ligne_id = ligne["id"]
                updates = []
                params = []

                for i in range(1, 11):
                    base = f"pal{str(i).zfill(2)}"

                    field_id   = f"{base}_id_{ligne_id}"
                    field_remp = f"{base}_remplacant_{ligne_id}"

                    val_id   = request.form.get(field_id, "").strip()
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

                    updates.extend([
                        f"{base}_id = ?",
                        f"{base}_remplacant = ?"
                    ])
                    params.extend([id_final, remp_final])

                params.append(ligne_id)
                cursor.execute(
                    f"UPDATE plannings_pal SET {', '.join(updates)} WHERE id = ?",
                    params
                )

            conn.commit()
            upload_database()
            flash("✅ Planning palettes mis à jour.", "success")
            return redirect(
                url_for("planning_palettes.gestion_planning_palettes", semaine=semaine_iso)
            )

        except Exception as e:
            conn.rollback()
            flash(f"❌ Erreur enregistrement : {e}", "danger")

    # ------------------------------------------------------------
    # Rendu
    # ------------------------------------------------------------
    type_benevole_options = get_type_benevole_options(conn)
    civilite_options = ["M", "Mme", "Mlle"]

    conn.close()

    return render_template(
        "planning/palettes/gestion_planning_palettes.html",
        lignes=lignes,
        semaine=semaine_iso,
        benevoles=benevoles,
        favoris=favoris,
        autres=autres,
        jours_dates={j: lundi + timedelta(i) for i, j in enumerate(jours)},
        planning_auto_modified=planning_auto_modified,
        type_benevole_options=type_benevole_options,
        civilite_options=civilite_options,
    )

