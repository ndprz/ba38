"""Planning ESOPE, phase 1 : liens personnels des bénévoles, mails et alertes.

Le lien personnel ouvre la page « Mes créneaux » sans compte Basilic. La page
publique n'a pas de session : la base visée est portée par le préfixe du jeton
(« t- » = base de test des comptes test_only, « p- » = base réelle), comme pour
le CERFA abandon de frais.

Destinataire des alertes (réponse d'un bénévole) :
- DEV : garde-fou d'envoyer_mail() → la personne qui a envoyé le pré-planning ;
- lien de test (jeton « t- », ou lien envoyé en mode TEST) → cette même
  personne, objet préfixé [TEST] ;
- sinon : la responsable ESOPE (ESOPE_MAIL_RESPONSABLE).
"""

import calendar
import os
import secrets
import sqlite3
from datetime import date, datetime

from flask import current_app, session

from ba38_utilitaires.core import envoyer_mail, render_modele_email, write_log
from ba38_utilitaires.taches_fond import lancer_tache_fond
from ba38_planning.esope import donnees as D

PREFIXE_TEST = "t-"
PREFIXE_REEL = "p-"
MAX_ENVOIS_TEST = 2

RESPONSABLE = os.getenv("ESOPE_MAIL_RESPONSABLE", "ba380.esoperesponsable@banquealimentaire.org")
# Expéditeur : adresse de service validée chez Mailjet (une adresse non validée
# est rejetée silencieusement) ; les réponses partent vers la responsable.
EXPEDITEUR = os.getenv("ESOPE_MAIL_EXPEDITEUR", "ba380@banquealimentaire.org")
NOM_EXPEDITEUR = "ESOPE – Banque Alimentaire de l'Isère"

STATUTS_PERIODE = {
    "brouillon": "Brouillon",
    "envoye": "Envoyé aux bénévoles",
    "verrouille": "Verrouillé",
}


# ─────────────────────────────────────────────────────────────
# Bases et jetons
# ─────────────────────────────────────────────────────────────

def chemin_base(test):
    base_dir = os.getenv("BA38_BASE_DIR")
    fichier = os.getenv("SQLITE_DB_TEST" if test else "SQLITE_DB")
    if not base_dir or not fichier:
        raise RuntimeError("BA38_BASE_DIR / SQLITE_DB non définis")
    return os.path.join(base_dir, fichier)


def connexion(test):
    conn = sqlite3.connect(chemin_base(test))
    conn.row_factory = sqlite3.Row
    return conn


def jeton_de_test(token):
    return (token or "").startswith(PREFIXE_TEST)


def connexion_token(token):
    """Connexion à la base du jeton, ou None si le jeton est mal formé."""
    if not token or len(token) < 20:
        return None
    if token.startswith(PREFIXE_TEST):
        return connexion(True)
    if token.startswith(PREFIXE_REEL):
        return connexion(False)
    return None


def session_en_base_test():
    try:
        return bool(session.get("test_user"))
    except RuntimeError:
        return False


def url_lien(token):
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    return f"{base}/planning/esope/c/{token}"


def lien_benevole(conn, benevole_id, mois):
    """Lien du bénévole pour le mois (créé au besoin)."""
    l = conn.execute("SELECT * FROM esope_liens WHERE benevole_id = ? AND mois = ?", (benevole_id, mois)).fetchone()
    if l:
        return l
    prefixe = PREFIXE_TEST if session_en_base_test() else PREFIXE_REEL
    conn.execute("INSERT INTO esope_liens (benevole_id, mois, token) VALUES (?, ?, ?)",
                 (benevole_id, mois, prefixe + secrets.token_urlsafe(24)))
    return conn.execute("SELECT * FROM esope_liens WHERE benevole_id = ? AND mois = ?", (benevole_id, mois)).fetchone()


# ─────────────────────────────────────────────────────────────
# Période (planning d'un mois)
# ─────────────────────────────────────────────────────────────

def periode(conn, mois):
    p = conn.execute("SELECT * FROM esope_periodes WHERE mois = ?", (mois,)).fetchone()
    if p:
        return dict(p)
    return {"mois": mois, "statut": "brouillon", "envoye_le": None, "envoye_par": None,
            "verrouille_le": None, "verrouille_par": None}


def changer_statut_periode(conn, mois, statut, utilisateur):
    conn.execute("INSERT OR IGNORE INTO esope_periodes (mois) VALUES (?)", (mois,))
    maintenant = datetime.now().isoformat(sep=" ", timespec="seconds")
    if statut == "envoye":
        conn.execute("UPDATE esope_periodes SET statut = CASE WHEN statut = 'verrouille' THEN statut ELSE 'envoye' END, "
                     "envoye_le = COALESCE(envoye_le, ?), envoye_par = ? WHERE mois = ?", (maintenant, utilisateur, mois))
    elif statut == "verrouille":
        conn.execute("UPDATE esope_periodes SET statut = 'verrouille', verrouille_le = ?, verrouille_par = ? "
                     "WHERE mois = ?", (maintenant, utilisateur, mois))
    else:  # déverrouillage → envoyé (ou brouillon si jamais envoyé)
        conn.execute("UPDATE esope_periodes SET statut = CASE WHEN envoye_le IS NULL THEN 'brouillon' ELSE 'envoye' END, "
                     "verrouille_le = NULL, verrouille_par = NULL WHERE mois = ?", (mois,))


def libelle_mois(mois):
    a, m = D.parse_mois(mois)
    return f"{D.MOIS[m - 1]} {a}"


# ─────────────────────────────────────────────────────────────
# Créneaux d'un bénévole
# ─────────────────────────────────────────────────────────────

def creneaux_benevole(conn, benevole_id, mois=None, jour=None):
    """Affectations d'un bénévole sur un mois (ou un jour), avec le détail du créneau."""
    if jour:
        debut = fin = jour.isoformat()
    else:
        a, m = D.parse_mois(mois)
        debut = date(a, m, 1).isoformat()
        fin = date(a, m, calendar.monthrange(a, m)[1]).isoformat()
    rows = conn.execute("""
        SELECT a.*, c.date, c.heure_debut AS c_debut, c.heure_fin AS c_fin, c.ferme, c.libelle AS c_libelle,
               c.lieu, c.poste, c.creneau_type_id, f.nom AS fonction
        FROM esope_affectations a
        JOIN esope_creneaux c ON c.id = a.creneau_id
        LEFT JOIN esope_fonctions f ON f.id = c.fonction_id
        WHERE a.benevole_id = ? AND c.date BETWEEN ? AND ?
        ORDER BY c.date, c.heure_debut
    """, (benevole_id, debut, fin)).fetchall()
    res = []
    for r in rows:
        d = dict(r)
        jd = date.fromisoformat(d["date"])
        d["date_txt"] = D.fmt_date_courte(jd).capitalize()
        d["passe"] = jd < date.today()
        debut_h = d["heure_debut"] or d["c_debut"]
        fin_h = d["heure_fin"] or d["c_fin"]
        d["plage"] = D.fmt_plage(debut_h, fin_h)
        d["partiel"] = bool(d["heure_debut"] or d["heure_fin"])
        if d["creneau_type_id"] is None:
            d["intitule"] = d["c_libelle"] + (f" ({d['lieu']})" if d["lieu"] else "")
        else:
            d["intitule"] = d["fonction"] + (f" – {d['poste']}" if d["poste"] else "") + (
                f" · {d['c_libelle']}" if d["c_libelle"] else "")
        res.append(d)
    return res


def html_creneaux(creneaux):
    """Liste HTML des créneaux pour les mails."""
    actifs = [c for c in creneaux if c["statut"] != "annule" and not c["ferme"]]
    if not actifs:
        return ("<p><i>Vous n'avez pas de créneau prévu ce mois-ci. N'hésitez pas à nous indiquer "
                "vos disponibilités avec le lien ci-dessous.</i></p>")
    lignes = "".join(
        f"<li><b>{c['date_txt']}</b> : {c['intitule']}, {c['plage']}"
        f"{' (présence partielle)' if c['partiel'] else ''}</li>" for c in actifs)
    return f"<ul>{lignes}</ul>"


def contexte_mail(benevole, mois, creneaux, token):
    return {
        "prenom": benevole["prenom"] or "",
        "nom": benevole["nom"] or "",
        "mois": libelle_mois(mois),
        "creneaux": html_creneaux(creneaux),
        "lien": url_lien(token),
    }


def modele_mail(conn, code):
    r = conn.execute("SELECT sujet, corps FROM modeles_emails WHERE code_modele = ?", (code,)).fetchone()
    if not r:
        raise RuntimeError(f"Modèle de mail {code} absent (lancer scripts/migrate_planning_esope.py)")
    return r["sujet"], r["corps"]


def rendre(texte, contexte):
    return render_modele_email(texte, contexte)


# ─────────────────────────────────────────────────────────────
# Alertes à la responsable
# ─────────────────────────────────────────────────────────────

def _envoyer_alerte(app, sujet, corps, destinataire, adresse_testeur):
    with app.app_context():
        try:
            envoyer_mail(sujet, [destinataire], corps, sender_override=EXPEDITEUR, sender_name=NOM_EXPEDITEUR,
                         is_html=True, current_user_email=adresse_testeur)
        except Exception as e:  # une alerte ratée ne doit jamais bloquer la réponse du bénévole
            write_log(f"❌ ESOPE alerte non envoyée ({sujet}) : {e}")


def alerter_responsable(conn, token, lien, benevole, lignes, commentaire=None):
    """Mail à la responsable après une réponse du bénévole.
    lignes : liste de textes décrivant les changements."""
    p = periode(conn, lien["mois"])
    testeur = p.get("envoye_par") or lien["envoye_par"] or os.getenv("MAIL_TEST_TO")
    test = jeton_de_test(token) or bool(lien["envoi_mode_test"])
    destinataire = (testeur or RESPONSABLE) if test else RESPONSABLE
    nom = f"{benevole['prenom'] or ''} {benevole['nom'] or ''}".strip()
    sujet = f"{'[TEST] ' if test else ''}ESOPE – réponse de {nom} ({libelle_mois(lien['mois'])})"
    a, m = D.parse_mois(lien["mois"])
    url_planning = os.getenv("APP_BASE_URL", "").rstrip("/") + "/planning/esope/envoi?mois=" + lien["mois"]
    items = "".join(f"<li>{l}</li>" for l in lignes)
    corps = (f"<p><b>{nom}</b> a répondu sur son lien personnel :</p><ul>{items}</ul>"
             + (f"<p>Commentaire : <i>{commentaire}</i></p>" if commentaire else "")
             + f"<p><a href=\"{url_planning}\">Voir le suivi des réponses de {D.MOIS[m - 1]} {a}</a></p>")
    lancer_tache_fond(target=_envoyer_alerte,
                      args=(current_app._get_current_object(), sujet, corps, destinataire, testeur),
                      nom="Alerte ESOPE", utilisateur=None)
