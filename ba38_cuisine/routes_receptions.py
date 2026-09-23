# ============================================================
# 📥 Réceptions marchandises (contrôle à réception)
# ============================================================

import re
import sqlite3
from datetime import date, timedelta

import os

from flask import render_template, request, redirect, url_for, flash, send_file, abort
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_cuisine import production_cuisine_bp
from ba38_cuisine.utils import (
    _connect, today_paris, now_paris_str, upload_dir_reception, save_uploaded_files, parse_temperature,
)

CONFORMITE_CHOICES = ("conforme", "non_conforme")
GROUPE_AUTRE = "__autre__"


def _clean_conformite(val):
    return val if val in CONFORMITE_CHOICES else None


def _ingredients_par_groupe(conn):
    """{groupe: [produit, ...]} — référentiel des ingrédients carnés, pour
    le sélecteur en 2 temps (groupe puis produit) de l'écran de réception."""
    rows = conn.execute(
        """SELECT groupe, produit FROM cuisine_ingredients_carnes
           WHERE actif = 1 ORDER BY groupe COLLATE NOCASE, produit COLLATE NOCASE"""
    ).fetchall()
    par_groupe = {}
    for row in rows:
        par_groupe.setdefault(row["groupe"], []).append(row["produit"])
    return par_groupe


@production_cuisine_bp.route("/receptions")
@login_required
@require_access("production_cuisine", "lecture")
def liste_receptions():
    date_filtre = request.args.get("date") or today_paris()

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        fournisseurs = conn.execute(
            "SELECT id, nom FROM fournisseurs WHERE actif = 'oui' ORDER BY nom COLLATE NOCASE"
        ).fetchall()
        receptions = conn.execute(
            """
            SELECT r.*, f.nom AS fournisseur_nom,
                   p.nom_recette AS production_nom
            FROM cuisine_receptions r
            LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
            LEFT JOIN cuisine_productions p ON p.id = r.production_id
            WHERE r.date_reception = ? AND r.actif = 1
            ORDER BY r.heure_arrivee, r.id
            """,
            (date_filtre,),
        ).fetchall()
        productions_du_jour = conn.execute(
            """SELECT id, nom_recette FROM cuisine_productions
               WHERE date_production = ? AND statut != 'annulee'
               ORDER BY id DESC""",
            (date_filtre,),
        ).fetchall()

    en_attente = [r for r in receptions if r["production_id"] is None]
    affectees = [r for r in receptions if r["production_id"] is not None]

    return render_template(
        "production_cuisine/receptions_liste.html",
        en_attente=en_attente,
        affectees=affectees,
        fournisseurs=fournisseurs,
        productions_du_jour=productions_du_jour,
        date_filtre=date_filtre,
    )


PERIODES_ETAT = (
    ("jour", "Jour"),
    ("hier", "Hier"),
    ("semaine", "Semaine en cours"),
    ("mois", "Mois en cours"),
    ("trimestre", "Trimestre en cours"),
    ("annee", "Année en cours"),
    ("perso", "Dates personnalisées"),
)


def _bornes_periode(periode, date_jour):
    """(date_debut, date_fin) AAAA-MM-JJ incluses. "jour" = la date choisie ;
    les autres périodes sont calculées par rapport à aujourd'hui (Paris)."""
    if periode == "jour":
        return date_jour, date_jour
    today = date.fromisoformat(today_paris())
    if periode == "hier":
        debut = fin = today - timedelta(days=1)
    elif periode == "semaine":
        debut = today - timedelta(days=today.weekday())
        fin = debut + timedelta(days=6)
    elif periode == "mois":
        debut = today.replace(day=1)
        fin = (debut + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    elif periode == "trimestre":
        mois_debut = 3 * ((today.month - 1) // 3) + 1
        debut = today.replace(month=mois_debut, day=1)
        fin = (debut + timedelta(days=95)).replace(day=1) - timedelta(days=1)
    else:  # annee
        debut = today.replace(month=1, day=1)
        fin = today.replace(month=12, day=31)
    return debut.isoformat(), fin.isoformat()


@production_cuisine_bp.route("/receptions/etat-journalier")
@login_required
@require_access("production_cuisine", "lecture")
def etat_journalier_receptions():
    """État journalier des réceptions — équivalent numérique de la feuille
    papier "CONTROLE DES VIANDES ET POISSONS A RECEPTION" (documents
    Nicolas.xlsx, onglet 2). Vue par défaut : groupée par fournisseur (demande
    du cuisinier) ; vue "produit" : groupée par groupe d'ingrédient (Boeuf,
    Agneau, Poisson...). Sous-total par groupe et total kg, sur une période."""
    periodes_valides = {code for code, _ in PERIODES_ETAT}
    periode = request.args.get("periode")
    if periode not in periodes_valides:
        periode = "jour"
    vue = "produit" if request.args.get("vue") == "produit" else "fournisseur"
    date_filtre = request.args.get("date") or today_paris()
    try:
        date.fromisoformat(date_filtre)
    except ValueError:
        date_filtre = today_paris()

    if periode == "perso":
        date_debut = request.args.get("du") or date_filtre
        date_fin = request.args.get("au") or date_debut
        try:
            date.fromisoformat(date_debut)
            date.fromisoformat(date_fin)
        except ValueError:
            date_debut = date_fin = date_filtre
        if date_fin < date_debut:
            date_debut, date_fin = date_fin, date_debut
    else:
        date_debut, date_fin = _bornes_periode(periode, date_filtre)
    multi_jours = date_debut != date_fin

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        receptions = conn.execute(
            """
            SELECT r.*, f.nom AS fournisseur_nom
            FROM cuisine_receptions r
            LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
            WHERE r.date_reception BETWEEN ? AND ? AND r.actif = 1
            ORDER BY r.date_reception, r.heure_arrivee, r.id
            """,
            (date_debut, date_fin),
        ).fetchall()

    groupes = {}
    total_kg = 0.0
    for r in receptions:
        if vue == "fournisseur":
            groupe = r["fournisseur_nom"] or "Fournisseur non renseigné"
        else:
            groupe = r["ingredient_groupe"] or "Autres / non référencé"
        entree = groupes.setdefault(groupe, {"lignes": [], "sous_total": 0.0})
        poids = r["poids_kg"] or 0
        entree["lignes"].append(r)
        entree["sous_total"] += poids
        total_kg += poids

    groupes_tries = sorted(groupes.items(), key=lambda kv: kv[0].lower())

    return render_template(
        "production_cuisine/receptions_etat_journalier.html",
        date_filtre=date_filtre,
        periode=periode,
        periodes=PERIODES_ETAT,
        vue=vue,
        date_debut=date_debut,
        date_fin=date_fin,
        multi_jours=multi_jours,
        groupes=groupes_tries,
        total_kg=total_kg,
        nb_receptions=len(receptions),
    )


@production_cuisine_bp.route("/receptions/<int:reception_id>/affecter", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def affecter_reception(reception_id):
    production_id = request.form.get("production_id") or None

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        reception = conn.execute(
            "SELECT id FROM cuisine_receptions WHERE id = ?", (reception_id,)
        ).fetchone()
        if not reception:
            flash("⛔ Réception introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_receptions"))

        if production_id:
            production = conn.execute(
                "SELECT id FROM cuisine_productions WHERE id = ?", (production_id,)
            ).fetchone()
            if not production:
                flash("⛔ Production introuvable.", "danger")
                return redirect(url_for("production_cuisine.liste_receptions"))

        conn.execute(
            "UPDATE cuisine_receptions SET production_id = ? WHERE id = ?",
            (production_id, reception_id),
        )
        conn.commit()

    upload_database()
    if production_id:
        flash("✅ Réception affectée à la production.", "success")
        return redirect(url_for("production_cuisine.detail_production", production_id=production_id))

    flash("✅ Réception remise en attente d'affectation.", "success")
    return redirect(url_for("production_cuisine.liste_receptions"))


@production_cuisine_bp.route("/receptions/<int:reception_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_reception(reception_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        reception = conn.execute(
            "SELECT id, production_id FROM cuisine_receptions WHERE id = ?", (reception_id,)
        ).fetchone()
        if not reception:
            flash("⛔ Réception introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_receptions"))

        if reception["production_id"] is not None:
            flash("⛔ Impossible de supprimer : cette réception est affectée à une recette en cours.", "danger")
            return redirect(url_for("production_cuisine.liste_receptions"))

        conn.execute("UPDATE cuisine_receptions SET actif = 0 WHERE id = ?", (reception_id,))
        conn.commit()

    upload_database()
    flash("🗑️ Réception supprimée.", "success")
    return redirect(url_for("production_cuisine.liste_receptions"))


@production_cuisine_bp.route("/receptions/creer", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def creer_reception():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        fournisseurs = conn.execute(
            "SELECT id, nom FROM fournisseurs WHERE actif = 'oui' ORDER BY nom COLLATE NOCASE"
        ).fetchall()
        ingredients_par_groupe = _ingredients_par_groupe(conn)

    if request.method == "POST":
        # Champs communs à toute la livraison — saisis une seule fois même
        # si plusieurs produits différents sont réceptionnés en même temps
        # (ex. un même camion apportant viande + légumes).
        benevole = (request.form.get("benevole") or "").strip()
        date_reception = request.form.get("date_reception") or today_paris()
        heure_arrivee = request.form.get("heure_arrivee") or None
        fournisseur_id = request.form.get("fournisseur_id") or None
        camion_libelle = (request.form.get("camion_libelle") or "").strip() or None
        temperature_mesuree = parse_temperature(request.form.get("temperature_mesuree"))

        # Les lignes produit sont ajoutées/retirées dynamiquement côté client
        # (JS) — leurs index ne sont donc pas forcément contigus (ex. 0 et 2
        # si la ligne 1 a été retirée). On les retrouve en scannant les clés
        # du formulaire plutôt qu'en supposant range(nb_lignes).
        indices = sorted({
            int(m.group(1))
            for k in request.form
            for m in [re.match(r"^ingredient_groupe_(\d+)$", k)]
            if m
        })

        lignes = []
        for i in indices:
            groupe = (request.form.get(f"ingredient_groupe_{i}") or "").strip()
            if groupe == GROUPE_AUTRE:
                # Produit hors référentiel (ex. légumes) : saisie libre.
                produit = (request.form.get(f"ingredient_produit_autre_{i}") or "").strip()
                ingredient_groupe, ingredient_produit = None, None
                libelle_produit = produit
            else:
                produit = (request.form.get(f"ingredient_produit_{i}") or "").strip()
                ingredient_groupe = groupe or None
                ingredient_produit = produit or None
                # Pas de repli sur le seul groupe : un groupe choisi sans
                # produit doit être signalé comme une erreur, pas enregistré
                # tel quel (cf. validation plus bas sur libelle_produit vide).
                libelle_produit = f"{groupe} – {produit}" if groupe and produit else ""
            lignes.append({
                "idx": i,
                "groupe": groupe,
                "produit": produit,
                "libelle_produit": libelle_produit,
                "ingredient_groupe": ingredient_groupe,
                "ingredient_produit": ingredient_produit,
                "poids_kg": request.form.get(f"poids_kg_{i}") or None,
                "aspect_conforme": _clean_conformite(request.form.get(f"aspect_conforme_{i}")),
                "emballage_conforme": _clean_conformite(request.form.get(f"emballage_conforme_{i}")),
                "etiquetage_conforme": _clean_conformite(request.form.get(f"etiquetage_conforme_{i}")),
                "commentaire": (request.form.get(f"commentaire_{i}") or "").strip() or None,
            })
        # Une ligne où seul un groupe a été choisi compte comme "démarrée" :
        # il manque alors juste le produit, ce qui doit être signalé comme
        # une erreur (et non silencieusement ignoré comme une ligne vide).
        lignes_remplies = [l for l in lignes if l["groupe"]]

        erreur = None
        if not benevole:
            erreur = "⚠️ Merci d'indiquer le nom du réceptionnaire."
        elif not lignes_remplies:
            erreur = "⚠️ Merci de renseigner au moins un produit réceptionné."
        else:
            for num, ligne in enumerate(lignes_remplies, start=1):
                if not ligne["libelle_produit"]:
                    erreur = f"⚠️ Produit {num} : merci de choisir (ou préciser) le produit réceptionné."
                elif not (ligne["aspect_conforme"] and ligne["emballage_conforme"] and ligne["etiquetage_conforme"]):
                    erreur = (
                        f"⚠️ Produit {num} ({ligne['libelle_produit']}) : merci d'indiquer "
                        "les 3 conformités (aspect, emballage, étiquetage)."
                    )
                if erreur:
                    break

        if erreur:
            flash(erreur, "warning")
            return render_template(
                "production_cuisine/receptions_creer.html",
                fournisseurs=fournisseurs,
                ingredients_par_groupe=ingredients_par_groupe,
                date_defaut=date_reception,
                form=request.form,
                lignes_soumises=lignes,
            )

        try:
            reception_ids = []
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                for ligne in lignes_remplies:
                    cur.execute(
                        """
                        INSERT INTO cuisine_receptions
                        (date_reception, heure_arrivee, fournisseur_id, camion_libelle,
                         libelle_produit, ingredient_groupe, ingredient_produit,
                         temperature_mesuree, poids_kg, aspect_conforme,
                         emballage_conforme, etiquetage_conforme, commentaire, user_creation)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            date_reception, heure_arrivee, fournisseur_id, camion_libelle,
                            ligne["libelle_produit"], ligne["ingredient_groupe"], ligne["ingredient_produit"],
                            temperature_mesuree, ligne["poids_kg"],
                            ligne["aspect_conforme"], ligne["emballage_conforme"], ligne["etiquetage_conforme"],
                            ligne["commentaire"], benevole,
                        ),
                    )
                    reception_id = cur.lastrowid
                    reception_ids.append(reception_id)

                    # 📸 Photos produit / étiquette (capture tablette)
                    dossier = upload_dir_reception(reception_id)
                    for type_photo, champ in (
                        ("produit", f"photos_produit_{ligne['idx']}"),
                        ("etiquette", f"photos_etiquette_{ligne['idx']}"),
                    ):
                        fichiers = request.files.getlist(champ)
                        chemins = save_uploaded_files(fichiers, dossier, prefix=type_photo)
                        for ordre, chemin in enumerate(chemins):
                            cur.execute(
                                """INSERT INTO cuisine_reception_photos
                                   (reception_id, type_photo, chemin_fichier, ordre)
                                   VALUES (?, ?, ?, ?)""",
                                (reception_id, type_photo, chemin, ordre),
                            )

                conn.commit()
            upload_database()
            if len(reception_ids) == 1:
                flash("✅ Réception enregistrée.", "success")
                return redirect(url_for("production_cuisine.detail_reception", reception_id=reception_ids[0]))
            flash(f"✅ {len(reception_ids)} réceptions enregistrées.", "success")
            return redirect(url_for("production_cuisine.liste_receptions"))

        except Exception as e:
            write_log(f"❌ Erreur création réception cuisine : {e}")
            flash("❌ Erreur lors de l'enregistrement de la réception.", "danger")
            return render_template(
                "production_cuisine/receptions_creer.html",
                fournisseurs=fournisseurs,
                ingredients_par_groupe=ingredients_par_groupe,
                date_defaut=date_reception,
                form=request.form,
                lignes_soumises=lignes,
            )

    return render_template(
        "production_cuisine/receptions_creer.html",
        fournisseurs=fournisseurs,
        ingredients_par_groupe=ingredients_par_groupe,
        date_defaut=today_paris(),
        form={},
        lignes_soumises=[],
    )


@production_cuisine_bp.route("/receptions/<int:reception_id>")
@login_required
@require_access("production_cuisine", "lecture")
def detail_reception(reception_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        reception = conn.execute(
            """
            SELECT r.*, f.nom AS fournisseur_nom, p.nom_recette AS production_nom
            FROM cuisine_receptions r
            LEFT JOIN fournisseurs f ON f.id = r.fournisseur_id
            LEFT JOIN cuisine_productions p ON p.id = r.production_id
            WHERE r.id = ?
            """,
            (reception_id,),
        ).fetchone()

        if not reception:
            flash("⛔ Réception introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_receptions"))

        photos = conn.execute(
            "SELECT * FROM cuisine_reception_photos WHERE reception_id = ? ORDER BY type_photo, ordre",
            (reception_id,),
        ).fetchall()

        productions_du_jour = conn.execute(
            """SELECT id, nom_recette FROM cuisine_productions
               WHERE date_production = ? AND statut != 'annulee'
               ORDER BY id DESC""",
            (reception["date_reception"],),
        ).fetchall()

    # Bouton "← Retour" contextuel : si on arrive depuis la fiche recette
    # (lien "Détail" d'un lot en traçabilité), on y revient plutôt que sur
    # la liste générale des réceptions. Le paramètre n'est suivi que s'il
    # correspond bien à la production réelle de cette réception (sinon on
    # retombe sur la liste).
    depuis_production = request.args.get("depuis_production", type=int)
    if depuis_production and depuis_production == reception["production_id"]:
        retour_url = url_for("production_cuisine.detail_production", production_id=depuis_production)
    else:
        retour_url = url_for("production_cuisine.liste_receptions")

    return render_template(
        "production_cuisine/receptions_detail.html",
        reception=reception,
        photos=photos,
        productions_du_jour=productions_du_jour,
        retour_url=retour_url,
    )


@production_cuisine_bp.route("/photo/reception/<int:photo_id>")
@login_required
@require_access("production_cuisine", "lecture")
def photo_reception(photo_id):
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        photo = conn.execute(
            "SELECT chemin_fichier FROM cuisine_reception_photos WHERE id = ?", (photo_id,)
        ).fetchone()
    chemin = photo["chemin_fichier"] if photo else None
    if not chemin or not os.path.exists(chemin):
        abort(404)
    return send_file(chemin, as_attachment=False)
