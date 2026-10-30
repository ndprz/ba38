# ba38_fiches_visite.py
import sqlite3
import os
import io
from urllib.parse import urlparse
from markupsafe import Markup, escape


from datetime import date, datetime

from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, send_file
)
from flask_login import login_required
from weasyprint import HTML, CSS

from ba38_utilitaires.core import (
    get_db_connection,
    write_log,
    get_db_path,
    upload_database,
    require_access
)
from ba38_utilitaires.organisation import get_organisation


fiches_visite_bp = Blueprint("fiches_visite", __name__)

@fiches_visite_bp.route("/fiches_visite/<int:partner_id>")
@login_required
@require_access("associations", "lecture")
def liste(partner_id):
    """
    Liste des fiches de visite pour un partenaire donné.
    """
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        partenaire = cursor.execute(
            "SELECT * FROM associations WHERE id = ?", (partner_id,)
        ).fetchone()

        fiches = cursor.execute(
            "SELECT * FROM fiches_visite WHERE partenaire_id = ? ORDER BY date_visite DESC",
            (partner_id,)
        ).fetchall()

        conn.close()
    except Exception as e:
        write_log(f"❌ Erreur chargement fiches_visite : {e}")
        flash("Erreur lors du chargement des fiches de visite.", "danger")
        return redirect(url_for("partenaires.update_partner", partner_id=partner_id))

    return render_template(
        "fiches_visite/fiches_visite.html",
        partenaire=clean_row(partenaire),
        fiches=[clean_row(f) for f in fiches],
        partner_id=partner_id
    )



# ========================================
# 🆕 Nouvelle fiche de visite
# ========================================
@fiches_visite_bp.route("/fiches_visite/<int:partner_id>/nouvelle", methods=["GET", "POST"])
@login_required
@require_access("associations", "ecriture")
def nouvelle(partner_id):

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 🔍 Infos partenaire
    partenaire = cur.execute(
        "SELECT * FROM associations WHERE id = ?", (partner_id,)
    ).fetchone()
    if not partenaire:
        conn.close()
        flash("⛔ Partenaire introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    if request.method == "POST":
        # 🔎 Récupération dynamique des colonnes de la table
        colonnes = [row[1] for row in cur.execute("PRAGMA table_info(fiches_visite)").fetchall()]

        # 🔽 Construction des données depuis request.form
        # On inclut partenaire_id, mais pas id (autoincrément)
        data = {col: request.form.get(col) for col in colonnes if col != "id"}
        data["partenaire_id"] = partner_id  # forçage du lien

        # ✅ Sécurité : si le champ car n'est pas envoyé ou est vide,
        # on reprend le CAR partenaire.
        if "car" in colonnes and not data.get("car"):
            data["car"] = clean_row(partenaire).get("CAR", "")

        # 🔽 Génération dynamique de la requête INSERT
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        params = list(data.values())

        cur.execute(f"INSERT INTO fiches_visite ({cols}) VALUES ({placeholders})", params)
        conn.commit()
        conn.close()

        upload_database()
        flash("✅ Nouvelle fiche de visite enregistrée.", "success")
        return redirect(url_for("fiches_visite.liste", partner_id=partner_id))

    # 🚗 Liste des CAR (depuis parametres)
    cars = [row["param_value"] for row in cur.execute(
        "SELECT param_value FROM parametres WHERE param_name = 'car' ORDER BY param_value"
    ).fetchall()]

    # 🔁 Pré-remplissage avec la dernière fiche
    last_fiche = cur.execute(
        "SELECT * FROM fiches_visite WHERE partenaire_id = ? AND lien_pdf_drive IS NULL "
        "ORDER BY date_visite DESC LIMIT 1",
        (partner_id,)
    ).fetchone()

    fiche_data = {}
    last_date = None

    if last_fiche:
        fiche_data = dict(last_fiche)
        last_date = last_fiche["date_visite"]
        fiche_data.pop("id", None)
        fiche_data["date_precedente_visite"] = last_date
        fiche_data["date_visite"] = ""

    # ✅ En création, le CAR par défaut vient toujours de la fiche partenaire
    # Il sera ensuite enregistré dans fiches_visite.car à l'enregistrement.
    partenaire_dict = clean_row(partenaire)
    fiche_data["car"] = partenaire_dict.get("CAR", "")

    conn.close()

    return render_template(
        "fiches_visite/fiche_visite_form.html",
        partenaire=partenaire_dict,
        fiche=fiche_data,
        cars=cars,
        partner_id=partner_id,
        last_date=last_date,
        lecture_seule=False,
        annee=date.today().year
    )


# ========================================
# 📎 Fiche de visite PDF déjà remplie (lien Drive)
# ========================================
_HOTES_DRIVE = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")


def _lien_drive_valide(lien):
    """Accepte uniquement un lien https Google Drive : le lien est affiché
    comme href, un javascript: ou un site tiers n'a rien à faire là."""
    try:
        u = urlparse(lien)
    except ValueError:
        return False
    return u.scheme == "https" and (u.hostname or "").lower() in _HOTES_DRIVE


@fiches_visite_bp.route("/fiches_visite/<int:partner_id>/nouvelle_pdf", methods=["GET", "POST"])
@login_required
@require_access("associations", "ecriture")
def nouvelle_pdf(partner_id):
    """Ajoute une ligne (date de visite + lien Drive) pour une association
    qui a rempli la fiche en PDF au lieu du formulaire en ligne. Le PDF n'est
    pas téléchargé sur le serveur : seul le lien de partage est conservé."""
    conn = get_db_connection()
    partenaire = conn.execute("SELECT * FROM associations WHERE id = ?", (partner_id,)).fetchone()
    if not partenaire:
        conn.close()
        flash("⛔ Partenaire introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))
    partenaire_dict = clean_row(partenaire)

    if request.method == "POST":
        date_visite = (request.form.get("date_visite") or "").strip()
        lien = (request.form.get("lien_pdf_drive") or "").strip()
        erreur = None
        if not date_visite:
            erreur = "La date de visite est obligatoire."
        elif not _lien_drive_valide(lien):
            erreur = "Le lien doit être un lien de partage Google Drive (https://drive.google.com/…)."
        if erreur:
            conn.close()
            flash(f"⛔ {erreur}", "danger")
            return render_template(
                "fiches_visite/fiche_visite_pdf_drive_form.html",
                partenaire=partenaire_dict, partner_id=partner_id, fiche={},
                date_visite=date_visite, lien=lien,
            )

        conn.execute(
            "INSERT INTO fiches_visite (partenaire_id, date_visite, car, lien_pdf_drive) VALUES (?, ?, ?, ?)",
            (partner_id, date_visite, partenaire_dict.get("CAR", ""), lien),
        )
        conn.commit()
        conn.close()
        upload_database()
        write_log(f"📎 Fiche de visite PDF (Drive) ajoutée pour l'association {partner_id}, visite du {date_visite}")
        flash("✅ Fiche de visite PDF ajoutée.", "success")
        return redirect(url_for("fiches_visite.liste", partner_id=partner_id))

    conn.close()
    return render_template(
        "fiches_visite/fiche_visite_pdf_drive_form.html",
        partenaire=partenaire_dict, partner_id=partner_id, fiche={},
        date_visite=date.today().isoformat(), lien="",
    )


# ========================================
# 👁️ Visualiser fiche de visite
# ========================================
@fiches_visite_bp.route("/fiches_visite/view/<int:fiche_id>")
@login_required
@require_access("associations", "lecture")
def view(fiche_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    fiche = cur.execute("SELECT * FROM fiches_visite WHERE id = ?", (fiche_id,)).fetchone()
    if fiche and fiche["lien_pdf_drive"]:
        conn.close()
        return redirect(fiche["lien_pdf_drive"])
    partenaire = cur.execute("SELECT * FROM associations WHERE id = ?", (fiche["partenaire_id"],)).fetchone()

    cars = [row["param_value"] for row in cur.execute(
        "SELECT param_value FROM parametres WHERE param_name = 'car' ORDER BY param_value"
    ).fetchall()]

    conn.close()

    fiche_dict = clean_row(fiche)
    partenaire_dict = clean_row(partenaire)

    return render_template(
        "fiches_visite/fiche_visite_form.html",
        partenaire=partenaire_dict,
        fiche=fiche_dict,
        cars=cars,
        partner_id=partenaire_dict.get("Id") or fiche_dict.get("partenaire_id"),
        lecture_seule=True
    )



# ========================================
# ✏️ Modifier fiche de visite
# ========================================
@fiches_visite_bp.route("/fiches_visite/modifier/<int:fiche_id>", methods=["GET", "POST"])
@login_required
@require_access("associations", "ecriture")
def modifier(fiche_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    fiche = cur.execute("SELECT * FROM fiches_visite WHERE id = ?", (fiche_id,)).fetchone()
    if not fiche:
        conn.close()
        flash("❌ Fiche introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

    if fiche["lien_pdf_drive"]:
        # Fiche PDF : le formulaire en ligne écraserait tous les champs par du vide
        conn.close()
        return _modifier_pdf(fiche_id, clean_row(fiche))

    if request.method == "POST":
        # 🔎 Récupération dynamique des colonnes de la table
        colonnes = [row[1] for row in cur.execute("PRAGMA table_info(fiches_visite)").fetchall()]

        # 🔽 Construction des données depuis request.form
        # On exclut les colonnes auto-gérées
        data = {col: request.form.get(col) for col in colonnes if col not in ("id", "partenaire_id")}

        # 🔽 Génération dynamique de la requête UPDATE
        sets = ", ".join([f"{col}=?" for col in data.keys()])
        params = list(data.values()) + [fiche_id]

        cur.execute(f"UPDATE fiches_visite SET {sets} WHERE id=?", params)
        conn.commit()
        conn.close()

        upload_database()
        flash("✅ Fiche de visite mise à jour avec succès.", "success")
        return redirect(url_for("fiches_visite.modifier", fiche_id=fiche_id))

    # 🔽 GET → Affichage du formulaire
    partenaire = cur.execute("SELECT * FROM associations WHERE id = ?", (fiche["partenaire_id"],)).fetchone()
    cars = [row["param_value"] for row in cur.execute(
        "SELECT param_value FROM parametres WHERE param_name = 'car' ORDER BY param_value"
    ).fetchall()]

    conn.close()
    fiche_dict = clean_row(fiche)
    partenaire_dict = clean_row(partenaire)

    return render_template(
        "fiches_visite/fiche_visite_form.html",
        partenaire=partenaire_dict,
        fiche=fiche_dict,
        cars=cars,
        partner_id=partenaire_dict.get("Id") or fiche_dict.get("partenaire_id"),
        lecture_seule=False
    )



def _modifier_pdf(fiche_id, fiche):
    partner_id = fiche["partenaire_id"]
    conn = get_db_connection()
    partenaire = conn.execute("SELECT * FROM associations WHERE id = ?", (partner_id,)).fetchone()
    partenaire_dict = clean_row(partenaire)

    if request.method == "POST":
        date_visite = (request.form.get("date_visite") or "").strip()
        lien = (request.form.get("lien_pdf_drive") or "").strip()
        erreur = None
        if not date_visite:
            erreur = "La date de visite est obligatoire."
        elif not _lien_drive_valide(lien):
            erreur = "Le lien doit être un lien de partage Google Drive (https://drive.google.com/…)."
        if erreur:
            conn.close()
            flash(f"⛔ {erreur}", "danger")
            return render_template(
                "fiches_visite/fiche_visite_pdf_drive_form.html",
                partenaire=partenaire_dict, partner_id=partner_id, fiche=fiche,
                date_visite=date_visite, lien=lien,
            )
        conn.execute(
            "UPDATE fiches_visite SET date_visite = ?, lien_pdf_drive = ? WHERE id = ?",
            (date_visite, lien, fiche_id),
        )
        conn.commit()
        conn.close()
        upload_database()
        flash("✅ Fiche de visite PDF mise à jour.", "success")
        return redirect(url_for("fiches_visite.liste", partner_id=partner_id))

    conn.close()
    return render_template(
        "fiches_visite/fiche_visite_pdf_drive_form.html",
        partenaire=partenaire_dict, partner_id=partner_id, fiche=fiche,
        date_visite=fiche["date_visite"], lien=fiche["lien_pdf_drive"],
    )


@fiches_visite_bp.route("/fiches_visite/pdf/<int:fiche_id>")
@login_required
@require_access("associations", "lecture")
def pdf(fiche_id):
    try:
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            fiche = conn.execute(
                "SELECT * FROM fiches_visite WHERE id = ?", (fiche_id,)
            ).fetchone()

            partenaire = None
            if fiche:
                partenaire = conn.execute(
                    "SELECT * FROM associations WHERE id = ?", (fiche["partenaire_id"],)
                ).fetchone()

        fiche = dict(fiche) if fiche else None
        partenaire = dict(partenaire) if partenaire else {}

        if not fiche:
            return "❌ Fiche non trouvée", 404
        if fiche.get("lien_pdf_drive"):
            return redirect(fiche["lien_pdf_drive"])

        org = get_organisation()
        logo_path = os.path.join(os.getcwd(), org["logo_path"])

        rendered_html = render_template(
            "fiches_visite/fiche_visite_pdf.html",
            fiche=fiche,
            partenaire=partenaire,
            lecture_seule=True,
            logo_path=logo_path,
            org=org,
        )

        pdf_io = io.BytesIO()
        css_path = os.path.join(os.getcwd(), "static/css/fiche_visite_pdf.css")

        HTML(
            string=rendered_html,
            base_url=request.url_root
        ).write_pdf(
            pdf_io,
            stylesheets=[
                CSS(css_path),
                CSS(string='@page { margin: 1.5cm; }')
            ]
        )
        pdf_io.seek(0)

        return send_file(
            pdf_io,
            as_attachment=True,
            download_name=f"fiche_visite_{fiche_id}.pdf",
            mimetype="application/pdf"
        )

    except Exception as e:
        write_log(f"❌ Erreur génération PDF fiche_visite {fiche_id} : {e}")
        return f"Erreur génération PDF : {e}", 500



# ============================
# 📝 Debug : afficher le rendu HTML de la fiche (avant conversion PDF)
# ============================
@fiches_visite_bp.route("/fiches_visite/html/<int:fiche_id>")
@login_required
@require_access("associations", "lecture")
def fiche_visite_html(fiche_id):
    """🔍 Affiche directement le template fiche_visite_pdf.html dans le navigateur (sans PDF).
       Utile pour vérifier le rendu avant export PDF."""
    try:
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            fiche = conn.execute(
                "SELECT * FROM fiches_visite WHERE id = ?", (fiche_id,)
            ).fetchone()

            partenaire = None
            if fiche:
                partenaire = conn.execute(
                    "SELECT * FROM associations WHERE id = ?", (fiche["partenaire_id"],)
                ).fetchone()

        if not fiche:
            return "❌ Fiche non trouvée", 404
        if fiche["lien_pdf_drive"]:
            return redirect(fiche["lien_pdf_drive"])

        org = get_organisation()
        logo_path = os.path.join(os.getcwd(), org["logo_path"])

        # Retourne le rendu HTML du template
        return render_template(
            "fiches_visite/fiche_visite_pdf.html", fiche=dict(fiche),
            partenaire=dict(partenaire) if partenaire else {},
            logo_path=logo_path, org=org,
        )

    except Exception as e:
        write_log(f"❌ Erreur debug HTML fiche_visite {fiche_id} : {e}")
        return f"Erreur affichage HTML : {e}", 500



def clean_row(row):
    """Convertit sqlite3.Row en dict et remplace None par '' """
    if not row:
        return {}
    return {k: (v if v is not None else "") for k, v in dict(row).items()}



@fiches_visite_bp.app_template_filter('datetimeformat')
def datetimeformat(value, format='%d/%m/%Y'):
    if not value:
        return ''
    try:
        return datetime.strptime(value, '%Y-%m-%d').strftime(format)
    except ValueError:
        try:
            return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').strftime(format)
        except ValueError:
            return value


@fiches_visite_bp.app_template_filter("nl2br")
def nl2br(value):
    """
    Convertit les retours à la ligne saisis dans les textarea en <br>
    pour l'affichage HTML/PDF WeasyPrint.
    """
    if not value:
        return ""

    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    return Markup("<br>".join(escape(line) for line in lines))


# ========================================
# 🗑️ Supprimer fiche de visite
# ========================================
@fiches_visite_bp.route("/fiches_visite/supprimer/<int:fiche_id>", methods=["POST"])
@login_required
@require_access("associations", "ecriture")
def supprimer(fiche_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 🔍 récupérer partner_id pour redirection
        fiche = cursor.execute(
            "SELECT partenaire_id FROM fiches_visite WHERE id = ?",
            (fiche_id,)
        ).fetchone()

        if not fiche:
            conn.close()
            flash("❌ Fiche introuvable.", "danger")
            return redirect(url_for("partenaires.partenaires"))

        partner_id = fiche["partenaire_id"]

        # 🗑️ suppression
        cursor.execute(
            "DELETE FROM fiches_visite WHERE id = ?",
            (fiche_id,)
        )

        conn.commit()
        conn.close()

        upload_database()

        flash("✅ Fiche de visite supprimée.", "success")
        return redirect(url_for("fiches_visite.liste", partner_id=partner_id))

    except Exception as e:
        write_log(f"❌ Erreur suppression fiche_visite {fiche_id} : {e}")
        flash("Erreur lors de la suppression.", "danger")
        return redirect(url_for("partenaires.partenaires"))
