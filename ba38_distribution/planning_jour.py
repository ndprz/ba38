# ba38_distribution/planning_jour.py
"""Planning du jour de distribution : associations attendues un jour donné,
avec leurs menus (champs du groupe « Planification » de la fiche association)."""

import re
import sqlite3
from datetime import date, datetime, timedelta

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from ba38_export.etat_passages import jours_feries
from ba38_partenaires.passage import _sans_accents, heure_affichage, passe_ce_jour, regle_frequence
from ba38_utilitaires.core import get_db_connection, has_access, require_access

from . import distribution_bp

JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


def _rang_heure(heure: str):
    """Matin, puis heures chronologiques, puis après-midi, puis le reste."""
    h = (heure or "").lower()
    if "matin" in h:
        return (0, 0, h)
    m = re.match(r"(\d{1,2})h(\d{2})", h)
    if m:
        return (1, int(m.group(1)) * 60 + int(m.group(2)), h)
    if "midi" in h:
        return (2, 0, h)
    return (3, 0, h)


def charger_fermetures(conn=None) -> dict:
    """{'AAAA-MM-JJ': motif} des jours de fermeture de la BA38."""
    ferme = conn is None
    conn = conn or get_db_connection()
    rows = conn.execute("SELECT date, COALESCE(motif, '') FROM ba_fermetures").fetchall()
    if ferme:
        conn.close()
    return {r[0]: r[1] for r in rows}


def est_ouvre(d: date, fermetures: dict) -> bool:
    return d.weekday() < 5 and d.isoformat() not in fermetures


def jour_ouvre_voisin(d: date, fermetures: dict, sens: str) -> date:
    """Prochain (sens='suivant') ou précédent jour ouvré (lundi-vendredi non fermé)."""
    pas = timedelta(days=1 if sens != "precedent" else -1)
    x = d + pas
    for _ in range(60):  # garde-fou
        if est_ouvre(x, fermetures):
            return x
        x += pas
    return x


def _ligne(r, regle, d_origine, **extra):
    heure = heure_affichage(r["heure"])
    return {
        "id": r["id"],                       # id de la ligne de menu (associations_menus)
        "association_id": r["association_id"],
        "nom": (r["nom_association"] or "").strip(),
        "code_vif": r["code_VIF"] or "",
        "heure": heure,
        "emplacement": re.sub(r"\s+", "", r["emplacement"] or "").upper() or "",
        "menu_sec": (r["menu"] or "").strip() if r["type_menu"] == "sec" else "",
        "menu_frais": (r["menu"] or "").strip() if r["type_menu"] == "frais" else "",
        "ids": [r["id"]],
        "frequence": (r["frequence"] or "").strip(),
        "frequence_inconnue": regle[0] == "inconnue",
        "beneficiaires": r["nbre_beneficiaires"],
        "remarque": (r["besoins_particuliers"] or "").strip(),
        "origine": d_origine.isoformat(),
        "statut": "normal", "sens": "", "destination": "", "deplacement": "",
        **extra,
    }


def _fusionner_menus(lignes: list[dict]) -> list[dict]:
    """Une association qui passe le même jour, à la même heure et au même
    emplacement avec plusieurs menus (ex. sec + frais) = UN seul passage :
    les menus sont regroupés sur une ligne, les bénéficiaires ne sont pas additionnés."""
    groupes, ordre = {}, []
    for a in lignes:
        cle = (a["association_id"], a["heure"], a["emplacement"], a["origine"], a["statut"], a["destination"])
        g = groupes.get(cle)
        if g is None:
            groupes[cle] = a
            ordre.append(cle)
            continue
        g["ids"] += a["ids"]
        for champ in ("menu_sec", "menu_frais"):
            if a[champ] and a[champ] not in g[champ].split(", "):
                g[champ] = f"{g[champ]}, {a[champ]}" if g[champ] else a[champ]
        for champ in ("frequence", "remarque"):
            if a[champ] and a[champ] not in g[champ].split(" / "):
                g[champ] = f"{g[champ]} / {a[champ]}" if g[champ] else a[champ]
        g["frequence_inconnue"] = g["frequence_inconnue"] or a["frequence_inconnue"]
        b1, b2 = g["beneficiaires"], a["beneficiaires"]
        g["beneficiaires"] = max([x for x in (b1, b2) if x is not None], default=None)
    return [groupes[c] for c in ordre]


def associations_du_jour(d: date) -> list[dict]:
    """Associations attendues le jour `d`.

    - jour de passage de la fiche + fréquence (appliquée au jour d'ORIGINE) ;
      fréquence non reconnue = non exclue ; validité « non » exclue ;
    - fermetures de la BA38 : une association dont le jour tombe un jour fermé
      est déplacée au jour ouvré suivant (défaut) ou précédent (choix enregistré
      dans planning_deplacements). Elle apparaît alors :
        · statut 'recue'    : sur le jour de destination ;
        · statut 'deplacee' : sur le jour fermé lui-même (pour pouvoir changer le sens)."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.id, m.association_id, a.nom_association, a.code_VIF,
               m.heure, m.emplacement, m.menu, m.type_menu, m.frequence,
               m.nbre_beneficiaires, m.besoins_particuliers, m.jours
        FROM associations_menus m
        JOIN associations a ON a.id = m.association_id
        WHERE m.actif = 1
          AND COALESCE(TRIM(m.jours), '') != ''
          AND TRIM(COALESCE(a.nom_association, '')) != ''
          AND COALESCE(LOWER(TRIM(a.planification)), 'oui') != 'non'
          AND (a.validite IS NULL OR LOWER(TRIM(a.validite)) != 'non')
    """).fetchall()
    assoc_ferm = {}
    for r in conn.execute("SELECT association_id, date_debut, date_fin, COALESCE(motif, '') FROM associations_fermetures"):
        assoc_ferm.setdefault(r[0], []).append((r[1], r[2], r[3]))
    fermetures = charger_fermetures(conn)
    deplacements = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT ligne_id, date_origine, sens FROM planning_deplacements")}
    conn.close()

    def passent(d0):
        jour = JOURS_FR[d0.weekday()].lower()
        for r in rows:
            if jour in (r["jours"] or "").lower():
                regle = regle_frequence(r["frequence"])
                if passe_ce_jour(regle, d0):
                    yield r, regle

    res = []
    if d.weekday() >= 5:
        return res

    if est_ouvre(d, fermetures):
        for r, regle in passent(d):
            res.append(_ligne(r, regle, d))
        # arrivées depuis un jour fermé voisin
        for delta in range(-14, 15):
            d0 = d + timedelta(days=delta)
            if d0 == d or d0.weekday() >= 5 or d0.isoformat() not in fermetures:
                continue
            for r, regle in passent(d0):
                sens = deplacements.get((r["id"], d0.isoformat()), "suivant")
                if jour_ouvre_voisin(d0, fermetures, sens) == d:
                    deja = next((x for x in res if x["id"] == r["id"]), None)
                    if deja:  # passe déjà ce jour-là : un seul passage, signalé
                        deja["deplacement"] = f"Passage du {d0.strftime('%d/%m')} regroupé"
                        deja["statut"] = "recue"
                        continue
                    res.append(_ligne(
                        r, regle, d0, statut="recue", sens=sens,
                        deplacement=f"Déplacée du {d0.strftime('%d/%m')} ({fermetures[d0.isoformat()] or 'fermeture'})"))
    else:
        for r, regle in passent(d):
            sens = deplacements.get((r["id"], d.isoformat()), "suivant")
            dest = jour_ouvre_voisin(d, fermetures, sens)
            res.append(_ligne(
                r, regle, d, statut="deplacee", sens=sens,
                destination=dest.isoformat(),
                deplacement=f"→ {JOURS_FR[dest.weekday()][:3]} {dest.strftime('%d/%m')}"))

    # associations fermées au jour effectif : listées pour information, sans menu
    def motif_ferme(aid, jour_iso):
        for debut, fin, motif in assoc_ferm.get(aid, []):
            if debut <= jour_iso and (not fin or jour_iso <= fin):
                return motif or "fermée"
        return None

    final, deja_fermees = [], set()
    for a in res:
        jour_eff = a["destination"] if a["statut"] == "deplacee" else d.isoformat()
        motif = motif_ferme(a["association_id"], jour_eff)
        if motif is None:
            final.append(a)
        elif a["association_id"] not in deja_fermees:   # une seule ligne par association
            deja_fermees.add(a["association_id"])
            a.update(statut="fermee", menu_sec="", menu_frais="", beneficiaires=None,
                     heure="", emplacement="", remarque="", frequence_inconnue=False,
                     deplacement=f"Fermée ({motif})")
            final.append(a)
    final = _fusionner_menus(final)
    final.sort(key=lambda a: (_sans_accents(a["nom"]), _rang_heure(a["heure"])))
    return final


@distribution_bp.route("/planning_jour/generation")
@login_required
@require_access("distribution", "lecture")
def planning_jour_generation():
    brut = request.args.get("date", "").strip()
    d = None
    erreur = None
    if brut:
        try:
            d = datetime.strptime(brut, "%Y-%m-%d").date()
        except ValueError:
            erreur = "Date invalide."

    associations = associations_du_jour(d) if d else []

    def _voisin(pas):  # jour précédent / suivant en sautant samedi et dimanche
        x = (d or date.today()) + timedelta(days=pas)
        while x.weekday() >= 5:
            x += timedelta(days=pas)
        return x.isoformat()

    def _entier(v):
        try:
            return int(float(str(v).replace(",", ".").strip()))
        except (TypeError, ValueError):
            return 0
    total_benef = sum(_entier(a["beneficiaires"]) for a in associations)
    return render_template(
        "distribution/planning_jour_generation.html",
        date_choisie=d,
        jour_nom=JOURS_FR[d.weekday()] if d else None,
        associations=associations,
        total_benef=total_benef,
        jour_precedent=_voisin(-1),
        jour_suivant=_voisin(1),
        week_end=bool(d and d.weekday() >= 5),
        ferme_motif=(charger_fermetures().get(d.isoformat()) if d else None),
        erreur=erreur,
        aujourdhui=date.today().isoformat(),
    )


@distribution_bp.route("/planning_jour/deplacement", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def planning_jour_deplacement():
    """Choix du sens de déplacement (suivant / précédent) d'une association
    dont le jour de passage tombe un jour de fermeture."""
    data = request.get_json(silent=True) or {}
    try:
        ligne_ids = [int(x) for x in (data.get("ligne_ids") or [])]
        if not ligne_ids:
            raise ValueError
        d0 = datetime.strptime(str(data.get("date_origine")), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return jsonify(ok=False, erreur="Paramètres invalides"), 400
    sens = data.get("sens")
    if sens not in ("suivant", "precedent"):
        return jsonify(ok=False, erreur="Sens invalide"), 400

    conn = get_db_connection()
    if d0.isoformat() not in charger_fermetures(conn):
        conn.close()
        return jsonify(ok=False, erreur="Ce jour n'est pas un jour de fermeture"), 400
    for ligne_id in ligne_ids:
        if sens == "suivant":   # le suivant est le défaut : on supprime l'exception
            conn.execute("DELETE FROM planning_deplacements WHERE ligne_id=? AND date_origine=?",
                         (ligne_id, d0.isoformat()))
        else:
            conn.execute("INSERT OR REPLACE INTO planning_deplacements (ligne_id, date_origine, sens) VALUES (?,?,?)",
                         (ligne_id, d0.isoformat(), sens))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ------------------------------------------------------------
# Jours de fermeture de la BA38
# ------------------------------------------------------------
NOMS_FERIES = {(1, 1): "Jour de l'An", (5, 1): "Fête du travail", (5, 8): "Victoire 1945",
               (7, 14): "Fête nationale", (8, 15): "Assomption", (11, 1): "Toussaint",
               (11, 11): "Armistice", (12, 25): "Noël"}


def _nom_ferie(d: date) -> str:
    if (d.month, d.day) in NOMS_FERIES:
        return NOMS_FERIES[(d.month, d.day)]
    return {0: "Lundi de Pâques", 3: "Jeudi de l'Ascension", 4: "Lundi de Pentecôte"}.get(
        d.weekday(), "Jour férié")


@distribution_bp.route("/planning_jour/fermetures", methods=["GET", "POST"])
@login_required
@require_access("distribution", "lecture")
def planning_jour_fermetures():
    if request.method == "POST":
        # écriture : même droit que les autres écrans de saisie de la distribution
        if not has_access("distribution", "ecriture"):
            flash("Droit d'écriture requis.", "danger")
            return redirect(url_for("distribution.planning_jour_fermetures"))
        action = request.form.get("action")
        conn = get_db_connection()
        try:
            if action == "ajouter":
                d = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d").date()
                conn.execute("INSERT OR REPLACE INTO ba_fermetures (date, motif) VALUES (?, ?)",
                             (d.isoformat(), request.form.get("motif", "").strip()))
                flash(f"Fermeture ajoutée : {d.strftime('%d/%m/%Y')}.", "success")
            elif action == "supprimer":
                conn.execute("DELETE FROM ba_fermetures WHERE date = ?", (request.form.get("date"),))
                conn.execute("DELETE FROM planning_deplacements WHERE date_origine = ?", (request.form.get("date"),))
                flash("Fermeture supprimée.", "success")
            elif action == "feries":
                annee = int(request.form.get("annee"))
                n = 0
                for f in sorted(jours_feries(annee)):
                    if f.weekday() < 5:  # samedi/dimanche : déjà non travaillés
                        cur = conn.execute("INSERT OR IGNORE INTO ba_fermetures (date, motif) VALUES (?, ?)",
                                           (f.isoformat(), _nom_ferie(f)))
                        n += cur.rowcount
                flash(f"{n} jour(s) férié(s) {annee} ajouté(s) (jours déjà présents conservés).", "success")
            conn.commit()
        except (ValueError, TypeError):
            flash("Date ou année invalide.", "danger")
        finally:
            conn.close()
        return redirect(url_for("distribution.planning_jour_fermetures", annee=request.form.get("annee") or ""))

    try:
        annee = int(request.args.get("annee") or date.today().year)
    except ValueError:
        annee = date.today().year
    fermetures = sorted(
        ({"date": k, "motif": v, "jour": JOURS_FR[datetime.strptime(k, '%Y-%m-%d').weekday()],
          "fr": datetime.strptime(k, '%Y-%m-%d').strftime('%d/%m/%Y')}
         for k, v in charger_fermetures().items() if k.startswith(f"{annee}-")),
        key=lambda x: x["date"])
    return render_template("distribution/planning_jour_fermetures.html",
                           annee=annee, fermetures=fermetures)


# ============================================================
# Saisie : associations / lignes de menu
# ============================================================
from ba38_partenaires.passage import (  # noqa: E402
    EXEMPLES_FREQUENCE, JOURS_PASSAGE, PARKINGS, PARKINGS_QUAI,
    normaliser_heure, normaliser_jours, normaliser_parking,
)


def _liste_associations(conn):
    return [dict(id=r[0], code=r[1] or "", nom=(r[2] or "").strip(), planification=(r[3] or "oui"))
            for r in conn.execute("""
                SELECT id, code_VIF, nom_association, planification FROM associations
                WHERE TRIM(COALESCE(nom_association, '')) != ''
                  AND (validite IS NULL OR LOWER(TRIM(validite)) != 'non')
                ORDER BY nom_association COLLATE NOCASE""")]


@distribution_bp.route("/planning_jour/menus")
@login_required
@require_access("distribution", "lecture")
def planning_jour_menus():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    lignes = [dict(r) for r in conn.execute("""
        SELECT m.id, m.association_id, a.code_VIF AS code_vif, a.nom_association AS nom,
               COALESCE(LOWER(TRIM(a.planification)), 'oui') AS planification,
               a.validite, m.menu, m.type_menu, m.jours, m.heure, m.emplacement,
               m.frequence, m.nbre_beneficiaires, m.besoins_particuliers, m.actif
        FROM associations_menus m JOIN associations a ON a.id = m.association_id
        WHERE a.validite IS NULL OR LOWER(TRIM(a.validite)) != 'non'   -- associations inactives : jamais gérées ici
        ORDER BY a.nom_association COLLATE NOCASE, m.id""")]
    associations = _liste_associations(conn)
    codes_sec = [r[0] for r in conn.execute("SELECT DISTINCT menu FROM associations_menus WHERE type_menu='sec' AND COALESCE(menu,'')<>'' ORDER BY 1")]
    codes_frais = [r[0] for r in conn.execute("SELECT DISTINCT menu FROM associations_menus WHERE type_menu='frais' AND COALESCE(menu,'')<>'' ORDER BY 1")]
    conn.close()
    return render_template(
        "distribution/planning_jour_menus.html", lignes=lignes, associations=associations,
        codes_sec=codes_sec, codes_frais=codes_frais, jours=JOURS_PASSAGE, parkings=PARKINGS,
        exemples_frequence=EXEMPLES_FREQUENCE, peut_ecrire=has_access("distribution", "ecriture"))


@distribution_bp.route("/planning_jour/menus/save", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def planning_jour_menu_save():
    """Crée ou modifie une ligne de menu (id absent = création)."""
    d = request.get_json(silent=True) or {}
    try:
        association_id = int(d.get("association_id"))
        ligne_id = int(d["id"]) if d.get("id") else None
    except (TypeError, ValueError):
        return jsonify(ok=False, erreur="Association invalide"), 400

    try:
        jours = normaliser_jours(d.get("jours") or [])
        heure = normaliser_heure(d.get("heure"))
        emplacement = normaliser_parking(d.get("emplacement"))
    except ValueError as e:
        return jsonify(ok=False, erreur=str(e)), 400
    if emplacement in PARKINGS_QUAI and not (jours and heure):
        return jsonify(ok=False, erreur="Jours et heure obligatoires pour un passage au quai (P1/P6)."), 400

    menu = (d.get("menu") or "").strip() or None
    type_menu = d.get("type_menu")
    if not menu:
        return jsonify(ok=False, erreur="Le menu est obligatoire (un menu par ligne)."), 400
    if type_menu not in ("sec", "frais"):
        return jsonify(ok=False, erreur="Choisissez le type de menu : sec ou frais."), 400

    frequence = (d.get("frequence") or "").strip() or None
    benef = d.get("nbre_beneficiaires")
    try:
        benef = int(benef) if str(benef or "").strip() != "" else None
        if benef is not None and benef < 0:
            raise ValueError
    except ValueError:
        return jsonify(ok=False, erreur="Nombre de bénéficiaires invalide"), 400

    conn = get_db_connection()
    try:
        if not conn.execute("SELECT 1 FROM associations WHERE id=?", (association_id,)).fetchone():
            return jsonify(ok=False, erreur="Association introuvable"), 404
        ancienne = None
        if ligne_id:
            row = conn.execute("SELECT frequence, association_id FROM associations_menus WHERE id=?", (ligne_id,)).fetchone()
            if not row:
                return jsonify(ok=False, erreur="Ligne introuvable"), 404
            ancienne, association_id = row[0], row[1]   # l'association d'une ligne ne change pas
        if frequence and frequence != (ancienne or None) and regle_frequence(frequence)[0] == "inconnue":
            return jsonify(ok=False, erreur=f"Fréquence non reconnue. Exemples : {EXEMPLES_FREQUENCE}"), 400

        valeurs = (
            menu, type_menu,
            jours, heure, emplacement, frequence, benef,
            (d.get("besoins_particuliers") or "").strip() or None, 1 if d.get("actif", True) else 0,
        )
        if ligne_id:
            conn.execute("""UPDATE associations_menus SET menu=?, type_menu=?, jours=?, heure=?,
                            emplacement=?, frequence=?, nbre_beneficiaires=?, besoins_particuliers=?, actif=?
                            WHERE id=?""", valeurs + (ligne_id,))
        else:
            cur = conn.execute("""INSERT INTO associations_menus (menu, type_menu, jours, heure,
                            emplacement, frequence, nbre_beneficiaires, besoins_particuliers, actif, association_id)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""", valeurs + (association_id,))
            ligne_id = cur.lastrowid
        if "planification" in d:
            conn.execute("UPDATE associations SET planification=? WHERE id=?",
                         ("oui" if d.get("planification") else "non", association_id))
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True, id=ligne_id)


@distribution_bp.route("/planning_jour/association/<int:association_id>/planification", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def planning_jour_association_planification(association_id):
    """Inclut / exclut (ponctuellement) une association du planning : champ `planification` de la fiche."""
    inclure = bool((request.get_json(silent=True) or {}).get("planification"))
    conn = get_db_connection()
    cur = conn.execute("UPDATE associations SET planification=? WHERE id=?",
                       ("oui" if inclure else "non", association_id))
    conn.commit()
    conn.close()
    if not cur.rowcount:
        return jsonify(ok=False, erreur="Association introuvable"), 404
    return jsonify(ok=True, planification="oui" if inclure else "non")


@distribution_bp.route("/planning_jour/menus/<int:ligne_id>/delete", methods=["POST"])
@login_required
@require_access("distribution", "ecriture")
def planning_jour_menu_delete(ligne_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM planning_deplacements WHERE ligne_id=?", (ligne_id,))
    conn.execute("DELETE FROM associations_menus WHERE id=?", (ligne_id,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ============================================================
# Saisie : fermetures d'associations
# ============================================================
@distribution_bp.route("/planning_jour/fermetures_associations", methods=["GET", "POST"])
@login_required
@require_access("distribution", "lecture")
def planning_jour_fermetures_assos():
    if request.method == "POST":
        if not has_access("distribution", "ecriture"):
            flash("Droit d'écriture requis.", "danger")
            return redirect(url_for("distribution.planning_jour_fermetures_assos"))
        action = request.form.get("action")
        conn = get_db_connection()
        try:
            if action == "ajouter":
                aid = int(request.form.get("association_id"))
                debut = datetime.strptime(request.form.get("date_debut", ""), "%Y-%m-%d").date()
                fin_brut = request.form.get("date_fin", "").strip()
                fin = datetime.strptime(fin_brut, "%Y-%m-%d").date() if fin_brut else None
                if fin and fin < debut:
                    flash("La date de fin précède la date de début.", "danger")
                elif not conn.execute("SELECT 1 FROM associations WHERE id=?", (aid,)).fetchone():
                    flash("Association introuvable : choisissez-la dans la liste.", "danger")
                else:
                    conn.execute("""INSERT INTO associations_fermetures (association_id, date_debut, date_fin, motif)
                                    VALUES (?,?,?,?)""", (aid, debut.isoformat(), fin.isoformat() if fin else None,
                                                          request.form.get("motif", "").strip() or None))
                    conn.commit()
                    flash("Fermeture enregistrée.", "success")
            elif action == "supprimer":
                conn.execute("DELETE FROM associations_fermetures WHERE id=?", (int(request.form.get("id")),))
                conn.commit()
                flash("Fermeture supprimée.", "success")
        except (ValueError, TypeError):
            flash("Association ou date invalide : choisissez l'association dans la liste.", "danger")
        finally:
            conn.close()
        return redirect(url_for("distribution.planning_jour_fermetures_assos"))

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    aujourdhui = date.today().isoformat()
    lignes = []
    for r in conn.execute("""
            SELECT f.id, f.association_id, a.code_VIF AS code, a.nom_association AS nom,
                   f.date_debut, f.date_fin, COALESCE(f.motif, '') AS motif
            FROM associations_fermetures f JOIN associations a ON a.id = f.association_id
            ORDER BY f.date_debut DESC, a.nom_association COLLATE NOCASE"""):
        l = dict(r)
        if l["date_fin"] and l["date_fin"] < aujourdhui:
            l["etat"] = "passée"
        elif l["date_debut"] > aujourdhui:
            l["etat"] = "à venir"
        else:
            l["etat"] = "en cours"
        for k in ("date_debut", "date_fin"):
            l[k + "_fr"] = datetime.strptime(l[k], "%Y-%m-%d").strftime("%d/%m/%Y") if l[k] else "—"
        lignes.append(l)
    associations = _liste_associations(conn)
    conn.close()
    return render_template("distribution/planning_jour_fermetures_assos.html",
                           lignes=lignes, associations=associations,
                           peut_ecrire=has_access("distribution", "ecriture"))
