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
    parse_poids, format_kg, receptions_en_stock, controle_dates, reception_avec_solde, ajouter_utilisation,
    SQL_RECEPTIONS_SOLDE, SOLDE_EPSILON,
    utilisateurs_cuisine, utilisateur_saisi, enregistrer_utilisateur_cuisine,
)

CONFORMITE_CHOICES = ("conforme", "non_conforme")
GROUPE_AUTRE = "__autre__"


def _clean_conformite(val):
    return val if val in CONFORMITE_CHOICES else None


@production_cuisine_bp.app_template_filter("kg")
def _filtre_kg(valeur):
    return format_kg(valeur)


def _parse_dlc(valeur):
    """DLC saisie (AAAA-MM-JJ, champ date) → texte ISO, sinon None."""
    valeur = (valeur or "").strip()
    try:
        return date.fromisoformat(valeur).isoformat()
    except ValueError:
        return None


def _lire_ligne_produit(form, i):
    """Lit la ligne produit d'index `i` du formulaire (champs suffixés _<i>),
    commune aux écrans de création (plusieurs lignes) et de modification
    (une seule ligne, index 0)."""
    groupe = (form.get(f"ingredient_groupe_{i}") or "").strip()
    if groupe == GROUPE_AUTRE:
        # Produit hors référentiel (ex. légumes) : saisie libre.
        produit = (form.get(f"ingredient_produit_autre_{i}") or "").strip()
        ingredient_groupe, ingredient_produit = None, None
        libelle_produit = produit
    else:
        produit = (form.get(f"ingredient_produit_{i}") or "").strip()
        ingredient_groupe = groupe or None
        ingredient_produit = produit or None
        # Pas de repli sur le seul groupe : un groupe choisi sans
        # produit doit être signalé comme une erreur, pas enregistré
        # tel quel (cf. _erreur_ligne_produit sur libelle_produit vide).
        libelle_produit = f"{groupe} – {produit}" if groupe and produit else ""
    poids_saisi = (form.get(f"poids_kg_{i}") or "").strip()
    return {
        "idx": i,
        "groupe": groupe,
        "produit": produit,
        "libelle_produit": libelle_produit,
        "ingredient_groupe": ingredient_groupe,
        "ingredient_produit": ingredient_produit,
        # poids_kg = valeur saisie (réaffichée telle quelle en cas d'erreur),
        # poids_valide = float > 0 réellement enregistré.
        "poids_kg": poids_saisi or None,
        "poids_valide": parse_poids(poids_saisi),
        "dlc_ddm": (form.get(f"dlc_ddm_{i}") or "").strip() or None,
        "dlc_valide": _parse_dlc(form.get(f"dlc_ddm_{i}")),
        "ddm": (form.get(f"ddm_{i}") or "").strip() or None,
        "ddm_valide": _parse_dlc(form.get(f"ddm_{i}")),
        "aspect_conforme": _clean_conformite(form.get(f"aspect_conforme_{i}")),
        "emballage_conforme": _clean_conformite(form.get(f"emballage_conforme_{i}")),
        "etiquetage_conforme": _clean_conformite(form.get(f"etiquetage_conforme_{i}")),
        "commentaire": (form.get(f"commentaire_{i}") or "").strip() or None,
    }


def _erreur_ligne_produit(num, ligne):
    """Message d'erreur pour une ligne produit incomplète, sinon None."""
    if not ligne["libelle_produit"]:
        return f"⚠️ Produit {num} : merci de choisir (ou préciser) le produit réceptionné."
    if ligne["poids_valide"] is None:
        return f"⚠️ Produit {num} ({ligne['libelle_produit']}) : merci d'indiquer le poids (en kg, supérieur à 0)."
    if ligne["dlc_valide"] is None and ligne["ddm_valide"] is None:
        return f"⚠️ Produit {num} ({ligne['libelle_produit']}) : merci d'indiquer la DLC ou la DDM."
    if not (ligne["aspect_conforme"] and ligne["emballage_conforme"] and ligne["etiquetage_conforme"]):
        return (
            f"⚠️ Produit {num} ({ligne['libelle_produit']}) : merci d'indiquer "
            "les 3 conformités (aspect, emballage, étiquetage)."
        )
    return None


def _enregistrer_photos(cur, reception_id, idx):
    """📸 Photos produit / étiquette (capture tablette) de la ligne `idx`."""
    dossier = upload_dir_reception(reception_id)
    for type_photo, champ in (
        ("produit", f"photos_produit_{idx}"),
        ("etiquette", f"photos_etiquette_{idx}"),
    ):
        fichiers = request.files.getlist(champ)
        chemins = save_uploaded_files(fichiers, dossier, prefix=type_photo)
        debut = cur.execute(
            "SELECT COALESCE(MAX(ordre) + 1, 0) FROM cuisine_reception_photos WHERE reception_id = ? AND type_photo = ?",
            (reception_id, type_photo),
        ).fetchone()[0]
        for ordre, chemin in enumerate(chemins, start=debut):
            cur.execute(
                """INSERT INTO cuisine_reception_photos
                   (reception_id, type_photo, chemin_fichier, ordre)
                   VALUES (?, ?, ?, ?)""",
                (reception_id, type_photo, chemin, ordre),
            )


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


def _productions_du_jour(conn, date_jour):
    return conn.execute(
        """SELECT id, nom_recette FROM cuisine_productions
           WHERE date_production = ? AND statut != 'annulee' AND actif = 1
           ORDER BY id DESC""",
        (date_jour,),
    ).fetchall()


def _livraisons(receptions):
    """Regroupe des lignes réception par livraison (lignes saisies ensemble
    sur le même écran), dans l'ordre d'arrivée : [{"id", "lignes", ...}]."""
    livraisons = {}
    for r in receptions:
        cle = r["livraison_id"] or r["id"]
        liv = livraisons.setdefault(cle, {"id": cle, "entete": r, "lignes": []})
        liv["lignes"].append(r)
    for liv in livraisons.values():
        liv["purgeable"] = any(
            l["actif"] and not l["date_purge"] and l["solde_kg"] > SOLDE_EPSILON for l in liv["lignes"]
        )
    return list(livraisons.values())


@production_cuisine_bp.route("/receptions")
@login_required
@require_access("production_cuisine", "lecture")
def liste_receptions():
    date_filtre = request.args.get("date") or today_paris()

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        # Stock : tout ce qui reste utilisable ou à purger, quel que soit le
        # jour de réception (un gros arrivage sert plusieurs jours).
        stock = receptions_en_stock(conn, date_filtre)
        receptions_jour = conn.execute(
            f"""SELECT * FROM ({SQL_RECEPTIONS_SOLDE})
                WHERE date_reception = ? AND actif = 1
                ORDER BY heure_arrivee, id""",
            (date_filtre,),
        ).fetchall()
        utilisations = conn.execute(
            """SELECT ru.reception_id, ru.poids_kg, p.id AS production_id, p.nom_recette, p.date_production
               FROM cuisine_reception_utilisations ru
               JOIN cuisine_receptions r ON r.id = ru.reception_id
               JOIN cuisine_productions p ON p.id = ru.production_id AND p.actif = 1
               WHERE r.date_reception = ? AND r.actif = 1
               ORDER BY ru.id""",
            (date_filtre,),
        ).fetchall()
        productions_du_jour = _productions_du_jour(conn, date_filtre)

    utilisations_par_reception = {}
    for u in utilisations:
        utilisations_par_reception.setdefault(u["reception_id"], []).append(u)

    return render_template(
        "production_cuisine/receptions_liste.html",
        stock=stock,
        livraisons=_livraisons(receptions_jour),
        utilisations_par_reception=utilisations_par_reception,
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
            f"""
            SELECT * FROM ({SQL_RECEPTIONS_SOLDE})
            WHERE date_reception BETWEEN ? AND ? AND actif = 1
            ORDER BY date_reception, heure_arrivee, id
            """,
            (date_debut, date_fin),
        ).fetchall()

    groupes = {}
    total_kg = 0.0
    total_consomme = 0.0
    for r in receptions:
        if vue == "fournisseur":
            groupe = r["fournisseur_nom"] or "Fournisseur non renseigné"
        else:
            groupe = r["ingredient_groupe"] or "Autres / non référencé"
        entree = groupes.setdefault(groupe, {"lignes": [], "sous_total": 0.0, "sous_total_consomme": 0.0})
        poids = r["poids_kg"] or 0
        consomme = r["poids_utilise"] or 0
        entree["lignes"].append(r)
        entree["sous_total"] += poids
        entree["sous_total_consomme"] += consomme
        total_kg += poids
        total_consomme += consomme

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
        total_consomme=total_consomme,
        nb_receptions=len(receptions),
    )


def _retour(defaut):
    """Page d'origine (champ `next`, chemin relatif uniquement), sinon `defaut`."""
    suivant = request.form.get("next") or ""
    if suivant.startswith("/") and not suivant.startswith("//"):
        return redirect(suivant)
    return redirect(defaut)


@production_cuisine_bp.route("/receptions/<int:reception_id>/affecter", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def affecter_reception(reception_id):
    """Utilise une partie (ou la totalité) du solde de la réception dans une
    production. Le reste demeure disponible pour une autre recette."""
    production_id = request.form.get("production_id", type=int)
    poids = parse_poids(request.form.get("poids_kg"))
    benevole = (request.form.get("benevole") or "").strip()
    forcer = request.form.get("forcer") == "1"
    liste_url = url_for("production_cuisine.liste_receptions")

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        production = conn.execute(
            "SELECT id, date_production FROM cuisine_productions WHERE id = ? AND actif = 1",
            (production_id,),
        ).fetchone()
        if not production:
            flash("⛔ Production introuvable.", "danger")
            return _retour(liste_url)
        erreur = ajouter_utilisation(conn, reception_id, production, poids, benevole, forcer)
        if erreur:
            flash(f"⛔ {erreur}", "danger")
            return _retour(liste_url)
        conn.commit()

    upload_database()
    flash(f"✅ {format_kg(poids)} kg affectés à la production.", "success")
    return redirect(url_for("production_cuisine.detail_production", production_id=production_id))


@production_cuisine_bp.route("/receptions/utilisations/<int:utilisation_id>/retirer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def retirer_utilisation(utilisation_id):
    """Annule l'utilisation d'une réception par une production : le poids
    correspondant revient dans le solde de la réception."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        utilisation = conn.execute(
            "SELECT reception_id, production_id, poids_kg FROM cuisine_reception_utilisations WHERE id = ?",
            (utilisation_id,),
        ).fetchone()
        if not utilisation:
            flash("⛔ Utilisation introuvable.", "danger")
            return _retour(url_for("production_cuisine.liste_receptions"))
        conn.execute("DELETE FROM cuisine_reception_utilisations WHERE id = ?", (utilisation_id,))
        conn.commit()

    write_log(
        f"↩️ Réception cuisine #{utilisation['reception_id']} : {utilisation['poids_kg']} kg retirés "
        f"de la production #{utilisation['production_id']}"
    )
    upload_database()
    flash(f"✅ {format_kg(utilisation['poids_kg'])} kg remis dans le stock de la réception.", "success")
    return _retour(url_for("production_cuisine.detail_reception", reception_id=utilisation["reception_id"]))


@production_cuisine_bp.route("/receptions/<int:reception_id>/supprimer", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def supprimer_reception(reception_id):
    """Suppression d'une saisie erronée (jamais utilisée). Pour sortir du
    stock une marchandise réellement reçue (DLC dépassée, jetée…), c'est la
    purge qui s'applique : elle garde la trace."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        reception = reception_avec_solde(conn, reception_id)
        if not reception:
            flash("⛔ Réception introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_receptions"))

        if reception["nb_utilisations"]:
            flash("⛔ Impossible de supprimer : cette réception est utilisée dans une production (la purger si besoin).", "danger")
            return _retour(url_for("production_cuisine.liste_receptions"))

        conn.execute("UPDATE cuisine_receptions SET actif = 0 WHERE id = ?", (reception_id,))
        conn.commit()

    upload_database()
    flash("🗑️ Réception supprimée.", "success")
    return _retour(url_for("production_cuisine.liste_receptions"))


def _purger(conn, reception_ids, benevole, motif):
    """Retire du stock le solde restant des réceptions données (non encore
    purgées) ; poids_purge = solde au moment de la purge. Retourne
    [(reception, poids_purge)] des lignes effectivement purgées."""
    purgees = []
    enregistrer_utilisateur_cuisine(conn, benevole)
    for reception_id in reception_ids:
        r = reception_avec_solde(conn, reception_id)
        if not r or not r["actif"] or r["date_purge"] or r["solde_kg"] <= SOLDE_EPSILON:
            continue
        conn.execute(
            """UPDATE cuisine_receptions
               SET date_purge = ?, user_purge = ?, motif_purge = ?, poids_purge = ?
               WHERE id = ? AND date_purge IS NULL""",
            (now_paris_str(), benevole, motif, r["solde_kg"], reception_id),
        )
        purgees.append((r, r["solde_kg"]))
    return purgees


def _lire_purge():
    benevole = utilisateur_saisi(request.form)
    motif = (request.form.get("motif") or "").strip()
    precision = (request.form.get("precision") or "").strip()
    if precision:
        motif = f"{motif} — {precision}" if motif else precision
    return benevole, motif or None


@production_cuisine_bp.route("/receptions/<int:reception_id>/purger", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def purger_reception(reception_id):
    benevole, motif = _lire_purge()
    detail_url = url_for("production_cuisine.detail_reception", reception_id=reception_id)
    if not benevole:
        flash("⚠️ Merci d'indiquer votre nom pour purger.", "warning")
        return _retour(detail_url)
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        purgees = _purger(conn, [reception_id], benevole, motif)
        conn.commit()
    if not purgees:
        flash("ℹ️ Rien à purger : réception déjà purgée ou entièrement utilisée.", "info")
        return _retour(detail_url)
    r, poids = purgees[0]
    write_log(f"🧹 Réception cuisine #{reception_id} purgée ({poids} kg) par {benevole} : {motif or '—'}")
    upload_database()
    flash(f"🧹 {r['libelle_produit'] or 'Réception'} : {format_kg(poids)} kg retirés du stock.", "success")
    return _retour(detail_url)


@production_cuisine_bp.route("/receptions/livraison/<int:livraison_id>/purger", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def purger_livraison(livraison_id):
    """Purge de toute une réception (toutes les lignes produit saisies
    ensemble) : chaque ligne encore en stock perd son solde restant."""
    benevole, motif = _lire_purge()
    liste_url = url_for("production_cuisine.liste_receptions")
    if not benevole:
        flash("⚠️ Merci d'indiquer votre nom pour purger.", "warning")
        return _retour(liste_url)
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM cuisine_receptions WHERE (livraison_id = ? OR id = ?) AND actif = 1",
                (livraison_id, livraison_id),
            ).fetchall()
        ]
        purgees = _purger(conn, ids, benevole, motif)
        conn.commit()
    if not purgees:
        flash("ℹ️ Rien à purger : toutes les lignes sont déjà purgées ou entièrement utilisées.", "info")
        return _retour(liste_url)
    total = sum(p for _, p in purgees)
    write_log(
        f"🧹 Livraison cuisine #{livraison_id} purgée ({len(purgees)} ligne(s), {total} kg) "
        f"par {benevole} : {motif or '—'}"
    )
    upload_database()
    flash(f"🧹 {len(purgees)} ligne(s) purgée(s), {format_kg(total)} kg retirés du stock.", "success")
    return _retour(liste_url)


@production_cuisine_bp.route("/receptions/<int:reception_id>/annuler-purge", methods=["POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def annuler_purge_reception(reception_id):
    """Purge faite par erreur : le solde purgé revient en stock."""
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE cuisine_receptions
               SET date_purge = NULL, user_purge = NULL, motif_purge = NULL, poids_purge = NULL
               WHERE id = ? AND actif = 1 AND date_purge IS NOT NULL""",
            (reception_id,),
        )
        conn.commit()
    if cur.rowcount:
        write_log(f"↩️ Purge annulée sur la réception cuisine #{reception_id}")
        upload_database()
        flash("✅ Purge annulée : le solde est de nouveau disponible.", "success")
    return redirect(url_for("production_cuisine.detail_reception", reception_id=reception_id))


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
        utilisateurs = utilisateurs_cuisine(conn)

    if request.method == "POST":
        # Champs communs à toute la livraison — saisis une seule fois même
        # si plusieurs produits différents sont réceptionnés en même temps
        # (ex. un même camion apportant viande + légumes).
        benevole = utilisateur_saisi(request.form)
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

        lignes = [_lire_ligne_produit(request.form, i) for i in indices]
        # Une ligne où seul un groupe a été choisi compte comme "démarrée" :
        # il manque alors juste le produit, ce qui doit être signalé comme
        # une erreur (et non silencieusement ignoré comme une ligne vide).
        lignes_remplies = [l for l in lignes if l["groupe"]]

        erreur = None
        if not benevole:
            erreur = "⚠️ Merci d'indiquer le nom du réceptionnaire."
        elif not fournisseur_id:
            erreur = "⚠️ Merci de choisir le fournisseur."
        elif not lignes_remplies:
            erreur = "⚠️ Merci de renseigner au moins un produit réceptionné."
        else:
            for num, ligne in enumerate(lignes_remplies, start=1):
                erreur = _erreur_ligne_produit(num, ligne)
                if erreur:
                    break

        if erreur:
            flash(erreur, "warning")
            return render_template(
                "production_cuisine/receptions_creer.html",
                fournisseurs=fournisseurs,
                utilisateurs=utilisateurs,
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
                benevole = enregistrer_utilisateur_cuisine(conn, benevole)
                for ligne in lignes_remplies:
                    cur.execute(
                        """
                        INSERT INTO cuisine_receptions
                        (date_reception, heure_arrivee, fournisseur_id, camion_libelle,
                         libelle_produit, ingredient_groupe, ingredient_produit,
                         temperature_mesuree, poids_kg, dlc_ddm, ddm, aspect_conforme,
                         emballage_conforme, etiquetage_conforme, commentaire, user_creation,
                         livraison_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            date_reception, heure_arrivee, fournisseur_id, camion_libelle,
                            ligne["libelle_produit"], ligne["ingredient_groupe"], ligne["ingredient_produit"],
                            temperature_mesuree, ligne["poids_valide"], ligne["dlc_valide"], ligne["ddm_valide"],
                            ligne["aspect_conforme"], ligne["emballage_conforme"], ligne["etiquetage_conforme"],
                            ligne["commentaire"], benevole,
                            reception_ids[0] if reception_ids else None,
                        ),
                    )
                    reception_id = cur.lastrowid
                    if not reception_ids:
                        # 1re ligne : elle donne son id à toute la livraison.
                        cur.execute(
                            "UPDATE cuisine_receptions SET livraison_id = ? WHERE id = ?",
                            (reception_id, reception_id),
                        )
                    reception_ids.append(reception_id)

                    _enregistrer_photos(cur, reception_id, ligne["idx"])

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
                utilisateurs=utilisateurs,
                ingredients_par_groupe=ingredients_par_groupe,
                date_defaut=date_reception,
                form=request.form,
                lignes_soumises=lignes,
            )

    return render_template(
        "production_cuisine/receptions_creer.html",
        fournisseurs=fournisseurs,
        utilisateurs=utilisateurs,
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
        reception = reception_avec_solde(conn, reception_id)

        if not reception:
            flash("⛔ Réception introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_receptions"))

        photos = conn.execute(
            "SELECT * FROM cuisine_reception_photos WHERE reception_id = ? ORDER BY type_photo, ordre",
            (reception_id,),
        ).fetchall()

        utilisations = conn.execute(
            """SELECT ru.*, p.nom_recette, p.date_production, p.statut
               FROM cuisine_reception_utilisations ru
               JOIN cuisine_productions p ON p.id = ru.production_id AND p.actif = 1
               WHERE ru.reception_id = ?
               ORDER BY ru.id""",
            (reception_id,),
        ).fetchall()

        # Autres lignes produit de la même livraison (purge groupée).
        autres_lignes = conn.execute(
            f"""SELECT * FROM ({SQL_RECEPTIONS_SOLDE})
                WHERE livraison_id = ? AND id != ? AND actif = 1 ORDER BY id""",
            (reception["livraison_id"] or reception_id, reception_id),
        ).fetchall()

        # Une réception en stock sert aussi les jours suivants : on propose
        # les productions du jour (pas celles du jour de réception).
        date_jour = today_paris()
        productions_du_jour = _productions_du_jour(conn, date_jour)

    blocage, avertissement = controle_dates(reception, date_jour)

    # Bouton "← Retour" contextuel : si on arrive depuis la fiche recette
    # (lien "Détail" d'un lot en traçabilité), on y revient plutôt que sur
    # la liste générale des réceptions. Le paramètre n'est suivi que s'il
    # correspond bien à une production utilisant cette réception (sinon on
    # retombe sur la liste).
    depuis_production = request.args.get("depuis_production", type=int)
    if depuis_production and any(u["production_id"] == depuis_production for u in utilisations):
        retour_url = url_for("production_cuisine.detail_production", production_id=depuis_production)
    else:
        retour_url = url_for("production_cuisine.liste_receptions")

    return render_template(
        "production_cuisine/receptions_detail.html",
        reception=reception,
        photos=photos,
        utilisations=utilisations,
        autres_lignes=autres_lignes,
        productions_du_jour=productions_du_jour,
        blocage=blocage,
        avertissement=avertissement,
        retour_url=retour_url,
    )


def _ligne_depuis_reception(reception):
    """Pré-remplissage de la ligne produit de l'écran de modification à
    partir d'une réception enregistrée (même format que _lire_ligne_produit)."""
    if reception["ingredient_groupe"]:
        groupe, produit = reception["ingredient_groupe"], reception["ingredient_produit"] or ""
    else:
        groupe, produit = GROUPE_AUTRE, reception["libelle_produit"] or ""
    return {
        "idx": 0,
        "groupe": groupe,
        "produit": produit,
        "poids_kg": reception["poids_kg"],
        "dlc_ddm": reception["dlc_ddm"],
        "ddm": reception["ddm"],
        "aspect_conforme": reception["aspect_conforme"],
        "emballage_conforme": reception["emballage_conforme"],
        "etiquetage_conforme": reception["etiquetage_conforme"],
        "commentaire": reception["commentaire"],
    }


@production_cuisine_bp.route("/receptions/<int:reception_id>/modifier", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def modifier_reception(reception_id):
    """Correction d'une réception tant qu'elle n'est utilisée dans aucune
    production ni purgée. Une fois utilisée, elle fait partie de la
    traçabilité de la recette : il faut d'abord retirer ses utilisations."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        reception = reception_avec_solde(conn, reception_id)
        if not reception or not reception["actif"]:
            flash("⛔ Réception introuvable.", "danger")
            return redirect(url_for("production_cuisine.liste_receptions"))
        if reception["nb_utilisations"] or reception["date_purge"]:
            flash("⛔ Modification impossible : cette réception est déjà utilisée dans une production ou purgée.", "danger")
            return redirect(url_for("production_cuisine.detail_reception", reception_id=reception_id))

        fournisseurs = conn.execute(
            "SELECT id, nom FROM fournisseurs WHERE actif = 'oui' ORDER BY nom COLLATE NOCASE"
        ).fetchall()
        ingredients_par_groupe = _ingredients_par_groupe(conn)
        utilisateurs = utilisateurs_cuisine(conn)
        photos = conn.execute(
            "SELECT * FROM cuisine_reception_photos WHERE reception_id = ? ORDER BY type_photo, ordre",
            (reception_id,),
        ).fetchall()

    # Groupe/produit enregistrés mais désactivés depuis dans le référentiel :
    # on les garde proposés pour ne pas forcer un changement de produit.
    if reception["ingredient_groupe"]:
        produits = ingredients_par_groupe.setdefault(reception["ingredient_groupe"], [])
        if reception["ingredient_produit"] and reception["ingredient_produit"] not in produits:
            produits.append(reception["ingredient_produit"])

    def _rendu(form, ligne):
        return render_template(
            "production_cuisine/receptions_modifier.html",
            reception=reception,
            fournisseurs=fournisseurs,
            utilisateurs=utilisateurs,
            ingredients_par_groupe=ingredients_par_groupe,
            photos=photos,
            form=form,
            ligne=ligne,
        )

    if request.method == "GET":
        form = {
            "date_reception": reception["date_reception"],
            "heure_arrivee": reception["heure_arrivee"] or "",
            "fournisseur_id": str(reception["fournisseur_id"] or ""),
            "camion_libelle": reception["camion_libelle"] or "",
            "temperature_mesuree": "" if reception["temperature_mesuree"] is None else reception["temperature_mesuree"],
        }
        return _rendu(form, _ligne_depuis_reception(reception))

    benevole = utilisateur_saisi(request.form)
    date_reception = request.form.get("date_reception") or reception["date_reception"]
    heure_arrivee = request.form.get("heure_arrivee") or None
    fournisseur_id = request.form.get("fournisseur_id") or None
    camion_libelle = (request.form.get("camion_libelle") or "").strip() or None
    temperature_mesuree = parse_temperature(request.form.get("temperature_mesuree"))
    ligne = _lire_ligne_produit(request.form, 0)

    if not benevole:
        erreur = "⚠️ Merci d'indiquer votre nom (personne qui corrige la réception)."
    elif not fournisseur_id:
        erreur = "⚠️ Merci de choisir le fournisseur."
    elif not ligne["groupe"]:
        erreur = "⚠️ Merci de renseigner le produit réceptionné."
    else:
        erreur = _erreur_ligne_produit(1, ligne)
    if erreur:
        flash(erreur, "warning")
        return _rendu(request.form, ligne)

    try:
        with _connect() as conn:
            cur = conn.cursor()
            benevole = enregistrer_utilisateur_cuisine(conn, benevole)
            # Garde en base : la réception a pu être utilisée dans une recette
            # (ou purgée) entre l'ouverture du formulaire et son enregistrement.
            cur.execute(
                """
                UPDATE cuisine_receptions
                SET date_reception = ?, heure_arrivee = ?, fournisseur_id = ?, camion_libelle = ?,
                    libelle_produit = ?, ingredient_groupe = ?, ingredient_produit = ?,
                    temperature_mesuree = ?, poids_kg = ?, dlc_ddm = ?, ddm = ?, aspect_conforme = ?,
                    emballage_conforme = ?, etiquetage_conforme = ?, commentaire = ?,
                    date_modif = ?, user_modif = ?
                WHERE id = ? AND actif = 1 AND date_purge IS NULL
                  AND NOT EXISTS (SELECT 1 FROM cuisine_reception_utilisations u
                                  WHERE u.reception_id = cuisine_receptions.id)
                """,
                (
                    date_reception, heure_arrivee, fournisseur_id, camion_libelle,
                    ligne["libelle_produit"], ligne["ingredient_groupe"], ligne["ingredient_produit"],
                    temperature_mesuree, ligne["poids_valide"], ligne["dlc_valide"], ligne["ddm_valide"],
                    ligne["aspect_conforme"], ligne["emballage_conforme"], ligne["etiquetage_conforme"],
                    ligne["commentaire"], now_paris_str(), benevole, reception_id,
                ),
            )
            if cur.rowcount == 0:
                flash("⛔ Modification impossible : cette réception vient d'être utilisée dans une production.", "danger")
                return redirect(url_for("production_cuisine.detail_reception", reception_id=reception_id))
            _enregistrer_photos(cur, reception_id, 0)
            conn.commit()
    except Exception as e:
        write_log(f"❌ Erreur modification réception cuisine #{reception_id} : {e}")
        flash("❌ Erreur lors de l'enregistrement de la modification.", "danger")
        return _rendu(request.form, ligne)

    write_log(f"✏️ Réception cuisine #{reception_id} modifiée par {benevole}")
    upload_database()
    flash("✅ Réception modifiée.", "success")
    return redirect(url_for("production_cuisine.detail_reception", reception_id=reception_id))


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
