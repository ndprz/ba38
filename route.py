from flask import request, render_template, redirect, url_for, flash
from flask_login import login_required
from datetime import datetime, timedelta
from utils import get_db_connection, write_log, upload_database
from ba38_planning_utils import filtrer

@planning_bp.route('/gestion_planning_ramasse', methods=['GET', 'POST'])
@login_required
def gestion_planning_ramasse():
    raw_semaine = request.args.get("semaine", "")
    try:
        numero_semaine = int(raw_semaine.split("-W")[1])
    except Exception:
        write_log(f"❌ Semaine invalide ou manquante : {raw_semaine}")
        flash("❌ La semaine fournie est invalide.", "danger")
        return redirect(url_for("planning.planning_main"))

    lundi = get_lundi_de_la_semaine(raw_semaine)
    if lundi is None:
        flash("❌ Erreur : semaine invalide", "danger")
        return redirect(url_for("planning.planning_main"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    tournees = cursor.execute("""
        SELECT tf.tournee_id, tf.nom,
               GROUP_CONCAT(f.nom, ' / ') AS fournisseurs
        FROM tournees_fournisseurs tf
        JOIN fournisseurs f ON f.id = tf.fournisseur_id
        GROUP BY tf.tournee_id, tf.nom
    """).fetchall()

    tournee_dict = {
        t["tournee_id"]: f"{t['nom']} — {t['fournisseurs']}" if t["fournisseurs"] else t["nom"]
        for t in tournees
    }

    benevoles = cursor.execute("SELECT * FROM benevoles ORDER BY nom COLLATE NOCASE").fetchall()
    benevoles = [dict(b) for b in benevoles]
    camions = cursor.execute("SELECT * FROM camions ORDER BY nom COLLATE NOCASE").fetchall()

    lignes = cursor.execute("""
        SELECT * FROM plannings_ramasse
        WHERE semaine = ?
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
    """, (numero_semaine,)).fetchall()
    lignes = [dict(l) for l in lignes]

    jours_dates = {day: lundi + timedelta(days=i) for i, day in enumerate(["lundi", "mardi", "mercredi", "jeudi", "vendredi"])}

    absences = cursor.execute("SELECT benevole_id, date_debut, date_fin FROM absences").fetchall()
    absents_par_jour = {j: set() for j in jours_dates}
    for a in absences:
        try:
            debut = datetime.strptime(a[1], "%d/%m/%Y").date()
            fin = datetime.strptime(a[2], "%d/%m/%Y").date()
            for jour, date_obj in jours_dates.items():
                if debut <= date_obj <= fin:
                    absents_par_jour[jour].add(a[0])
        except:
            continue

    chauffeurs_du_jour = {}
    responsables_du_jour = {}
    equipiers_du_jour = {}
    ramasse_tri1_du_jour = {}
    ramasse_tri2_du_jour = {}
    ramasse_tri3_du_jour = {}

    for l in lignes:
        jour = l.get("jour", "").lower()
        if not jour:
            continue
        
        for champ in [
            "chauffeur_id", "equipier_id", "responsable_id",
            "ramasse_tri1_id", "ramasse_tri2_id", "ramasse_tri3_id",
            "camion_id",
            "remplacant_chauffeur_id", "remplacant_equipier_id", "remplacant_responsable_id",
            "remplacant_ramasse_tri1_id", "remplacant_ramasse_tri2_id", "remplacant_ramasse_tri3_id"
        ]:
            val = l.get(champ)
            if isinstance(val, str) and val.strip().lower() == "none":
                l[champ] = None
            elif val not in (None, "", "NULL"):
                try:
                    l[champ] = int(val)
                except:
                    l[champ] = None

        tournee_id = l.get("tournee_id")
        l["tournee_display"] = tournee_dict.get(tournee_id, l.get("tournee", ""))

        l["chauffeur_absent"] = 1 if l.get("chauffeur_id") in absents_par_jour.get(jour, set()) else 0
        l["equipier_absent"] = 1 if l.get("equipier_id") in absents_par_jour.get(jour, set()) else 0
        l["responsable_absent"] = 1 if l.get("responsable_id") in absents_par_jour.get(jour, set()) else 0

        if l.get("chauffeur_id"):
            chauffeurs_du_jour.setdefault(jour, set()).add(l["chauffeur_id"])
        if l.get("responsable_id"):
            responsables_du_jour.setdefault(jour, set()).add(l["responsable_id"])
        if l.get("equipier_id"):
            equipiers_du_jour.setdefault(jour, set()).add(l["equipier_id"])
        if l.get("ramasse_tri1_id"):
            ramasse_tri1_du_jour.setdefault(jour, set()).add(l["ramasse_tri1_id"])
        if l.get("ramasse_tri2_id"):
            ramasse_tri2_du_jour.setdefault(jour, set()).add(l["ramasse_tri2_id"])
        if l.get("ramasse_tri3_id"):
            ramasse_tri3_du_jour.setdefault(jour, set()).add(l["ramasse_tri3_id"])

    conn.close()
    upload_database()

    return render_template(
        "gestion_planning_ramasse.html",
        semaine=raw_semaine,
        lignes=lignes,
        chauffeurs=filtrer("chauffeur", benevoles),
        responsables=filtrer("responsable", benevoles),
        equipiers=filtrer("equipier", benevoles),
        camions=camions,
        chauffeurs_du_jour=chauffeurs_du_jour,
        responsables_du_jour=responables_du_jour,
        equipiers_du_jour=equipiers_du_jour,
        ramasse_tri1_du_jour=ramasse_tri1_du_jour,
        ramasse_tri2_du_jour=ramasse_tri2_du_jour,
        ramasse_tri3_du_jour=ramasse_tri3_du_jour,
        tournee_dict=tournee_dict,
        absents_par_jour=absents_par_jour,
        jours_dates=jours_dates,
        benevoles=benevoles
    )
