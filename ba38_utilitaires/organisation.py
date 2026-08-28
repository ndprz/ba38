"""
Accès à l'identité de l'organisme hébergeant l'instance (nom, adresse,
IBAN/BIC, SIREN/SIRET/NAF/RNA, logo, signature, couleur primaire...).

Ces valeurs vivaient auparavant en dur dans ba38_tresorerie/constants.py et
dans plusieurs modules de génération de PDF (cotisations, participation,
CERFA, fiches partenaires) — elles sont désormais dans la table
`organisation` (une seule ligne, id=1) créée par
scripts/migrate_organisation.py, pour permettre de redéployer l'application
pour une autre banque alimentaire sans toucher au code.
"""

import sqlite3

from ba38_utilitaires.core import get_db_path


def get_organisation():
    """Retourne l'identité de l'organisme sous forme de dict (colonnes de
    la table `organisation`, ligne id=1)."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM organisation WHERE id = 1").fetchone()
    finally:
        conn.close()

    if row is None:
        raise RuntimeError(
            "Table `organisation` vide ou absente — exécuter "
            "scripts/migrate_organisation.py"
        )

    return dict(row)
