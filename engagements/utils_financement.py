"""
utils_financement.py
=====================

Calcul du montant utilisé d'un financement (subvention) à partir
de la somme réelle des engagements qui lui sont rattachés (tous
statuts sauf refusé), plutôt que depuis une saisie manuelle qui
se désynchronise des engagements réels.
"""

from decimal import Decimal


def calculer_montants_utilises(conn, subvention_ids):
    """
    Retourne {subvention_id: Decimal} = somme des engagements
    (non supprimés, non refusés) rattachés à chaque subvention.
    """

    subvention_ids = [sid for sid in subvention_ids if sid]

    if not subvention_ids:
        return {}

    placeholders = ",".join("?" for _ in subvention_ids)

    rows = conn.execute(f"""
        SELECT
            d.subvention_id,
            SUM(d.montant_total) AS montant_utilise
        FROM engagements_depenses d
        JOIN engagements e
            ON e.id = d.engagement_id
        WHERE d.subvention_id IN ({placeholders})
          AND COALESCE(e.deleted, 0) = 0
          AND e.statut != 'refuse'
        GROUP BY d.subvention_id
    """, subvention_ids).fetchall()

    return {
        r["subvention_id"]: Decimal(str(r["montant_utilise"] or 0))
        for r in rows
    }


def calculer_montant_utilise(conn, subvention_id):
    """Version unitaire pour une seule subvention."""

    return calculer_montants_utilises(
        conn,
        [subvention_id]
    ).get(subvention_id, Decimal("0"))
