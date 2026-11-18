# ============================================================
# Suivi des dépôts VIF dans la table exports_vif
#
# Fonctions de base de données sans Flask (une connexion sqlite3 en entrée).
# Statuts (colonne `statut`) :
#   NULL             téléchargé à la main (lignes historiques, aucun dépôt)
#   'depose'         fichier posé dans le dossier lu par VIF, pas encore retiré
#   'pris_en_charge' fichier retiré du dossier = VIF l'a récupéré
#
# « Pris en charge » est une DÉDUCTION : VIF supprime/déplace le fichier après
# lecture (à confirmer avec la FFBA). Tant que ce n'est pas confirmé, ce statut
# veut seulement dire « le fichier n'est plus dans le dossier ».
# ============================================================

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

STATUT_DEPOSE = "depose"
STATUT_PRIS_EN_CHARGE = "pris_en_charge"

# type d'export (colonne type_export) -> paramètre (table parametres) du dossier de dépôt
PARAM_DOSSIER = {
    "bl": "vif_depot_dossier_bl",
    "reception": "vif_depot_dossier_receptions",
}


def colonnes_presentes(conn):
    """False tant que la migration add_exports_vif_depot_columns n'est pas passée."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(exports_vif)")}
    return {"fichier", "dossier", "statut", "depose_le", "pris_en_charge_le"} <= cols


def dossier_depot(conn, type_export):
    """Dossier de dépôt configuré pour ce flux, ou None (bouton désactivé)."""
    nom = PARAM_DOSSIER.get(type_export)
    if not nom:
        return None
    row = conn.execute("SELECT param_value FROM parametres WHERE param_name = ?", (nom,)).fetchone()
    valeur = (row[0] or "").strip() if row else ""
    return Path(valeur) if valeur else None


def export_existant(conn, mois, type_export):
    """Dernière ligne exports_vif de ce mois/flux (téléchargement OU dépôt), ou None.
    Sert à éviter le double import : un fichier déjà téléchargé puis déposé (ou
    l'inverse) serait importé deux fois dans VIF."""
    return conn.execute(
        "SELECT * FROM exports_vif WHERE mois = ? AND type_export = ? ORDER BY id DESC LIMIT 1",
        (mois, type_export),
    ).fetchone()


def enregistrer_depot(conn, mois, type_export, utilisateur, fichier, dossier):
    conn.execute(
        """INSERT INTO exports_vif (mois, type_export, date_export, user, fichier, dossier, statut, depose_le)
           VALUES (?, ?, datetime('now'), ?, ?, ?, ?, datetime('now'))""",
        (mois, type_export, utilisateur, fichier, str(dossier), STATUT_DEPOSE),
    )
    conn.commit()


def actualiser_prises_en_charge(conn):
    """Passe à 'pris_en_charge' les dépôts dont le fichier a disparu de son dossier.

    Prudent : un dossier illisible/absent ne prouve rien, on ne touche à rien.
    Retourne le nombre de lignes mises à jour."""
    n = 0
    for r in conn.execute(
        "SELECT id, fichier, dossier FROM exports_vif WHERE statut = ? AND fichier IS NOT NULL AND dossier IS NOT NULL",
        (STATUT_DEPOSE,),
    ).fetchall():
        dossier = Path(r["dossier"])
        try:
            if not dossier.is_dir() or not os.access(dossier, os.R_OK | os.X_OK):
                continue
            if (dossier / r["fichier"]).exists():
                continue
        except OSError:
            continue
        conn.execute(
            "UPDATE exports_vif SET statut = ?, pris_en_charge_le = datetime('now') WHERE id = ?",
            (STATUT_PRIS_EN_CHARGE, r["id"]),
        )
        n += 1
    if n:
        conn.commit()
    return n


def _heure_locale(valeur):
    """'2026-11-13 09:30:00' (UTC, datetime('now')) -> '13/11/2026 10:30' (Paris)."""
    if not valeur:
        return ""
    try:
        d = datetime.strptime(valeur[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return d.astimezone(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return valeur


LIBELLES_STATUT = {
    None: ("Téléchargé", "secondary"),
    "": ("Téléchargé", "secondary"),
    STATUT_DEPOSE: ("Déposé, en attente de VIF", "warning"),
    STATUT_PRIS_EN_CHARGE: ("Pris en charge par VIF", "success"),
}
LIBELLES_TYPE = {"reception": "Réception", "bl": "Bon de livraison"}


def historique(conn, mois, limite=20):
    """Lignes d'historique du mois, prêtes pour l'affichage (dictionnaires)."""
    out = []
    for r in conn.execute(
        "SELECT * FROM exports_vif WHERE mois = ? ORDER BY id DESC LIMIT ?", (mois, limite)
    ).fetchall():
        libelle, couleur = LIBELLES_STATUT.get(r["statut"], (r["statut"], "secondary"))
        out.append({
            "type": LIBELLES_TYPE.get(r["type_export"], r["type_export"]),
            "fichier": r["fichier"] or "(téléchargement manuel)",
            "date": _heure_locale(r["depose_le"] or r["date_export"]),
            "par": r["user"] or "",
            "statut": libelle,
            "couleur": couleur,
            "prise_en_charge": _heure_locale(r["pris_en_charge_le"]),
        })
    return out
