"""Cotisations bénévoles par année — table `benevoles_cotisations`.

Une ligne = un bénévole à jour de cotisation pour une année (absence de ligne =
pas à jour). Plus aucune colonne `cotisation_AAAA` à créer : les années sont
déduites des données + l'année en cours.

Pour que les écrans génériques (liste, générateur Excel, filtres, profils
enregistrés) continuent de fonctionner, les années sont exposées sous forme de
champs « virtuels » nommés `cotisation_AAAA` (oui/non), comme avant.
"""
import re
from datetime import date

GROUPE = "vie association"
_RE_CHAMP = re.compile(r"^cotisation_(\d{4})$")


def assurer_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS benevoles_cotisations (
            id_benevole INTEGER NOT NULL,
            annee INTEGER NOT NULL,
            date_maj TEXT,
            user_maj TEXT,
            date_paiement TEXT,
            PRIMARY KEY (id_benevole, annee)
        )
    """)
    if "date_paiement" not in {r[1] for r in conn.execute("PRAGMA table_info(benevoles_cotisations)")}:
        conn.execute("ALTER TABLE benevoles_cotisations ADD COLUMN date_paiement TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bc_annee ON benevoles_cotisations(annee)")


def annee_du_champ(field_name):
    """2026 pour 'cotisation_2026', sinon None."""
    m = _RE_CHAMP.match(field_name or "")
    return int(m.group(1)) if m else None


def annee_affichee():
    """Année en cours ; l'année suivante apparaît dès le 1er décembre."""
    today = date.today()
    return today.year + 1 if today.month == 12 else today.year


def annees(conn):
    """Toutes les années de la première cotisation à max(dernière, année affichée)."""
    row = conn.execute("SELECT MIN(annee), MAX(annee) FROM benevoles_cotisations").fetchone()
    courante = annee_affichee()
    if not row or row[0] is None:
        return [courante]
    return list(range(row[0], max(row[1], courante) + 1))


def champs_virtuels(conn):
    """Champs `cotisation_AAAA` (dicts façon field_groups), ordre d'affichage croissant."""
    return [{
        "field_name": f"cotisation_{a}",
        "group_name": GROUPE,
        "display_order": 2000 + a,
        "type_champ": "oui_non",
        "is_required": 0,
        "virtuel": True,
    } for a in annees(conn)]


def expression_sql(field_name, alias_benevoles="benevoles"):
    """Sous-requête 'oui'/'non' pour un champ virtuel (nom validé : pas d'injection)."""
    a = annee_du_champ(field_name)
    if a is None:
        return None
    return (f"(CASE WHEN EXISTS (SELECT 1 FROM benevoles_cotisations bc "
            f"WHERE bc.id_benevole = {alias_benevoles}.id AND bc.annee = {a}) THEN 'oui' ELSE 'non' END)")


def a_jour_par_annee(conn, id_benevole):
    return {r[0] for r in conn.execute(
        "SELECT annee FROM benevoles_cotisations WHERE id_benevole = ?", (id_benevole,))}


def ids_a_jour(conn, annee):
    """Ensemble des ids de bénévoles à jour pour l'année."""
    return {r[0] for r in conn.execute(
        "SELECT id_benevole FROM benevoles_cotisations WHERE annee = ?", (int(annee),))}


def dates_par_annee(conn, id_benevole):
    """{annee: date_paiement ISO ou ''} pour un bénévole."""
    return {r[0]: r[1] or "" for r in conn.execute(
        "SELECT annee, date_paiement FROM benevoles_cotisations WHERE id_benevole = ?", (id_benevole,))}


def annees_par_benevole(conn):
    """{id_benevole: {annee: date_paiement}} pour tous (liste/tableau)."""
    res = {}
    for bid, a, d in conn.execute("SELECT id_benevole, annee, date_paiement FROM benevoles_cotisations"):
        res.setdefault(bid, {})[a] = d or ""
    return res


def fixer(conn, id_benevole, annee, a_jour, user=None, date_paiement=None):
    """Coche/décoche une année pour un bénévole. Pas de commit.

    date_paiement (ISO) : enregistrée à la coche ; si None/vide, la date déjà
    enregistrée est conservée. Décocher supprime la ligne (et donc la date)."""
    from datetime import datetime
    if not a_jour:
        conn.execute("DELETE FROM benevoles_cotisations WHERE id_benevole = ? AND annee = ?",
                     (id_benevole, int(annee)))
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute("SELECT date_paiement FROM benevoles_cotisations WHERE id_benevole = ? AND annee = ?",
                       (id_benevole, int(annee))).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO benevoles_cotisations (id_benevole, annee, date_maj, user_maj, date_paiement) VALUES (?,?,?,?,?)",
            (id_benevole, int(annee), now, user, date_paiement or None))
    elif date_paiement and date_paiement != (row[0] or ""):
        conn.execute(
            "UPDATE benevoles_cotisations SET date_paiement = ?, date_maj = ?, user_maj = ? "
            "WHERE id_benevole = ? AND annee = ?",
            (date_paiement, now, user, id_benevole, int(annee)))


def supprimer_benevole(conn, id_benevole):
    conn.execute("DELETE FROM benevoles_cotisations WHERE id_benevole = ?", (id_benevole,))
