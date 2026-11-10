# ba38_distribution/fiche_logistique.py
"""Fiche logistique partenaire : PDF d'une association avec ses informations générales, ses responsables
(distribution, hygiène/HySA) et ses lignes de menu (type, jours, heure, emplacement, fréquence, bénéficiaires,
besoins particuliers, présence au planning), avec ou sans le détail des articles, exceptions comprises."""

import sqlite3
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from flask import flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from ba38_utilitaires.core import get_db_connection, require_access

from . import distribution_bp


def _t(v) -> str:
    return (str(v).strip() if v is not None else "")


def _adresse(*parties) -> str:
    return " — ".join(p for p in (_t(x) for x in parties) if p)


def charger_fiche(conn, association_id: int, detail: bool):
    a = conn.execute("SELECT * FROM associations WHERE Id = ?", (association_id,)).fetchone()
    if not a:
        return None
    a = dict(a)
    planif_asso = (_t(a.get("planification")) or "oui").lower() != "non"

    def pers(prefixe_nom, tel1, tel2, mail, statut):
        return {"nom": _t(a.get(prefixe_nom)), "statut": _t(a.get(statut)),
                "tels": [t for t in (_t(a.get(tel1)), _t(a.get(tel2))) if t], "mail": _t(a.get(mail))}

    general = [
        ("Association", _t(a.get("nom_association"))),
        ("Code VIF", _t(a.get("code_VIF"))),
        ("Raison sociale VIF", _t(a.get("raison_sociale_VIF"))),
        ("SIRET", _t(a.get("code_SIRET"))),
        ("Adresse", _adresse(a.get("adresse_association_1"), a.get("adresse_association_2"),
                             " ".join(x for x in (_t(a.get("CP")), _t(a.get("COMMUNE"))) if x))),
        ("Téléphone", _t(a.get("tel_association"))),
        ("Courriel", _t(a.get("courriel_association"))),
        ("Adresse de distribution", _adresse(a.get("adresse_distribution_1"), a.get("adresse_distribution_2"),
                                             " ".join(x for x in (_t(a.get("CP2")), _t(a.get("COMMUNE2"))) if x))),
        ("Téléphone distribution", _t(a.get("tel_distribution"))),
        ("Président / représentant", _adresse(a.get("nom_president_ou_officiel"), a.get("tel_president_officiel_1"),
                                              a.get("courriel_president"))),
        ("Bénéficiaires (VIF)", _t(a.get("nbre_beneficiaires_vif"))),
    ]
    general = [(k, v) for k, v in general if v]

    lignes = []
    for m in conn.execute("""
            SELECT m.id, m.menu, m.type_menu, m.jours, m.heure, m.emplacement, m.frequence, m.nbre_beneficiaires,
                   m.besoins_particuliers, m.actif, COALESCE(mn.libelle, '') AS libelle_menu
            FROM associations_menus m LEFT JOIN menus mn ON mn.code = m.menu
            WHERE m.association_id = ? ORDER BY CASE m.type_menu WHEN 'sec' THEN 0 ELSE 1 END, m.id""", (association_id,)):
        l = dict(m)
        l["planning"] = planif_asso and bool(l["actif"])
        exc = {e["article"]: dict(e) for e in conn.execute(
            "SELECT article, type, quantite, unite FROM associations_menus_exceptions WHERE ligne_id = ?", (l["id"],))}
        libelle = lambda art: (conn.execute("SELECT libelle FROM articles WHERE article = ?", (art,)).fetchone() or [""])[0] or ""
        l["exclus"] = [{"article": k, "libelle": libelle(k)} for k, e in exc.items() if e["type"] == "exclu"]
        l["ajouts"] = [{"article": k, "libelle": libelle(k), "quantite": e["quantite"], "unite": e["unite"] or ""}
                       for k, e in exc.items() if e["type"] == "ajout"]
        l["articles"] = []
        if detail:
            for r in conn.execute("""SELECT ma.article, a.libelle, a.portion, ma.quantite, ma.unite FROM menus_articles ma
                                     LEFT JOIN articles a ON a.article = ma.article
                                     WHERE ma.menu_code = ? ORDER BY ma.ordre""", ((l["menu"] or "").strip(),)):
                l["articles"].append({"article": r["article"], "libelle": r["libelle"] or "", "portion": r["portion"],
                                      "quantite": r["quantite"], "unite": r["unite"] or "",
                                      "exclu": exc.get(r["article"], {}).get("type") == "exclu", "ajout": False})
            for aj in l["ajouts"]:
                port = (conn.execute("SELECT portion FROM articles WHERE article = ?", (aj["article"],)).fetchone() or [None])[0]
                l["articles"].append({"article": aj["article"], "libelle": aj["libelle"], "portion": port,
                                      "quantite": aj["quantite"], "unite": aj["unite"], "exclu": False, "ajout": True})
        lignes.append(l)

    return {
        "nom": _t(a.get("nom_association")), "general": general,
        "resp_distribution": pers("responsable_distribution", "tel_resp_distribution_1", "tel_resp_distribution_2",
                                  "courriel_distribution", "statut_resp_distribution"),
        "resp_hysa": pers("responsable_HySA", "teL_resp_Hysa_1", "tel_resp_Hysa_2", "courriel_resp_Hysa", "statut_resp_hysa"),
        "planification": planif_asso, "lignes": lignes, "detail": detail,
        "genere_le": datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y %H:%M"),
    }


@distribution_bp.route("/planning_jour/menus/fiche_logistique/<int:association_id>")
@login_required
@require_access("distribution", "lecture")
def fiche_logistique_pdf(association_id):
    detail = request.args.get("detail") == "1"
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        fiche = charger_fiche(conn, association_id, detail)
    finally:
        conn.close()
    if not fiche:
        flash("Association introuvable.", "danger")
        return redirect(url_for("distribution.planning_jour_menus"))
    from weasyprint import HTML

    from ba38_utilitaires.organisation import get_organisation
    html = render_template("distribution/fiche_logistique_pdf.html", f=fiche, org=get_organisation())
    buf = BytesIO()
    HTML(string=html).write_pdf(buf)
    buf.seek(0)
    nom = "".join(c if c.isalnum() else "_" for c in fiche["nom"])[:40] or f"association_{association_id}"
    return send_file(buf, mimetype="application/pdf", download_name=f"fiche_logistique_{nom}.pdf")
