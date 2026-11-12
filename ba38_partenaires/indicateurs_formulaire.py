# =========================================
# 📝 Indicateurs : formulaire de réponse en ligne (association)
# =========================================
# Lien personnel (jeton) dans le mail de demande, page publique calquée sur le
# PDF joint. Une fois validé, le formulaire est verrouillé (la BAI peut le
# rouvrir depuis la page Résultats) et l'association reçoit un mail avec le
# PDF rempli.

import os
import re
import secrets
import sqlite3
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from ba38_partenaires.indicateurs import indicateurs_bp
from ba38_utilitaires.core import (
    REPLY_TO_INDICATEURS, emails_indicateurs, envoyer_mail, get_db_connection,
    get_templates_pdf_dir, require_access, write_log,
)

MAX_VALEUR = 10_000_000

# Colonne « Répondu » (indicateurs_suivi.reponse_mode)
REPONSE_NON_RECU, REPONSE_MAIL, REPONSE_FORMULAIRE, REPONSE_COURRIER = 1, 2, 3, 4
REPONSE_LIBELLES = {1: "Non reçu", 2: "Réponse mail", 3: "Réponse formulaire", 4: "Courrier"}

# (clé, libellé, aide) — d'après les cases du PDF
CHAMPS_TRIM = [
    ("foyers_trim", "Foyers inscrits dans le trimestre",
     "Tous les foyers du mois 1 + nouveaux foyers du mois 2 + nouveaux foyers du mois 3. "
     "Ex : 1 foyer = un couple avec 2 enfants, ou une mère et un enfant, ou une personne seule…"),
    ("beneficiaires_trim", "Bénéficiaires inscrits dans le trimestre",
     "Tous les bénéficiaires du mois 1 + nouveaux bénéficiaires du mois 2 + nouveaux bénéficiaires du mois 3. "
     "On comptabilise les inscrits, même s'ils ne sont pas venus."),
    ("passages_trim", "Nombre total de passages dans le trimestre",
     "Tous les passages du mois 1 + du mois 2 + du mois 3. On compte par personne et non par foyer : "
     "1 foyer de 4 personnes représente 4 passages à chaque fois."),
]
CHAMPS_ANNUEL = [
    ("foyers_trim", "Foyers inscrits dans le trimestre", CHAMPS_TRIM[0][2], "Trimestre"),
    ("foyers_annee", "Foyers inscrits dans l'année",
     "Attention, ce n'est PAS l'addition des 4 trimestres. Si un foyer, inscrit en janvier, est toujours "
     "présent en septembre, on ne le compte qu'une seule fois.", "Année"),
    ("beneficiaires_trim", "Bénéficiaires inscrits dans le trimestre", CHAMPS_TRIM[1][2], "Trimestre"),
    ("personnes_annee", "Personnes inscrites dans l'année",
     "Attention, ce n'est PAS l'addition des 4 trimestres. Si une personne, inscrite en janvier, est toujours "
     "présente en septembre, on ne la compte qu'une seule fois.", "Année"),
    ("passages_trim", "Nombre total de passages dans le trimestre", CHAMPS_TRIM[2][2], "Trimestre"),
    ("passages_annee", "Total des passages dans l'année",
     "Total des passages dans l'année = total des passages des 4 trimestres. On compte par personne et non par foyer.",
     "Année"),
]

# Noms des champs AcroForm des deux PDF
PDF_CHAMPS_TRIM = {
    "foyers_trim": "Foyers_Trimestre", "beneficiaires_trim": "Bénéficiaires_Trimestre",
    "passages_trim": "Passages_Trimestre", "remarques": "remarques",
}
PDF_CHAMPS_ANNUEL = {
    "foyers_trim": "Foyers trimestre", "foyers_annee": "Foyers année",
    "beneficiaires_trim": "beneficiaires trimestre", "personnes_annee": "Personnes année",
    "passages_trim": "Passages trimestre", "passages_annee": "Passages année",
    "remarques": "Remarques",
}


def est_annuel(campagne):
    return str(campagne["periode"]).lower().startswith("année")


def champs_pour(campagne):
    if est_annuel(campagne):
        return [(k, lib, aide, col) for k, lib, aide, col in CHAMPS_ANNUEL]
    return [(k, lib, aide, "Trimestre") for k, lib, aide in CHAMPS_TRIM]


def libelle_trimestre(campagne):
    """« 4ème trimestre » pour une campagne annuelle (le T4 clôt l'année)."""
    n = int(campagne["trimestre"] or 4)
    return f"{n}{'er' if n == 1 else 'ème'} trimestre"


# -----------------------------------------
# 🔗 Jeton / lien
# -----------------------------------------
def assurer_token(conn, suivi_id):
    row = conn.execute("SELECT token FROM indicateurs_suivi WHERE id = ?", (suivi_id,)).fetchone()
    token = row[0] if row else None
    if not token:
        token = secrets.token_urlsafe(16)
        conn.execute("UPDATE indicateurs_suivi SET token = ? WHERE id = ?", (token, suivi_id))
        conn.commit()
    return token


def url_formulaire(token):
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    return f"{base}/indicateurs/repondre/{token}"


def ajouter_lien_formulaire(corps, lien):
    """Le modèle de mail peut placer le lien avec <<lien_formulaire>> ;
    sinon il est ajouté en fin de message."""
    if "<<lien_formulaire>>" in (corps or ""):
        return corps.replace("<<lien_formulaire>>", lien)
    return (
        f"{corps or ''}\n\n"
        f"👉 Vous pouvez aussi répondre en ligne, sans imprimer ni renvoyer le PDF : {lien}\n"
        f"(lien personnel à votre association ; une fois validé, le formulaire ne peut plus être modifié "
        f"et vous recevez le formulaire rempli par mail)"
    )


def bouton_html(lien, libelle="📝 Saisie en ligne"):
    """Gros bouton compatible clients mail (table + styles en ligne)."""
    return (
        '<div style="text-align:center; margin:22px 0;">'
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" align="center" style="margin:0 auto;">'
        '<tr><td align="center" bgcolor="#d9531e" style="background:#d9531e; border-radius:8px;">'
        f'<a href="{lien}" target="_blank" style="display:inline-block; padding:18px 44px; '
        'font-family:Arial,Helvetica,sans-serif; font-size:22px; font-weight:bold; color:#ffffff; '
        f'text-decoration:none; border-radius:8px;">{libelle}</a>'
        '</td></tr></table></div>'
    )


def rendre_corps_modele(modele, contexte, lien):
    """(corps, is_html) d'un modèle de mail indicateurs.
    - texte brut (is_html=0) : <<lien_formulaire>> ou <<bouton_formulaire>> = URL, ajoutée en fin sinon ;
    - mail évolué (is_html=1, éditeur Quill) : variables échappées, <<bouton_formulaire>> = gros
      bouton « Saisie en ligne », <<lien_formulaire>> = lien cliquable ; si aucun des deux n'est
      présent, le bouton est ajouté en fin de message."""
    import html as _html
    from ba38_utilitaires.core import render_modele_email
    corps = modele["corps"] or ""
    if not modele["is_html"]:
        corps = render_modele_email(corps.replace("<<bouton_formulaire>>", "<<lien_formulaire>>"), contexte)
        return ajouter_lien_formulaire(corps, lien), False

    # Quill enregistre « << >> » sous la forme &lt;&lt; &gt;&gt;
    corps = re.sub(r"&lt;&lt;(\w+)&gt;&gt;", r"<<\1>>", corps)
    ctx = {k: _html.escape(str(v or "")) for k, v in contexte.items()}
    corps = render_modele_email(corps, ctx)
    bouton = bouton_html(lien)
    present = "<<bouton_formulaire>>" in corps or "<<lien_formulaire>>" in corps
    corps = re.sub(r"<p[^>]*>\s*<<bouton_formulaire>>\s*</p>", lambda m: bouton, corps)
    corps = corps.replace("<<bouton_formulaire>>", bouton)
    corps = re.sub(r"(?<!href=\")(?<!href=')<<lien_formulaire>>", lambda m: f'<a href="{lien}">{lien}</a>', corps)
    corps = corps.replace("<<lien_formulaire>>", lien)
    if not present:
        corps += bouton + (
            '<p style="text-align:center; font-size:12px; color:#666;">Lien personnel à votre association ; '
            'une fois validé, le formulaire ne peut plus être modifié et vous recevez le formulaire rempli par mail.</p>')
    return corps, True


# -----------------------------------------
# 📄 PDF rempli (même gabarit que la demande)
# -----------------------------------------
def _aplatir(template, output_path, data, champs_numeriques=()):
    """Écrit les valeurs comme du texte fixe aux emplacements des champs, puis
    supprime le formulaire : PDF statique, non modifiable, instantané à
    l'ouverture (un PDF à champs + NeedAppearances oblige le lecteur du
    navigateur à tout régénérer)."""
    import io
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import simpleSplit

    reader = PdfReader(template)
    page = reader.pages[0]
    w, h = float(page.mediabox.width), float(page.mediabox.height)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    for annot in page.get("/Annots", []):
        a = annot.get_object()
        nom = a.get("/T")
        if nom is None and a.get("/Parent") is not None:
            nom = a["/Parent"].get_object().get("/T")
        valeur = data.get(str(nom)) if nom is not None else None
        if not valeur:
            continue
        x1, y1, x2, y2 = [float(v) for v in a["/Rect"]]
        x1, x2, y1, y2 = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
        haut = y2 - y1
        if str(nom).lower() == "remarques":  # zone multi-lignes (observations)
            taille = 9
            lignes = simpleSplit(str(valeur), "Helvetica", taille, x2 - x1 - 6)
            y = y2 - taille - 3
            for ligne in lignes:
                if y < y1:
                    break
                c.setFont("Helvetica", taille); c.drawString(x1 + 3, y, ligne)
                y -= taille + 2
        else:
            taille = 13 if str(nom) in champs_numeriques else (11 if haut >= 14 else max(haut - 3, 6))
            texte = str(valeur)
            while taille > 6 and c.stringWidth(texte, "Helvetica", taille) > x2 - x1 - 6:
                taille -= 0.5
            c.setFont("Helvetica", taille)
            if str(nom) in champs_numeriques:
                c.drawRightString(x2 - 4, y1 + (haut - taille) / 2 + 1, texte)
            else:
                c.drawString(x1 + 3, y1 + (haut - taille) / 2 + 1, texte)
    c.save()
    buf.seek(0)

    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    page = writer.pages[0]
    page.merge_page(PdfReader(buf).pages[0])
    if "/Annots" in page:
        del page["/Annots"]
    if "/AcroForm" in writer._root_object:
        del writer._root_object["/AcroForm"]
    with open(output_path, "wb") as f:
        writer.write(f)


def remplir_pdf_reponse(output_path, assoc, campagne, rep):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, NumberObject

    annuel = est_annuel(campagne)
    template = os.path.join(get_templates_pdf_dir(), "indicateurs_annuels.pdf" if annuel else "indicateurs_trimestriels.pdf")
    mapping = PDF_CHAMPS_ANNUEL if annuel else PDF_CHAMPS_TRIM

    parts = str(campagne["periode"]).split()
    d = campagne["date_limite"] or ""
    try:
        d = datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        pass
    data = {
        "annee": parts[1] if len(parts) > 1 else "",
        "date_limite": d,
        "nom_association": assoc["nom_association"] or "",
        "responsable_IE": rep["responsable_ie"] or "",
        "tel_resp_IE": rep["tel_resp_ie"] or "",
        "courriel_resp_IE1": rep["courriel_ie"] or "",
        "courriel_resp_IE2": "",
        "CAR": assoc["CAR"] or "",
    }
    if not annuel:
        data["trimestre"] = parts[0] if parts else ""
        data["code_vif"] = assoc["code_vif"] or ""
    for cle, nom_pdf in mapping.items():
        v = rep[cle]
        data[nom_pdf] = "" if v is None else str(v)

    _aplatir(template, output_path, data,
             [n for k, n in mapping.items() if k != "remarques"])
    return output_path


# -----------------------------------------
# 🔍 Chargement
# -----------------------------------------
def _charger(conn, token):
    if not token or len(token) > 64:
        abort(404)
    suivi = conn.execute("""
        SELECT s.*, a.nom_association, a.code_VIF AS code_vif, a.CAR, a.responsable_IE, a.tel_resp_IE,
               a.courriel_resp_IE1, a.courriel_resp_IE2, a.courriel_association
        FROM indicateurs_suivi s JOIN associations a ON a.id = s.association_id
        WHERE s.token = ?""", (token,)).fetchone()
    if suivi is None:
        abort(404)
    campagne = conn.execute("SELECT * FROM indicateurs_campagnes WHERE id = ?", (suivi["campagne_id"],)).fetchone()
    if campagne is None:
        abort(404)
    rep = conn.execute("SELECT * FROM indicateurs_reponses WHERE suivi_id = ?", (suivi["id"],)).fetchone()
    return suivi, campagne, rep


def _masquer(email):
    local, _, dom = email.partition("@")
    return f"{local[:2]}{'*' * max(len(local) - 2, 1)}@{dom}"


def _entier(brut):
    brut = re.sub(r"[\s .]", "", brut or "")
    if not re.fullmatch(r"\d{1,8}", brut):
        return None
    v = int(brut)
    return v if v <= MAX_VALEUR else None


def _valeurs_defaut(suivi, rep):
    base = {
        "responsable_ie": suivi["responsable_IE"] or "",
        "tel_resp_ie": suivi["tel_resp_IE"] or "",
        "courriel_ie": (suivi["courriel_resp_IE1"] or suivi["courriel_association"] or "").split(";")[0].strip(),
        "remarques": "",
    }
    if rep is not None:
        for k in ("responsable_ie", "tel_resp_ie", "courriel_ie", "remarques"):
            if rep[k] is not None:
                base[k] = rep[k]
        for k in ("foyers_trim", "beneficiaires_trim", "passages_trim", "foyers_annee", "personnes_annee", "passages_annee"):
            base[k] = "" if rep[k] is None else rep[k]
    return base


# -----------------------------------------
# 🌐 Page publique
# -----------------------------------------
@indicateurs_bp.route("/indicateurs/repondre/<token>", methods=["GET", "POST"])
def repondre(token):
    with get_db_connection() as conn:
        suivi, campagne, rep = _charger(conn, token)
        champs = champs_pour(campagne)
        verrouille = rep is not None and rep["soumis_le"] is not None
        valeurs = _valeurs_defaut(suivi, rep)
        erreurs, mail_info = [], None

        if request.method == "POST":
            if verrouille:
                flash("Ce formulaire a déjà été validé : il ne peut plus être modifié.", "warning")
                return redirect(url_for("indicateurs.repondre", token=token))

            valeurs["responsable_ie"] = (request.form.get("responsable_ie") or "").strip()[:120]
            valeurs["tel_resp_ie"] = (request.form.get("tel_resp_ie") or "").strip()[:40]
            valeurs["courriel_ie"] = (request.form.get("courriel_ie") or "").strip()[:200]
            valeurs["remarques"] = (request.form.get("remarques") or "").strip()[:1500]

            nombres = {}
            for cle, lib, _aide, _col in champs:
                brut = request.form.get(cle, "")
                valeurs[cle] = brut
                n = _entier(brut)
                if n is None:
                    erreurs.append(f"« {lib} » : indiquez un nombre entier (0 si aucun).")
                nombres[cle] = n
            if not valeurs["responsable_ie"]:
                erreurs.append("Indiquez la personne en charge du calcul des indicateurs.")
            if not valeurs["courriel_ie"] or "@" not in valeurs["courriel_ie"]:
                erreurs.append("Indiquez une adresse courriel valide.")
            if not erreurs and est_annuel(campagne):
                for t, a, nom in (("foyers_trim", "foyers_annee", "foyers"),
                                  ("beneficiaires_trim", "personnes_annee", "personnes"),
                                  ("passages_trim", "passages_annee", "passages")):
                    if nombres[a] < nombres[t]:
                        erreurs.append(f"Le nombre de {nom} de l'année ne peut pas être inférieur à celui du trimestre.")

            if not erreurs:
                now = datetime.now().isoformat(timespec="seconds")
                conn.execute("INSERT OR IGNORE INTO indicateurs_reponses (suivi_id) VALUES (?)", (suivi["id"],))
                cur = conn.execute("""
                    UPDATE indicateurs_reponses SET
                        responsable_ie=?, tel_resp_ie=?, courriel_ie=?,
                        foyers_trim=?, beneficiaires_trim=?, passages_trim=?,
                        foyers_annee=?, personnes_annee=?, passages_annee=?,
                        remarques=?, soumis_le=?, soumis_ip=?
                    WHERE suivi_id=? AND soumis_le IS NULL""",
                    (valeurs["responsable_ie"], valeurs["tel_resp_ie"], valeurs["courriel_ie"],
                     nombres.get("foyers_trim"), nombres.get("beneficiaires_trim"), nombres.get("passages_trim"),
                     nombres.get("foyers_annee"), nombres.get("personnes_annee"), nombres.get("passages_annee"),
                     valeurs["remarques"], now, request.headers.get("X-Real-IP") or request.remote_addr, suivi["id"]))
                conn.commit()
                if cur.rowcount == 0:  # validé entre-temps (double clic, 2 onglets)
                    return redirect(url_for("indicateurs.repondre", token=token))
                conn.execute("UPDATE indicateurs_suivi SET reponse_mode = 3 WHERE id = ?", (suivi["id"],))
                conn.commit()
                write_log(f"📝 Indicateurs {campagne['periode']} : réponse en ligne de {suivi['nom_association']}")
                _envoyer_confirmation(conn, suivi, campagne)
                return redirect(url_for("indicateurs.repondre", token=token, ok=1))

        if verrouille:
            mail_info = [_masquer(e) for e in emails_indicateurs_reponse(suivi, rep)]

    return render_template(
        "indicateurs/repondre.html", suivi=suivi, campagne=campagne, champs=champs, annuel=est_annuel(campagne),
        valeurs=valeurs, erreurs=erreurs, verrouille=verrouille, rep=rep, ok=request.args.get("ok") == "1",
        mail_info=mail_info, trimestre_lib=libelle_trimestre(campagne))


@indicateurs_bp.errorhandler(404)
def _lien_invalide(e):
    if request.endpoint == "indicateurs.repondre":
        return render_template("indicateurs/repondre_invalide.html"), 404
    return e


# -----------------------------------------
# 📧 Mail de confirmation (PDF rempli)
# -----------------------------------------
def emails_indicateurs_reponse(suivi, rep=None):
    adresses = list(emails_indicateurs(suivi))
    if rep is not None and rep["courriel_ie"] and "@" in rep["courriel_ie"]:
        adresses.append(rep["courriel_ie"])
    return list(dict.fromkeys(a.strip() for a in adresses if a and a.strip()))


def _envoyer_confirmation(conn, suivi, campagne):
    rep = conn.execute("SELECT * FROM indicateurs_reponses WHERE suivi_id = ?", (suivi["id"],)).fetchone()
    pdf = f"/tmp/indicateurs_reponse_{suivi['id']}.pdf"
    now = datetime.now().isoformat(timespec="seconds")
    try:
        remplir_pdf_reponse(pdf, suivi, campagne, rep)
        dest = emails_indicateurs_reponse(suivi, rep)
        valide_le = datetime.fromisoformat(rep["soumis_le"]).strftime("%d/%m/%Y à %H:%M")
        corps = (
            f"Bonjour,\n\n"
            f"Nous avons bien reçu les indicateurs état « {campagne['periode']} » de {suivi['nom_association']}, "
            f"validés en ligne le {valide_le}.\n\n"
            f"Vous trouverez ci-joint le formulaire rempli. Il est définitif : si vous constatez une erreur, "
            f"répondez simplement à ce message et nous rouvrirons le formulaire.\n\n"
            f"Merci et bien cordialement,\nLa Banque Alimentaire de l'Isère"
        )
        envoyer_mail(
            sujet=f"Indicateurs état {campagne['periode']} – {suivi['nom_association']} – formulaire reçu",
            destinataires=dest, texte=corps, sender_override=REPLY_TO_INDICATEURS,
            reply_to=REPLY_TO_INDICATEURS, attachment_path=pdf, bcc=[REPLY_TO_INDICATEURS],
            attachment_filename=f"Indicateurs_{campagne['periode'].replace(' ', '_')}_{suivi['nom_association']}.pdf"
            .replace("/", "-"))
        conn.execute("UPDATE indicateurs_reponses SET mail_confirmation_le=?, mail_confirmation_erreur=NULL WHERE suivi_id=?",
                     (now, suivi["id"]))
    except Exception as e:
        write_log(f"❌ Confirmation indicateurs {suivi['nom_association']} : {e}")
        conn.execute("UPDATE indicateurs_reponses SET mail_confirmation_erreur=? WHERE suivi_id=?", (str(e)[:300], suivi["id"]))
    finally:
        conn.commit()
        if os.path.exists(pdf):
            os.remove(pdf)


# -----------------------------------------
# 👤 Côté BAI : voir le PDF rempli, rouvrir, copier le lien
# -----------------------------------------
@indicateurs_bp.route("/indicateurs/<int:campagne_id>/saisie_ams")
@login_required
@require_access("indicateurs", "lecture")
def saisie_ams(campagne_id):
    """Aide à la saisie manuelle dans l'AMS : réponses reçues (mail, formulaire,
    courrier) avec leurs valeurs ; le filtre sur le Statut AMS (par défaut « non
    saisi ») fait disparaître ce qui est déjà saisi après chaque réimport AMS."""
    with get_db_connection() as conn:
        campagne = conn.execute("SELECT * FROM indicateurs_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        if not campagne:
            abort(404)
        lignes = conn.execute("""
            SELECT s.id AS suivi_id, s.statut_csv, s.present_csv, COALESCE(s.reponse_mode, 1) AS reponse_mode,
                   a.nom_association, a.code_VIF AS code_vif,
                   r.soumis_le, r.foyers_trim, r.beneficiaires_trim, r.passages_trim,
                   r.foyers_annee, r.personnes_annee, r.passages_annee, r.remarques
            FROM indicateurs_suivi s
            JOIN associations a ON a.id = s.association_id
            LEFT JOIN indicateurs_reponses r ON r.suivi_id = s.id AND r.soumis_le IS NOT NULL
            WHERE s.campagne_id = ? AND COALESCE(s.reponse_mode, 1) IN (2, 3, 4)
            ORDER BY a.nom_association COLLATE NOCASE""", (campagne_id,)).fetchall()
    lignes = [dict(l) for l in lignes]
    for l in lignes:
        if not l["present_csv"]:
            l["statut_cle"] = "absent"
        else:
            st = (l["statut_csv"] or "").strip().lower()
            l["statut_cle"] = {"validé": "valide", "a valider": "avalider", "a revoir": "revoir"}.get(st, "non")
    return render_template("indicateurs/saisie_ams.html", campagne=campagne, lignes=lignes,
                           champs=champs_pour(campagne), libelles=REPONSE_LIBELLES)


# Cartouche du formulaire -> colonnes de la fiche association
COORDONNEES = [
    ("responsable_ie", "responsable_IE", "Personne en charge"),
    ("tel_resp_ie", "tel_resp_IE", "Téléphone"),
    ("courriel_ie", "courriel_resp_IE1", "Courriel"),
]


def _norm_coord(v):
    return " ".join(str(v or "").split()).lower()


def _changements_coordonnees(rep, asso):
    """Champs du cartouche saisis par l'association qui diffèrent de la fiche.
    Le formulaire était prérempli avec la fiche (courriel : 1re adresse IE1, à défaut la boîte
    générale) : une valeur identique à ce préremplissage n'est pas un changement."""
    res = []
    for cle_rep, col, libelle in COORDONNEES:
        nouveau = (rep[cle_rep] or "").strip()
        ancien = (asso[col] or "").strip()
        if cle_rep == "courriel_ie":
            reference = {_norm_coord(a) for a in re.split(r"[;,]", ancien) if a.strip()}
            reference.add(_norm_coord((asso["courriel_association"] or "").split(";")[0]))
            reference.discard("")
            identique = _norm_coord(nouveau) in reference
        else:
            identique = _norm_coord(nouveau) == _norm_coord(ancien)
        if not identique and nouveau:
            res.append({"cle": cle_rep, "col": col, "libelle": libelle, "ancien": ancien, "nouveau": nouveau})
    return res


@indicateurs_bp.route("/indicateurs/reponse_maj_association/<int:suivi_id>", methods=["POST"])
@login_required
@require_access("indicateurs", "ecriture")
@require_access("associations", "ecriture")
def reponse_maj_association(suivi_id):
    """Reporte dans la fiche association les champs cochés du cartouche."""
    choisis = set(request.form.getlist("champ"))
    with get_db_connection() as conn:
        suivi = conn.execute("SELECT association_id FROM indicateurs_suivi WHERE id = ?", (suivi_id,)).fetchone()
        rep = conn.execute("SELECT * FROM indicateurs_reponses WHERE suivi_id = ? AND soumis_le IS NOT NULL",
                           (suivi_id,)).fetchone()
        if not suivi or not rep:
            abort(404)
        maj = []
        for cle_rep, col, libelle in COORDONNEES:   # liste blanche des colonnes
            if cle_rep in choisis and (rep[cle_rep] or "").strip():
                ancien = conn.execute(f"SELECT {col} FROM associations WHERE id = ?", (suivi["association_id"],)).fetchone()[0]
                conn.execute(f"UPDATE associations SET {col} = ? WHERE id = ?", (rep[cle_rep].strip(), suivi["association_id"]))
                maj.append(libelle)
                write_log(f"✏️ Fiche association {suivi['association_id']} : {col} « {ancien} » → « {rep[cle_rep].strip()} » "
                          f"(formulaire indicateurs, par {current_user.email})")
        conn.commit()
    flash(("✅ Fiche association mise à jour : " + ", ".join(maj)) if maj else "Aucun champ sélectionné.",
          "success" if maj else "warning")
    return redirect(url_for("indicateurs.reponse_page", suivi_id=suivi_id))


@indicateurs_bp.route("/indicateurs/reponse/<int:suivi_id>")
@login_required
@require_access("indicateurs", "lecture")
def reponse_page(suivi_id):
    """Réponse d'une association en page web (rapide, pour la saisie manuelle
    dans l'AMS) ; le PDF reste disponible en export."""
    with get_db_connection() as conn:
        suivi = conn.execute("""
            SELECT s.*, a.nom_association, a.code_VIF AS code_vif, a.CAR
            FROM indicateurs_suivi s JOIN associations a ON a.id = s.association_id WHERE s.id = ?""",
            (suivi_id,)).fetchone()
        if not suivi:
            abort(404)
        campagne = conn.execute("SELECT * FROM indicateurs_campagnes WHERE id = ?", (suivi["campagne_id"],)).fetchone()
        rep = conn.execute("SELECT * FROM indicateurs_reponses WHERE suivi_id = ?", (suivi_id,)).fetchone()
        asso = conn.execute("""SELECT responsable_IE, tel_resp_IE, courriel_resp_IE1, courriel_association
                               FROM associations WHERE id = ?""", (suivi["association_id"],)).fetchone()
    if not rep or not rep["soumis_le"]:
        abort(404)
    return render_template("indicateurs/reponse_voir.html", suivi=suivi, campagne=campagne, rep=rep,
                           champs=champs_pour(campagne), annuel=est_annuel(campagne),
                           changements=_changements_coordonnees(rep, asso))


@indicateurs_bp.route("/indicateurs/reponse_pdf/<int:suivi_id>")
@login_required
@require_access("indicateurs", "lecture")
def reponse_pdf(suivi_id):
    with get_db_connection() as conn:
        suivi = conn.execute("""
            SELECT s.*, a.nom_association, a.code_VIF AS code_vif, a.CAR
            FROM indicateurs_suivi s JOIN associations a ON a.id = s.association_id WHERE s.id = ?""",
            (suivi_id,)).fetchone()
        if not suivi:
            abort(404)
        campagne = conn.execute("SELECT * FROM indicateurs_campagnes WHERE id = ?", (suivi["campagne_id"],)).fetchone()
        rep = conn.execute("SELECT * FROM indicateurs_reponses WHERE suivi_id = ?", (suivi_id,)).fetchone()
    if not rep or not rep["soumis_le"]:
        abort(404)
    pdf = f"/tmp/indicateurs_reponse_voir_{suivi_id}.pdf"
    remplir_pdf_reponse(pdf, suivi, campagne, rep)
    return send_file(pdf, mimetype="application/pdf")


@indicateurs_bp.route("/indicateurs/reponse_rouvrir/<int:suivi_id>", methods=["POST"])
@login_required
@require_access("indicateurs", "ecriture")
def reponse_rouvrir(suivi_id):
    with get_db_connection() as conn:
        s = conn.execute("SELECT campagne_id FROM indicateurs_suivi WHERE id = ?", (suivi_id,)).fetchone()
        if not s:
            abort(404)
        conn.execute("""UPDATE indicateurs_reponses SET soumis_le=NULL, rouvert_le=?, rouvert_par=?
                        WHERE suivi_id=?""",
                     (datetime.now().isoformat(timespec="seconds"), current_user.email, suivi_id))
        conn.execute("UPDATE indicateurs_suivi SET reponse_mode = 1 WHERE id = ? AND reponse_mode = 3", (suivi_id,))
        conn.commit()
        write_log(f"🔓 Indicateurs : formulaire rouvert (suivi {suivi_id}) par {current_user.email}")
    flash("🔓 Formulaire rouvert : l'association peut le modifier puis le valider à nouveau avec son lien.", "info")
    return redirect(url_for("indicateurs.resultats", campagne_id=s["campagne_id"]))


@indicateurs_bp.route("/indicateurs/reponse_mode/<int:suivi_id>", methods=["POST"])
@login_required
@require_access("indicateurs", "ecriture")
def reponse_mode(suivi_id):
    """Saisie manuelle de la colonne « Répondu » : 1 Non reçu, 2 Réponse mail,
    4 Courrier. Le 3 (formulaire) n'est posé que par la validation en ligne ;
    une réponse formulaire se change via « Rouvrir »."""
    from flask import jsonify
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in (1, 2, 4):
        return jsonify(ok=False, erreur="valeur invalide"), 400
    with get_db_connection() as conn:
        s = conn.execute("SELECT reponse_mode FROM indicateurs_suivi WHERE id = ?", (suivi_id,)).fetchone()
        if not s:
            return jsonify(ok=False, erreur="introuvable"), 404
        if s["reponse_mode"] == 3:
            return jsonify(ok=False, erreur="réponse par formulaire : utilisez « Rouvrir »"), 409
        conn.execute("UPDATE indicateurs_suivi SET reponse_mode = ? WHERE id = ?", (mode, suivi_id))
        conn.commit()
    write_log(f"📝 Indicateurs : « Répondu » = {REPONSE_LIBELLES[mode]} (suivi {suivi_id}) par {current_user.email}")
    return jsonify(ok=True, mode=mode)
