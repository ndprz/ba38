# ba38_planning_distribution.py
from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import login_required
from utils import upload_database, write_log, get_db_connection
from ba38_planning_utils import get_parametre_valeur

import sqlite3
from datetime import datetime, timedelta
from ba38_planning_utils import (
    get_lundi_de_la_semaine,
    get_benevole_infos,
    parse_id,
    parse_numero_semaine,
    get_nom,
    get_nom_benevole,
    get_nom_camion,
    get_fournisseurs_par_tournee_id,
    get_fournisseurs_pour_tournee,
    get_nom_tournee,
    get_absents_par_jour,
    filtrer
)



planning_dist_bp = Blueprint('planning_dist', __name__)

@planning_dist_bp.route("/planning_main")
@login_required
def planning_main():
    return render_template("planning_main.html")


@planning_dist_bp.route("/maj_modele_planning_distribution", methods=["GET", "POST"])
@login_required
def maj_modele_planning_distribution():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if request.method == "POST":
        # Liste des IDs existants
        cur.execute("SELECT id FROM planning_standard_distribution_ids")
        ids = [row["id"] for row in cur.fetchall()]

        champs = [
            "froid1_id", "froid2_id", "froid3_id", "froid4_id",
            "frais_sec1_id", "frais_sec2_id", "frais_sec3_id", "frais_sec4_id",
            "duree"
        ]

        for ligne_id in ids:
            valeurs = []
            for champ in champs:
                if champ == "duree":
                    v = request.form.get(f"duree_{ligne_id}")
                    valeurs.append(int(v) if v and v.isdigit() else None)
                else:
                    v = request.form.get(f"{champ}_{ligne_id}")
                    valeurs.append(v if v not in (None, "", "None") else None)

            cur.execute(f"""
                UPDATE planning_standard_distribution_ids
                SET {', '.join([f'{c} = ?' for c in champs])}
                WHERE id = ?
            """, (*valeurs, ligne_id))

        conn.commit()
        upload_database()
        flash("✅ Modèle de planning distribution mis à jour.", "success")
        return redirect(url_for("planning_dist.maj_modele_planning_distribution"))

    # --- GET ---
    cur.execute("SELECT * FROM planning_standard_distribution_ids ORDER BY id")
    planning = cur.fetchall()

    # Liste bénévoles
    cur.execute("SELECT id, nom, prenom, distrib_froid, distrib_frais_sec FROM benevoles ORDER BY nom, prenom")
    benevoles = [dict(row) for row in cur.fetchall()]

    froid = sorted(benevoles, key=lambda b: (b["distrib_froid"] != 'oui', b["nom"], b["prenom"]))
    frais_sec = sorted(benevoles, key=lambda b: (b["distrib_frais_sec"] != 'oui', b["nom"], b["prenom"]))

    return render_template(
        "maj_modele_planning_distribution.html",
        planning=planning,
        froid=froid,
        frais_sec=frais_sec
    )


@planning_dist_bp.route("/creation_planning_distribution", methods=["GET", "POST"])
@login_required
def creation_planning_distribution():
    """
    Création du planning de distribution à partir du modèle standard.

    Fonctionnement :
    - Choix d’une semaine
    - Si un planning existe déjà :
        -> proposer régénération ou modification
    - Sinon :
        -> générer le planning depuis planning_standard_distribution_ids
        -> détecter les absences
        -> enrichir avec les noms des bénévoles
        -> afficher un pré-planning
        -> permettre l’enregistrement silencieux
    """

    from utils import get_db_connection
    from ba38_planning_utils import get_lundi_de_la_semaine
    from datetime import datetime, timedelta

    conn = get_db_connection()
    cursor = conn.cursor()

    semaine = request.form.get("semaine") or request.args.get("semaine")
    planning = []
    planning_existe = False

    # --------------------------------------------------
    # 1️⃣ Aucune semaine sélectionnée → simple affichage
    # --------------------------------------------------
    if not semaine:
        conn.close()
        return render_template(
            "creation_planning_distribution.html",
            semaine="",
            planning=None,
            planning_existe=False,
        )

    # --------------------------------------------------
    # 2️⃣ Extraction du numéro de semaine
    # --------------------------------------------------
    try:
        annee, numero_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        conn.close()
        return redirect(
            url_for("planning_dist.creation_planning_distribution")
        )

    lundi = get_lundi_de_la_semaine(semaine)
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]

    # --------------------------------------------------
    # 3️⃣ Vérifier si un planning existe déjà
    # --------------------------------------------------
    existe = cursor.execute(
        "SELECT COUNT(*) FROM plannings_distribution WHERE annee = ? AND semaine = ?",
        (annee, numero_semaine),
    ).fetchone()[0]

    if existe > 0 and request.form.get("action") != "forcer_generation":
        conn.close()
        return render_template(
            "creation_planning_distribution.html",
            semaine=semaine,
            planning=None,
            planning_existe=True,
        )

    # --------------------------------------------------
    # 4️⃣ Charger le modèle standard
    # --------------------------------------------------
    modele = cursor.execute(
        """
        SELECT *
        FROM planning_standard_distribution_ids
        ORDER BY
            CASE LOWER(jour)
                WHEN 'lundi' THEN 1
                WHEN 'mardi' THEN 2
                WHEN 'mercredi' THEN 3
                WHEN 'jeudi' THEN 4
                WHEN 'vendredi' THEN 5
                ELSE 99
            END
        """
    ).fetchall()

    # 🔧 IMPORTANT : convertir sqlite3.Row → dict
    modele = [dict(l) for l in modele]

    # --------------------------------------------------
    # 5️⃣ Charger les bénévoles (ID → Nom Prénom)
    # --------------------------------------------------
    benevoles_map = {
        b["id"]: f"{b['prenom']} {b['nom']}"
        for b in cursor.execute(
            "SELECT id, prenom, nom FROM benevoles"
        ).fetchall()
    }

    # --------------------------------------------------
    # 6️⃣ Charger les absences
    # --------------------------------------------------
    absences = cursor.execute(
        "SELECT benevole_id, date_debut, date_fin FROM absences"
    ).fetchall()

    absences = [
        {
            "benevole_id": a["benevole_id"],
            "debut": datetime.strptime(a["date_debut"], "%d/%m/%Y").date(),
            "fin": datetime.strptime(a["date_fin"], "%d/%m/%Y").date(),
        }
        for a in absences
    ]

    # --------------------------------------------------
    # 7️⃣ Génération du pré-planning
    # --------------------------------------------------
    for ligne in modele:
        jour = ligne["jour"].lower()
        date_jour = lundi + timedelta(jours.index(jour))

        row = {
            "jour": ligne["jour"],
        }

        # ----------- FROID ----------- #
        for i in range(1, 5):
            bid = ligne.get(f"froid{i}_id")

            est_absent = any(
                a["benevole_id"] == bid
                and a["debut"] <= date_jour <= a["fin"]
                for a in absences
            ) if bid else False

            row[f"froid{i}_id"] = bid
            row[f"froid{i}_remplacant"] = None
            row[f"froid{i}_absent"] = "oui" if est_absent else "non"
            row[f"froid{i}_nom"] = benevoles_map.get(bid, "") if bid else ""
            row[f"froid{i}_remplacant_nom"] = ""

        # ----------- FRAIS SEC ----------- #
        for i in range(1, 5):
            bid = ligne.get(f"frais_sec{i}_id")

            est_absent = any(
                a["benevole_id"] == bid
                and a["debut"] <= date_jour <= a["fin"]
                for a in absences
            ) if bid else False

            row[f"frais_sec{i}_id"] = bid
            row[f"frais_sec{i}_remplacant"] = None
            row[f"frais_sec{i}_absent"] = "oui" if est_absent else "non"
            row[f"frais_sec{i}_nom"] = benevoles_map.get(bid, "") if bid else ""
            row[f"frais_sec{i}_remplacant_nom"] = ""

        planning.append(row)

        # --------------------------------------------------
        # 7bis️⃣ Enregistrement en base du planning généré
        # --------------------------------------------------

        # Suppression préalable si régénération
        cursor.execute(
            "DELETE FROM plannings_distribution WHERE annee = ? AND semaine = ?",
            (annee, numero_semaine)
        )

        insert_sql = """
            INSERT INTO plannings_distribution (
                annee, semaine, jour,

                froid1_id, froid1_absent, froid1_remplacant,
                froid2_id, froid2_absent, froid2_remplacant,
                froid3_id, froid3_absent, froid3_remplacant,
                froid4_id, froid4_absent, froid4_remplacant,

                frais_sec1_id, frais_sec1_absent, frais_sec1_remplacant,
                frais_sec2_id, frais_sec2_absent, frais_sec2_remplacant,
                frais_sec3_id, frais_sec3_absent, frais_sec3_remplacant,
                frais_sec4_id, frais_sec4_absent, frais_sec4_remplacant
            )
            VALUES (
                ?, ?, ?,

                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,

                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?
            )
        """

        for row in planning:
            cursor.execute(insert_sql, (
                annee,
                numero_semaine,
                row["jour"],

                row["froid1_id"], row["froid1_absent"], None,
                row["froid2_id"], row["froid2_absent"], None,
                row["froid3_id"], row["froid3_absent"], None,
                row["froid4_id"], row["froid4_absent"], None,

                row["frais_sec1_id"], row["frais_sec1_absent"], None,
                row["frais_sec2_id"], row["frais_sec2_absent"], None,
                row["frais_sec3_id"], row["frais_sec3_absent"], None,
                row["frais_sec4_id"], row["frais_sec4_absent"], None,
            ))

        conn.commit()
        upload_database()


    conn.close()

    # --------------------------------------------------
    # 8️⃣ Rendu final
    # --------------------------------------------------
    return render_template(
        "creation_planning_distribution.html",
        semaine=semaine,
        planning=planning,
        planning_existe=False,
    )



@planning_dist_bp.route('/gestion_planning_distribution', methods=['GET', 'POST'])
@login_required
def gestion_planning_distribution():
    from utils import get_db_connection, upload_database, get_db_connection
    from ba38_planning_utils import get_lundi_de_la_semaine

    semaine = request.args.get("semaine")
    try:
        annee, numero_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Semaine invalide", "danger")
        return redirect(url_for("planning_dist.creation_planning_distribution"))

    lundi = get_lundi_de_la_semaine(semaine)
    jours_dates = {j: lundi + timedelta(days=i) for i, j in enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi"])}

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 📦 Charger les lignes du planning
    lignes = cursor.execute("""
        SELECT * FROM plannings_distribution
        WHERE annee = ? AND semaine = ?
        ORDER BY 
            CASE LOWER(jour)
                WHEN 'lundi' THEN 1
                WHEN 'mardi' THEN 2
                WHEN 'mercredi' THEN 3
                WHEN 'jeudi' THEN 4
                WHEN 'vendredi' THEN 5
                ELSE 99
            END, id ASC
    """, (annee, numero_semaine)).fetchall()
    lignes = [dict(l) for l in lignes]

    # 📦 Charger les absences
    absences = cursor.execute("SELECT benevole_id, date_debut, date_fin FROM absences").fetchall()
    absents_par_jour = {j: set() for j in jours_dates}
    for a in absences:
        try:
            debut = datetime.strptime(a["date_debut"], "%d/%m/%Y").date()
            fin = datetime.strptime(a["date_fin"], "%d/%m/%Y").date()
            for jour, date_j in jours_dates.items():
                if debut <= date_j <= fin:
                    absents_par_jour[jour].add(a["benevole_id"])
        except:
            continue


    # 📋 Liste des bénévoles, filtrée et triée
    tous_benevoles = cursor.execute("SELECT * FROM benevoles ORDER BY nom, prenom").fetchall()
    tous_benevoles = [dict(b) for b in tous_benevoles]

    def filtrer_par_role(benevoles, champ_role):
        prioritaires = [b for b in benevoles if (b.get(champ_role) or "").strip().lower() == "oui"]
        autres = [b for b in benevoles if (b.get(champ_role) or "").strip().lower() != "oui"]
        return sorted(prioritaires, key=lambda b: b["nom"].lower()), sorted(autres, key=lambda b: b["nom"].lower())

    froid_oui, froid_non = filtrer_par_role(tous_benevoles, "distrib_froid")
    frais_oui, frais_non = filtrer_par_role(tous_benevoles, "distrib_frais_sec")

    froid = froid_oui + froid_non
    frais_sec = frais_oui + frais_non


    # 🔽 Types de bénévoles depuis la table parametres
    rows = cursor.execute("""
        SELECT param_value FROM parametres
        WHERE LOWER(param_name) = 'type_benevole'
        ORDER BY param_value
    """).fetchall()
    types_benevoles = [r["param_value"] for r in rows]

    # fallback si la table n'est pas encore remplie
    if not types_benevoles:
        types_benevoles = ["benevole", "stagiaire", "autre"]


    # ---------- POST : Enregistrement ----------
    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "enregistrer":
            try:
                ids = request.form.getlist("ligne_ids[]")

                # Tous les champs distribution
                fields = [
                    'froid1', 'froid2', 'froid3', 'froid4',
                    'frais_sec1', 'frais_sec2', 'frais_sec3', 'frais_sec4'
                ]

                # Récupération des valeurs envoyées
                form_data = {
                    f: request.form.getlist(f"{f}_ids[]")
                    for f in fields
                }

                def parse(val):
                    try:
                        return int(val)
                    except Exception:
                        return None

                update_sql = f"""
                    UPDATE plannings_distribution SET
                    {", ".join([
                        f"{f}_id = ?, {f}_remplacant = ?"
                        for f in fields
                    ])}
                    WHERE id = ?
                """

                for idx, ligne_id in enumerate(ids):
                    ancienne = cursor.execute(
                        "SELECT * FROM plannings_distribution WHERE id = ?",
                        (ligne_id,)
                    ).fetchone()

                    params = []

                    for f in fields:
                        ancien_id = ancienne[f"{f}_id"]
                        absent = str(ancienne[f"{f}_absent"]).lower() == "oui"
                        nouveau_id = parse(form_data[f][idx])

                        if absent and nouveau_id and nouveau_id != ancien_id:
                            # titulaire absent → remplaçant
                            params.extend([ancien_id, nouveau_id])
                        else:
                            params.extend([nouveau_id, None])

                    params.append(ligne_id)
                    cursor.execute(update_sql, params)

                conn.commit()
                upload_database()

                flash("✅ Planning distribution enregistré avec succès.", "success")
                return redirect(
                    url_for("planning_dist.gestion_planning_distribution", semaine=semaine)
                )

            except Exception as e:
                conn.rollback()
                flash(f"❌ Erreur lors de l'enregistrement : {e}", "danger")

    # --- Types de bénévoles depuis la table parametres (pour la modale partagée)
    rows = cursor.execute("""
        SELECT param_value FROM parametres
        WHERE LOWER(param_name) = 'type_benevole'
        ORDER BY param_value COLLATE NOCASE
    """).fetchall()
    type_benevole_options = [r["param_value"] for r in rows] or ["benevole", "service civique", "pass region", "salarie"]

    # --- Civilités pour la modale
    civilite_options = ["M", "Mme", "Mlle"]

    # ------------------------------------------------------------------
    # 🔁 Synchroniser les champs *_absent en base avec la table absences
    #     (nécessaire pour que la logique "remplaçant" fonctionne)
    # ------------------------------------------------------------------
    roles = [
        'froid1', 'froid2', 'froid3', 'froid4',
        'frais_sec1', 'frais_sec2', 'frais_sec3', 'frais_sec4'
    ]

    updates_count = 0
    planning_auto_modified = False

    for l in lignes:
        jour = (l.get("jour") or "").lower()
        absents_du_jour = absents_par_jour.get(jour, set())

        set_clauses = []
        set_params = []

        for role in roles:
            titulaire_id = l.get(f"{role}_id")
            absent_field = f"{role}_absent"
            remplacant_id = l.get(f"{role}_remplacant")

            # Valeur DB actuelle
            db_val = (l.get(absent_field) or "non")
            db_is_absent = str(db_val).strip().lower() == "oui"

            # Vérité métier
            is_absent_now = bool(titulaire_id and titulaire_id in absents_du_jour)

            # 🔒 On ne touche pas si un remplaçant est défini
            if remplacant_id:
                continue

            if is_absent_now != db_is_absent:
                set_clauses.append(f"{absent_field} = ?")
                set_params.append("oui" if is_absent_now else "non")
                updates_count += 1

            # Mise à jour pour affichage
            l[absent_field] = "oui" if is_absent_now else "non"

        if set_clauses:
            cursor.execute(
                f"UPDATE plannings_distribution SET {', '.join(set_clauses)} WHERE id = ?",
                (*set_params, l["id"])
            )

    if updates_count:
        conn.commit()
        upload_database()
        planning_auto_modified = True

    return render_template(
        "gestion_planning_distribution.html",
        semaine=semaine,
        lignes=lignes,
        froid=froid,
        frais_sec=frais_sec,
        absents_par_jour=absents_par_jour,
        type_benevole_options=type_benevole_options,
        civilite_options=civilite_options,
        planning_auto_modified=planning_auto_modified

)


@planning_dist_bp.route('/apercu_planning_distribution')
@login_required
def apercu_planning_distribution():
    semaine = request.args.get("semaine")
    if not semaine:
        flash("Semaine manquante", "danger")
        return redirect(url_for("planning_dist.creation_planning_distribution"))

    try:
        annee, num_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("Semaine invalide", "danger")
        return redirect(url_for("planning_dist.creation_planning_distribution"))


    lundi = get_lundi_de_la_semaine(semaine)

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Lecture des données planning
    lignes = cursor.execute("SELECT * FROM plannings_distribution WHERE annee = ? AND semaine = ?", (annee, num_semaine)).fetchall()
    lignes = [dict(l) for l in lignes]

    # Lecture des noms de tous les bénévoles
    benevoles = cursor.execute("SELECT id, nom, prenom FROM benevoles").fetchall()
    bene_dict = {b["id"]: f"{b['nom']} {b['prenom']}" for b in benevoles}

    # Ajout des noms pour affichage
    for ligne in lignes:
        for role in ['froid1', 'froid2', 'froid3', 'froid4', 'frais_sec1', 'frais_sec2', 'frais_sec3', 'frais_sec4']:
            titulaire_id = ligne.get(f"{role}_id")
            remplaçant_id = ligne.get(f"{role}_remplacant")

            ligne[f"{role}_nom"] = bene_dict.get(titulaire_id, "") if titulaire_id else ""
            ligne[f"remplacant_{role}_nom"] = bene_dict.get(remplaçant_id, "") if remplaçant_id else ""

    jours_dates = {j: (lundi + timedelta(days=i)) for i, j in enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi"])}
    for l in lignes:
        jour = l["jour"]
        l["date_jour"] = jours_dates[jour].strftime("%d/%m/%Y")

    conn.close()
    return render_template("apercu_planning_distribution.html", semaine=semaine, lignes=lignes)



