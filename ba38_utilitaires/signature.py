# ba38_signature.py
"""
Signature électronique générique via LibreSign : un utilisateur BA38 uploade
n'importe quel PDF (bon de commande, courrier, etc.) et le fait signer, soit
par lui-même, soit en l'envoyant par email à un tiers. Contrairement à
l'Annexe 1 bis (ba38_annexe1bis.py), le document n'a pas de mise en page
connue à l'avance : le signataire place lui-même son pavé de signature dans
l'interface LibreSign (cf. utils_libresign.envoyer_signature_request,
paramètre coordonnees=None).
"""

import os
import shutil

import requests
import sqlite3
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from ba38_utilitaires.core import get_db_connection, write_log, upload_database, require_access


signature_bp = Blueprint("signature", __name__)

STATUTS_SIGNATURE = ("envoyee", "signee", "refusee", "expiree")


LONGUEUR_MAX_TEXTE_PAVE = 60


def _dossier(signature_id):
    base_dir = os.getenv("BA38_BASE_DIR", ".")
    dossier = os.path.join(base_dir, "uploads", "signature_electronique", str(signature_id))
    os.makedirs(dossier, exist_ok=True)
    return dossier


def charger_mentions(conn):
    """Mentions proposées au-dessus du pavé ("Bon pour accord"...), paramétrables
    dans la table parametres (param_name = signature_mention)."""
    return [r[0] for r in conn.execute(
        "SELECT param_value FROM parametres WHERE param_name = 'signature_mention' "
        "AND COALESCE(param_value, '') != '' ORDER BY id"
    ).fetchall()]


def _figer_champs(writer, page):
    """
    Fige dans le contenu de la page tous les champs de formulaire (sauf
    signature) : leur apparence actuelle, valeurs saisies comprises, est
    dessinée dans la page puis le widget est retiré. Deux raisons (2026-10-21) :
    - sans ça, un formulaire signé reste modifiable dans Acrobat (la
      modification invalide la signature, mais rien ne l'empêche) ;
    - les champs s'affichent PAR-DESSUS le contenu et masquaient la mention /
      fonction (zone jaune "Observations") ; une annotation /Stamp à la place
      ne marche pas : Acrobat la range dans les commentaires sans l'afficher.
    Le signataire ne peut de toute façon pas remplir de champ dans LibreSign.
    Retourne les opérations PDF à ajouter au contenu de la page.
    """
    from pypdf.generic import DictionaryObject, NameObject

    annots = page.get("/Annots")
    if not annots:
        return []
    annots = annots.get_object()
    acroform = writer._root_object.get("/AcroForm")
    champs = acroform.get_object().get("/Fields") if acroform is not None else None
    operations = []
    for ref in list(annots):
        annot = ref.get_object()
        if annot.get("/Subtype") != "/Widget":
            continue
        parent = annot.get("/Parent")
        type_champ = annot.get("/FT") or (parent.get_object().get("/FT") if parent else None)
        if type_champ == "/Sig":
            continue
        x0, y0, x1, y1 = (float(v) for v in annot["/Rect"])
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))

        # Référence brute (indirecte en pratique) : réutilisée comme XObject.
        ref_apparence = annot.get("/AP", {}).get("/N")
        apparence = ref_apparence.get_object() if ref_apparence is not None else None
        if apparence is not None and "/BBox" not in apparence and annot.get("/AS") in apparence:
            ref_apparence = apparence.raw_get(annot["/AS"])   # cases à cocher : état courant
            apparence = ref_apparence.get_object()
        # Champ masqué (/F bit 2) : retiré sans être dessiné.
        if apparence is not None and "/BBox" in apparence and not int(annot.get("/F", 0)) & 2:
            bx0, by0, bx1, by1 = (float(v) for v in apparence["/BBox"])
            sx = (x1 - x0) / ((bx1 - bx0) or 1)
            sy = (y1 - y0) / ((by1 - by0) or 1)
            ressources = page.setdefault(NameObject("/Resources"), DictionaryObject()).get_object()
            xobjects = ressources.setdefault(NameObject("/XObject"), DictionaryObject()).get_object()
            nom = f"/ChampFige{len(xobjects)}"
            xobjects[NameObject(nom)] = ref_apparence
            operations.append(f"q {sx:.4f} 0 0 {sy:.4f} {x0 - bx0 * sx:.4f} {y0 - by0 * sy:.4f} cm {nom} Do Q")

        annots.remove(ref)
        if champs is not None and ref in champs.get_object():
            champs.get_object().remove(ref)
        if parent is not None:
            kids = parent.get_object().get("/Kids")
            if kids is not None and ref in kids:
                kids.remove(ref)
            if kids is not None and len(kids) == 0 and champs is not None and parent in champs.get_object():
                champs.get_object().remove(parent)
    return operations


def preparer_pdf_pour_signature(pdf_bytes, coordonnees, lignes):
    """
    Avant l'envoi à LibreSign : fige tous les champs de formulaire du PDF
    (cf. _figer_champs) et inscrit `lignes` (mention, fonction ; peut être
    vide) juste au-dessus du pavé de signature, dans le contenu de la page.
    Le modèle de texte du pavé LibreSign n'a pas de variable libre par
    signataire, et passer ces infos dans le displayName les ferait apparaître
    dans les emails LibreSign ("Bonjour Jean Dupont | Trésorier...") et le
    pied de page : on les écrit donc nous-mêmes.
    `coordonnees` : mêmes conventions que LibreSign (points, depuis le HAUT).
    Retourne les octets inchangés s'il n'y a ni champ ni texte.
    """
    import io
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

    TAILLE, INTERLIGNE, ECART = 10, 14, 4

    def _echapper(texte):
        # Polices standard en WinAnsiEncoding : accents français OK en cp1252.
        brut = texte.encode("cp1252", errors="replace").decode("latin-1")
        return brut.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if not lignes and "/AcroForm" not in reader.trailer["/Root"]:
        return pdf_bytes
    # PDF déjà signé (signature existante) : le réécrire invaliderait cette
    # signature ; on l'envoie tel quel, sans figer ni mention.
    if any((champ.get("/FT") == "/Sig" and champ.get("/V") is not None)
           for champ in (reader.get_fields() or {}).values()):
        write_log("⚠️ PDF déjà signé : envoyé sans figer les champs ni ajouter de mention")
        return pdf_bytes
    writer = PdfWriter()
    writer.append(reader)

    operations_par_page = {i: _figer_champs(writer, page) for i, page in enumerate(writer.pages)}

    if lignes:
        index_page = coordonnees["page"] - 1
        page = writer.pages[index_page]
        box = page.mediabox
        x = float(box.left) + coordonnees["left"]
        y_bas = float(box.top) - coordonnees["top"] + ECART   # ligne de base collée au pavé
        ressources = page.setdefault(NameObject("/Resources"), DictionaryObject()).get_object()
        polices = ressources.setdefault(NameObject("/Font"), DictionaryObject()).get_object()
        for nom, base in (("/MentionR", "/Helvetica"), ("/MentionB", "/Helvetica-Bold")):
            polices[NameObject(nom)] = writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject(base),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }))
        # Dernière ligne collée au pavé, les précédentes au-dessus ; 1re en gras s'il y en a 2.
        for i, ligne in enumerate(reversed(lignes)):
            police = "/MentionB" if (i == len(lignes) - 1 and len(lignes) > 1) else "/MentionR"
            operations_par_page[index_page].append(
                f"BT {police} {TAILLE} Tf 0 g {x:.2f} {y_bas + INTERLIGNE * i:.2f} Td ({_echapper(ligne)}) Tj ET")

    for i, operations in operations_par_page.items():
        if not operations:
            continue
        page = writer.pages[i]
        # Contenu existant encadré par q/Q : il peut laisser la matrice modifiée.
        debut, fin = DecodedStreamObject(), DecodedStreamObject()
        debut.set_data(b"q\n")
        fin.set_data(("Q\n" + "\n".join(operations) + "\n").encode("latin-1"))
        contenus = page.get("/Contents")
        if contenus is None:
            existants = []
        elif isinstance(contenus.get_object(), ArrayObject):
            existants = list(contenus.get_object())
        else:
            existants = [contenus]
        page[NameObject("/Contents")] = ArrayObject([writer._add_object(debut), *existants, writer._add_object(fin)])

    # Plus aucun champ : retirer le formulaire (LibreSign recrée le sien pour la signature).
    acroform = writer._root_object.get("/AcroForm")
    if acroform is not None and not acroform.get_object().get("/Fields"):
        del writer._root_object["/AcroForm"]

    sortie = io.BytesIO()
    writer.write(sortie)
    return sortie.getvalue()


# ========================================
# 📋 Liste + formulaire d'envoi
# ========================================
@signature_bp.route("/signature", methods=["GET"])
@login_required
@require_access("signature_electronique", "ecriture")
def index():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    demandes = conn.execute(
        "SELECT * FROM signatures_electroniques WHERE user_creation = ? ORDER BY id DESC",
        (current_user.email,)
    ).fetchall()
    mentions = charger_mentions(conn)
    # Pré-remplissage : dernière mention utilisée, et dernière fonction saisie
    # quand l'utilisateur signait lui-même (pas de titre type "Trésorier" dans
    # la fiche bénévole, seulement des cases de fonctions opérationnelles).
    derniere = conn.execute(
        "SELECT mention FROM signatures_electroniques WHERE user_creation = ? "
        "ORDER BY id DESC LIMIT 1", (current_user.email,)
    ).fetchone()
    derniere_fonction = conn.execute(
        "SELECT fonction_signataire FROM signatures_electroniques WHERE user_creation = ? "
        "AND type_signataire = 'moi_meme' AND COALESCE(fonction_signataire, '') != '' "
        "ORDER BY id DESC LIMIT 1", (current_user.email,)
    ).fetchone()
    conn.close()
    return render_template(
        "signature/index.html", demandes=demandes, mentions=mentions,
        derniere_mention=(derniere["mention"] or "") if derniere else None,
        derniere_fonction=derniere_fonction["fonction_signataire"] if derniere_fonction else "",
    )


# ========================================
# 📤 Envoyer un document pour signature via LibreSign
# ========================================
@signature_bp.route("/signature/envoyer", methods=["POST"])
@login_required
@require_access("signature_electronique", "ecriture")
def envoyer():
    from ba38_utilitaires.libresign import envoyer_signature_request, LibreSignError

    fichier = request.files.get("fichier")
    if not fichier or not fichier.filename:
        flash("❌ Aucun fichier sélectionné.", "danger")
        return redirect(url_for("signature.index"))

    if not fichier.filename.lower().endswith(".pdf"):
        flash("❌ Seuls les fichiers PDF sont acceptés.", "danger")
        return redirect(url_for("signature.index"))

    nom_document = (request.form.get("nom_document") or "").strip() or fichier.filename
    type_signataire = request.form.get("type_signataire")
    if type_signataire not in ("moi_meme", "tiers"):
        flash("❌ Choix du signataire invalide.", "danger")
        return redirect(url_for("signature.index"))

    if type_signataire == "moi_meme":
        destinataire_nom = current_user.username
        destinataire_email = current_user.email
    else:
        destinataire_nom = (request.form.get("destinataire_nom") or "").strip()
        destinataire_email = (request.form.get("destinataire_email") or "").strip()
        if not destinataire_nom or not destinataire_email:
            flash("❌ Nom et email du destinataire requis.", "danger")
            return redirect(url_for("signature.index"))

    try:
        coordonnees = {
            "page": int(request.form["sig_page"]),
            "top": round(float(request.form["sig_top"])),
            "left": round(float(request.form["sig_left"])),
            "width": round(float(request.form["sig_width"])),
            "height": round(float(request.form["sig_height"])),
        }
    except (KeyError, ValueError):
        flash("❌ Merci de positionner la signature sur le document avant l'envoi.", "danger")
        return redirect(url_for("signature.index"))
    # Curseur "Taille du pavé" : 50 à 100 % de 350x100 (en dessous, LibreSign
    # réduit la signature et son texte au point d'être illisibles).
    if not (170 <= coordonnees["width"] <= 350 and 48 <= coordonnees["height"] <= 100):
        flash("❌ Taille du pavé de signature invalide.", "danger")
        return redirect(url_for("signature.index"))

    mention = (request.form.get("mention") or "").strip()
    if mention == "__autre__":
        mention = (request.form.get("mention_autre") or "").strip()
    fonction_signataire = (request.form.get("fonction_signataire") or "").strip()
    if len(mention) > LONGUEUR_MAX_TEXTE_PAVE or len(fonction_signataire) > LONGUEUR_MAX_TEXTE_PAVE:
        flash(f"❌ Mention et fonction : {LONGUEUR_MAX_TEXTE_PAVE} caractères maximum.", "danger")
        return redirect(url_for("signature.index"))

    conn = get_db_connection()
    cur = conn.execute(
        """
        INSERT INTO signatures_electroniques (
            user_creation, date_creation, nom_document, type_signataire,
            destinataire_nom, destinataire_email, statut, mention, fonction_signataire
        ) VALUES (?, ?, ?, ?, ?, ?, 'envoyee', ?, ?)
        """,
        (current_user.email, date.today().strftime("%Y-%m-%d"), nom_document,
         type_signataire, destinataire_nom, destinataire_email, mention, fonction_signataire)
    )
    signature_id = cur.lastrowid
    conn.commit()

    chemin_original = os.path.join(_dossier(signature_id), secure_filename(fichier.filename))
    fichier.save(chemin_original)
    with open(chemin_original, "rb") as f:
        pdf_bytes = f.read()

    # Certains PDF (scanners, pilotes d'impression) ont des octets parasites
    # avant l'en-tête %PDF : tolérés par la plupart des lecteurs, mais rejetés
    # par le validateur strict de LibreSign ("Fichier Base64 invalide").
    debut_pdf = pdf_bytes.find(b"%PDF")
    if debut_pdf > 0:
        pdf_bytes = pdf_bytes[debut_pdf:]
        with open(chemin_original, "wb") as f:
            f.write(pdf_bytes)

    # L'original reste tel qu'uploadé sur disque ; seul le PDF envoyé à
    # LibreSign (donc le PDF signé) est figé et porte la mention / fonction.
    lignes = [t for t in (mention, fonction_signataire) if t]
    try:
        pdf_bytes = preparer_pdf_pour_signature(pdf_bytes, coordonnees, lignes)
    except Exception as e:
        write_log(f"❌ Préparation PDF (signature {signature_id}) : {e}")
        conn.execute("UPDATE signatures_electroniques SET statut = 'erreur' WHERE id = ?", (signature_id,))
        conn.commit()
        conn.close()
        flash(f"❌ Impossible de préparer ce PDF pour la signature : {e}", "danger")
        return redirect(url_for("signature.index"))

    prenom, *reste = destinataire_nom.split(" ", 1)
    nom = reste[0] if reste else ""

    try:
        resultat = envoyer_signature_request(
            pdf_bytes=pdf_bytes,
            nom_document=nom_document,
            signataire_prenom=prenom,
            signataire_nom=nom,
            signataire_email=destinataire_email,
            coordonnees=coordonnees,
        )
    except LibreSignError as e:
        write_log(f"❌ LibreSign envoyer (signature {signature_id}) : {e}")
        conn.execute("UPDATE signatures_electroniques SET statut = 'erreur' WHERE id = ?", (signature_id,))
        conn.commit()
        conn.close()
        flash(f"❌ Erreur lors de l'envoi à LibreSign : {e}", "danger")
        return redirect(url_for("signature.index"))

    conn.execute(
        """
        UPDATE signatures_electroniques
        SET libresign_uuid = ?, libresign_file_id = ?, libresign_sign_request_id = ?,
            document_original_path = ?
        WHERE id = ?
        """,
        (resultat["uuid"], resultat["file_id"], resultat["sign_request_id"], chemin_original, signature_id)
    )
    conn.commit()
    conn.close()

    upload_database()
    flash(f"📤 Document envoyé pour signature à {destinataire_email}.", "success")
    return redirect(url_for("signature.index"))


# ========================================
# 🔄 Vérifier le statut auprès de LibreSign (pas de webhook configuré)
# ========================================
@signature_bp.route("/signature/verifier_statut/<int:signature_id>", methods=["POST"])
@login_required
@require_access("signature_electronique", "ecriture")
def verifier_statut(signature_id):
    from ba38_utilitaires.libresign import recuperer_statut, telecharger_document_signe, LibreSignError

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    demande = conn.execute(
        "SELECT * FROM signatures_electroniques WHERE id = ? AND user_creation = ?",
        (signature_id, current_user.email)
    ).fetchone()

    if not demande or not demande["libresign_file_id"]:
        conn.close()
        flash("ℹ️ Aucune demande de signature LibreSign associée.", "info")
        return redirect(url_for("signature.index"))

    try:
        statut_data = recuperer_statut(demande["libresign_file_id"])
    except LibreSignError as e:
        conn.close()
        write_log(f"❌ LibreSign verifier_statut (signature {signature_id}) : {e}")
        flash(f"❌ Erreur lors de la vérification auprès de LibreSign : {e}", "danger")
        return redirect(url_for("signature.index"))

    # Même mapping que ba38_annexe1bis.verifier_statut (codes confirmés le 2026-07-16).
    mapping = {3: "signee"}
    nouveau_statut = mapping.get(statut_data.get("status"))

    if not nouveau_statut:
        conn.close()
        flash(f"ℹ️ Statut LibreSign actuel : « {statut_data.get('statusText')} » (pas encore de changement).", "info")
        return redirect(url_for("signature.index"))

    document_signe_path = None
    if nouveau_statut == "signee":
        try:
            pdf_signe = telecharger_document_signe(demande["libresign_uuid"])
            chemin = os.path.join(_dossier(signature_id), f"signe_{int(datetime.now().timestamp())}.pdf")
            with open(chemin, "wb") as f:
                f.write(pdf_signe)
            document_signe_path = chemin
        except LibreSignError as e:
            write_log(f"⚠️ Téléchargement document signé échoué (signature {signature_id}) : {e}")

    conn.execute(
        """
        UPDATE signatures_electroniques
        SET statut = ?,
            date_signature = CASE WHEN ? = 'signee' THEN ? ELSE date_signature END,
            document_signe_path = COALESCE(?, document_signe_path)
        WHERE id = ?
        """,
        (nouveau_statut, nouveau_statut, date.today().strftime("%Y-%m-%d"), document_signe_path, signature_id)
    )
    conn.commit()
    conn.close()

    upload_database()
    flash(f"✅ Statut mis à jour : {nouveau_statut}.", "success")
    return redirect(url_for("signature.index"))


# ========================================
# 📥 Téléchargement (original ou signé)
# ========================================
@signature_bp.route("/signature/telecharger/<int:signature_id>/<variante>", methods=["GET"])
@login_required
@require_access("signature_electronique", "ecriture")
def telecharger(signature_id, variante):
    if variante not in ("original", "signe"):
        flash("❌ Variante de téléchargement invalide.", "danger")
        return redirect(url_for("signature.index"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    demande = conn.execute(
        "SELECT * FROM signatures_electroniques WHERE id = ? AND user_creation = ?",
        (signature_id, current_user.email)
    ).fetchone()
    conn.close()

    if not demande:
        flash("❌ Demande de signature introuvable.", "danger")
        return redirect(url_for("signature.index"))

    colonne = "document_signe_path" if variante == "signe" else "document_original_path"
    chemin = demande[colonne]
    if not chemin or not os.path.exists(chemin):
        flash("❌ Fichier introuvable.", "danger")
        return redirect(url_for("signature.index"))

    suffixe = "_signe" if variante == "signe" else ""
    nom_fichier = f"{demande['nom_document']}{suffixe}.pdf"
    return send_file(chemin, as_attachment=True, download_name=nom_fichier, mimetype="application/pdf")


# ========================================
# 🗑️ Supprimer une demande (base + fichiers)
# ========================================
@signature_bp.route("/signature/supprimer/<int:signature_id>", methods=["POST"])
@login_required
@require_access("signature_electronique", "ecriture")
def supprimer(signature_id):
    nb, erreurs_libresign = _supprimer_demandes([signature_id])
    if not nb:
        flash("❌ Demande de signature introuvable.", "danger")
        return redirect(url_for("signature.index"))
    flash("🗑️ Demande de signature supprimée.", "success")
    _avertir_erreurs_libresign(erreurs_libresign)
    return redirect(url_for("signature.index"))


# ========================================
# 🗑️ Supprimer plusieurs demandes (cases à cocher de la liste)
# ========================================
@signature_bp.route("/signature/supprimer_selection", methods=["POST"])
@login_required
@require_access("signature_electronique", "ecriture")
def supprimer_selection():
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    if not ids:
        flash("ℹ️ Aucune demande sélectionnée.", "info")
        return redirect(url_for("signature.index"))
    nb, erreurs_libresign = _supprimer_demandes(ids)
    flash(f"🗑️ {nb} demande(s) de signature supprimée(s).", "success")
    _avertir_erreurs_libresign(erreurs_libresign)
    return redirect(url_for("signature.index"))


def _avertir_erreurs_libresign(erreurs):
    if erreurs:
        flash("⚠️ Supprimé dans Basilic, mais pas dans LibreSign pour : "
              + ", ".join(erreurs) + " (voir les logs).", "warning")


def _supprimer_demandes(ids):
    """Supprime les demandes de `ids` appartenant à l'utilisateur courant
    (ignore les autres) : côté LibreSign (demande + PDF original et signé
    stockés dans Nextcloud), puis en base et le dossier local (original +
    PDF signé rapatrié). Une erreur LibreSign n'empêche pas la suppression
    locale ; elle est journalisée et remontée.
    Retourne (nombre supprimé, [noms des documents en erreur LibreSign])."""
    from ba38_utilitaires.libresign import supprimer_document, LibreSignError

    conn = get_db_connection()
    marques = ",".join("?" * len(ids))
    lignes = conn.execute(
        f"SELECT id, nom_document, libresign_file_id FROM signatures_electroniques "
        f"WHERE id IN ({marques}) AND user_creation = ?",
        (*ids, current_user.email)
    ).fetchall()
    a_supprimer = [r["id"] for r in lignes]

    erreurs_libresign = []
    for r in lignes:
        if not r["libresign_file_id"]:
            continue
        try:
            supprimer_document(r["libresign_file_id"])
        except (LibreSignError, requests.RequestException) as e:
            write_log(f"❌ LibreSign suppression (signature {r['id']}) : {e}")
            erreurs_libresign.append(r["nom_document"])

    if a_supprimer:
        conn.execute(
            f"DELETE FROM signatures_electroniques WHERE id IN ({','.join('?' * len(a_supprimer))})",
            a_supprimer
        )
        conn.commit()
    conn.close()

    for signature_id in a_supprimer:
        shutil.rmtree(_dossier(signature_id), ignore_errors=True)
    if a_supprimer:
        upload_database()
    return len(a_supprimer), erreurs_libresign
