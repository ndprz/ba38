import sqlite3
import shutil
import datetime

db_path = "/home/ndprz/ba380/ba380.sqlite"  # à adapter (dev/prod)

def migrate_associations():
    # 🔒 sauvegarde avant toute manip
    backup_path = f"{db_path}.backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy(db_path, backup_path)
    print(f"✅ Sauvegarde créée : {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. Lire la structure de la table associations
    cur.execute("PRAGMA table_info(associations)")
    cols_info = cur.fetchall()
    col_names = [c["name"] for c in cols_info]

    if "Id" not in col_names:
        print("❌ Pas de colonne Id trouvée dans associations")
        return

    # 2. Construire la définition des colonnes
    new_cols = []
    for col in cols_info:
        name = col["name"]
        type_ = col["type"]
        notnull = "NOT NULL" if col["notnull"] else ""
        dflt = f"DEFAULT {col['dflt_value']}" if col["dflt_value"] else ""

        if name.lower() == "id":  # on force l'auto-incrément
            new_cols.append("Id INTEGER PRIMARY KEY AUTOINCREMENT")
        else:
            new_cols.append(f"`{name}` {type_} {notnull} {dflt}".strip())

    new_cols_sql = ",\n    ".join(new_cols)

    # 3. Renommer l’ancienne table
    cur.execute("ALTER TABLE associations RENAME TO associations_old")

    # 4. Créer la nouvelle table
    cur.execute(f"CREATE TABLE associations (\n    {new_cols_sql}\n)")

    # 5. Copier les données (on exclut l’Id si déjà en doublon → SQLite réattribuera)
    col_names_no_id = [c for c in col_names if c.lower() != "id"]
    cols_list_sql = ", ".join([f"`{c}`" for c in col_names_no_id])
    cur.execute(f"""
        INSERT INTO associations ({cols_list_sql})
        SELECT {cols_list_sql} FROM associations_old
    """)

    # 6. Supprimer l’ancienne table
    cur.execute("DROP TABLE associations_old")

    conn.commit()
    conn.close()
    print("✅ Migration terminée avec succès.")

if __name__ == "__main__":
    migrate_associations()
