# ba38_fiches_visite.py
import sqlite3
import os
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from utils import get_db_connection, write_log, get_db_path, has_access, upload_database
from datetime import date
annee = date.today().year

from datetime import datetime
from flask import send_file, render_template, request
from flask_login import login_required
from utils import get_db_connection, write_log, get_db_path
from weasyprint import HTML
import io
import os
        
fiches_visite_bp = Blueprint("fiches_visite", __name__)

@fiches_visite_bp.route("/fiches_visite/<int:partner_id>")
@login_required
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
        "fiches_visite.html",
        partenaire=clean_row(partenaire),
        fiches=[clean_row(f) for f in fiches],
        partner_id=partner_id
    )



# ========================================
# 🆕 Nouvelle fiche de visite
# ========================================
@fiches_visite_bp.route("/fiches_visite/<int:partner_id>/nouvelle", methods=["GET", "POST"])
@login_required
def nouvelle(partner_id):
    # 🔒 Vérification des droits
    if not has_access("associations", "ecriture"):
        flash("⛔ Vous n’avez pas les droits pour créer une fiche visite.", "danger")
        return redirect(url_for("partenaires.partenaires"))

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
        "SELECT * FROM fiches_visite WHERE partenaire_id = ? ORDER BY date_visite DESC LIMIT 1",
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

    conn.close()

    return render_template(
        "fiche_visite_form.html",
        partenaire=clean_row(partenaire),
        fiche=fiche_data,
        cars=cars,
        partner_id=partner_id,
        last_date=last_date,
        lecture_seule=False
    )


# ========================================
# 👁️ Visualiser fiche de visite
# ========================================
@fiches_visite_bp.route("/fiches_visite/view/<int:fiche_id>")
@login_required
def view(fiche_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    fiche = cur.execute("SELECT * FROM fiches_visite WHERE id = ?", (fiche_id,)).fetchone()
    partenaire = cur.execute("SELECT * FROM associations WHERE id = ?", (fiche["partenaire_id"],)).fetchone()

    cars = [row["param_value"] for row in cur.execute(
        "SELECT param_value FROM parametres WHERE param_name = 'car' ORDER BY param_value"
    ).fetchall()]

    conn.close()

    fiche_dict = clean_row(fiche)
    partenaire_dict = clean_row(partenaire)

    return render_template(
        "fiche_visite_form.html",
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
def modifier(fiche_id):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    fiche = cur.execute("SELECT * FROM fiches_visite WHERE id = ?", (fiche_id,)).fetchone()
    if not fiche:
        conn.close()
        flash("❌ Fiche introuvable.", "danger")
        return redirect(url_for("partenaires.partenaires"))

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
        "fiche_visite_form.html",
        partenaire=partenaire_dict,
        fiche=fiche_dict,
        cars=cars,
        partner_id=partenaire_dict.get("Id") or fiche_dict.get("partenaire_id"),
        lecture_seule=False
    )



@fiches_visite_bp.route("/fiches_visite/pdf/<int:fiche_id>")
@login_required
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

        rendered_html = render_template(
            "fiche_visite_pdf.html",
            fiche=fiche,
            partenaire=partenaire,
            lecture_seule=True
        )

        pdf_io = io.BytesIO()
        HTML(string=rendered_html, base_url=request.url_root).write_pdf(pdf_io)
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

        if not fiche:
            return "❌ Fiche non trouvée", 404

        # Retourne le rendu HTML du template
        return render_template("fiche_visite_pdf.html", fiche=dict(fiche))

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
        


def header_footer_fiche_visite(canvas, doc):
    """En-tête et pied de page spécifique aux PDF Fiches Visite"""
    canvas.saveState()

    # Logo gauche
    logo_path = "static/images/logo.png"
    if os.path.exists(logo_path):
        canvas.drawImage(logo_path, x=40, y=A4[1] - 60,
                         width=1.5*cm, height=1.5*cm, preserveAspectRatio=True)

    # Titre centré
    canvas.setFont("Helvetica-Bold", 14)
    canvas.setFillColorRGB(1, 0.5, 0)  # Orange
    canvas.drawCentredString(A4[0] / 2, A4[1] - 40, "Fiche de visite")
    canvas.setFillColorRGB(0, 0, 0)

    # Texte à droite
    canvas.setFont("Helvetica", 10)
    canvas.drawRightString(A4[0] - 40, A4[1] - 40, "Version ISÈRE 2025")

    # Pied de page
    page_num = canvas.getPageNumber()
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(A4[0] - 40, 20, f"Page {page_num}")

    canvas.restoreState()
