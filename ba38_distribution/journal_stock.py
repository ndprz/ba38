# ba38_distribution/journal_stock.py
"""Journal des mouvements de stock de Basilic (une ligne par mouvement et par lot).

Alimenté à chaque validation de document :
  - ramasse journalière validée  -> entrées (type RA, une ligne par article/lot) ;
  - bon de livraison validé      -> sorties (une ligne par lot affecté ; lot vide si la ligne part sans lot).
Une réouverture (ramasse ou BL) ANNULE les lignes du document (annule = 1, conservées pour l'audit) ;
la re-validation en crée de nouvelles. La table est créée à la demande (aucune migration préalable).

Ce journal est une trace : le calcul du stock Basilic (stock_basilic.py) continue de partir des documents."""

JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS mouvements_stock (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date_mvt     TEXT NOT NULL,                 -- horodatage UTC (comme CURRENT_TIMESTAMP)
    type_mvt     TEXT NOT NULL,                 -- RA (réception ramasse) | BL (livraison)
    sens         TEXT NOT NULL,                 -- E (entrée) | S (sortie)
    article      TEXT NOT NULL,
    libelle      TEXT,
    lot          TEXT NOT NULL DEFAULT '',
    depot        TEXT,
    quantite     REAL NOT NULL,
    unite        TEXT NOT NULL DEFAULT 'Kg',
    doc_type     TEXT NOT NULL,                 -- ramasse_jour | bon_prepa
    doc_id       INTEGER NOT NULL,
    doc_ref      TEXT,                          -- n° / nom de fichier du document
    tiers_type   TEXT,                          -- fournisseur | association
    tiers_id     INTEGER,
    tiers_nom    TEXT,
    tiers_code   TEXT,                          -- code VIF
    cree_par     TEXT,
    annule       INTEGER NOT NULL DEFAULT 0,
    annule_le    TEXT
);
CREATE INDEX IF NOT EXISTS idx_mvts_doc ON mouvements_stock(doc_type, doc_id);
CREATE INDEX IF NOT EXISTS idx_mvts_article ON mouvements_stock(article, lot);
CREATE INDEX IF NOT EXISTS idx_mvts_date ON mouvements_stock(date_mvt);
"""

DEPOT = "03"


def assurer_table(conn):
    for ordre in JOURNAL_DDL.split(";"):
        if ordre.strip():
            conn.execute(ordre)


def annuler_document(conn, doc_type: str, doc_id: int):
    """Marque comme annulés les mouvements encore actifs d'un document (réouverture)."""
    assurer_table(conn)
    conn.execute("""UPDATE mouvements_stock SET annule = 1, annule_le = CURRENT_TIMESTAMP
                    WHERE doc_type = ? AND doc_id = ? AND annule = 0""", (doc_type, doc_id))


def _deja(conn, doc_type: str, doc_id: int) -> bool:
    return conn.execute("SELECT 1 FROM mouvements_stock WHERE doc_type = ? AND doc_id = ? AND annule = 0 LIMIT 1",
                        (doc_type, doc_id)).fetchone() is not None


def enregistrer_ramasse(conn, ramasse_id: int, date_mvt: str | None = None) -> int:
    """Entrées d'une ramasse validée. Idempotent (rien si le document est déjà au journal). Retourne le nombre de lignes."""
    assurer_table(conn)
    if _deja(conn, "ramasse_jour", ramasse_id):
        return 0
    r = conn.execute("""SELECT r.*, f.nom AS f_nom, f.code_vif AS f_code FROM ramasses_jour r
                        LEFT JOIN fournisseurs f ON f.id = r.id_fournisseur WHERE r.id = ? AND r.statut = 'validee'""",
                     (ramasse_id,)).fetchone()
    if not r:
        return 0
    n = 0
    for l in conn.execute("""SELECT article, libelle, COALESCE(lot, '') AS lot, quantite_kg FROM ramasses_jour_lignes
                             WHERE ramasse_id = ? AND quantite_kg > 0 ORDER BY ordre, id""", (ramasse_id,)):
        conn.execute("""INSERT INTO mouvements_stock (date_mvt, type_mvt, sens, article, libelle, lot, depot, quantite, unite,
                          doc_type, doc_id, doc_ref, tiers_type, tiers_id, tiers_nom, tiers_code, cree_par)
                        VALUES (COALESCE(?, CURRENT_TIMESTAMP),'RA','E',?,?,?,?,?,'Kg','ramasse_jour',?,?,'fournisseur',?,?,?,?)""",
                     (date_mvt or r["valide_le"], l["article"], l["libelle"], l["lot"], DEPOT, l["quantite_kg"], ramasse_id,
                      r["reception_fichier"] or "", r["id_fournisseur"], r["f_nom"], r["f_code"], r["valide_par"]))
        n += 1
    return n


def enregistrer_bl(conn, bon_id: int, date_mvt: str | None = None) -> int:
    """Sorties d'un bon validé en BL (une ligne par lot affecté, ou par ligne sans lot). Idempotent."""
    assurer_table(conn)
    if _deja(conn, "bon_prepa", bon_id):
        return 0
    b = conn.execute("""SELECT b.*, a.nom_association, a.code_VIF FROM bons_preparation b
                        JOIN associations a ON a.id = b.association_id
                        WHERE b.id = ? AND b.statut = 'bl'""", (bon_id,)).fetchone()
    if not b:
        return 0
    n = 0
    for l in conn.execute("SELECT * FROM bons_preparation_lignes WHERE bon_id = ? ORDER BY ordre, id", (bon_id,)):
        q = l["quantite_livree"] if l["quantite_livree"] is not None else l["quantite"]
        if not q or q <= 0:
            continue
        lots = conn.execute("SELECT lot, depot, quantite FROM bons_preparation_lots WHERE ligne_id = ? ORDER BY id",
                            (l["id"],)).fetchall()
        sorties = [(t["lot"], t["depot"] or DEPOT, t["quantite"]) for t in lots] or [("", DEPOT, q)]
        for lot, depot, qte in sorties:
            conn.execute("""INSERT INTO mouvements_stock (date_mvt, type_mvt, sens, article, libelle, lot, depot, quantite, unite,
                              doc_type, doc_id, doc_ref, tiers_type, tiers_id, tiers_nom, tiers_code, cree_par)
                            VALUES (COALESCE(?, CURRENT_TIMESTAMP),'BL','S',?,?,?,?,?,?,'bon_prepa',?,?,'association',?,?,?,?)""",
                         (date_mvt or b["valide_le"], l["article"], l["libelle"], lot, depot, qte, l["unite"] or "Kg",
                          bon_id, b["bl_fichier"] or "", b["association_id"], b["nom_association"], b["code_VIF"], b["valide_par"]))
            n += 1
    return n


def reprendre_historique(conn) -> dict:
    """Reprise des ramasses et BL déjà validés (idempotent : un document déjà au journal n'est pas recréé)."""
    assurer_table(conn)
    res = {"ramasses": 0, "bl": 0, "lignes": 0}
    for (rid,) in conn.execute("SELECT id FROM ramasses_jour WHERE statut = 'validee' ORDER BY valide_le, id").fetchall():
        n = enregistrer_ramasse(conn, rid)
        res["ramasses"] += 1 if n else 0
        res["lignes"] += n
    for (bid,) in conn.execute("SELECT id FROM bons_preparation WHERE statut = 'bl' ORDER BY valide_le, id").fetchall():
        n = enregistrer_bl(conn, bid)
        res["bl"] += 1 if n else 0
        res["lignes"] += n
    conn.commit()
    return res


def enregistrer_transfert_frais(conn, ajout_id: int) -> int:
    """Transfert 02 -> 03 lié à un lot choisi dans « Produits ajoutables » : une sortie du dépôt 02 et une entrée au
    dépôt 03 (kg brut). Remplace les lignes précédentes du même transfert (quantité modifiée)."""
    assurer_table(conn)
    annuler_document(conn, "transfert_frais", ajout_id)
    a = conn.execute("""SELECT f.*, ar.libelle FROM frais_ajouts_lots f LEFT JOIN articles ar ON ar.article = f.article
                        WHERE f.id = ? AND f.depot = '02' AND f.transfert_kg > 0""", (ajout_id,)).fetchone()
    if not a:
        return 0
    ref = f"MVT{a['date_distribution'].replace('-', '')}FRAIS"
    for sens, depot in (("S", "02"), ("E", "03")):
        conn.execute("""INSERT INTO mouvements_stock (date_mvt, type_mvt, sens, article, libelle, lot, depot, quantite, unite,
                          doc_type, doc_id, doc_ref, tiers_type, tiers_id, tiers_nom, tiers_code, cree_par)
                        VALUES (CURRENT_TIMESTAMP,'TR',?,?,?,?,?,?,'Kg','transfert_frais',?,?,NULL,NULL,'Transfert 02 vers 03',NULL,?)""",
                     (sens, a["article"], a["libelle"], a["lot"], depot, a["transfert_kg"], ajout_id, ref, a["cree_par"]))
    return 2
