# ba38_distribution/stock_basilic.py
"""Stock « Basilic » = dernier stock VIF reçu (table stocks, renouvelée chaque nuit)
+ ce qui s'est passé dans Basilic depuis ce stock :
  - entrées : ramasses journalières validées (lot RJ…) ;
  - sorties : bons de livraison validés (lots affectés).
VIF reçoit des fichiers d'import ; le stock revenu le lendemain l'intègre et ces mouvements cessent de compter."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
DEPOT_SORTIE = "03"


def _paris_vers_utc(txt: str) -> str:
    d = datetime.strptime(txt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=PARIS)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def snapshot_utc(conn) -> str:
    """Instant (UTC, même format que CURRENT_TIMESTAMP) à partir duquel les mouvements Basilic
    ne sont pas encore dans le stock VIF : date d'envoi du mail STKA38 si connue, sinon
    minuit du lendemain de la date de stock enregistrée (repli)."""
    try:
        r = conn.execute("SELECT date_import, date_mail FROM stock_meta WHERE id = 1").fetchone()
    except Exception:          # colonne date_mail absente (ancien schéma)
        r = conn.execute("SELECT date_import, NULL FROM stock_meta WHERE id = 1").fetchone()
    if r and r[1]:
        try:
            return _paris_vers_utc(r[1])
        except ValueError:
            pass
    if r and r[0]:
        try:
            d = datetime.strptime(r[0], "%d/%m/%Y") + timedelta(days=1)
            return _paris_vers_utc(d.strftime("%Y-%m-%d 00:00:00"))
        except ValueError:
            pass
    return "1970-01-01 00:00:00"


def entrees_ramasse(conn, article: str | None = None) -> list[dict]:
    """Entrées en stock (kg) par article/lot : ramasses journalières validées depuis le stock VIF.
    Un lot vide = produit non loti (une seule ligne de stock par article et dépôt)."""
    params = [snapshot_utc(conn)]
    filtre = ""
    if article:
        filtre, params = " AND l.article = ?", params + [article]
    return [dict(r) for r in conn.execute(f"""
        SELECT l.article, l.libelle, COALESCE(l.lot, '') AS lot, MIN(NULLIF(l.dlc, '')) AS dlc, SUM(l.quantite_kg) AS kg
        FROM ramasses_jour_lignes l JOIN ramasses_jour r ON r.id = l.ramasse_id
        WHERE r.statut = 'validee' AND r.valide_le > ?{filtre}
        GROUP BY l.article, COALESCE(l.lot, '')""", params)]


def sorties_bl(conn, article: str, unite: str, exclure_bon_id: int = 0) -> dict:
    """{(lot, depot): quantité} affectée par les BL validés depuis le stock VIF (dans l'unité demandée)."""
    return {(r["lot"], r["depot"] or ""): r["q"] or 0 for r in conn.execute("""
        SELECT t.lot, t.depot, SUM(t.quantite) q FROM bons_preparation_lots t
        JOIN bons_preparation_lignes l ON l.id = t.ligne_id
        JOIN bons_preparation b ON b.id = l.bon_id
        WHERE l.article = ? AND t.unite = ? AND b.statut = 'bl' AND b.valide_le > ? AND b.id != ?
        GROUP BY t.lot, t.depot""", (article, unite, snapshot_utc(conn), exclure_bon_id))}
