"""CERFA abandon de frais bénévoles — questionnaire en ligne, sans compte,
accessible par le lien personnel envoyé par mail (jeton aléatoire stocké en
base, cf. frais_commun.connexion_token).

Parcours : 1) écran « imposable ou non » (pièces et document différents) ;
2) coordonnées, véhicule (imposables), journées par lieu et par mois, km
aller-retour calculés automatiquement depuis l'adresse ; 3) dépôt des pièces ;
4) certification + transmission. Modifiable jusqu'à la validation par la
trésorerie."""

import json
import os
import uuid
from datetime import date, datetime

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from ba38_utilitaires.core import write_log

from ba38_cerfa import cerfa_bp
from ba38_cerfa.calculs import (
    adresse_complete, bareme_campagne, champs_manquants, charger_json, km_auto_par_lieu, totaux_declaration,
)
from ba38_cerfa.constants import (
    EXTENSIONS_PIECES, MOIS, TAILLE_MAX_PIECE, TYPES_PIECE, TYPES_VEHICULE,
)
from ba38_cerfa.frais_commun import connexion_token, dossier_pieces, lieux_declaration, pieces_declaration

STATUTS_MODIFIABLES = ("a_inviter", "invite", "en_cours", "soumis", "decline")


def _maintenant():
    return datetime.now().isoformat(timespec="seconds")


def _charger(token):
    """Retourne (conn, declaration, campagne) ou abort(404) — même réponse
    pour un jeton inconnu ou mal formé (pas d'énumération possible)."""
    conn = connexion_token(token)
    if conn is None:
        abort(404)
    d = conn.execute("SELECT * FROM cerfa_frais_declarations WHERE token = ?", (token,)).fetchone()
    if d is None:
        conn.close()
        abort(404)
    campagne = conn.execute("SELECT * FROM cerfa_frais_campagnes WHERE id = ?", (d["campagne_id"],)).fetchone()
    return conn, d, campagne


@cerfa_bp.errorhandler(404)
def _lien_invalide(_e):
    if request.endpoint and request.endpoint.startswith("cerfa.frais_q"):
        return render_template("cerfa/frais/public_lien_invalide.html"), 404
    return _e


def _modifiable(d):
    return d["statut"] in STATUTS_MODIFIABLES


def _marquer_en_cours(conn, d):
    conn.execute("""
        UPDATE cerfa_frais_declarations
        SET statut = CASE WHEN statut IN ('a_inviter', 'invite', 'decline') THEN 'en_cours' ELSE statut END,
            decline_le = NULL, derniere_modif_le = ?
        WHERE id = ?
    """, (_maintenant(), d["id"]))


# ============================================================================
# 📝 Page principale
# ============================================================================
@cerfa_bp.route("/frais/q/<token>")
def frais_questionnaire(token):
    conn, d, campagne = _charger(token)
    try:
        # Ouverture par la trésorerie (saisie d'un retour papier) : ne compte pas
        if not d["premiere_ouverture_le"] and not current_user.is_authenticated:
            conn.execute("UPDATE cerfa_frais_declarations SET premiere_ouverture_le = ? WHERE id = ?",
                         (_maintenant(), d["id"]))
            conn.commit()

        if d["statut"] == "decline":
            return render_template("cerfa/frais/public_decline.html", d=d, campagne=campagne, token=token)

        if d["imposable"] not in (0, 1) or request.args.get("situation"):
            if not _modifiable(d):
                return redirect(url_for("cerfa.frais_questionnaire", token=token))
            return render_template("cerfa/frais/public_situation.html", d=d, campagne=campagne, token=token)

        lieux = lieux_declaration(conn, d)

        # Premier affichage (imposable) : km calculés automatiquement depuis l'adresse connue
        if d["imposable"] == 1 and not d["km_auto_json"] and _modifiable(d) and d["rue"] and d["ville"]:
            _calculer_km_auto(conn, d, lieux, ecraser_saisie=False)
            conn.commit()
            d = conn.execute("SELECT * FROM cerfa_frais_declarations WHERE id = ?", (d["id"],)).fetchone()

        pieces = pieces_declaration(conn, d["id"])
        totaux = totaux_declaration(d, campagne, lieux)
        manquants = champs_manquants(d, campagne, lieux, pieces)
        date_depassee = bool(campagne["date_limite"]) and date.today().isoformat() > campagne["date_limite"]

        return render_template(
            "cerfa/frais/public_formulaire.html",
            d=d, campagne=campagne, token=token, lieux=lieux, mois=MOIS, pieces=pieces, totaux=totaux,
            manquants=manquants, modifiable=_modifiable(d), date_depassee=date_depassee,
            journees=charger_json(d["journees_json"]), km=charger_json(d["km_json"]),
            km_auto=charger_json(d["km_auto_json"]), types_vehicule=TYPES_VEHICULE, types_piece=TYPES_PIECE,
            bareme=bareme_campagne(campagne),
        )
    finally:
        conn.close()


@cerfa_bp.route("/frais/q/<token>/situation", methods=["POST"])
def frais_q_situation(token):
    conn, d, _ = _charger(token)
    try:
        if not _modifiable(d):
            return redirect(url_for("cerfa.frais_questionnaire", token=token))
        choix = request.form.get("imposable")
        if choix not in ("0", "1"):
            flash("Merci d'indiquer si vous êtes imposable ou non.", "warning")
            return redirect(url_for("cerfa.frais_questionnaire", token=token, situation=1))
        conn.execute("UPDATE cerfa_frais_declarations SET imposable = ?, certifie_le = NULL WHERE id = ?",
                     (int(choix), d["id"]))
        if d["statut"] == "soumis" and int(choix) != d["imposable"]:
            # Changement de situation après transmission : à revoir et retransmettre
            conn.execute("UPDATE cerfa_frais_declarations SET statut = 'en_cours', soumis_le = NULL WHERE id = ?",
                         (d["id"],))
        _marquer_en_cours(conn, d)
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("cerfa.frais_questionnaire", token=token))


# ============================================================================
# 💾 Enregistrement / transmission
# ============================================================================
def _entier(valeur, maxi=None):
    try:
        n = int(str(valeur).strip() or 0)
    except ValueError:
        return 0
    n = max(n, 0)
    return min(n, maxi) if maxi else n


def _decimal(valeur):
    try:
        v = float(str(valeur).replace(",", ".").strip())
        return round(v, 1) if v > 0 else None
    except ValueError:
        return None


@cerfa_bp.route("/frais/q/<token>/enregistrer", methods=["POST"])
def frais_q_enregistrer(token):
    conn, d, campagne = _charger(token)
    try:
        if not _modifiable(d):
            flash("Votre déclaration a déjà été validée par la Banque Alimentaire : elle n'est plus modifiable.",
                  "warning")
            return redirect(url_for("cerfa.frais_questionnaire", token=token))

        lieux = lieux_declaration(conn, d)
        journees = {}
        km = {}
        for lieu in lieux:
            cle = str(lieu["id"])
            par_mois = {str(m): _entier(request.form.get(f"j-{cle}-{m}"), 31) for m in range(1, 13)}
            journees[cle] = {m: n for m, n in par_mois.items() if n}
            valeur_km = _decimal(request.form.get(f"km-{cle}"))
            if valeur_km is not None:
                km[cle] = valeur_km

        f = request.form
        champs = {
            "civilite": (f.get("civilite") or "").strip() or None,
            "nom": (f.get("nom") or "").strip(),
            "prenom": (f.get("prenom") or "").strip(),
            "rue": (f.get("rue") or "").strip(),
            "complement_adresse": (f.get("complement_adresse") or "").strip() or None,
            "code_postal": (f.get("code_postal") or "").strip(),
            "ville": (f.get("ville") or "").strip(),
            "telephone": (f.get("telephone") or "").strip() or None,
            "journees_json": json.dumps(journees),
            "km_json": json.dumps(km),
            "commentaire_autres": (f.get("commentaire_autres") or "").strip() or None,
            "commentaire_benevole": (f.get("commentaire_benevole") or "").strip() or None,
            "derniere_modif_le": _maintenant(),
        }
        if d["imposable"] == 1:
            vehicule_type = f.get("vehicule_type") if f.get("vehicule_type") in TYPES_VEHICULE else None
            champs.update({
                "vehicule_type": vehicule_type,
                "vehicule_marque": (f.get("vehicule_marque") or "").strip() or None,
                "vehicule_immatriculation": (f.get("vehicule_immatriculation") or "").strip().upper() or None,
                "vehicule_cv": _entier(f.get("vehicule_cv"), 99) or None,
                "vehicule_electrique": 1 if f.get("vehicule_electrique") else 0,
            })

        conn.execute(
            f"UPDATE cerfa_frais_declarations SET {', '.join(f'{k} = ?' for k in champs)} WHERE id = ?",
            list(champs.values()) + [d["id"]],
        )
        _marquer_en_cours(conn, d)
        nb_fichiers = _enregistrer_fichiers(conn, d)
        if nb_fichiers:
            flash(f"📎 {nb_fichiers} fichier(s) ajouté(s).", "success")

        d = conn.execute("SELECT * FROM cerfa_frais_declarations WHERE id = ?", (d["id"],)).fetchone()

        if f.get("action") == "transmettre":
            manquants = champs_manquants(d, campagne, lieux, pieces_declaration(conn, d["id"]))
            if not f.get("certification"):
                manquants.append("la case de certification sur l'honneur")
            if manquants:
                conn.commit()
                flash("Votre saisie est enregistrée, mais la déclaration ne peut pas encore être transmise. "
                      "Il manque : " + " ; ".join(manquants) + ".", "warning")
                return redirect(url_for("cerfa.frais_questionnaire", token=token) + "#transmettre")
            totaux = totaux_declaration(d, campagne, lieux)
            conn.execute("""
                UPDATE cerfa_frais_declarations SET statut = 'soumis', soumis_le = ?, certifie_le = ?,
                    total_journees = ?, distance_totale = ?, montant = ?, montant_arrondi = ?
                WHERE id = ?
            """, (_maintenant(), _maintenant(), totaux["total_journees"], totaux["distance_totale"],
                  totaux["montant"], totaux["montant_arrondi"], d["id"]))
            conn.commit()
            write_log(f"📨 CERFA frais : déclaration {d['id']} ({d['nom']} {d['prenom']}) transmise par le bénévole")
            flash("✅ Merci ! Votre déclaration a bien été transmise à la Banque Alimentaire de l'Isère. "
                  "Vous pouvez encore la modifier tant qu'elle n'a pas été validée.", "success")
        else:
            if d["statut"] == "soumis":
                # Modification après transmission : reste transmise, totaux recalculés
                totaux = totaux_declaration(d, campagne, lieux)
                conn.execute("""UPDATE cerfa_frais_declarations SET total_journees = ?, distance_totale = ?,
                                    montant = ?, montant_arrondi = ? WHERE id = ?""",
                             (totaux["total_journees"], totaux["distance_totale"], totaux["montant"],
                              totaux["montant_arrondi"], d["id"]))
            conn.commit()
            flash("💾 Saisie enregistrée. Vous pouvez revenir la compléter plus tard avec le même lien.", "success")
    finally:
        conn.close()
    return redirect(url_for("cerfa.frais_questionnaire", token=token))


# ============================================================================
# 📍 Calcul automatique des km
# ============================================================================
def _calculer_km_auto(conn, d, lieux, ecraser_saisie, adresse=None):
    adresse = adresse or adresse_complete(d["rue"], None, d["code_postal"], d["ville"])
    km_auto, geo = km_auto_par_lieu(adresse, lieux)
    if not geo:
        return None, None
    km = charger_json(d["km_json"])
    for cle, valeur in km_auto.items():
        if ecraser_saisie or cle not in km:
            km[cle] = valeur
    conn.execute("""
        UPDATE cerfa_frais_declarations SET km_auto_json = ?, km_json = ?, adresse_geocode_label = ?,
            adresse_geocode_score = ? WHERE id = ?
    """, (json.dumps(km_auto), json.dumps(km), geo[2], geo[3], d["id"]))
    return km_auto, geo


@cerfa_bp.route("/frais/q/<token>/km_auto", methods=["POST"])
def frais_q_km_auto(token):
    """AJAX : calcule les km A/R depuis l'adresse saisie (sans enregistrer le
    reste du formulaire). Renvoie {lieu_id: km}."""
    conn, d, _ = _charger(token)
    try:
        if not _modifiable(d):
            return jsonify({"ok": False, "message": "Déclaration non modifiable"}), 400
        donnees = request.get_json(silent=True) or {}
        adresse = adresse_complete(donnees.get("rue"), None, donnees.get("code_postal"), donnees.get("ville"))
        if not adresse.strip():
            return jsonify({"ok": False, "message": "Renseignez d'abord votre adresse."})
        km_auto, geo = _calculer_km_auto(conn, d, lieux_declaration(conn, d), ecraser_saisie=True, adresse=adresse)
        conn.commit()
        if not geo:
            return jsonify({"ok": False, "message": "Adresse introuvable : vérifiez-la, ou saisissez vos km "
                                                    "vous-même."})
        return jsonify({"ok": True, "km": km_auto, "adresse": geo[2], "score": geo[3]})
    finally:
        conn.close()


# ============================================================================
# 📎 Pièces justificatives
# ============================================================================
def _enregistrer_fichiers(conn, d):
    """Pièces déposées avec le formulaire (champs fichier_<type_piece>).
    Retourne le nombre de fichiers acceptés ; les refus sont signalés par flash."""
    nb = 0
    for type_piece in TYPES_PIECE:
        for fichier in request.files.getlist(f"fichier_{type_piece}"):
            if not fichier or not fichier.filename:
                continue
            ext = fichier.filename.rsplit(".", 1)[-1].lower() if "." in fichier.filename else ""
            if ext not in EXTENSIONS_PIECES:
                flash(f"« {fichier.filename} » refusé : formats acceptés PDF, JPG, PNG, HEIC.", "danger")
                continue
            chemin = os.path.join(dossier_pieces(d), f"{type_piece}_{uuid.uuid4().hex[:12]}.{ext}")
            fichier.save(chemin)
            taille = os.path.getsize(chemin)
            if taille == 0 or taille > TAILLE_MAX_PIECE:
                os.remove(chemin)
                flash(f"« {fichier.filename} » refusé : fichier vide ou de plus de 10 Mo.", "danger")
                continue
            conn.execute("""
                INSERT INTO cerfa_frais_pieces (declaration_id, type_piece, chemin, nom_original, taille, depose_le)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (d["id"], type_piece, chemin, os.path.basename(fichier.filename)[:200], taille, _maintenant()))
            nb += 1
    return nb


def _piece(conn, d, piece_id):
    piece = conn.execute("SELECT * FROM cerfa_frais_pieces WHERE id = ? AND declaration_id = ?",
                         (piece_id, d["id"])).fetchone()
    if not piece:
        abort(404)
    return piece


@cerfa_bp.route("/frais/q/<token>/piece/<int:piece_id>")
def frais_q_voir_piece(token, piece_id):
    conn, d, _ = _charger(token)
    try:
        piece = _piece(conn, d, piece_id)
    finally:
        conn.close()
    if not os.path.exists(piece["chemin"]):
        abort(404)
    return send_file(piece["chemin"], download_name=piece["nom_original"] or os.path.basename(piece["chemin"]))


@cerfa_bp.route("/frais/q/<token>/piece/<int:piece_id>/supprimer", methods=["POST"])
def frais_q_supprimer_piece(token, piece_id):
    """AJAX : supprime une pièce sans recharger la page (saisie en cours préservée)."""
    conn, d, _ = _charger(token)
    try:
        if not _modifiable(d):
            return jsonify({"ok": False, "message": "Déclaration non modifiable"}), 400
        piece = _piece(conn, d, piece_id)
        conn.execute("DELETE FROM cerfa_frais_pieces WHERE id = ?", (piece["id"],))
        conn.commit()
        if os.path.exists(piece["chemin"]):
            os.remove(piece["chemin"])
    finally:
        conn.close()
    return jsonify({"ok": True})


# ============================================================================
# 🚫 Ne participe pas
# ============================================================================
@cerfa_bp.route("/frais/q/<token>/decliner", methods=["POST"])
def frais_q_decliner(token):
    conn, d, _ = _charger(token)
    try:
        if d["statut"] in ("a_inviter", "invite", "en_cours"):
            conn.execute("UPDATE cerfa_frais_declarations SET statut = 'decline', decline_le = ? WHERE id = ?",
                         (_maintenant(), d["id"]))
            conn.commit()
            write_log(f"🚫 CERFA frais : déclaration {d['id']} ({d['nom']} {d['prenom']}) — ne participe pas")
    finally:
        conn.close()
    return redirect(url_for("cerfa.frais_questionnaire", token=token))


@cerfa_bp.route("/frais/q/<token>/reprendre", methods=["POST"])
def frais_q_reprendre(token):
    conn, d, _ = _charger(token)
    try:
        if d["statut"] == "decline":
            _marquer_en_cours(conn, d)
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for("cerfa.frais_questionnaire", token=token))
