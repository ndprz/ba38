"""Planning ESOPE, phase 1 : rappel par mail la veille du créneau.

Lancé une fois par jour par une tâche planifiée (scripts/esope_rappels.py).
Un seul mail par bénévole, avec tous ses créneaux du lendemain. Seuls les mois
envoyés ou verrouillés sont concernés, et seules les affectations prévues ou
confirmées sur un créneau ouvert. Un rappel n'est jamais envoyé deux fois
(esope_affectations.rappel_envoye_le).
"""

from datetime import date, datetime, timedelta

from ba38_utilitaires.core import envoyer_mail
from ba38_planning.esope import liens as L


def envoyer_rappels(jour=None, test_db=False, simulation=False, journal=print):
    """Envoie les rappels pour les créneaux du jour `jour` (demain par défaut).
    Retourne (nb_mails, nb_erreurs)."""
    jour = jour or (date.today() + timedelta(days=1))
    conn = L.connexion(test_db)
    try:
        mois = jour.strftime("%Y-%m")
        p = L.periode(conn, mois)
        if p["statut"] not in ("envoye", "verrouille"):
            journal(f"ESOPE rappels {jour} : planning de {mois} non envoyé ({p['statut']}), rien à faire")
            return 0, 0
        benevoles = conn.execute("""
            SELECT DISTINCT b.id, b.nom, b.prenom, b.email
            FROM esope_affectations a
            JOIN esope_creneaux c ON c.id = a.creneau_id
            JOIN benevoles b ON b.id = a.benevole_id
            WHERE c.date = ? AND c.ferme = 0 AND a.statut IN ('prevu', 'confirme') AND a.rappel_envoye_le IS NULL
        """, (jour.isoformat(),)).fetchall()
        sujet_m, corps_m = L.modele_mail(conn, "esope_rappel")
        nb = ko = 0
        for b in benevoles:
            if not b["email"] or "@" not in b["email"]:
                journal(f"  – {b['nom']} {b['prenom']} : pas de mail")
                continue
            creneaux = [c for c in L.creneaux_benevole(conn, b["id"], jour=jour)
                        if c["statut"] in ("prevu", "confirme") and not c["ferme"] and not c["rappel_envoye_le"]]
            if not creneaux:
                continue
            lien = conn.execute("SELECT * FROM esope_liens WHERE benevole_id = ? AND mois = ?", (b["id"], mois)).fetchone()
            if not lien:
                import secrets
                prefixe = L.PREFIXE_TEST if test_db else L.PREFIXE_REEL
                conn.execute("INSERT INTO esope_liens (benevole_id, mois, token) VALUES (?, ?, ?)",
                             (b["id"], mois, prefixe + secrets.token_urlsafe(24)))
                lien = conn.execute("SELECT * FROM esope_liens WHERE benevole_id = ? AND mois = ?",
                                    (b["id"], mois)).fetchone()
            ctx = L.contexte_mail(b, mois, creneaux, lien["token"])
            sujet = L.rendre(sujet_m, ctx).strip()
            # base de test : jamais vers le vrai bénévole
            destinataire = (p["envoye_par"] or "ba380.informatique2@banquealimentaire.org") if test_db else b["email"]
            if simulation:
                journal(f"  [simulation] {b['nom']} {b['prenom']} → {destinataire} : {len(creneaux)} créneau(x)")
                nb += 1
                continue
            try:
                envoyer_mail(("[TEST] " if test_db else "") + sujet, [destinataire], L.rendre(corps_m, ctx),
                             sender_override=L.EXPEDITEUR, sender_name=L.NOM_EXPEDITEUR, reply_to=L.RESPONSABLE,
                             is_html=True, current_user_email=p["envoye_par"])
                maintenant = datetime.now().isoformat(sep=" ", timespec="seconds")
                conn.executemany("UPDATE esope_affectations SET rappel_envoye_le = ? WHERE id = ?",
                                 [(maintenant, c["id"]) for c in creneaux])
                conn.commit()
                nb += 1
                journal(f"  ✉ {b['nom']} {b['prenom']} : {len(creneaux)} créneau(x)")
            except Exception as e:
                ko += 1
                journal(f"  ❌ {b['nom']} {b['prenom']} : {e}")
        journal(f"ESOPE rappels {jour} : {nb} mail(s){' (simulation)' if simulation else ''}, {ko} erreur(s)")
        return nb, ko
    finally:
        conn.close()
