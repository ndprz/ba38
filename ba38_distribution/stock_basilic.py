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
        WHERE r.statut = 'validee' AND r.valide_le > ? AND l.quantite_kg > 0{filtre}
        GROUP BY l.article, COALESCE(l.lot, '')""", params)]


def sorties_bl(conn, article: str, unite: str, exclure_bon_id: int = 0) -> dict:
    """{(lot, depot): quantité} affectée par les BL validés depuis le stock VIF (dans l'unité demandée)."""
    return {(r["lot"], r["depot"] or ""): r["q"] or 0 for r in conn.execute("""
        SELECT t.lot, t.depot, SUM(t.quantite) q FROM bons_preparation_lots t
        JOIN bons_preparation_lignes l ON l.id = t.ligne_id
        JOIN bons_preparation b ON b.id = l.bon_id
        WHERE l.article = ? AND t.unite = ? AND b.statut = 'bl' AND b.valide_le > ? AND b.id != ?
        GROUP BY t.lot, t.depot""", (article, unite, snapshot_utc(conn), exclure_bon_id))}


AJOUTS_LOTS_DDL = """
CREATE TABLE IF NOT EXISTS frais_ajouts_lots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date_distribution TEXT NOT NULL,
    article          TEXT NOT NULL,
    lot              TEXT NOT NULL DEFAULT '',
    depot            TEXT NOT NULL,            -- dépôt d'origine du lot : 02 ou 03
    quantite_kg      REAL NOT NULL DEFAULT 0,  -- affectée à la distribution FRAIS du jour (kg brut)
    transfert_kg     REAL NOT NULL DEFAULT 0,  -- transférée du dépôt 02 vers 03 (kg brut) ; >= quantite_kg ; 0 si dépôt 03
    mvt_id           INTEGER,                  -- ligne mvt_stocks_v2 du transfert (BROUILLON puis EXPORTE)
    cree_par         TEXT,
    cree_le          TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (date_distribution, article, lot, depot)
)"""


def assurer_table_ajouts_lots(conn):
    conn.execute(AJOUTS_LOTS_DDL)


def transferts_depuis_snapshot(conn, article: str | None = None) -> list[dict]:
    """Transferts 02 -> 03 faits dans Basilic depuis le dernier stock VIF : [{article, lot, kg}] (kg brut).
    Le stock VIF de la nuit les intègre (une fois le fichier de mouvement importé dans VIF) : ils cessent alors de compter."""
    try:
        filtre, params = "", [snapshot_utc(conn)]
        if article:
            filtre, params = " AND article = ?", params + [article]
        return [dict(r) for r in conn.execute(
            f"SELECT article, lot, SUM(transfert_kg) AS kg FROM frais_ajouts_lots WHERE depot = '02' AND transfert_kg > 0 "
            f"AND cree_le > ?{filtre} GROUP BY article, lot", params)]
    except Exception:   # table pas encore créée
        return []


def lots_ajoutes_frais(conn, article: str, iso: str) -> set:
    """Lots choisis dans « Produits ajoutables » pour la distribution FRAIS du jour : servis en premier avec la ramasse."""
    try:
        return {r[0] for r in conn.execute(
            "SELECT lot FROM frais_ajouts_lots WHERE article = ? AND date_distribution = ? AND quantite_kg > 0", (article, iso))}
    except Exception:
        return set()


def reservations_prepa(conn, article: str, unite: str, exclure_bon_id: int = 0, exclure_frais_iso: str | None = None) -> dict:
    """{(lot, depot): quantité} réservée par les bons de préparation PAS ENCORE validés (statut 'preparation'),
    dans l'unité demandée. La réservation tombe d'elle-même quand le bon passe en BL (les lots deviennent
    alors des sorties, voir sorties_bl) ou quand il est supprimé."""
    return {(r["lot"], r["depot"] or ""): r["q"] or 0 for r in conn.execute("""
        SELECT t.lot, t.depot, SUM(t.quantite) q FROM bons_preparation_lots t
        JOIN bons_preparation_lignes l ON l.id = t.ligne_id
        JOIN bons_preparation b ON b.id = l.bon_id
        WHERE l.article = ? AND t.unite = ? AND b.statut = 'preparation' AND b.id != ?
          AND NOT (b.type_bon = 'frais' AND b.date_distribution = ?)
        GROUP BY t.lot, t.depot""", (article, unite, exclure_bon_id, exclure_frais_iso or ""))}


def reservations_par_lot_kg(conn) -> dict:
    """{(article, lot, depot): kg} réservés par les bons en préparation, lignes en Kg (visualisation des stocks)."""
    return {(r[0], r[1], r[2] or ""): r[3] or 0 for r in conn.execute("""
        SELECT l.article, t.lot, t.depot, SUM(t.quantite) FROM bons_preparation_lots t
        JOIN bons_preparation_lignes l ON l.id = t.ligne_id
        JOIN bons_preparation b ON b.id = l.bon_id
        WHERE t.unite = 'Kg' AND b.statut = 'preparation' GROUP BY l.article, t.lot, t.depot""")}
