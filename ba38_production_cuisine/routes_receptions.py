# ============================================================
# 📥 Réceptions marchandises (contrôle à réception)
# ============================================================

import sqlite3

import os

from flask import render_template, request, redirect, url_for, flash, send_file, abort
from flask_login import login_required

from ba38_utilitaires.core import require_access, write_log, upload_database
from ba38_production_cuisine import production_cuisine_bp
from ba38_production_cuisine.utils import (
    _connect, today_paris, now_paris_str, upload_dir_reception, save_uploaded_files,
)

CONFORMITE_CHOICES = ("conforme", "non_conforme")


def _clean_conformite(val):
    return val if val in CONFORMITE_CHOICES else None


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


@production_cuisine_bp.route("/receptions/creer", methods=["GET", "POST"])
@login_required
@require_access("production_cuisine", "ecriture")
def creer_reception():
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        fournisseurs = conn.execute(
            "SELECT id, nom FROM fournisseurs WHERE actif = 'oui' ORDER BY nom COLLATE NOCASE"
        ).fetchall()

    if request.method == "POST":
        benevole = (request.form.get("benevole") or "").strip()
        date_reception = request.form.get("date_reception") or today_paris()
        heure_arrivee = request.form.get("heure_arrivee") or None
        fournisseur_id = request.form.get("fournisseur_id") or None
        camion_libelle = (request.form.get("camion_libelle") or "").strip() or None
        libelle_produit = (request.form.get("libelle_produit") or "").strip() or None
        temperature_mesuree = request.form.get("temperature_mesuree") or None
        poids_kg = request.form.get("poids_kg") or None
        aspect_conforme = _clean_conformite(request.form.get("aspect_conforme"))
        emballage_conforme = _clean_conformite(request.form.get("emballage_conforme"))
        etiquetage_conforme = _clean_conformite(request.form.get("etiquetage_conforme"))
        dlc_ddm = request.form.get("dlc_ddm") or None
        numero_lot = (request.form.get("numero_lot") or "").strip() or None
        commentaire = (request.form.get("commentaire") or "").strip() or None

        if not benevole or not libelle_produit:
            flash("⚠️ Merci d'indiquer le prénom du bénévole et le produit réceptionné.", "warning")
            return render_template(
                "production_cuisine/receptions_creer.html",
                fournisseurs=fournisseurs,
                date_defaut=today_paris(),
                form=request.form,
            )

        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO cuisine_receptions
                    (date_reception, heure_arrivee, fournisseur_id, camion_libelle,
                     libelle_produit, temperature_mesuree, poids_kg, aspect_conforme,
                     emballage_conforme, etiquetage_conforme, dlc_ddm, numero_lot,
                     commentaire, user_creation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date_reception, heure_arrivee, fournisseur_id, camion_libelle,
                        libelle_produit, temperature_mesuree, poids_kg, aspect_conforme,
                        emballage_conforme, etiquetage_conforme, dlc_ddm, numero_lot,
                        commentaire, benevole,
                    ),
                )
                reception_id = cur.lastrowid

                # 📸 Photos produit / étiquette (capture tablette)
                dossier = upload_dir_reception(reception_id)
                for type_photo, champ in (("produit", "photos_produit"), ("etiquette", "photos_etiquette")):
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
            flash("✅ Réception enregistrée.", "success")
            return redirect(url_for("production_cuisine.detail_reception", reception_id=reception_id))

        except Exception as e:
            write_log(f"❌ Erreur création réception cuisine : {e}")
            flash("❌ Erreur lors de l'enregistrement de la réception.", "danger")
            return render_template(
                "production_cuisine/receptions_creer.html",
                fournisseurs=fournisseurs,
                date_defaut=today_paris(),
                form=request.form,
            )

    return render_template(
        "production_cuisine/receptions_creer.html",
        fournisseurs=fournisseurs,
        date_defaut=today_paris(),
        form={},
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

    return render_template(
        "production_cuisine/receptions_detail.html",
        reception=reception,
        photos=photos,
        productions_du_jour=productions_du_jour,
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
