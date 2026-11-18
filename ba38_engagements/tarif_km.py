"""Tarif kilométrique des notes de frais (module engagements).

Valeur paramétrable dans la table parametres
(param_name = engagements_tarif_km, ex. 0.636), modifiable sans déploiement.
Décision du bureau : barème fiscal 5 CV, jusqu'à 5 000 km = 0,636 €/km.
"""

import sqlite3
from decimal import Decimal, InvalidOperation

from ba38_utilitaires.core import get_db_path

PARAM_TARIF_KM = "engagements_tarif_km"
TARIF_KM_DEFAUT = Decimal("0.636")


def _creer_parametre_defaut():
    """Crée la ligne dans parametres si elle n'existe pas, pour qu'elle soit
    modifiable depuis la page maj_parametres (catégorie « engagements »).
    Ne touche jamais à une ligne existante."""
    try:
        with sqlite3.connect(get_db_path(), timeout=5) as c:
            existe = c.execute(
                "SELECT 1 FROM parametres WHERE param_name = ?",
                (PARAM_TARIF_KM,),
            ).fetchone()
            if not existe:
                c.execute(
                    "INSERT INTO parametres "
                    "(param_name, param_value, phone, mail, categorie) "
                    "VALUES (?, ?, '', '', 'engagements')",
                    (PARAM_TARIF_KM, str(TARIF_KM_DEFAUT)),
                )
    except sqlite3.Error as e:
        try:
            from ba38_utilitaires.core import write_log
            write_log(f"[ENGAGEMENTS] Création param {PARAM_TARIF_KM} impossible : {e}")
        except Exception:
            pass


def charger_tarif_km(conn=None):
    """Retourne le tarif €/km (Decimal) lu en base, défaut 0,636 si absent/invalide."""
    sql = (
        "SELECT param_value FROM parametres "
        "WHERE param_name = ? AND COALESCE(param_value, '') != '' "
        "ORDER BY id DESC LIMIT 1"
    )
    try:
        if conn is not None:
            row = conn.execute(sql, (PARAM_TARIF_KM,)).fetchone()
        else:
            with sqlite3.connect(get_db_path()) as c:
                row = c.execute(sql, (PARAM_TARIF_KM,)).fetchone()
        if row is None:
            _creer_parametre_defaut()
        valeur = Decimal(str(row[0]).strip().replace(",", ".")) if row else None
    except (sqlite3.Error, InvalidOperation, ValueError):
        valeur = None
    if valeur is None or valeur <= 0:
        return TARIF_KM_DEFAUT
    return valeur
