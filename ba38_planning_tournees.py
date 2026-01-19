from flask import Blueprint, render_template, request, redirect, url_for, flash, g
from flask_login import login_required
from utils import get_db_connection, upload_database, write_log  # get_db_connection déjà importé ici
from ba38_planning_utils import get_nom_tournee, get_fournisseurs_par_tournee_id

import sqlite3

planning_tournees_bp = Blueprint("planning_tournees", __name__)


@planning_tournees_bp.route('/planning_tournees', methods=['GET', 'POST'])
@login_required
def planning_tournees():
    conn = get_db_connection()
    cursor = conn.cursor()

    # ============================================================
    # POST : création / modification d'une tournée
    # ============================================================
    if request.method == 'POST':
        tournee_id = request.form.get("tournee_id")
        nom = request.form.get("nom", "").strip()
        duree_str = request.form.get("duree", "").strip()

        # Durée : on essaie de convertir, sinon None
        try:
            duree = int(duree_str) if duree_str else None
        except ValueError:
            duree = None

        fournisseurs_ids = request.form.getlist("fournisseurs[]")
        ordres = {
            int(fid): int(request.form.get(f"ordre_{fid}", "0") or "0")
            for fid in fournisseurs_ids
        }

        try:
            if tournee_id:
                # 🔄 Mise à jour : on supprime les anciennes associations
                cursor.execute(
                    "DELETE FROM tournees_fournisseurs WHERE tournee_id = ?",
                    (tournee_id,),
                )
            else:
                # ➕ Création d’un nouvel ID
                max_id = cursor.execute(
                    "SELECT MAX(tournee_id) FROM tournees_fournisseurs"
                ).fetchone()[0] or 0
                tournee_id = max_id + 1

            # 📝 Insertion du nom (au moins une ligne avec le nom + durée)
            cursor.execute(
                """
                INSERT INTO tournees_fournisseurs (tournee_id, fournisseur_id, ordre, nom, duree)
                VALUES (?, NULL, NULL, ?, ?)
            """,
                (tournee_id, nom, duree),
            )

            # ➕ Insertion des fournisseurs sélectionnés
            for fid in fournisseurs_ids:
                fid = int(fid)
                ordre = ordres.get(fid, 0)
                cursor.execute(
                    """
                    INSERT INTO tournees_fournisseurs (tournee_id, fournisseur_id, ordre, nom, duree)
                    VALUES (?, ?, ?, NULL, ?)
                """,
                    (tournee_id, fid, ordre, duree),
                )

            conn.commit()
            flash("✅ Tournée enregistrée avec succès.", "success")
        except Exception as e:
            conn.rollback()
            flash(f"❌ Erreur lors de l’enregistrement : {e}", "danger")

    # ============================================================
    # GET : affichage des données
    # ============================================================
    # 📥 Chargement des données
    # (on garde row_factory du get_db_connection, généralement sqlite3.Row)
    conn.row_factory = None  # on conserve la logique existante : le cursor actuel reste en Row

    tournee_ids = [
        dict(row)
        for row in cursor.execute(
            "SELECT DISTINCT tournee_id FROM tournees_fournisseurs ORDER BY tournee_id"
        ).fetchall()
    ]

    fournisseurs = cursor.execute(
        "SELECT id, nom FROM fournisseurs ORDER BY nom COLLATE NOCASE"
    ).fetchall()

    # On relit aussi duree pour chaque ligne
    lignes = cursor.execute(
        "SELECT tournee_id, fournisseur_id, ordre, duree FROM tournees_fournisseurs"
    ).fetchall()

    # mapping des fournisseurs par tournée
    mapping = {}
    for row in lignes:
        t_id = row["tournee_id"]
        f_id = row["fournisseur_id"]
        mapping.setdefault(t_id, []).append(f_id)

    # noms des tournées (une ligne par tournee_id)
    noms = cursor.execute(
        "SELECT tournee_id, nom FROM tournees_fournisseurs "
        "WHERE nom IS NOT NULL GROUP BY tournee_id"
    ).fetchall()
    for row in noms:
        mapping[str(row["tournee_id"]) + "_nom"] = row["nom"]

    # tri des tournées par nom
    tournee_ids.sort(key=lambda t: mapping.get(str(t["tournee_id"]) + "_nom", "").lower())

    # mapping des ordres
    mapping_ordre = {}
    for row in lignes:
        tid = row["tournee_id"]
        fid = row["fournisseur_id"]
        ordre = row["ordre"]
        if tid is not None and fid is not None:
            mapping_ordre.setdefault(tid, {})[fid] = ordre or 999

    # mapping des durées
    mapping_duree = {}
    durees = cursor.execute(
        "SELECT tournee_id, duree FROM tournees_fournisseurs "
        "WHERE fournisseur_id IS NULL"
    ).fetchall()
    for row in durees:
        mapping_duree[row["tournee_id"]] = row["duree"]

    conn.close()
    upload_database()

    # ============================================================
    # Calcul du nom détaillé de la tournée (comme en planning ramasse)
    # ============================================================
    details = {}
    for t in tournee_ids:
        tid = t["tournee_id"]
        try:
            nom_court = get_nom_tournee(tid)
            fournisseurs_liste = get_fournisseurs_par_tournee_id(tid)
            details[tid] = f"{nom_court} — {fournisseurs_liste}"
        except Exception:
            details[tid] = ""

    # 🔀 TRI PAR NOM COURT
    tournee_ids.sort(
        key=lambda t: mapping.get(str(t["tournee_id"]) + "_nom", "").lower()
    )



    conn.close()
    upload_database()

    return render_template(
        "planning_tournees.html",
        tournees=tournee_ids,
        fournisseurs=fournisseurs,
        mapping=mapping,
        mapping_ordre=mapping_ordre,
        mapping_duree=mapping_duree,
        details=details
    )


@planning_tournees_bp.route('/modifier_tournee/<int:tournee_id>', methods=['POST'])
@login_required
def modifier_tournee(tournee_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    nom = request.form.get('nom', '').strip()
    duree_str = request.form.get("duree", "").strip()
    try:
        duree = int(duree_str) if duree_str else None
    except ValueError:
        duree = None

    fournisseurs_ids = request.form.getlist('fournisseurs')

    try:
        # 🔁 Mise à jour du nom + durée pour toutes les lignes de la tournée
        cursor.execute(
            "UPDATE tournees_fournisseurs SET nom = ?, duree = ? WHERE tournee_id = ?",
            (nom, duree, tournee_id),
        )

        # 🔁 Supprimer uniquement les lignes fournisseurs
        cursor.execute(
            "DELETE FROM tournees_fournisseurs "
            "WHERE tournee_id = ? AND fournisseur_id IS NOT NULL",
            (tournee_id,),
        )

        # 🔁 Réinsertion des fournisseurs avec la durée
        for f_id in fournisseurs_ids:
            ordre = request.form.get(f"ordre_{f_id}", 0)
            cursor.execute(
                """
                INSERT INTO tournees_fournisseurs (tournee_id, fournisseur_id, ordre, nom, duree)
                VALUES (?, ?, ?, NULL, ?)
            """,
                (tournee_id, f_id, ordre, duree),
            )

        conn.commit()
        flash("✅ Tournée modifiée", "success")
    except Exception as e:
        flash(f"❌ Erreur : {e}", "danger")
    finally:
        conn.close()
        upload_database()

    return redirect(url_for('planning_tournees.planning_tournees'))


@planning_tournees_bp.route('/supprimer_tournee/<int:tournee_id>', methods=['POST'])
@login_required
def supprimer_tournee(tournee_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM tournees_fournisseurs WHERE tournee_id = ?", (tournee_id,))
        conn.commit()
        flash("🗑️ Tournée supprimée", "warning")
    except Exception as e:
        flash(f"❌ Erreur lors de la suppression : {e}", "danger")
    finally:
        conn.close()
        upload_database()

    return redirect(url_for('planning_tournees.planning_tournees'))


@planning_tournees_bp.route('/ajouter_fournisseur', methods=['POST'])
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

    return redirect(url_for('planning_tournees.planning_tournees'))


@planning_tournees_bp.route('/print_tournees')
@login_required
def print_tournees():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row   # ✅ permet row["colonne"]
    cursor = conn.cursor()

    # Liste des fournisseurs (Row → OK)
    fournisseurs = cursor.execute(
        "SELECT id, nom FROM fournisseurs ORDER BY nom"
    ).fetchall()

    fournisseurs_dict = {f["id"]: f["nom"] for f in fournisseurs}

    # Toutes les lignes des tournées
    lignes = cursor.execute(
        "SELECT tournee_id, fournisseur_id, nom, ordre "
        "FROM tournees_fournisseurs ORDER BY tournee_id, ordre"
    ).fetchall()

    tournees = {}
    for row in lignes:
        t_id = row["tournee_id"]

        # Si nouvelle tournée → init
        if t_id not in tournees:
            tournees[t_id] = {
                "nom": row["nom"],
                "fournisseurs": []
            }

        fid = row["fournisseur_id"]

        # Ajouter fournisseur
        if fid is not None:
            tournees[t_id]["fournisseurs"].append({
                "nom": fournisseurs_dict.get(fid, "❓"),
                "ordre": row["ordre"] if row["ordre"] else 999
            })

    conn.close()

    return render_template("print_tournees.html", tournees=tournees)


@planning_tournees_bp.route("/maj_fournisseurs", methods=["GET", "POST"])
@login_required
def maj_fournisseurs():

    if g.user_role not in ['admin', 'gestionnaire']:
        flash("❌ Accès réservé aux gestionnaires", "danger")
        return redirect(url_for("planning_tournees.planning_tournees"))

    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "ajouter":
            data = {
                k: request.form.get(k, "").strip()
                for k in [
                    "nom",
                    "adresse",
                    "cp",
                    "ville",
                    "tel",
                    "mail",
                    "lundi",
                    "horaire_lundi",
                    "mardi",
                    "horaire_mardi",
                    "mercredi",
                    "horaire_mercredi",
                    "jeudi",
                    "horaire_jeudi",
                ]
            }
            columns = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            values = list(data.values())
            cursor.execute(
                f"INSERT INTO fournisseurs ({columns}) VALUES ({placeholders})",
                values,
            )

        elif action == "modifier_global":
            fournisseurs = request.form.to_dict(flat=False)
            for i in range(len(fournisseurs["fournisseurs[0][id]"])):
                d = {
                    key.split("[")[2][:-1]: fournisseurs[key][i]
                    for key in fournisseurs
                    if key.startswith(f"fournisseurs[{i}]")
                }
                placeholders = ", ".join([f"{k} = ?" for k in d if k != "id"])
                values = [d[k] for k in d if k != "id"] + [d["id"]]
                cursor.execute(
                    f"UPDATE fournisseurs SET {placeholders} WHERE id = ?", values
                )

        elif action == "supprimer":
            fournisseur_id = request.form.get("id")
            cursor.execute("DELETE FROM fournisseurs WHERE id = ?", (fournisseur_id,))

        conn.commit()
        upload_database()
        return redirect(url_for("planning_tournees.maj_fournisseurs"))

    fournisseurs = cursor.execute(
        "SELECT * FROM fournisseurs ORDER BY nom COLLATE NOCASE"
    ).fetchall()
    return render_template("maj_fournisseurs.html", fournisseurs=fournisseurs)


@planning_tournees_bp.route('/maj_camions', methods=['GET', 'POST'])
@login_required
def maj_camions():
    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == 'POST':
        action = request.form.get('action')
        camion_id = request.form.get('id')
        nom = request.form.get('nom', '').strip()
        immat = request.form.get('immat', '').strip()

        try:
            if action == 'ajouter':
                cursor.execute(
                    "INSERT INTO camions (nom, immat) VALUES (?, ?)", (nom, immat)
                )
                flash("✅ Camion ajouté", "success")
            elif action == 'modifier':
                cursor.execute(
                    "UPDATE camions SET nom = ?, immat = ? WHERE id = ?",
                    (nom, immat, camion_id),
                )
                flash("🔄 Camion modifié", "info")
            elif action == 'supprimer':
                cursor.execute("DELETE FROM camions WHERE id = ?", (camion_id,))
                flash("🗑️ Camion supprimé", "warning")
            conn.commit()
        except Exception as e:
            flash(f"❌ Erreur : {e}", "danger")

    camions = cursor.execute(
        "SELECT * FROM camions ORDER BY nom COLLATE NOCASE"
    ).fetchall()
    conn.close()
    upload_database()
    return render_template("maj_camions.html", camions=camions)

@planning_tournees_bp.route("/save_all", methods=["POST"])
@login_required
def save_all():
    conn = get_db_connection()
    cursor = conn.cursor()

    tournees = {}

    # ➜ Liste des ID de tournées envoyées dans le formulaire
    ids = request.form.getlist("tournees_ids")

    for tid in ids:
        tid = int(tid)

        # Lecture des champs simples
        nom = request.form.get(f"tournees[{tid}][nom]", "").strip()
        duree = request.form.get(f"tournees[{tid}][duree]", "").strip()
        try:
            duree = int(duree)
        except:
            duree = None

        # Liste des fournisseurs cochés
        fournisseurs = request.form.getlist(f"tournees[{tid}][fournisseurs][]")

        # Lecture des ordres
        ordres = {}
        for fid in fournisseurs:
            fid_int = int(fid)
            ordre = request.form.get(
                f"tournees[{tid}][ordre][{fid}]",
                "0"
            )
            try:
                ordre = int(ordre)
            except:
                ordre = 0
            ordres[fid_int] = ordre

        tournees[tid] = {
            "nom": nom,
            "duree": duree,
            "fournisseurs": [int(fid) for fid in fournisseurs],
            "ordre": ordres
        }

    try:
        for tid, data in tournees.items():

            nom = data["nom"]
            duree = data["duree"]
            fournisseurs = data["fournisseurs"]
            ordres = data["ordre"]

            # 🔄 Suppression des anciennes lignes
            cursor.execute(
                "DELETE FROM tournees_fournisseurs WHERE tournee_id = ?",
                (tid,)
            )

            # 🧭 Ligne principale (nom + durée)
            cursor.execute(
                """
                INSERT INTO tournees_fournisseurs
                (tournee_id, fournisseur_id, ordre, nom, duree)
                VALUES (?, NULL, NULL, ?, ?)
                """,
                (tid, nom, duree)
            )

            # 🏪 Réinsertion des fournisseurs cochés
            for fid in fournisseurs:
                cursor.execute(
                    """
                    INSERT INTO tournees_fournisseurs
                    (tournee_id, fournisseur_id, ordre, nom, duree)
                    VALUES (?, ?, ?, NULL, ?)
                    """,
                    (tid, fid, ordres.get(fid, 0), duree)
                )

        conn.commit()
        flash("💾 Toutes les tournées ont été enregistrées.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"❌ Erreur lors de l’enregistrement : {e}", "danger")

    conn.close()
    upload_database()

    return redirect(url_for("planning_tournees.planning_tournees"))
