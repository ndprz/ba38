# ba38_distribution/ramasse_jour.py
"""Ramasse journalière : produits ramassés (frais) entrés en stock.
Une ramasse = un jour + un fournisseur. À la validation :
  - entrée en stock Basilic (lot RJ + AAMMJJ + article, dépôt 03) ;
  - fichier de réception VIF (CSV, format recepba38) conservé avec la ramasse.
Le tonnage validé du jour est ensuite réparti sur les bons de préparation FRAIS."""

from datetime import date, datetime
from io import BytesIO

from flask import flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from ba38_utilitaires.core import get_db_connection, has_access, require_access

from . import distribution_bp
from .bons_prepa import _parse_date


def lot_ramasse_jour(d: date, article: str, libelle: str) -> str:
    """Un lot par jour et article ; vide pour un article « non loti »."""
    if "non loti" in (libelle or "").lower():
        return ""
    return f"RJ{d:%y%m%d}{article}"


def _nombre(v):
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def totaux_du_jour(conn, iso: str) -> list[dict]:
    """Tonnage validé du jour par article (à répartir sur les bons FRAIS)."""
    return [dict(r) for r in conn.execute("""
        SELECT l.article, MAX(l.libelle) AS libelle, SUM(l.quantite_kg) AS kg
        FROM ramasses_jour_lignes l JOIN ramasses_jour r ON r.id = l.ramasse_id
        WHERE r.date_ramasse = ? AND r.statut = 'validee'
        GROUP BY l.article ORDER BY l.article""", (iso,))]


@distribution_bp.route("/ramasse_jour")
@login_required
@require_access("distribution", "lecture")
def ramasse_jour():
    d = _parse_date(request.args.get("date")) or date.today()
    conn = get_db_connection()
    try:
        ramasses = [dict(r) for r in conn.execute("""
            SELECT r.*, COALESCE(f.nom, '?') AS fournisseur, f.code_vif AS code_vif_fournisseur
            FROM ramasses_jour r LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur
            WHERE r.date_ramasse = ? ORDER BY f.nom COLLATE NOCASE""", (d.isoformat(),))]
        for r in ramasses:
            r["lignes"] = [dict(x) for x in conn.execute(
                "SELECT * FROM ramasses_jour_lignes WHERE ramasse_id = ? ORDER BY ordre, id", (r["id"],))]
            r["total_kg"] = sum(x["quantite_kg"] or 0 for x in r["lignes"])
        deja = {r["id_fournisseur"] for r in ramasses}
        fournisseurs = [dict(r) for r in conn.execute(
            """SELECT id, nom, code_vif FROM fournisseurs
               WHERE COALESCE(LOWER(actif), 'oui') != 'non' ORDER BY nom COLLATE NOCASE""") if r["id"] not in deja]
        articles = [dict(r) for r in conn.execute("SELECT article, libelle FROM articles ORDER BY article")]
        totaux = totaux_du_jour(conn, d.isoformat())
        nb_bons_frais = conn.execute(
            "SELECT COUNT(*) FROM bons_preparation WHERE date_distribution = ? AND type_bon = 'frais'",
            (d.isoformat(),)).fetchone()[0]
    finally:
        conn.close()
    return render_template(
        "distribution/ramasse_jour.html", date_choisie=d, ramasses=ramasses, fournisseurs=fournisseurs,
        articles=articles, totaux=totaux, total_kg=sum(t["kg"] for t in totaux), nb_bons_frais=nb_bons_frais,
        aujourdhui=date.today().isoformat(), peut_ecrire=has_access("distribution", "ecriture"))


def _reponse(ok, **kw):
    return jsonify(ok=ok, **kw), (200 if ok else 400)


@distribution_bp.route("/ramasse_jour/action", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def ramasse_jour_action():
    """Actions JSON : creer, ajouter_ligne, maj_ligne, suppr_ligne, valider, rouvrir, supprimer."""
    from ba38_vif.generation import ErreursVif, generer_reception_ramasse_jour

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    par = getattr(current_user, "email", "") or ""
    conn = get_db_connection()
    try:
        if action == "creer":
            d = _parse_date(data.get("date"))
            fid = int(data.get("id_fournisseur") or 0)
            frs = conn.execute("SELECT nom, code_vif FROM fournisseurs WHERE id=?", (fid,)).fetchone()
            if not d or not frs:
                return _reponse(False, erreur="Date ou fournisseur invalide")
            if not (frs["code_vif"] or "").strip():
                return _reponse(False, erreur=f"Saisie interdite : {frs['nom']} n'a pas de code VIF : le saisir dans sa fiche fournisseur (cocher « Sans coordonnées » s'il n'y a pas d'e-mail, sinon la fiche ne s'enregistre pas).")
            conn.execute("INSERT OR IGNORE INTO ramasses_jour (date_ramasse, id_fournisseur, cree_par) VALUES (?,?,?)",
                         (d.isoformat(), fid, par))
            conn.commit()
            return _reponse(True)

        rid = data.get("ramasse_id")
        if action in ("maj_ligne", "suppr_ligne"):
            r = conn.execute("""SELECT r.id, r.statut FROM ramasses_jour r JOIN ramasses_jour_lignes l ON l.ramasse_id = r.id
                                WHERE l.id = ?""", (data.get("ligne_id"),)).fetchone()
            rid = r["id"] if r else None
        ram = conn.execute("SELECT * FROM ramasses_jour WHERE id = ?", (rid,)).fetchone() if rid else None
        if not ram:
            return _reponse(False, erreur="Ramasse introuvable")

        if action == "rouvrir":
            if ram["statut"] != "validee":
                return _reponse(False, erreur="Ramasse non validée")
            utilise = conn.execute("""
                SELECT 1 FROM bons_preparation_lots t
                JOIN bons_preparation_lignes bl ON bl.id = t.ligne_id
                JOIN bons_preparation b ON b.id = bl.bon_id
                WHERE b.statut = 'bl' AND t.lot IN (SELECT lot FROM ramasses_jour_lignes WHERE ramasse_id = ? AND lot != '')
                LIMIT 1""", (rid,)).fetchone()
            if utilise:
                return _reponse(False, erreur="Des bons de livraison utilisent déjà les lots de cette ramasse.")
            if ram["reception_depose_le"]:
                return _reponse(False, erreur="Le fichier de réception a déjà été déposé pour VIF.")
            conn.execute("""UPDATE ramasses_jour SET statut='saisie', valide_le=NULL, valide_par=NULL,
                            reception_fichier=NULL, reception_csv=NULL WHERE id=?""", (rid,))
            conn.commit()
            return _reponse(True)

        if ram["statut"] != "saisie":
            return _reponse(False, erreur="Ramasse validée : la rouvrir pour la modifier.")

        if action in ("ajouter_ligne", "maj_ligne"):   # pas de saisie sans code VIF du fournisseur
            frs0 = conn.execute("SELECT nom, code_vif FROM fournisseurs WHERE id=?", (ram["id_fournisseur"],)).fetchone()
            if not frs0 or not (frs0["code_vif"] or "").strip():
                return _reponse(False, erreur=f"Saisie interdite : {frs0['nom'] if frs0 else 'fournisseur inconnu'} n'a pas de code VIF : le saisir dans sa fiche fournisseur (cocher « Sans coordonnées » s'il n'y a pas d'e-mail, sinon la fiche ne s'enregistre pas).")

        if action == "supprimer":
            conn.execute("DELETE FROM ramasses_jour_lignes WHERE ramasse_id=?", (rid,))
            conn.execute("DELETE FROM ramasses_jour WHERE id=?", (rid,))
        elif action == "ajouter_ligne":
            code = str(data.get("article") or "").strip()
            art = conn.execute("SELECT article, libelle FROM articles WHERE article=?", (code,)).fetchone()
            qte = _nombre(data.get("quantite_kg"))
            if not art:
                return _reponse(False, erreur=f"Article inconnu : {code or '(vide)'}")
            if qte is None or qte <= 0:
                return _reponse(False, erreur="Poids invalide (kg > 0)")
            ordre = conn.execute("SELECT COALESCE(MAX(ordre),0)+1 FROM ramasses_jour_lignes WHERE ramasse_id=?",
                                 (rid,)).fetchone()[0]
            conn.execute("""INSERT INTO ramasses_jour_lignes (ramasse_id, ordre, article, libelle, quantite_kg, dlc)
                            VALUES (?,?,?,?,?,?)""",
                         (rid, ordre, art["article"], art["libelle"] or "", qte, (data.get("dlc") or "").strip()))
        elif action == "maj_ligne":
            lid = data.get("ligne_id")
            if "quantite_kg" in data:
                qte = _nombre(data["quantite_kg"])
                if qte is None or qte <= 0:
                    return _reponse(False, erreur="Poids invalide (kg > 0)")
                conn.execute("UPDATE ramasses_jour_lignes SET quantite_kg=? WHERE id=?", (qte, lid))
            if "dlc" in data:
                conn.execute("UPDATE ramasses_jour_lignes SET dlc=? WHERE id=?", ((data["dlc"] or "").strip(), lid))
            if "commentaire" in data:
                conn.execute("UPDATE ramasses_jour_lignes SET commentaire=? WHERE id=?",
                             ((data["commentaire"] or "").strip(), lid))
        elif action == "suppr_ligne":
            conn.execute("DELETE FROM ramasses_jour_lignes WHERE id=?", (data.get("ligne_id"),))
        elif action == "valider":
            lignes = conn.execute("SELECT * FROM ramasses_jour_lignes WHERE ramasse_id=? ORDER BY ordre, id", (rid,)).fetchall()
            frs = conn.execute("SELECT nom, code_vif FROM fournisseurs WHERE id=?", (ram["id_fournisseur"],)).fetchone()
            d = datetime.strptime(ram["date_ramasse"], "%Y-%m-%d").date()
            if not (frs["code_vif"] or "").strip():
                return _reponse(False, erreur=(
                    f"{frs['nom']} n'a pas de code VIF. Le saisir dans sa fiche fournisseur ; s'il n'y a pas d'e-mail, "
                    "cocher « Sans coordonnées », sinon la fiche ne s'enregistre pas et le code est perdu."))
            # lot déjà renseigné (ex. ramasse importée d'un fichier VIF) conservé ; sinon RJ + AAMMJJ + article
            a_stocker = [(l["id"], l["lot"] if l["lot"] is not None else lot_ramasse_jour(d, l["article"], l["libelle"]))
                         for l in lignes]
            csv_lignes = [{"article": l["article"], "quantite_kg": l["quantite_kg"], "lot": lot}
                          for l, (_, lot) in zip(lignes, a_stocker)]
            try:
                csv = generer_reception_ramasse_jour((frs["code_vif"] or "").strip(), d, csv_lignes)
            except ErreursVif as e:
                return _reponse(False, erreur=" ; ".join(e.messages))
            nom = f"recepba38_{d:%Y%m%d}_{frs['code_vif']}_{rid:06d}.csv"
            for lid, lot in a_stocker:
                conn.execute("UPDATE ramasses_jour_lignes SET lot=? WHERE id=?", (lot, lid))
            conn.execute("""UPDATE ramasses_jour SET statut='validee', valide_le=CURRENT_TIMESTAMP, valide_par=?,
                            reception_fichier=?, reception_csv=? WHERE id=?""", (par, nom, csv, rid))
        else:
            return _reponse(False, erreur="Action inconnue")
        conn.commit()
        return _reponse(True)
    finally:
        conn.close()


@distribution_bp.route("/ramasse_jour/<int:rid>/reception.csv")
@login_required
@require_access("distribution", "lecture")
def ramasse_jour_reception_csv(rid):
    conn = get_db_connection()
    try:
        r = conn.execute("SELECT reception_fichier, reception_csv, date_ramasse FROM ramasses_jour WHERE id=?",
                         (rid,)).fetchone()
    finally:
        conn.close()
    if not r or not r["reception_csv"]:
        flash("Aucun fichier de réception pour cette ramasse.", "warning")
        return redirect(url_for("distribution.ramasse_jour"))
    return send_file(BytesIO(r["reception_csv"].encode("utf-8")), mimetype="text/csv",
                     as_attachment=True, download_name=r["reception_fichier"])
