# Mode « premier inscrit, premier servi » : la place est attribuée à l'inscription,
# au-delà du nombre de billets l'inscrit va en liste d'attente ; un désistement libère
# une place attribuée automatiquement au premier de la liste d'attente.
from ba38_billets import mails
from ba38_utilitaires.core import write_log


def _debut_transaction(conn):
    """Verrou d'écriture : deux inscriptions simultanées ne peuvent pas prendre la même dernière place."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def nb_gagnants(conn, campagne_id):
    return conn.execute("SELECT COUNT(*) FROM billets_participants WHERE campagne_id=? AND resultat='gagnant'",
                        (campagne_id,)).fetchone()[0]


def _origine(p):
    return "salarie" if p["categorie"] == "salarie" else "inscrit"


def inscrire(conn, c, p, par=None):
    """Inscrit la personne et lui attribue une place ('gagnant') ou un rang d'attente ('attente').
    Retourne le résultat. Le commit est fait par l'appelant."""
    _debut_transaction(conn)
    cur = conn.execute("SELECT etat, resultat FROM billets_participants WHERE id=?", (p["id"],)).fetchone()
    if cur["etat"] == "inscrit" and cur["resultat"]:
        return cur["resultat"]
    res = "gagnant" if nb_gagnants(conn, c["id"]) < c["nb_billets"] else "attente"
    rang = (conn.execute("SELECT COALESCE(MAX(ordre_tirage),0) FROM billets_participants WHERE campagne_id=?",
                         (c["id"],)).fetchone()[0] or 0) + 1
    conn.execute(
        """UPDATE billets_participants SET etat='inscrit', inscrit_le=?, inscrit_par=?, resultat=?,
             origine_place=?, ordre_tirage=?, confirmation_le=NULL, resultat_envoye_le=NULL WHERE id=?""",
        (mails.maintenant(), par, res, _origine(p), rang, p["id"]))
    return res


def promouvoir(conn, c):
    """Attribue les places libres aux premiers de la liste d'attente. Retourne les lignes promues."""
    promus = []
    while nb_gagnants(conn, c["id"]) < c["nb_billets"]:
        w = conn.execute(
            "SELECT * FROM billets_participants WHERE campagne_id=? AND resultat='attente' "
            "ORDER BY ordre_tirage, id LIMIT 1", (c["id"],)).fetchone()
        if not w:
            break
        conn.execute("UPDATE billets_participants SET resultat='gagnant', origine_place=? WHERE id=?",
                     (_origine(w), w["id"]))
        promus.append(w["id"])
    return promus


def desinscrire(conn, c, pid):
    """Désinscrit (désistement) et fait remonter la liste d'attente. Retourne les ids promus."""
    _debut_transaction(conn)
    conn.execute("UPDATE billets_participants SET etat='desiste', resultat=NULL, origine_place=NULL, "
                 "ordre_tirage=NULL WHERE id=? AND campagne_id=?", (pid, c["id"]))
    return promouvoir(conn, c)


def rang_attente(conn, p):
    """Position (1 = prochain servi) d'une personne en liste d'attente."""
    return conn.execute(
        "SELECT COUNT(*) + 1 FROM billets_participants WHERE campagne_id=? AND resultat='attente' "
        "AND ordre_tirage < ?",
        (p["campagne_id"], p["ordre_tirage"])).fetchone()[0]


def prevenir_promus(conn, c, ids):
    """Mail « une place s'est libérée » aux promus (après commit). Les erreurs sont journalisées."""
    for pid in ids:
        p = conn.execute("SELECT * FROM billets_participants WHERE id=?", (pid,)).fetchone()
        if not p or not p["email"]:
            continue
        try:
            sujet, corps = mails.mail_place_liberee(c, p)
            mails.envoyer(c, p["email"], sujet, corps)
            conn.execute("UPDATE billets_participants SET confirmation_le=?, resultat_envoye_le=? WHERE id=?",
                         (mails.maintenant(), mails.maintenant(), pid))
            conn.commit()
        except Exception as e:
            write_log(f"❌ Billets place libérée → {p['email']} : {e}")
            conn.execute("UPDATE billets_participants SET resultat_erreur=? WHERE id=?", (str(e)[:400], pid))
            conn.commit()
