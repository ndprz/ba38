# ============================================================
# 🖥️ Écran kiosk (affichage cuisine, sans login) : suivi live des
#     productions du jour, grille recette × étape.
#
# Modèle : ba38_evenements/routes.py::api_evenements_actifs +
# affichage_evenement (routes PUBLIQUES, polling JSON côté front).
# ⚠️ "Aujourd'hui" est calculé en heure de Paris (zoneinfo), jamais en UTC
# brut — cf. bug de fuseau horaire déjà rencontré sur ce type de calcul
# (voir mémoire "Événements timezone fix").
# ============================================================

import sqlite3

from flask import render_template, jsonify

from ba38_production_cuisine import production_cuisine_bp
from ba38_production_cuisine.utils import _connect, today_paris


@production_cuisine_bp.route("/api/production_active")
def api_production_active():
    date_jour = today_paris()

    with _connect() as conn:
        conn.row_factory = sqlite3.Row

        etapes_ref = conn.execute(
            "SELECT * FROM cuisine_etapes_ref WHERE actif = 1 ORDER BY ordre"
        ).fetchall()

        productions = conn.execute(
            """
            SELECT * FROM cuisine_productions
            WHERE date_production = ? AND actif = 1 AND statut = 'en_cours'
            ORDER BY id
            """,
            (date_jour,),
        ).fetchall()

        data = []
        for prod in productions:
            etapes_saisies = conn.execute(
                """SELECT * FROM cuisine_production_etapes
                   WHERE production_id = ? ORDER BY id""",
                (prod["id"],),
            ).fetchall()

            # dernière ligne (la plus récente) par code d'étape
            derniere_par_code = {}
            for e in etapes_saisies:
                derniere_par_code[e["etape_code"]] = e

            etapes = []
            for ref in etapes_ref:
                ligne = derniere_par_code.get(ref["code"])
                if ligne is None:
                    statut = "non_demarree"
                    conforme = None
                elif ligne["heure_fin"] is None:
                    statut = "en_cours"
                    conforme = None
                else:
                    statut = "terminee"
                    conforme = ligne["conforme"]

                etapes.append({
                    "code": ref["code"],
                    "libelle": ref["libelle"],
                    "ordre": ref["ordre"],
                    "statut": statut,
                    "conforme": conforme,
                })

            data.append({
                "id": prod["id"],
                "nom_recette": prod["nom_recette"],
                "espece": prod["espece"],
                "mode_cuisson": prod["mode_cuisson"],
                "etapes": etapes,
            })

    return jsonify({"date": date_jour, "productions": data})


@production_cuisine_bp.route("/affichage")
def affichage_production():
    return render_template("production_cuisine/affichage.html")
