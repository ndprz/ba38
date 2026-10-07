# =========================================
# 🏛️ Participation V2 — dépôt Chorus Pro + export des PDF sur Drive
# =========================================
# Routes ajoutées au blueprint participation (écran Résultats) :
# - dépôt Chorus Pro d'une facture ou de toutes celles des partenaires
#   publics (associations.chorus_pro = 'oui'), actualisation des statuts ;
# - export de tous les PDF de la campagne dans Drive
#   Participations › AAAA_TN › Factures PDF.
#
# Le mail de facture reste envoyé aussi aux partenaires Chorus Pro (à titre
# informatif, choix de la trésorerie) : rien ne change dans envoyer().
#
# Environnement Chorus : voir ba38_chorus.environnement_chorus() — DEV et
# comptes test_only déposent TOUJOURS en qualification.

import os
import re
import sqlite3
from datetime import datetime

from flask import flash, redirect, url_for, current_app
from flask_login import login_required, current_user

from ba38_chorus import ClientChorus, ChorusErreur, environnement_chorus
from ba38_utilitaires.core import get_db_path, require_access, write_log, nom_piece_jointe_facture
from ba38_utilitaires.taches_fond import lancer_tache_fond
from ba38_tresorerie.participation import (
    participation_bp, generer_facture_participation_pdf, donnees_pdf_facture, _service_drive,
)

NOM_DOSSIER_PDF = "Factures PDF"


def _maintenant():
    return datetime.now().isoformat(timespec="seconds")


# ============================================================================
# 🏛️ CHORUS PRO
# ============================================================================
def _deposer_une_facture(conn, client, f, periode, utilisateur):
    """
    Vérifie la fiche puis dépose une facture. Écrit le résultat (succès ou
    refus) sur la ligne participation_factures. Retourne True si déposée.
    """
    try:
        assoc = conn.execute("SELECT * FROM associations WHERE Id = ?", (f["association_id"],)).fetchone()
        if not assoc or (assoc["chorus_pro"] or "").strip().lower() != "oui":
            raise ChorusErreur("association non marquée Chorus Pro (fiche association)")

        siret = re.sub(r"\D", "", assoc["code_SIRET"] or "")
        if len(siret) != 14:
            raise ChorusErreur(f"SIRET absent ou invalide sur la fiche ({assoc['code_SIRET'] or 'vide'})")

        code_service = (assoc["chorus_code_service"] or "").strip()
        numero_ej = (assoc["chorus_numero_engagement"] or "").strip()

        if client.env == "prod":
            exig = client.exigences_destinataire(siret)
            manque = []
            if exig["service"] and not code_service:
                manque.append("code service")
            if exig["ej"] and not numero_ej:
                manque.append("numéro d'engagement")
            if exig["ej_ou_service"] and not (code_service or numero_ej):
                manque.append("numéro d'engagement ou code service")
            if manque:
                raise ChorusErreur(f"{exig['designation']} exige : {', '.join(manque)} — à saisir sur la fiche association")
            if code_service:
                services = client.services_destinataire(exig["id"])
                if services is not None and code_service not in services:
                    raise ChorusErreur(
                        f"code service « {code_service} » inconnu de {exig['designation']} — "
                        f"codes valides : {', '.join(services) or 'aucun'} (fiche association)")

        data_pdf = donnees_pdf_facture(conn, f)
        nom_fichier = nom_piece_jointe_facture(f["numero_facture"], f["nom_association"], code_vif=f["code_vif"])
        pdf_path = f"/tmp/participation_chorus_{f['id']}.pdf"
        try:
            generer_facture_participation_pdf(data_pdf, pdf_path)
            with open(pdf_path, "rb") as fic:
                pdf_bytes = fic.read()
        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

        res = client.deposer_facture(
            pdf_bytes=pdf_bytes,
            nom_fichier=nom_fichier,
            numero_facture=f["numero_facture"],
            siret_destinataire=siret,
            montant=f["montant_total"],
            designation=f"Participation {periode}",
            code_service=code_service,
            numero_ej=numero_ej,
            commentaire=f"Participation de solidarité {periode} - {f['nom_association']}",
        )

        conn.execute("""
            UPDATE participation_factures
            SET chorus_env = ?, chorus_id_facture = ?, chorus_numero = ?, chorus_statut = ?,
                chorus_depose_le = ?, chorus_depose_par = ?, chorus_erreur = NULL,
                chorus_statut_verifie_le = ?
            WHERE id = ?
        """, (client.env, res["identifiant"], res["numero"], res["statut"],
              _maintenant(), utilisateur, _maintenant(), f["id"]))
        conn.commit()
        write_log(f"🏛️ Chorus Pro [{client.env}] facture participation {f['numero_facture']} "
                  f"({f['nom_association']}) déposée : id {res['identifiant']}, {res['statut']}")
        return True

    except Exception as e:
        message = str(e) if isinstance(e, ChorusErreur) else f"Erreur technique : {e}"
        conn.execute("""
            UPDATE participation_factures SET chorus_erreur = ?, chorus_statut_verifie_le = ?
            WHERE id = ?
        """, (f"[{client.env}] {message}", _maintenant(), f["id"]))
        conn.commit()
        write_log(f"❌ Chorus Pro [{client.env}] facture participation {f['numero_facture']} "
                  f"({f['nom_association']}) : {message}")
        return False


def _factures_a_deposer(conn, campagne_id, env):
    """Factures des partenaires Chorus Pro pas encore déposées dans cet environnement."""
    return conn.execute("""
        SELECT pf.* FROM participation_factures pf
        JOIN associations a ON a.Id = pf.association_id
        WHERE pf.campagne_id = ? AND LOWER(TRIM(a.chorus_pro)) = 'oui'
          AND (pf.chorus_id_facture IS NULL OR IFNULL(pf.chorus_env, '') != ?)
        ORDER BY pf.numero_facture
    """, (campagne_id, env)).fetchall()


def deposer_chorus_background(app, db_path, campagne_id, env, utilisateur):
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        campagne = conn.execute("SELECT * FROM participation_campagnes WHERE id = ?", (campagne_id,)).fetchone()
        periode = f"T{campagne['trimestre']} {campagne['annee']}"
        factures = _factures_a_deposer(conn, campagne_id, env)

        nb_ok = nb_erreur = 0
        try:
            client = ClientChorus(env)
        except ChorusErreur as e:
            write_log(f"❌ Dépôt Chorus participation {periode} impossible : {e}")
            conn.close()
            return

        for f in factures:
            if _deposer_une_facture(conn, client, f, periode, utilisateur):
                nb_ok += 1
            else:
                nb_erreur += 1
        conn.close()
        write_log(f"🏛️ Dépôt Chorus participation {periode} [{env}] terminé : {nb_ok} déposée(s), {nb_erreur} refus")


def actualiser_statuts_chorus_background(app, db_path, campagne_id, env):
    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        lignes = conn.execute("""
            SELECT id, chorus_id_facture FROM participation_factures
            WHERE campagne_id = ? AND chorus_id_facture IS NOT NULL AND chorus_env = ?
        """, (campagne_id, env)).fetchall()
        try:
            client = ClientChorus(env)
            for l in lignes:
                try:
                    statut = client.statut_facture(l["chorus_id_facture"])
                    conn.execute(
                        "UPDATE participation_factures SET chorus_statut = ?, chorus_statut_verifie_le = ? WHERE id = ?",
                        (statut, _maintenant(), l["id"]))
                    conn.commit()
                except ChorusErreur as e:
                    write_log(f"⚠️ Statut Chorus facture id {l['chorus_id_facture']} illisible : {e}")
        except ChorusErreur as e:
            write_log(f"❌ Actualisation statuts Chorus impossible : {e}")
        conn.close()


@participation_bp.route("/participation/chorus/deposer/<int:facture_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def chorus_deposer(facture_id):
    env = environnement_chorus()
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    f = conn.execute("SELECT * FROM participation_factures WHERE id = ?", (facture_id,)).fetchone()
    if not f:
        conn.close()
        flash("❌ Facture introuvable", "danger")
        return redirect(url_for("participation.selection"))

    retour = redirect(url_for("participation.resultats", campagne_id=f["campagne_id"]))
    if f["chorus_id_facture"] and f["chorus_env"] == env:
        conn.close()
        flash(f"ℹ️ Facture {f['numero_facture']} déjà déposée sur Chorus Pro (id {f['chorus_id_facture']}).", "warning")
        return retour

    campagne = conn.execute("SELECT * FROM participation_campagnes WHERE id = ?", (f["campagne_id"],)).fetchone()
    try:
        client = ClientChorus(env)
    except ChorusErreur as e:
        conn.close()
        flash(f"❌ {e}", "danger")
        return retour

    ok = _deposer_une_facture(conn, client, f, f"T{campagne['trimestre']} {campagne['annee']}", current_user.email)
    ligne = conn.execute("SELECT chorus_id_facture, chorus_statut, chorus_erreur FROM participation_factures WHERE id = ?",
                         (facture_id,)).fetchone()
    conn.close()

    if ok:
        flash(f"✅ Facture {f['numero_facture']} ({f['nom_association']}) déposée sur Chorus Pro "
              f"{client.libelle_env} : id {ligne['chorus_id_facture']}, statut {ligne['chorus_statut']}.",
              "success" if env == "prod" else "warning")
    else:
        flash(f"❌ Chorus Pro a refusé la facture {f['numero_facture']} ({f['nom_association']}) : {ligne['chorus_erreur']}", "danger")
    return retour


@participation_bp.route("/participation/chorus/deposer_tout/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def chorus_deposer_tout(campagne_id):
    env = environnement_chorus()
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    nb = len(_factures_a_deposer(conn, campagne_id, env))
    conn.close()

    if not nb:
        flash("ℹ️ Aucune facture Chorus Pro à déposer pour cette campagne.", "warning")
        return redirect(url_for("participation.resultats", campagne_id=campagne_id))

    lancer_tache_fond(
        target=deposer_chorus_background,
        args=(current_app._get_current_object(), db_path, campagne_id, env, current_user.email),
        nom="Dépôt Chorus Pro participation",
        utilisateur=current_user.email,
    )
    flash(f"🏛️ Dépôt de {nb} facture(s) sur Chorus Pro "
          f"{'PRODUCTION' if env == 'prod' else 'qualification (test)'} lancé en arrière-plan "
          f"— actualisez la page dans 1 à 2 minutes.", "info")
    return redirect(url_for("participation.resultats", campagne_id=campagne_id))


@participation_bp.route("/participation/chorus/actualiser/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def chorus_actualiser(campagne_id):
    lancer_tache_fond(
        target=actualiser_statuts_chorus_background,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, environnement_chorus()),
        nom="Statuts Chorus Pro participation",
        utilisateur=current_user.email,
    )
    flash("🔄 Actualisation des statuts Chorus Pro lancée — actualisez la page dans une minute.", "info")
    return redirect(url_for("participation.resultats", campagne_id=campagne_id))


# ============================================================================
# 📂 EXPORT DES PDF SUR DRIVE (Participations › AAAA_TN › Factures PDF)
# ============================================================================
def _dossier(service, parent_id, nom):
    """Id du dossier `nom` sous `parent_id` (créé si absent), sans rien purger."""
    from ba38_tresorerie.participation_ebp import _trouver_dossier, _creer_dossier
    existants = _trouver_dossier(service, parent_id, nom)
    return existants[0]["id"] if existants else _creer_dossier(service, parent_id, nom)


def _vider_dossier(service, dossier_id):
    res = service.files().list(
        q=f"'{dossier_id}' in parents and trashed=false",
        fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True, pageSize=1000,
    ).execute()
    for fic in res.get("files", []):
        service.files().delete(fileId=fic["id"], supportsAllDrives=True).execute()


def exporter_pdf_drive_background(app, db_path, campagne_id, dossier_id, dossier_chemin, mode_test, utilisateur):
    from googleapiclient.http import MediaFileUpload

    with app.app_context():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        factures = conn.execute("""
            SELECT * FROM participation_factures
            WHERE campagne_id = ? AND association_id IS NOT NULL
            ORDER BY numero_facture
        """, (campagne_id,)).fetchall()

        nb_ok = nb_erreur = 0
        try:
            service = _service_drive()
            _vider_dossier(service, dossier_id)
        except Exception as e:
            write_log(f"❌ Export PDF participation (campagne {campagne_id}) : Drive inaccessible : {e}")
            conn.close()
            return

        for f in factures:
            pdf_path = f"/tmp/participation_export_{f['id']}.pdf"
            nom = nom_piece_jointe_facture(f["numero_facture"], f["nom_association"], code_vif=f["code_vif"])
            try:
                generer_facture_participation_pdf(donnees_pdf_facture(conn, f), pdf_path)
                service.files().create(
                    body={"name": nom, "parents": [dossier_id]},
                    media_body=MediaFileUpload(pdf_path, mimetype="application/pdf", resumable=False),
                    fields="id", supportsAllDrives=True,
                ).execute()
                nb_ok += 1
            except Exception as e:
                nb_erreur += 1
                write_log(f"❌ Export PDF participation : {nom} : {e}")
            finally:
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)

        conn.execute("""
            UPDATE participation_campagnes
            SET pdf_export_le = ?, pdf_export_par = ?, pdf_export_nb = ?, pdf_export_nb_erreur = ?,
                pdf_export_dossier_id = ?, pdf_export_dossier_chemin = ?, pdf_export_mode_test = ?
            WHERE id = ?
        """, (_maintenant(), utilisateur, nb_ok, nb_erreur, dossier_id, dossier_chemin,
              1 if mode_test else 0, campagne_id))
        conn.commit()
        conn.close()
        write_log(f"📂 Export PDF participation (campagne {campagne_id}) : {nb_ok} PDF, {nb_erreur} erreur(s) → {dossier_chemin}")


@participation_bp.route("/participation/export_pdf_drive/<int:campagne_id>", methods=["POST"])
@login_required
@require_access("tresorerie", "ecriture")
def export_pdf_drive(campagne_id):
    from ba38_tresorerie.participation_ebp import dossier_racine_participation, _mode_test_drive

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    campagne = conn.execute("SELECT * FROM participation_campagnes WHERE id = ?", (campagne_id,)).fetchone()
    conn.close()
    if not campagne:
        flash("❌ Campagne introuvable", "danger")
        return redirect(url_for("participation.selection"))

    # Dossiers résolus ici (dans la requête) : le mode test Drive dépend de
    # la session, illisible depuis la tâche de fond.
    try:
        service = _service_drive()
        racine_id, racine_chemin = dossier_racine_participation(service)
        nom_trimestre = f"{campagne['annee']}_T{campagne['trimestre']}"
        trimestre_id = _dossier(service, racine_id, nom_trimestre)
        dossier_id = _dossier(service, trimestre_id, NOM_DOSSIER_PDF)
    except Exception as e:
        write_log(f"❌ Export PDF participation (campagne {campagne_id}) : {e}")
        flash(f"❌ Accès au Drive impossible : {e}", "danger")
        return redirect(url_for("participation.resultats", campagne_id=campagne_id))

    dossier_chemin = f"{racine_chemin} › {nom_trimestre} › {NOM_DOSSIER_PDF}"
    lancer_tache_fond(
        target=exporter_pdf_drive_background,
        args=(current_app._get_current_object(), get_db_path(), campagne_id, dossier_id, dossier_chemin,
              _mode_test_drive(), current_user.email),
        nom="Export PDF participation Drive",
        utilisateur=current_user.email,
    )
    flash(f"📂 Export des PDF lancé vers {dossier_chemin} (le dossier est vidé puis rempli) "
          f"— actualisez la page dans 1 à 2 minutes.", "info")
    return redirect(url_for("participation.resultats", campagne_id=campagne_id))
