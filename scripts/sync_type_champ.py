#!/usr/bin/env python3
"""
Synchronisation des valeurs autorisées pour le paramètre 'type_champ'
dans la table parametres.

- Sans dépendance python-dotenv
- Compatible nouvel environnement Linux (/srv/ba38)
- DEV / PROD via variables d'environnement
- Script idempotent (aucun doublon)
"""

import os
import sqlite3
from pathlib import Path

# -------------------------------------------------------------------
# Environnement
# -------------------------------------------------------------------
ENV = os.getenv("ENVIRONMENT", "DEV").upper()

if ENV == "PROD":
    base_dir = Path("/srv/ba38/prod")
    db_name = os.getenv("SQLITE_DB_PROD")
else:
    base_dir = Path("/srv/ba38/dev")
    db_name = os.getenv("SQLITE_DB_DEV")

if not db_name:
    raise ValueError(
        "❌ Nom de base SQLite non défini "
        "(SQLITE_DB_DEV ou SQLITE_DB_PROD manquant)"
    )

db_path = base_dir / db_name

if not db_path.exists():
    raise FileNotFoundError(f"❌ Base SQLite introuvable : {db_path}")

# -------------------------------------------------------------------
# Valeurs attendues pour type_champ
# -------------------------------------------------------------------
TYPE_CHAMP_VALUES = [
    "text",
    "email",
    "tel",
    "number",
    "oui_non",
]

# -------------------------------------------------------------------
# Synchronisation
# -------------------------------------------------------------------
inserted = 0

with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()

    # Vérification table parametres
    cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table'
          AND name='parametres'
    """)
    if cursor.fetchone() is None:
        raise RuntimeError("❌ Table 'parametres' absente de la base")

    for value in TYPE_CHAMP_VALUES:
        cursor.execute(
            """
            SELECT 1
            FROM parametres
            WHERE param_name = 'type_champ'
              AND param_value = ?
            """,
            (value,)
        )

        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO parametres (param_name, param_value)
                VALUES ('type_champ', ?)
                """,
                (value,)
            )
            inserted += 1

    conn.commit()

print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
print("✅ Synchronisation type_champ terminée")
print(f"📦 Environnement : {ENV}")
print(f"🗄️  Base utilisée : {db_path}")
print(f"➕ Valeurs ajoutées : {inserted}")
print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
