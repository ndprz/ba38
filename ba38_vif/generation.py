# ============================================================
# Génération du texte des imports VIF (CSV « ; »)
#
# Fonctions pures vis-à-vis de Flask : elles reçoivent une connexion SQLite
# (row_factory = sqlite3.Row) et renvoient le texte du fichier. Elles
# n'écrivent RIEN en base (la trace exports_vif reste à la charge de
# l'appelant) et lèvent ErreursVif si les données ne permettent pas
# d'importer (code VIF manquant...).
#
# ⚠️ Les formats ci-dessous sont ceux déjà importés à la main dans VIF :
# ne pas les modifier sans validation FFBA (fin de ligne, séparateur décimal
# et nombre de colonnes diffèrent volontairement entre réceptions et BL
# tant que la FFBA n'a pas confirmé l'encodage/format attendu).
# ============================================================

from calendar import monthrange
from datetime import datetime


# Préfixes des fichiers déposés pour VIF : UN par flux, car VIF sélectionne les
# fichiers par motif (« blba38* » ne doit jamais attraper une réception).
# ⚠️ Hypothèse à confirmer par la FFBA (leur modèle BA21 : « blba21* » pour les
# BL) ; les réceptions reprennent le nom du téléchargement manuel actuel.
PREFIXE_BL = "blba38"
PREFIXE_RECEPTIONS = "recepba38"
PREFIXE_MOUVEMENTS = "mvtba38"  # proposition (par analogie), à communiquer à la FFBA


class ErreursVif(Exception):
    """Données non exportables. `messages` : liste de textes lisibles."""

    def __init__(self, messages):
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))


class AucunMouvement(ErreursVif):
    """Rien à exporter (aucune ligne ne correspond à la demande)."""

    def __init__(self):
        super().__init__(["Aucun mouvement à exporter."])


def _decouper_mois(mois):
    if not mois or "-" not in mois:
        raise ErreursVif([f"Format de mois invalide : {mois}"])
    annee, m = mois.split("-")
    return annee, m


def generer_receptions_ramasses(conn, mois, date_livraison):
    """Réceptions VIF (fichier type recepba38.csv) des ramasses partenaires
    d'un mois « AAAA-MM », une ligne par fournisseur × article.

    date_livraison : texte « jj/mm/aaaa » mis dans les 2 colonnes de date
    de livraison prévue (historiquement : aujourd'hui + 2 jours).
    """
    rows = conn.execute("""
        SELECT
            r.id_fournisseur,
            f.code_vif,
            f.nom,
            l.code_vif AS article_code_vif,
            ar.libelle,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l
            ON l.id_ramasse = r.id
        LEFT JOIN fournisseurs f
            ON f.id = r.id_fournisseur
        LEFT JOIN articles ar
            ON ar.article = l.code_vif
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_fournisseur, l.code_vif
        ORDER BY f.nom, l.code_vif
    """, (mois,)).fetchall()

    # 🔴 contrôle VIF
    erreurs = [f"Fournisseur sans code VIF : {r['nom']}" for r in rows if not r["code_vif"]]
    if erreurs:
        raise ErreursVif(erreurs)

    annee, m = _decouper_mois(mois)
    dernier_jour = monthrange(int(annee), int(m))[1]
    date_recep = f"{dernier_jour}/{m}/{annee}"

    lignes_csv = []

    for r in rows:
        article = (r["article_code_vif"] or "").strip()
        libelle = (r["libelle"] or "").lower()

        if "non loti" in libelle:
            lot = ""
        else:
            lot = f"RP{annee[-2:]}{m}{article}"

        ligne = [
            "01",
            "38",
            date_recep,
            r["code_vif"],
            "01",
            "03",
            r["article_code_vif"],
            f"{float(r['total_kg'] or 0):.3f}",
            "KG",
            lot,
            date_livraison,
            date_livraison,
            "",
            "RA"
        ]

        lignes_csv.append(";".join(map(str, ligne)))

    return "\n".join(lignes_csv) + "\n" if lignes_csv else ""


# Variantes du BL des ramasses partenaires
FORMAT_BL_MANUEL = "manuel"  # téléchargement actuel (BL_38.CSV), 17 colonnes
FORMAT_BL_SFTP = "sftp"      # import automatique validé FFBA le 2026-11-05, 12 colonnes


def generer_bl_ramasses(conn, mois, variante=FORMAT_BL_MANUEL):
    """BL VIF des ramasses partenaires d'un mois « AAAA-MM », une ligne par
    association × article, daté du dernier jour du mois.

    variante FORMAT_BL_MANUEL : fichier type BL_38.CSV (17 colonnes, quantité à
    3 décimales, « KG », date jj/mm/aa) — inchangé.
    variante FORMAT_BL_SFTP : fichier type blba38*.csv (12 colonnes, quantité à
    2 décimales, « Kg », date jj/mm/aaaa), format corrigé et validé par la FFBA.
    """
    if variante not in (FORMAT_BL_MANUEL, FORMAT_BL_SFTP):
        raise ValueError(f"Variante de BL inconnue : {variante}")
    rows = conn.execute("""
        SELECT
            r.id_association,
            a.code_vif,
            a.nom_association,
            l.code_vif AS article,
            SUM(l.quantite_kg) AS total_kg
        FROM ramasses_partenaire r
        JOIN ramasses_partenaire_lignes l ON l.id_ramasse = r.id
        LEFT JOIN associations a ON a.id = r.id_association
        WHERE strftime('%Y-%m', r.date_ramasse) = ?
        GROUP BY r.id_association, l.code_vif
        ORDER BY a.code_vif, l.code_vif
    """, (mois,)).fetchall()

    # 🔴 contrôle VIF (AVANT le tri : trier un code_vif NULL plantait en TypeError
    # et masquait ce message, bug de la version d'origine des routes)
    erreurs = []
    for r in rows:
        if not r["code_vif"]:
            erreurs.append(f"Association sans code VIF : {r['nom_association']}")
    if erreurs:
        raise ErreursVif(erreurs)

    rows = sorted(rows, key=lambda x: (x["code_vif"], x["article"] or ""))

    annee, mois_num = _decouper_mois(mois)

    # 🔹 date BL = dernier jour du mois
    dernier_jour = monthrange(int(annee), int(mois_num))[1]
    if variante == FORMAT_BL_SFTP:
        date_bl = f"{dernier_jour:02d}/{mois_num}/{annee}"
    else:
        date_bl = f"{dernier_jour:02d}/{mois_num}/{annee[-2:]}"

    lignes_csv = []

    for r in rows:

        article = (r["article"] or "").strip()

        # Détermination du lot : si le libellé contient "non loti", pas de lot
        row_article = conn.execute("""
            SELECT libelle
            FROM articles
            WHERE article = ?
        """, (article,)).fetchone()

        libelle = (row_article["libelle"] or "").lower() if row_article else ""

        if "non loti" in libelle:
            lot = ""
        else:
            lot = f"RP{annee[-2:]}{mois_num}{article}"

        if variante == FORMAT_BL_SFTP:
            lignes_csv.append(";".join([
                "01",                                               # 1 Société
                "38",                                               # 2 BA
                "01",                                               # 3 Société
                "38",                                               # 4 BA
                "NORM",                                             # 5 Type BL
                r["code_vif"],                                      # 6 Association
                date_bl,                                            # 7 Date BL
                article,                                            # 8 Article
                lot,                                                # 9 NO LOT
                f"{float(r['total_kg'] or 0):.2f}".replace(".", ","),  # 10 Qte
                "Kg",                                               # 11 Unité
                "03",                                               # 12 Dépôt de sortie
            ]))
            continue

        ligne = [
            "01",                                                   # 1 Société
            "38",                                                   # 2 BA
            "01",                                                   # 3 Société
            "38",                                                   # 4 BA
            "NORM",                                                 # 5 Type BL
            r["code_vif"],                                          # 6 Association
            f"Ramasse partenaire {mois}",                           # 7 ref cde client
            f"Ramasse partenaire {mois}",                           # 8 commentaire ENTETE
            date_bl,                                                # 9 Date BL
            date_bl,                                                # 10 date livraison
            article,                                                # 11 Article
            lot,                                                    # 12 NO LOT
            f"{float(r['total_kg'] or 0):.3f}".replace(".", ","),   # 13 Qte
            "KG",                                                   # 14 Unité
            "03",                                                   # 15 Dépôt de sortie
            "1",                                                    # 16 Gratuit
            "0"                                                     # 17 Prix
        ]

        lignes_csv.append(";".join(ligne))

    return "\r\n".join(lignes_csv) + "\r\n"


def generer_bl_collecte_gardee(conn, lignes, date_bl):
    """BL VIF de la collecte gardée (format manuel 17 colonnes, comme
    generer_bl_ramasses) : `lignes` = dicts {code_vif, article, total_kg,
    gratuit, prix_kg}, déjà validés par l'appelant ; `date_bl` = datetime.
    Lot « COG<article> » (vide si l'article est « non loti »)."""
    d = date_bl.strftime("%d/%m/%y")
    ref = f"Collecte gardee {date_bl.strftime('%Y-%m')}"
    out = []
    for r in sorted(lignes, key=lambda x: (x["code_vif"], x["article"])):
        article = (r["article"] or "").strip()
        row = conn.execute("SELECT libelle FROM articles WHERE article = ?", (article,)).fetchone()
        libelle = (row["libelle"] or "").lower() if row else ""
        lot = "" if "non loti" in libelle else f"COG{article}"
        gratuit = bool(r["gratuit"])
        out.append(";".join([
            "01", "38", "01", "38", "NORM", r["code_vif"], ref, ref, d, d,
            article, lot,
            f"{float(r['total_kg'] or 0):.3f}".replace(".", ","),
            "KG", "03",
            "1" if gratuit else "0",
            "0" if gratuit else f"{float(r['prix_kg']):.4f}".replace(".", ","),
        ]))
    return "\r\n".join(out) + "\r\n"


def generer_bl_distribution(code_vif, date_bl, lignes, depot="03"):
    """BL VIF d'UN bon de préparation (format SFTP 12 colonnes validé FFBA, comme
    generer_bl_ramasses variante FORMAT_BL_SFTP) : `lignes` = dicts {article, quantite,
    unite}, `date_bl` = date. Quantité à 2 décimales (virgule), CRLF.
    Une ligne par lot (`lot` facultatif : vide si la ligne part sans lot) ; la sortie se fait
    toujours du dépôt `depot` (03)."""
    if not code_vif:
        raise ErreursVif(["Association sans code VIF"])
    out = []
    for l in lignes:
        out.append(";".join([
            "01", "38", "01", "38", "NORM",
            code_vif,
            date_bl.strftime("%d/%m/%Y"),
            (l["article"] or "").strip(),
            l.get("lot") or "",
            f"{float(l['quantite']):.2f}".replace(".", ","),
            l["unite"],
            depot,                    # dépôt de sortie : toujours le même (03) pour tous les BL de distribution
        ]))
    if not out:
        raise ErreursVif(["Aucune ligne avec une quantité"])
    return "\r\n".join(out) + "\r\n"


# Colonnes du fichier de mouvements VIF (format TRANSCO). Documentation : VIF
# n'attend PAS de ligne d'entête, elle n'est pas écrite dans le fichier.
COLONNES_MOUVEMENTS = [
    "Ste", "Etab", "Prechro", "Chrono", "ligne",
    "Nature mouvement de stock",
    "Date", "Heure", "Libellé du mouvement",
    "Motif", "Nature Tiers", "Tiers", "Transporteur",
    "Article entrant", "Lot entrant", "Dépôt", "Emplacement entrant",
    "Quantité 1 entrant", "Unité 1 entrant",
    "Qte2", "UN2", "Qte3", "UN3",
    "Article sortant", "Lot sortant", "Dépôt sortant", "Emplacement sortant",
    "Quantité 1 sortant", "Unité 1 sortant",
    "Quantité 2 sortant", "Unité 2 sortant",
    "Quantité 3 sortant", "Unité 3 sortant",
    "Utilisateur",
    "Evolutions futures", "Evolutions futures", "Evolutions futures", "Evolutions futures",
    "Evolutions futures", "Evolutions futures", "Evolutions futures",
    "Critère 1 du lot entrant", "Valeur du critère 1",
    "Critère 2 du lot entrant", "Valeur du critère 2",
    "Critère 3 du lot entrant", "Valeur du critère 3",
]


def generer_mouvements(conn, num_mvt, mode="new", ids=None, maintenant=None):
    """Mouvements de stock VIF (fichier type mvt_<num>.csv, transferts
    TRANSCO entre dépôts) d'un numéro de mouvement.

    mode : « new » (lignes BROUILLON, défaut), « all » (toutes) ou
    « selected » (celles de `ids`). N'écrit rien en base : le passage au
    statut EXPORTE reste à la charge de l'appelant, une fois le fichier livré.
    Lève AucunMouvement si aucune ligne ne correspond.

    Attention (format d'origine conservé) : pas de saut de ligne final.
    """
    if mode == "selected":
        ids = list(ids or [])
        if not ids:
            raise AucunMouvement()
        placeholders = ",".join("?" * len(ids))
        lignes = conn.execute(f"""
            SELECT *
            FROM mvt_stocks_v2
            WHERE num_mvt = ?
            AND id IN ({placeholders})
            ORDER BY article, lot
        """, (num_mvt, *ids)).fetchall()
    elif mode == "all":
        lignes = conn.execute("""
            SELECT *
            FROM mvt_stocks_v2
            WHERE num_mvt = ?
            ORDER BY article, lot
        """, (num_mvt,)).fetchall()
    else:
        lignes = conn.execute("""
            SELECT *
            FROM mvt_stocks_v2
            WHERE num_mvt = ?
            AND statut = 'BROUILLON'
            ORDER BY article, lot
        """, (num_mvt,)).fetchall()

    if not lignes:
        raise AucunMouvement()

    # Emplacements réels (table stocks)
    emplacements_stock = {
        (
            str(r["article"]),
            r["lot"] or "",
            str(r["depot"]).zfill(2)
        ): (r["emplacement"] or "")
        for r in conn.execute("SELECT article, lot, depot, emplacement FROM stocks").fetchall()
    }

    date_vif = (maintenant or datetime.now()).strftime("%d/%m/%Y")

    rows = []
    for i, l in enumerate(lignes, start=1):

        qte = float(l["qte_kgn"] or 0)
        depot_to = str(l["depot_to"]).zfill(2)
        depot_from = str(l["depot_from"]).zfill(2)
        article = str(l["article"])
        lot = l["lot"] or ""

        emplacement_entree = emplacements_stock.get((article, lot, depot_to), "")
        emplacement_sortie = emplacements_stock.get((article, lot, depot_from), "")

        rows.append([
            # entête
            "01", "38", "1OD", "0", str(i - 1),
            "TRANSCO",
            date_vif, "", "Transfert",
            "", "", "", "",
            # entrée
            article, lot, depot_to, emplacement_entree,
            str(qte).replace(".", ","), "KG",
            "", "", "", "",
            # sortie
            article, lot, depot_from, emplacement_sortie,
            str(qte).replace(".", ","), "KG",
            "", "", "", "",
            # utilisateur (champ conservé pour le format VIF, valeur vidée)
            "",
            # évolutions futures
            "", "", "", "", "", "", "",
            # critères lot
            "", "", "", "", "", "",
        ])

    return "\n".join(";".join(map(str, r)) for r in rows)

