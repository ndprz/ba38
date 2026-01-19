#!/usr/bin/env python3
import sqlite3
from pathlib import Path

db_path = Path("/home/ndprz/ba380/ba380.sqlite")
values = ["text", "email", "tel", "number", "oui_non"]

with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    inserted = 0
    for val in values:
        exists = cursor.execute(
            "SELECT 1 FROM parametres WHERE param_name = 'type_champ' AND param_value = ?",
            (val,)
        ).fetchone()
        if not exists:
            cursor.execute(
                "INSERT INTO parametres (param_name, param_value) VALUES (?, ?)",
                ("type_champ", val)
            )
            inserted += 1
    conn.commit()

print(f"✅ Paramètres insérés : {inserted}")
