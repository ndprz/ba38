
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
import sqlite3
from ba38_utilitaires.core import get_db_connection, upload_database


planning_utils_bp = Blueprint('planning_utils', __name__)

def get_etat_plannings(conn):
    """
    Retourne une liste de dicts :
    [
        {
            "type": "Ramasse",
            "type_code": "ramasse",
            "table": "plannings_ramasse",
            "semaine": 48,
            "annee": 2025
        },
        ...
    ]
    """

    cursor = conn.cursor()

    def semaines(table):
        rows = cursor.execute(
            f"SELECT DISTINCT semaine, annee FROM {table}"
        ).fetchall()

        result = []
        for r in rows:
            try:
                result.append((int(r["semaine"]), int(r["annee"])))
            except Exception:
                continue
        return result

    configs = [
        ("Ramasse",      "ramasse",      "plannings_ramasse"),
        ("Distribution", "distribution", "plannings_distribution"),
        ("Palettes",     "palettes",     "plannings_pal"),
        ("Pesée",        "pesee",         "plannings_pesee"),
        ("VIF",          "vif",           "plannings_vif"),
        ("Cuisine",      "cuisine",       "plannings_cuisine"),
    ]

    data = []

    for label, code, table in configs:
        for sem, annee in semaines(table):
            data.append({
                "type": label,
                "type_code": code,
                "table": table,
                "semaine": sem,
                "annee": annee
            })

    # Tri par défaut : année puis semaine décroissantes, puis type
    data.sort(key=lambda x: (-x["annee"], -x["semaine"], x["type"]))

    return data

@planning_utils_bp.route("/etat_plannings")
@login_required
def etat_plannings():
    import sqlite3
    from flask import url_for

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    data = get_etat_plannings(conn)

    for d in data:
        d["semaine_iso"] = f"{d['annee']}-W{int(d['semaine']):02d}"

        # 🔗 URLs centralisées ICI (logique globale)
        if d["type_code"] == "ramasse":
            d["url"] = url_for(
                "planning.apercu_planning_ramasse",
                semaine=d["semaine_iso"]
            )

        elif d["type_code"] == "distribution":
            d["url"] = url_for(
                "planning_dist.apercu_planning_distribution",
                semaine=d["semaine_iso"]
            )

        elif d["type_code"] == "palettes":
            d["url"] = url_for(
                "planning_palettes.apercu_planning_palettes",
                semaine=d["semaine_iso"]
            )

        elif d["type_code"] == "pesee":
            d["url"] = url_for(
                "planning_pesee.apercu_planning_pesee",
                semaine=d["semaine_iso"]
            )

        elif d["type_code"] == "vif":
            d["url"] = url_for(
                "planning_vif.apercu_planning_vif",
                semaine=d["semaine_iso"]
            )

        elif d["type_code"] == "cuisine":
            d["url"] = url_for(
                "planning_cuisine.apercu_planning_cuisine",
                semaine=d["semaine_iso"]
            )

    conn.close()

    return render_template(
        "planning/utils_planning/etat_plannings.html",
        plannings=data
    )

@planning_utils_bp.route("/etat_plannings_delete", methods=["POST"])
@login_required
def etat_plannings_delete():
    selections = request.form.getlist("selections[]")

    if not selections:
        flash("Aucun planning sélectionné.", "warning")
        return redirect(url_for("planning_utils.etat_plannings"))

    conn = get_db_connection()
    cur = conn.cursor()

    total = 0

    try:
        for sel in selections:
            # format attendu : table|annee|semaine
            table, annee, semaine = sel.split("|")
            annee = int(annee)
            semaine = int(semaine)

            cur.execute(
                f"DELETE FROM {table} WHERE semaine = ? AND annee = ?",
                (semaine, annee)
            )
            total += cur.rowcount

        conn.commit()
        upload_database()
        flash(f"🗑️ {total} ligne(s) supprimée(s).", "success")

    except Exception as e:
        conn.rollback()
        flash(f"❌ Erreur suppression : {e}", "danger")

    finally:
        conn.close()

    return redirect(url_for("planning_utils.etat_plannings"))


def get_lundi_de_la_semaine(iso_week_str):
    """Retourne le lundi de la semaine ISO '2024-W12'"""
    annee, semaine = map(int, iso_week_str.split("-W"))
    return datetime.fromisocalendar(annee, semaine, 1).date()

def get_benevole_infos(benevole_id):
    if not benevole_id:
        return {}
    conn = get_db_connection()
    cursor = conn.cursor()
    r = cursor.execute("SELECT nom, prenom, telephone_portable AS phone, email FROM benevoles WHERE id = ?", (benevole_id,)).fetchone()
    conn.close()
    return dict(r) if r else {}

def parse_id(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return None

def parse_numero_semaine(valeur_week):
    """Extrait un entier à partir d'un champ <input type=week>, ex: 2025-W17 → 17"""
    try:
        return int(valeur_week.split("-W")[1])
    except Exception:
        return None


def get_nom(t, table, champs):
    if not t:
        return "-"
    conn = get_db_connection()
    cursor = conn.cursor()
    row = cursor.execute(f"SELECT {', '.join(champs)} FROM {table} WHERE id = ?", (t,)).fetchone()
    conn.close()
    if row:
        return " ".join(str(r) for r in row if r)
    return "-"

def get_nom_benevole(bid):
    return get_nom(bid, "benevoles", ["prenom", "nom"])

def get_nom_camion(cid):
    return get_nom(cid, "camions", ["nom", "immat"])

def get_fournisseurs_par_tournee_id(tournee_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT f.nom FROM tournees_fournisseurs tf
        JOIN fournisseurs f ON f.id = tf.fournisseur_id
        WHERE tf.tournee_id = ?
    """, (tournee_id,)).fetchall()
    conn.close()
    return " / ".join(r[0] for r in rows) if rows else "-"

def get_fournisseurs_pour_tournee(nom_tournee):
    conn = get_db_connection()
    cursor = conn.cursor()
    row = cursor.execute("SELECT fournisseur1, fournisseur2, fournisseur3, fournisseur4, fournisseur5 FROM tournees WHERE nom = ?", (nom_tournee,)).fetchone()
    conn.close()
    if not row:
        return ""
    return " / ".join([f for f in row if f and f.strip()])


def get_nom_tournee(tournee_id):
    if not tournee_id:
        return ""
    conn = get_db_connection()
    cursor = conn.cursor()
    row = cursor.execute("""
        SELECT nom FROM tournees_fournisseurs
        WHERE tournee_id = ? AND nom IS NOT NULL
        ORDER BY id LIMIT 1
    """, (tournee_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else f"Tournée {tournee_id}"


def get_absents_par_jour(absences, jours_dates):
    absents_par_jour = {j: set() for j in jours_dates}
    for a in absences:
        try:
            debut = datetime.strptime(a[1], "%d/%m/%Y").date()
            fin = datetime.strptime(a[2], "%d/%m/%Y").date()
            for jour, date_obj in jours_dates.items():
                if debut <= date_obj <= fin:
                    absents_par_jour[jour].add(a[0])
        except Exception:
            continue
    return absents_par_jour

def filtrer(role, benevoles):
    """
    Filtre les bénévoles en fonction de leur rôle : chauffeur, responsable ou équipier.
    - Priorise ceux marqués "oui" pour ce rôle.
    - Trie ensuite alphabétiquement par nom.

    :param role: str, un des "chauffeur", "responsable" ou "equipier"
    :param benevoles: list of dict, liste des bénévoles avec leurs attributs
    :return: list triée de bénévoles
    """
    champs_mapping = {
        "chauffeur": "ramasse_chauffeur",
        "responsable": "ramasse_responsable_tri",
        "equipier": "ramasse_equipier"
    }
    champ_reel = champs_mapping.get(role, role)

    return sorted(
        benevoles,
        key=lambda b: (
            (str(b.get(champ_reel) or '').strip().lower() != 'oui'),  # ✅ Priorise "oui"
            b.get('nom', '').lower()  # ✅ Trie alphabétiquement ensuite
        )
    )


def get_parametre_valeur(cle, default=""):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT param_value FROM parametres WHERE param_name = ?", (cle,))
        result = cur.fetchone()
        return result[0] if result else default


@planning_utils_bp.route("/planning_absences", methods=["GET", "POST"])
@login_required
def planning_absences():
    conn = get_db_connection()
    cur = conn.cursor()

    voir_toutes = request.args.get("voir_toutes") == "1"

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "ajouter_absence":
            try:
                # 1) ID bénévole fiable
                benevole_id = request.form.get("benevole_id")
                try:
                    benevole_id = int(benevole_id)
                except (TypeError, ValueError):
                    raise ValueError("Bénévole invalide")

                existe = cur.execute("SELECT 1 FROM benevoles WHERE id = ?", (benevole_id,)).fetchone()
                if not existe:
                    raise ValueError("Bénévole inexistant")

                # 2) Dates (on vérifie juste le format jj/mm/aaaa)
                from datetime import datetime
                date_debut = (request.form.get("date_debut") or "").strip()
                date_fin   = (request.form.get("date_fin") or "").strip()
                # Validation rapide du format attendu
                datetime.strptime(date_debut, "%d/%m/%Y")
                datetime.strptime(date_fin,   "%d/%m/%Y")

                # 3) Insertion
                cur.execute("""
                    INSERT INTO absences (benevole_id, date_debut, date_fin)
                    VALUES (?, ?, ?)
                """, (benevole_id, date_debut, date_fin))
                conn.commit()
                upload_database()
                flash("✅ Absence ajoutée.", "success")

                return redirect(url_for("planning_utils.planning_absences"))

            except Exception as e:
                flash(f"❌ Erreur lors de l'ajout : {e}", "danger")

        elif action == "purger_absences":
            try:
                # 1) Combien seraient supprimées ?
                to_delete = cur.execute("""
                    SELECT COUNT(*)
                    FROM absences
                    WHERE length(trim(date_fin)) = 10
                    AND substr(trim(date_fin), 3, 1) = '/'
                    AND substr(trim(date_fin), 6, 1) = '/'
                    AND date(
                            substr(trim(date_fin), 7, 4) || '-' ||
                            substr(trim(date_fin), 4, 2) || '-' ||
                            substr(trim(date_fin), 1, 2)
                        ) <= date('now','localtime')
                """).fetchone()[0]

                # 2) Suppression effective
                cur.execute("""
                    DELETE FROM absences
                    WHERE length(trim(date_fin)) = 10
                    AND substr(trim(date_fin), 3, 1) = '/'
                    AND substr(trim(date_fin), 6, 1) = '/'
                    AND date(
                            substr(trim(date_fin), 7, 4) || '-' ||
                            substr(trim(date_fin), 4, 2) || '-' ||
                            substr(trim(date_fin), 1, 2)
                        ) <= date('now','localtime')
                """)
                conn.commit()
                upload_database()
                flash(f"🧹 {to_delete} absence(s) close(s) supprimée(s).", "success")

                return redirect(url_for("planning_utils.planning_absences"))

            except Exception as e:
                flash(f"❌ Erreur purge : {e}", "danger")

    # Affichage : absences + liste des bénévoles
    # Par défaut, on masque les absences closes (date_fin déjà passée) :
    # même logique de parsing jj/mm/aaaa que la purge ci-dessus.
    where_clause = ""
    if not voir_toutes:
        where_clause = """
            WHERE NOT (
                length(trim(a.date_fin)) = 10
                AND substr(trim(a.date_fin), 3, 1) = '/'
                AND substr(trim(a.date_fin), 6, 1) = '/'
                AND date(
                        substr(trim(a.date_fin), 7, 4) || '-' ||
                        substr(trim(a.date_fin), 4, 2) || '-' ||
                        substr(trim(a.date_fin), 1, 2)
                    ) <= date('now','localtime')
            )
        """

    absences = cur.execute(f"""
        SELECT a.id, a.benevole_id, a.date_debut, a.date_fin, b.nom, b.prenom
        FROM absences a
        JOIN benevoles b ON a.benevole_id = b.id
        {where_clause}
        ORDER BY b.nom COLLATE NOCASE, b.prenom COLLATE NOCASE, a.date_debut
    """).fetchall()

    benevoles = cur.execute("""
        SELECT id, nom, prenom
        FROM benevoles
        ORDER BY nom COLLATE NOCASE, prenom COLLATE NOCASE
    """).fetchall()

    conn.close()
    return render_template(
        "planning/utils_planning/planning_absences.html",
        absences=absences,
        benevoles=benevoles,
        voir_toutes=voir_toutes,
    )



@planning_utils_bp.route('/modifier_absence/<int:absence_id>', methods=['POST'])
@login_required
def modifier_absence(absence_id):
    date_debut = request.form.get('date_debut', '').strip()
    date_fin = request.form.get('date_fin', '').strip()

    if not date_debut or not date_fin:
        flash("❌ Les deux dates sont obligatoires.", "danger")
    else:
        try:
            # Validation rapide format jj/mm/aaaa
            for d in [date_debut, date_fin]:
                datetime.strptime(d, "%d/%m/%Y")  # ➜ lève une exception si invalide

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE absences SET date_debut = ?, date_fin = ?
                WHERE id = ?
            """, (date_debut, date_fin, absence_id))
            conn.commit()
            conn.close()
            flash("✅ Absence modifiée avec succès.", "success")
        except ValueError:
            flash("❌ Format de date invalide (attendu : jj/mm/aaaa).", "danger")
        except Exception as e:
            flash(f"❌ Erreur lors de la modification : {e}", "danger")

    upload_database()
    return redirect(url_for('planning_utils.planning_absences'))


@planning_utils_bp.route('/supprimer_absence/<int:absence_id>', methods=['POST'])
@login_required
def supprimer_absence(absence_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM absences WHERE id = ?", (absence_id,))
        conn.commit()
        conn.close()
        flash("🗑️ Absence supprimée.", "warning")
    except Exception as e:
        flash(f"❌ Erreur lors de la suppression : {e}", "danger")

    upload_database()
    return redirect(url_for('planning_utils.planning_absences'))



# ba38_planning_utils.py
def get_type_benevole_options(conn=None):
    """
    Lit la table `parametres` et renvoie la liste des valeurs pour param_name='type_benevole',
    triées alphabétiquement (insensible à la casse). Si `conn` est None, ouvre/ferme sa propre connexion.
    """
    close_after = False
    if conn is None:
        from ba38_utilitaires.core import get_db_connection
        conn = get_db_connection()
        close_after = True

    cur = conn.cursor()
    rows = cur.execute("""
        SELECT param_value
        FROM parametres
        WHERE param_name = 'type_benevole'
        ORDER BY param_value COLLATE NOCASE
    """).fetchall()

    # rows peut être sqlite3.Row ou tuple -> on gère les deux
    vals = []
    for r in rows:
        try:
            v = (r["param_value"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]) or ""
        except Exception:
            v = (r[0] if isinstance(r, (list, tuple)) and r else "") or ""
        v = v.strip()
        if v:
            vals.append(v)

    if close_after:
        conn.close()
    return vals

def get_civilite_options():
    # si un jour tu les stockes en base, tu feras la même logique que ci-dessus
    return ["M", "Mme", "Mlle"]
