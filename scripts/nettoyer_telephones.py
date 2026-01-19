import sqlite3
import re
import os
from datetime import datetime

DB_PATH = "/home/ndprz/ba380/ba380.sqlite"
LOG_FILE = "/home/ndprz/dev/app.log"

def write_log(message):
    timestamp = datetime.now().isoformat(sep=" ", timespec="seconds")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception as e:
        print(f"Erreur écriture log : {e}")

def get_fields_by_type(conn, appli, type_champ):
    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT field_name FROM field_groups
        WHERE appli = ? AND type_champ = ?
    """, (appli, type_champ)).fetchall()
    return [r[0] for r in rows]

def nettoyer_telephones():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    total_modifs = 0

    for table in ["benevoles", "associations"]:
        champs = get_fields_by_type(conn, table, "tel")
        for champ in champs:
            try:
                rows = cursor.execute(f"SELECT id, {champ} FROM {table}").fetchall()
                for row in rows:
                    tel = row[champ]
                    if tel:
                        tel_nettoye = re.sub(r"\D", "", tel)
                        if tel_nettoye != tel:
                            cursor.execute(
                                f"UPDATE {table} SET {champ} = ? WHERE id = ?",
                                (tel_nettoye, row["id"])
                            )
                            total_modifs += 1
            except sqlite3.OperationalError:
                # Champ absent → on ignore
                continue

    conn.commit()
    conn.close()
    write_log(f"📞 Téléphones nettoyés dynamiquement : {total_modifs} modifiés.")
    print(f"✅ {total_modifs} numéros nettoyés.")

if __name__ == "__main__":
    nettoyer_telephones()
