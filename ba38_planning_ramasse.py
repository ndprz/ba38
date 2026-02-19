# ba38_planning_ramasse.py

from flask import Blueprint, render_template, request, flash, redirect, url_for, flash
from ba38_planning_utils import get_type_benevole_options
from flask_login import login_required
from datetime import datetime, timedelta
from utils import get_db_connection, write_log, upload_database

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

import sqlite3

planning_bp = Blueprint('planning', __name__)

@planning_bp.route('/planning_main')
@login_required
def planning_main():
    return render_template('planning_main.html')

def safe(row, key):
    return row[key] if key in row.keys() and row[key] else "-"



def purge_plannings_ramasse():
    from datetime import datetime, timedelta
    semaine_limite = datetime.now().isocalendar()[1] - 4  # Garde 4 semaines
    conn = get_db_connection()
    conn.execute("DELETE FROM plannings_ramasse WHERE semaine < ?", (semaine_limite,))
    conn.commit()
    conn.close()

@planning_bp.route('/creation_planning_ramasse', methods=['GET', 'POST'])
@login_required
def creation_planning_ramasse():
    semaine = ""
    planning = []
    jours = []
    planning_existe = False

    if request.method == 'POST':
        semaine = request.form.get("semaine")
        action = request.form.get("action")
        annee, num_semaine = map(int, semaine.split("-W"))


        conn = get_db_connection()
        cursor = conn.cursor()

        # Vérifie si un planning existe déjà
        nb = cursor.execute("SELECT COUNT(*) FROM plannings_ramasse WHERE annee = ? AND semaine = ?", (annee, num_semaine,)).fetchone()[0]
        planning_existe = nb > 0

        if planning_existe and action != "forcer_generation":
            return render_template("creation_planning_ramasse.html", semaine=semaine, planning_existe=True)

        # Récupération du paramètre "travail_vendredi"
        param = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'").fetchone()
        travail_vendredi = param and param[0].strip().lower() == "oui"
        jours = ["lundi", "mardi", "mercredi", "jeudi"]
        if travail_vendredi:
            jours.append("vendredi")

        # Détection des absences
        lundi = get_lundi_de_la_semaine(semaine)
        jours_dates = {j: lundi + timedelta(days=i) for i, j in enumerate(jours)}
        absences = cursor.execute("SELECT benevole_id, date_debut, date_fin FROM absences").fetchall()
        absents_par_jour = get_absents_par_jour(absences, jours_dates)

        # Modèle de planning
        lignes = cursor.execute("SELECT * FROM planning_standard_ramasse_ids ORDER BY jour, numero").fetchall()
        if not lignes:
            conn.close()
            flash("⚠️ Aucun modèle de planning trouvé. Veuillez d’abord définir le modèle avant de générer un planning.", "warning")
            return render_template("creation_planning_ramasse.html", semaine=semaine_iso, planning_existe=False)


        for ligne in lignes:
            jour = ligne['jour'].strip().lower()
            if jour not in jours:
                continue

            absents = absents_par_jour.get(jour, set())
            c_id = ligne['chauffeur_id']
            r_id = ligne['responsable_id']
            e_id = ligne['equipier_id']
            t1_id = ligne['ramasse_tri1_id']
            t2_id = ligne['ramasse_tri2_id']
            t3_id = ligne['ramasse_tri3_id']

            planning.append({
                "jour": jour,
                "tournee_id": ligne["tournee_id"],
                "tournee": f"{get_nom_tournee(ligne['tournee_id'])} — {get_fournisseurs_par_tournee_id(ligne['tournee_id'])}",
                "chauffeur": get_nom_benevole(c_id), "chauffeur_id": c_id,
                "responsable": get_nom_benevole(r_id), "responsable_id": r_id,
                "equipier": get_nom_benevole(e_id), "equipier_id": e_id,
                "camion": get_nom_camion(ligne["camion_id"]), "camion_id": ligne["camion_id"],
                "ramasse_tri1": get_nom_benevole(t1_id), "ramasse_tri1_id": t1_id,
                "ramasse_tri2": get_nom_benevole(t2_id), "ramasse_tri2_id": t2_id,
                "ramasse_tri3": get_nom_benevole(t3_id), "ramasse_tri3_id": t3_id,
                "absent": any(i in absents for i in [c_id, r_id, e_id, t1_id, t2_id, t3_id]),
                "chauffeur_absent": "oui" if c_id in absents else "non",
                "responsable_absent": "oui" if r_id in absents else "non",
                "equipier_absent": "oui" if e_id in absents else "non",
                "ramasse_tri1_absent": "oui" if t1_id in absents else "non",
                "ramasse_tri2_absent": "oui" if t2_id in absents else "non",
                "ramasse_tri3_absent": "oui" if t3_id in absents else "non",
            })

        # 🔧 Conversion explicite en booléens pour affichage
        for l in planning:
            for champ in [
                "chauffeur_absent", "responsable_absent", "equipier_absent",
                "ramasse_tri1_absent", "ramasse_tri2_absent", "ramasse_tri3_absent"
            ]:
                l[champ] = str(l.get(champ)).strip().lower() == "oui"

        conn.close()
        upload_database()

        # Enregistrement en base (si action == forcer_generation)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM plannings_ramasse WHERE annee = ? AND semaine = ?", (annee, num_semaine,))
        for l in planning:
            cursor.execute("""
                INSERT INTO plannings_ramasse (
                    type, annee, semaine, tournee_id, tournee,
                    chauffeur_id, responsable_id, equipier_id,
                    ramasse_tri1_id, ramasse_tri2_id, ramasse_tri3_id,
                    camion_id, jour,
                    chauffeur_absent, responsable_absent, equipier_absent,
                    ramasse_tri1_absent, ramasse_tri2_absent, ramasse_tri3_absent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "Ramasse", annee, num_semaine, l["tournee_id"], l["tournee"],
                l["chauffeur_id"], l["responsable_id"], l["equipier_id"],
                l["ramasse_tri1_id"], l["ramasse_tri2_id"], l["ramasse_tri3_id"],
                l["camion_id"], l["jour"],
                "oui" if l["chauffeur_absent"] else "non",
                "oui" if l["responsable_absent"] else "non",
                "oui" if l["equipier_absent"] else "non",
                "oui" if l["ramasse_tri1_absent"] else "non",
                "oui" if l["ramasse_tri2_absent"] else "non",
                "oui" if l["ramasse_tri3_absent"] else "non"
            ))
        conn.commit()
        conn.close()

    return render_template("creation_planning_ramasse.html", semaine=semaine, planning=planning, jours=jours, planning_existe=planning_existe)



@planning_bp.route('/gestion_planning_ramasse', methods=['GET', 'POST'])
@login_required
def gestion_planning_ramasse():
    """
    Gestion du planning ramasse existant.

    - Le planning n'est jamais régénéré ici
    - Les remplaçants manuels sont conservés
    - Les nouvelles absences sont détectées automatiquement
    - Aucun mélange bool / string dans la logique
    """

    from ba38_planning_utils import get_lundi_de_la_semaine, get_type_benevole_options
    from datetime import datetime, timedelta
    import sqlite3

    # ------------------------------------------------------------------
    # 📅 Semaine
    # ------------------------------------------------------------------
    raw_semaine = request.args.get("semaine", "")
    try:
        annee, numero_semaine = map(int, raw_semaine.split("-W"))
    except Exception:
        flash("❌ Semaine invalide", "danger")
        return redirect(url_for("planning.planning_main"))

    lundi = get_lundi_de_la_semaine(raw_semaine)
    if not lundi:
        flash("❌ Semaine invalide", "danger")
        return redirect(url_for("planning.planning_main"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # ------------------------------------------------------------------
    # 🚚 Tournées
    # ------------------------------------------------------------------
    tournees = cursor.execute("""
        SELECT t.tournee_id,
            (
                SELECT nom
                FROM tournees_fournisseurs
                WHERE tournee_id = t.tournee_id
                AND nom IS NOT NULL AND TRIM(nom) <> ''
                LIMIT 1
            ) AS nom,
            (
                SELECT GROUP_CONCAT(f.nom, ' / ')
                FROM tournees_fournisseurs tf
                JOIN fournisseurs f ON f.id = tf.fournisseur_id
                WHERE tf.tournee_id = t.tournee_id
                ORDER BY tf.ordre
            ) AS fournisseurs
        FROM tournees_fournisseurs t
        GROUP BY t.tournee_id
    """).fetchall()

    tournee_dict = {
        t["tournee_id"]: (
            f"{t['nom']} — {t['fournisseurs']}"
            if t["fournisseurs"] else t["nom"]
        )
        for t in tournees
    }

    # ------------------------------------------------------------------
    # 👤 Bénévoles / 🚛 Camions
    # ------------------------------------------------------------------
    benevoles = [dict(b) for b in cursor.execute(
        "SELECT * FROM benevoles ORDER BY nom COLLATE NOCASE"
    ).fetchall()]

    camions = [dict(c) for c in cursor.execute(
        "SELECT * FROM camions ORDER BY nom COLLATE NOCASE"
    ).fetchall()]

    # ------------------------------------------------------------------
    # 📋 Planning semaine
    # ------------------------------------------------------------------
    lignes = [dict(l) for l in cursor.execute("""
        SELECT * FROM plannings_ramasse
        WHERE annee = ? AND semaine = ?
        ORDER BY
            CASE LOWER(jour)
                WHEN 'lundi' THEN 1
                WHEN 'mardi' THEN 2
                WHEN 'mercredi' THEN 3
                WHEN 'jeudi' THEN 4
                WHEN 'vendredi' THEN 5
                ELSE 99
            END, id
    """, (annee, numero_semaine,)).fetchall()]

    # ------------------------------------------------------------------
    # 🔄 Conversion SQL → Python (UNE FOIS)
    # ------------------------------------------------------------------
    for l in lignes:
        for champ in [
            "chauffeur_absent", "responsable_absent", "equipier_absent",
            "ramasse_tri1_absent", "ramasse_tri2_absent", "ramasse_tri3_absent"
        ]:
            l[champ] = (str(l.get(champ)).lower() == "oui")

    # ------------------------------------------------------------------
    # 💾 POST : enregistrement manuel
    # ------------------------------------------------------------------
    if request.method == "POST":
        try:
            ids = request.form.getlist("ligne_ids[]")
            chauffeurs = request.form.getlist("chauffeur_ids[]")
            responsables = request.form.getlist("responsable_ids[]")
            equipiers = request.form.getlist("equipier_ids[]")
            tri1 = request.form.getlist("tri1_ids[]")
            tri2 = request.form.getlist("tri2_ids[]")
            tri3 = request.form.getlist("tri3_ids[]")
            camions_form = request.form.getlist("camion_ids[]")

            def to_int(v):
                try:
                    return int(v)
                except Exception:
                    return None

            sql = """
                UPDATE plannings_ramasse SET
                    chauffeur_id = ?, remplacant_chauffeur_id = ?,
                    responsable_id = ?, remplacant_responsable_id = ?,
                    equipier_id = ?, remplacant_equipier_id = ?,
                    ramasse_tri1_id = ?, remplacant_ramasse_tri1_id = ?,
                    ramasse_tri2_id = ?, remplacant_ramasse_tri2_id = ?,
                    ramasse_tri3_id = ?, remplacant_ramasse_tri3_id = ?,
                    camion_id = ?
                WHERE id = ?
            """

            for i, lid in enumerate(ids):
                old = cursor.execute(
                    "SELECT * FROM plannings_ramasse WHERE id = ?", (lid,)
                ).fetchone()

                params = []

                for role, new_val in [
                    ("chauffeur", chauffeurs[i]),
                    ("responsable", responsables[i]),
                    ("equipier", equipiers[i]),
                ]:
                    old_id = old[f"{role}_id"]
                    absent = (str(old[f"{role}_absent"]).lower() == "oui")
                    new_id = to_int(new_val)

                    if absent and new_id and new_id != old_id:
                        params.extend([old_id, new_id])
                    else:
                        params.extend([new_id, None])

                for role, new_val in [
                    ("ramasse_tri1", tri1[i]),
                    ("ramasse_tri2", tri2[i]),
                    ("ramasse_tri3", tri3[i]),
                ]:
                    old_id = old[f"{role}_id"]
                    absent = (str(old[f"{role}_absent"]).lower() == "oui")
                    new_id = to_int(new_val)

                    if absent and new_id and new_id != old_id:
                        params.extend([old_id, new_id])
                    else:
                        params.extend([new_id, None])

                params.append(to_int(camions_form[i]))
                params.append(lid)

                cursor.execute(sql, params)

            conn.commit()
            upload_database()
            flash("✅ Planning enregistré", "success")
            return redirect(url_for("planning.gestion_planning_ramasse", semaine=raw_semaine))

        except Exception as e:
            flash(f"❌ Erreur : {e}", "danger")

    # ------------------------------------------------------------------
    # 🟥 Synchronisation ABSENCES (logique SAINE)
    # ------------------------------------------------------------------
    jours_dates = {
        "lundi": lundi,
        "mardi": lundi + timedelta(days=1),
        "mercredi": lundi + timedelta(days=2),
        "jeudi": lundi + timedelta(days=3),
        "vendredi": lundi + timedelta(days=4),
    }

    absences = cursor.execute("""
        SELECT benevole_id, date_debut, date_fin
        FROM absences
    """).fetchall()

    absents_par_jour = {j: set() for j in jours_dates}

    for a in absences:
        try:
            debut = datetime.strptime(a["date_debut"], "%d/%m/%Y").date()
            fin = datetime.strptime(a["date_fin"], "%d/%m/%Y").date()

            for jour, d in jours_dates.items():
                if debut <= d <= fin:
                    absents_par_jour[jour].add(a["benevole_id"])
        except Exception:
            continue

    updated = False

    for l in lignes:
        jour = l["jour"].lower()
        absents = absents_par_jour.get(jour, set())

        for role in [
            "chauffeur", "responsable", "equipier",
            "ramasse_tri1", "ramasse_tri2", "ramasse_tri3"
        ]:
            bene_id = l.get(f"{role}_id")
            remp_id = l.get(f"remplacant_{role}_id")
            absent_flag = l.get(f"{role}_absent", False)

            if remp_id is None:
                is_absent_today = bene_id in absents if bene_id else False

                if l.get(f"{role}_absent") != is_absent_today:
                    l[f"{role}_absent"] = is_absent_today
                    updated = True

    if updated:
        for l in lignes:
            cursor.execute("""
                UPDATE plannings_ramasse SET
                    chauffeur_absent = ?,
                    responsable_absent = ?,
                    equipier_absent = ?,
                    ramasse_tri1_absent = ?,
                    ramasse_tri2_absent = ?,
                    ramasse_tri3_absent = ?
                WHERE id = ?
            """, (
                "oui" if l["chauffeur_absent"] else "non",
                "oui" if l["responsable_absent"] else "non",
                "oui" if l["equipier_absent"] else "non",
                "oui" if l["ramasse_tri1_absent"] else "non",
                "oui" if l["ramasse_tri2_absent"] else "non",
                "oui" if l["ramasse_tri3_absent"] else "non",
                l["id"]
            ))

        conn.commit()
        upload_database()

    # ------------------------------------------------------------------
    # 🎨 Affichage
    # ------------------------------------------------------------------
    for l in lignes:
        l["tournee_display"] = tournee_dict.get(l["tournee_id"], l["tournee"])

    type_benevole_options = get_type_benevole_options(conn)
    civilite_options = ["M", "Mme", "Mlle"]


    # ------------------------------------------------------------------
    # 📌 Collections par jour (pour affichage "déjà affecté")
    # ------------------------------------------------------------------
    chauffeurs_du_jour = {}
    responsables_du_jour = {}
    equipiers_du_jour = {}

    for l in lignes:
        jour = (l.get("jour") or "").lower()
        if not jour:
            continue

        if l.get("chauffeur_id"):
            chauffeurs_du_jour.setdefault(jour, set()).add(l["chauffeur_id"])

        if l.get("responsable_id"):
            responsables_du_jour.setdefault(jour, set()).add(l["responsable_id"])

        if l.get("equipier_id"):
            equipiers_du_jour.setdefault(jour, set()).add(l["equipier_id"])

    collections_du_jour = {
        "chauffeur": chauffeurs_du_jour,
        "responsable": responsables_du_jour,
        "equipier": equipiers_du_jour,
    }

    conn.close()

    return render_template(
        "gestion_planning_ramasse.html",
        semaine=raw_semaine,
        lignes=lignes,
        chauffeurs=filtrer("chauffeur", benevoles),
        responsables=filtrer("responsable", benevoles),
        equipiers=filtrer("equipier", benevoles),
        camions=camions,
        absents_par_jour=absents_par_jour,
        collections_du_jour=collections_du_jour,  # ✅ OBLIGATOIRE
        type_benevole_options=type_benevole_options,
        civilite_options=civilite_options,
    )




@planning_bp.route('/enregistrer_planning_ramasse', methods=['POST'])  # ✅ pas d'espace
@login_required
def enregistrer_planning_ramasse():
    from utils import get_db_connection, write_log, upload_database

    def to_int(val):
        """Convertit proprement en int. Gère '', None, 'None', 'null'."""
        if val is None:
            return None
        s = str(val).strip().lower()
        if s in ("", "none", "null"):
            return None
        try:
            return int(s)
        except ValueError:
            return None

    semaine = request.form.get("semaine")
    if not semaine or "-W" not in semaine:
        flash("❌ Semaine manquante ou invalide", "danger")
        return redirect(url_for('planning.planning_main'))

    try:
        annee, num_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Format de semaine invalide", "danger")
        return redirect(url_for('planning.planning_main'))

    # 🔄 Récupération des données postées (listes parallèles)
    jours         = request.form.getlist("jours[]")
    tournees      = request.form.getlist("tournees[]")
    tournee_ids   = request.form.getlist("tournee_ids[]")
    chauffeurs    = request.form.getlist("chauffeurs[]")
    responsables  = request.form.getlist("responsables[]")
    equipiers     = request.form.getlist("equipiers[]")
    camions       = request.form.getlist("camions[]")
    tri1_ids      = request.form.getlist("ramasse_tri1_id[]")
    tri2_ids      = request.form.getlist("ramasse_tri2_id[]")
    tri3_ids      = request.form.getlist("ramasse_tri3_id[]")

    n = len(jours)
    write_log(f"📋 {n} lignes à enregistrer pour la semaine {num_semaine}")

    # 🧱 Construire les lignes avec conversion robuste
    lignes = []
    for i in range(n):
        lignes.append({
            "jour":              jours[i],
            "tournee_id":        to_int(tournee_ids[i]) if i < len(tournee_ids) else None,
            "tournee":           tournees[i] if i < len(tournees) else "",
            "chauffeur_id":      to_int(chauffeurs[i]) if i < len(chauffeurs) else None,
            "responsable_id":    to_int(responsables[i]) if i < len(responsables) else None,
            "equipier_id":       to_int(equipiers[i]) if i < len(equipiers) else None,
            "ramasse_tri1_id":   to_int(tri1_ids[i]) if i < len(tri1_ids) else None,
            "ramasse_tri2_id":   to_int(tri2_ids[i]) if i < len(tri2_ids) else None,
            "ramasse_tri3_id":   to_int(tri3_ids[i]) if i < len(tri3_ids) else None,
            "camion_id":         to_int(camions[i]) if i < len(camions) else None,
        })

    # 💾 Écriture en base
    conn = get_db_connection()
    cursor = conn.cursor()

    # On repart propre pour cette semaine
    cursor.execute("DELETE FROM plannings_ramasse WHERE annee = ? AND semaine = ?", (annee, num_semaine,))

    # INSERT avec le bon nombre de colonnes/placeholders
    insert_sql = """
        INSERT INTO plannings_ramasse (
            type, semaine, tournee_id, tournee,
            chauffeur_id, responsable_id, equipier_id,
            ramasse_tri1_id, ramasse_tri2_id, ramasse_tri3_id,
            camion_id, jour
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    for l in lignes:
        cursor.execute(
            insert_sql,
            (
                "Ramasse", num_semaine, l["tournee_id"], l["tournee"],
                l["chauffeur_id"], l["responsable_id"], l["equipier_id"],
                l["ramasse_tri1_id"], l["ramasse_tri2_id"], l["ramasse_tri3_id"],
                l["camion_id"], l["jour"]
            )
        )

    conn.commit()
    conn.close()
    upload_database()

    flash(f"✅ Planning de la semaine {num_semaine} enregistré avec succès.", "success")
    return redirect(url_for('planning.gestion_planning_ramasse', semaine=semaine))


@planning_bp.route('/print_planning_ramasse', methods=['POST'])
@login_required
def print_planning_ramasse():
    from utils import get_db_connection
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from io import BytesIO

    impression_partielle = request.form.get("impression_partielle") == "on"

    semaine = request.form.get("semaine")
    try:
        annee, numero_semaine = map(int, semaine.split("-W"))
    except Exception:
        flash("❌ Semaine invalide", "danger")
        return redirect(url_for('planning.planning_main'))

    conn = get_db_connection()
    cursor = conn.cursor()

    lignes = cursor.execute("""
        SELECT jour, tournee, chauffeur_id, responsable_id, equipier_id, camion_id
        FROM plannings_ramasse
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
    """, (annee, numero_semaine,)).fetchall()

    conn.close()

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Planning Ramasse - Semaine {numero_semaine}")

    largeur, hauteur = A4
    x = 2 * cm
    y = hauteur - 2 * cm
    line_height = 14

    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(x, y, f"Planning de Ramasse - Semaine {numero_semaine}")
    y -= 1.5 * cm

    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(x, y, "Jour")
    pdf.drawString(x + 3*cm, y, "Tournée")
    pdf.drawString(x + 8*cm, y, "Chauffeur")

    if not impression_partielle:
        pdf.drawString(x + 13*cm, y, "Responsable")
        pdf.drawString(x + 18*cm, y, "Équipier")
        pdf.drawString(x + 23*cm, y, "Camion")    
        y -= line_height

    pdf.setFont("Helvetica", 10)
    for ligne in lignes:
        if y < 2 * cm:
            pdf.showPage()
            y = hauteur - 2 * cm
        pdf.drawString(x, y, ligne["jour"].capitalize())
        pdf.drawString(x + 3*cm, y, ligne["tournee"] or "-")
        pdf.drawString(x + 8*cm, y, get_nom_benevole(ligne["chauffeur_id"]))
        if not impression_partielle:
            pdf.drawString(x + 13*cm, y, get_nom_benevole(ligne["responsable_id"]))
            pdf.drawString(x + 18*cm, y, get_nom_benevole(ligne["equipier_id"]))
            pdf.drawString(x + 23*cm, y, get_nom_camion(ligne["camion_id"]))
        y -= line_height

    pdf.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True,
                     download_name=f"planning_ramasse_semaine_{numero_semaine}.pdf",
                     mimetype='application/pdf')




@planning_bp.route('/apercu_planning_ramasse', methods=['GET'], endpoint='apercu_planning_ramasse')
@login_required
def apercu_planning_ramasse():
    from datetime import datetime, timedelta
    from utils import get_db_connection, write_log

    semaine = request.args.get("semaine")
    if not semaine or "-W" not in semaine:
        flash("❌ Semaine invalide", "danger")
        return redirect(url_for('planning.planning_main'))

    annee, num_semaine = map(int, semaine.split("-W"))
    lundi = datetime.fromisocalendar(annee, num_semaine, 1).date()

    conn = get_db_connection()
    cursor = conn.cursor()

    lignes = cursor.execute("""
        SELECT * FROM plannings_ramasse
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
    """, (annee, num_semaine,)).fetchall()

    def get_nom(table, id, champs):
        if not id:
            return ""
        r = cursor.execute(f"SELECT {', '.join(champs)} FROM {table} WHERE id = ?", (id,)).fetchone()
        return " ".join([str(r[c]) for c in champs]) if r else ""

    def get_nom_camion(cid):
        if not cid:
            return ""
        r = cursor.execute("SELECT nom, immat FROM camions WHERE id = ?", (cid,)).fetchone()
        return f"{r['nom']} ({r['immat']})" if r else ""

    lignes = [dict(l) for l in lignes]

    # ✅ Conversion des champs *_absent en booléens
    for l in lignes:
        for champ in [
            "chauffeur_absent", "responsable_absent", "equipier_absent",
            "ramasse_tri1_absent", "ramasse_tri2_absent", "ramasse_tri3_absent"
        ]:
            l[champ] = str(l.get(champ)).strip().lower() == "oui"

    for l in lignes:
        jour = l["jour"].lower()
        delta = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"].index(jour)
        l["date_jour"] = (lundi + timedelta(days=delta)).strftime("%d/%m/%Y")

        # Chauffeur
        if l.get("remplacant_chauffeur_id"):
            id_chauffeur = l["remplacant_chauffeur_id"]
            l["chauffeur"] = get_nom("benevoles", id_chauffeur, ["prenom", "nom"]) + " <span class='remplacant'>(remplaçant)</span>"
        else:
            id_chauffeur = l["chauffeur_id"]
            l["chauffeur"] = get_nom("benevoles", id_chauffeur, ["prenom", "nom"])
            if l["chauffeur_absent"]:
                l["chauffeur"] += " <span class='absent'>(absent)</span>"

        # Responsable
        if l.get("remplacant_responsable_id"):
            id_responsable = l["remplacant_responsable_id"]
            l["responsable"] = get_nom("benevoles", id_responsable, ["prenom", "nom"]) + " <span class='remplacant'>(remplaçant)</span>"
        else:
            id_responsable = l["responsable_id"]
            l["responsable"] = get_nom("benevoles", id_responsable, ["prenom", "nom"])
            if l["responsable_absent"]:
                l["responsable"] += " <span class='absent'>(absent)</span>"

        # Équipier
        if l.get("remplacant_equipier_id"):
            id_equipier = l["remplacant_equipier_id"]
            l["equipier"] = get_nom("benevoles", id_equipier, ["prenom", "nom"]) + " <span class='remplacant'>(remplaçant)</span>"
        else:
            id_equipier = l["equipier_id"]
            l["equipier"] = get_nom("benevoles", id_equipier, ["prenom", "nom"])
            if l["equipier_absent"]:
                l["equipier"] += " <span class='absent'>(absent)</span>"

        # Tri 1
        if l.get("remplacant_ramasse_tri1_id"):
            id_tri1 = l["remplacant_ramasse_tri1_id"]
            l["ramasse_tri1"] = get_nom("benevoles", id_tri1, ["prenom", "nom"]) + " <span class='remplacant'>(remplaçant)</span>"
        else:
            id_tri1 = l["ramasse_tri1_id"]
            l["ramasse_tri1"] = get_nom("benevoles", id_tri1, ["prenom", "nom"])
            if l["ramasse_tri1_absent"]:
                l["ramasse_tri1"] += " <span class='absent'>(absent)</span>"

        # Tri 2
        if l.get("remplacant_ramasse_tri2_id"):
            id_tri2 = l["remplacant_ramasse_tri2_id"]
            l["ramasse_tri2"] = get_nom("benevoles", id_tri2, ["prenom", "nom"]) + " <span class='remplacant'>(remplaçant)</span>"
        else:
            id_tri2 = l["ramasse_tri2_id"]
            l["ramasse_tri2"] = get_nom("benevoles", id_tri2, ["prenom", "nom"])
            if l["ramasse_tri2_absent"]:
                l["ramasse_tri2"] += " <span class='absent'>(absent)</span>"

        # Tri 3
        if l.get("remplacant_ramasse_tri3_id"):
            id_tri3 = l["remplacant_ramasse_tri3_id"]
            l["ramasse_tri3"] = get_nom("benevoles", id_tri3, ["prenom", "nom"]) + " <span class='remplacant'>(remplaçant)</span>"
        else:
            id_tri3 = l["ramasse_tri3_id"]
            l["ramasse_tri3"] = get_nom("benevoles", id_tri3, ["prenom", "nom"])
            if l["ramasse_tri3_absent"]:
                l["ramasse_tri3"] += " <span class='absent'>(absent)</span>"

        # Camion
        l["camion"] = get_nom_camion(l["camion_id"])

        # Tournée
        tournee_id = l.get("tournee_id")
        if tournee_id:
            nom_tournee = cursor.execute("""
                SELECT nom FROM tournees_fournisseurs
                WHERE tournee_id = ? AND nom IS NOT NULL
                LIMIT 1
            """, (tournee_id,)).fetchone()

            fournisseurs = cursor.execute("""
                SELECT f.nom
                FROM tournees_fournisseurs tf
                JOIN fournisseurs f ON tf.fournisseur_id = f.id
                WHERE tf.tournee_id = ?
            """, (tournee_id,)).fetchall()

            noms_fournisseurs = " / ".join(f["nom"] for f in fournisseurs)
            nom_affiche = nom_tournee["nom"] if nom_tournee else f"Tournée {tournee_id}"
            l["tournee"] = f"{nom_affiche} — {noms_fournisseurs}" if noms_fournisseurs else nom_affiche
        else:
            l["tournee"] = l.get("tournee", "-")

    date_debut = lundi.strftime("%d/%m/%Y")
    date_fin = (lundi + timedelta(days=4)).strftime("%d/%m/%Y")

    conn.close()
    return render_template(
        "apercu_planning_ramasse.html",
        lignes=lignes,
        semaine=semaine,
        date_debut=date_debut,
        date_fin=date_fin
    )


@planning_bp.route('/apercu_modele_planning_ramasse')
@login_required
def apercu_modele_planning_ramasse():
    conn = get_db_connection()
    cursor = conn.cursor()

    # 🔄 Charger le modèle de planning ramasse
    model = cursor.execute("""
        SELECT * FROM planning_standard_ramasse_ids
        ORDER BY
            CASE jour
                WHEN 'lundi' THEN 1
                WHEN 'mardi' THEN 2
                WHEN 'mercredi' THEN 3
                WHEN 'jeudi' THEN 4
                WHEN 'vendredi' THEN 5
            END, numero
    """).fetchall()

    # 👤 Bénévoles
    benevoles = cursor.execute("SELECT id, nom || ' ' || prenom AS nom FROM benevoles").fetchall()
    bene_dict = {b["id"]: b["nom"] for b in benevoles}

    # 🚛 Camions
    camions = cursor.execute("SELECT id, nom || ' (' || immat || ')' AS nom FROM camions").fetchall()
    camions_dict = {c["id"]: c["nom"] for c in camions}

    # 📅 Vendredi travaillé ?
    param = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'").fetchone()
    travail_vendredi = param and param["param_value"].strip().lower() == "oui"

    # 📦 Noms des tournées + fournisseurs
    tournee_ids = {ligne["tournee_id"] for ligne in model if ligne["tournee_id"]}
    tournees_dict = {}
    for tid in tournee_ids:
        # Récupérer le nom unique de la tournée (non NULL)
        nom_row = cursor.execute("""
            SELECT nom FROM tournees_fournisseurs
            WHERE tournee_id = ? AND nom IS NOT NULL
            ORDER BY id LIMIT 1
        """, (tid,)).fetchone()
        nom = nom_row["nom"] if nom_row else f"Tournee{tid}"

        # Récupérer les fournisseurs associés à cette tournée
        rows = cursor.execute("""
            SELECT f.nom AS nom_fournisseur
            FROM tournees_fournisseurs tf
            JOIN fournisseurs f ON f.id = tf.fournisseur_id
            WHERE tf.tournee_id = ?
            ORDER BY tf.ordre
        """, (tid,)).fetchall()
        fournisseurs = " / ".join(r["nom_fournisseur"] for r in rows)

        tournees_dict[tid] = f"{nom} – {fournisseurs}"


    conn.close()

    return render_template("apercu_modele_planning_ramasse.html",
                           model=model,
                           tournees_dict=tournees_dict,
                           bene_dict=bene_dict,
                           camions_dict=camions_dict,
                           travail_vendredi=travail_vendredi)




@planning_bp.route('/maj_modele_planning_ramasse', methods=['GET', 'POST'])
@login_required
def maj_modele_planning_ramasse():
    conn = get_db_connection()
    cursor = conn.cursor()

    # 📥 Chargement des listes déroulantes
    tournees = cursor.execute("""
        SELECT tf.tournee_id,
            MAX(tf.nom) AS nom,
            GROUP_CONCAT(f.nom, ' / ') AS fournisseurs
        FROM (
            SELECT * FROM tournees_fournisseurs ORDER BY ordre
        ) tf
        JOIN fournisseurs f ON f.id = tf.fournisseur_id
        GROUP BY tf.tournee_id
        ORDER BY nom
    """).fetchall()

    # Remplacer les noms None par get_nom_tournee
    tournees = [
        dict(tournee) | {"nom": get_nom_tournee(tournee["tournee_id"])}
        for tournee in tournees
    ]


    benevoles = cursor.execute("""
        SELECT id, nom || ' ' || prenom AS nom_complet,
            ramasse_chauffeur, ramasse_equipier, ramasse_responsable_tri, ramasse_tri_externe
        FROM benevoles
        ORDER BY nom, prenom
    """).fetchall()

    benevoles_externes = cursor.execute("""
        SELECT id, nom || ' ' || prenom AS nom_complet
        FROM benevoles
        WHERE ramasse_tri_externe = 'oui'
        ORDER BY nom, prenom
    """).fetchall()

    camions = cursor.execute("""
        SELECT id, nom || ' (' || immat || ')' AS nom_complet
        FROM camions ORDER BY nom
    """).fetchall()

    model = cursor.execute("""
        SELECT * FROM planning_standard_ramasse_ids
        ORDER BY
            CASE jour
                WHEN 'lundi' THEN 1
                WHEN 'mardi' THEN 2
                WHEN 'mercredi' THEN 3
                WHEN 'jeudi' THEN 4
                WHEN 'vendredi' THEN 5
            END, numero
    """).fetchall()

    if request.method == "POST" and request.form.get("action") == "supprimer":
        ligne_id = request.form.get("ligne_id")
        if ligne_id:
            try:
                cursor.execute("DELETE FROM plannings_ramasse WHERE id = ?", (ligne_id,))
                conn.commit()
                flash("🗑️ Ligne supprimée du planning.", "warning")
            except Exception as e:
                flash(f"❌ Erreur lors de la suppression : {e}", "danger")
        else:
            flash("❌ ID de ligne manquant pour la suppression.", "danger")

        return redirect(url_for('planning.maj_modele_planning_ramasse'))


    if request.method == 'POST':
        try:
            cursor.execute("DELETE FROM planning_standard_ramasse_ids")

            for jour in ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi']:
                for i in range(1, 6):
                    suffix = f"{jour}_{i}"
                    cursor.execute("""
                        INSERT INTO planning_standard_ramasse_ids (
                            jour, numero, tournee_id,
                            chauffeur_id, responsable_id, equipier_id,
                            ramasse_tri1_id, ramasse_tri2_id, ramasse_tri3_id,
                            camion_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        jour,
                        i,
                        request.form.get(f"tournee_{suffix}") or None,
                        request.form.get(f"chauffeur_{suffix}") or None,
                        request.form.get(f"responsable_{suffix}") or None,
                        request.form.get(f"equipier_{suffix}") or None,
                        request.form.get(f"ramasse_tri1_id_{suffix}") or None,
                        request.form.get(f"ramasse_tri2_id_{suffix}") or None,
                        request.form.get(f"ramasse_tri3_id_{suffix}") or None,
                        request.form.get(f"camion_{suffix}") or None,
                    ))

            conn.commit()
            flash("✅ Modèle de planning mis à jour avec succès.", "success")
            return redirect(url_for('planning.maj_modele_planning_ramasse'))
        except Exception as e:
            flash(f"❌ Erreur : {e}", "danger")

    param = cursor.execute("SELECT param_value FROM parametres WHERE param_name = 'travail_vendredi'").fetchone()
    travail_vendredi = param and param["param_value"].strip().lower() == "oui"

    conn.close()
    upload_database()

    return render_template("maj_modele_planning_ramasse.html", model=model, tournees=tournees, benevoles=benevoles, benevoles_externes=benevoles_externes, camions=camions, travail_vendredi=travail_vendredi)


@planning_bp.route('/ajouter_fournisseur', methods=['POST'])
@login_required
def ajouter_fournisseur():
    nom = request.form.get('nom', '').strip()
    if nom:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO fournisseurs (nom) VALUES (?)", (nom,))
            conn.commit()
            conn.close()
            flash("✅ Fournisseur ajouté avec succès", "success")
        except Exception as e:
            flash(f"❌ Erreur : {e}", "danger")
    else:
        flash("❌ Nom de fournisseur vide", "warning")

    return redirect(url_for('planning.planning_tournees'))





