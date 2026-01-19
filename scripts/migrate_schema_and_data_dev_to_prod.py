import sqlite3
import os

DEV_DB = "/home/ndprz/dev/ba380dev.sqlite"
PROD_DB = "/home/ndprz/ba380/ba380.sqlite"

def get_table_names(conn):
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [row[0] for row in cursor.fetchall()]

def get_columns(conn, table):
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row[1]: row[2] for row in cursor.fetchall()}  # nom_colonne: type

def table_is_empty(conn, table):
    cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
    return cursor.fetchone()[0] == 0

def copy_table_data(dev_conn, prod_conn, table):
    print(f"📥 Copie intégrale de la table '{table}' (table vide en PROD)...")
    dev_cursor = dev_conn.execute(f"SELECT * FROM {table}")
    rows = dev_cursor.fetchall()
    if not rows:
        print("  ⚠️ Aucune donnée à copier.")
        return
    placeholders = ",".join("?" * len(rows[0]))
    prod_conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    prod_conn.commit()
    print(f"  ✅ {len(rows)} lignes copiées.")

def sync_columns(dev_conn, prod_conn, table):
    dev_cols = get_columns(dev_conn, table)
    prod_cols = get_columns(prod_conn, table)

    new_cols = {col: typ for col, typ in dev_cols.items() if col not in prod_cols}
    if not new_cols:
        print(f"✅ Table '{table}' : structure identique.")
        return

    print(f"🧩 Table '{table}' : ajout de {len(new_cols)} colonne(s) manquante(s)...")
    for col, typ in new_cols.items():
        alter_sql = f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
        prod_conn.execute(alter_sql)
        print(f"  ➕ {col} ({typ})")

    if not table_is_empty(prod_conn, table):
        for col in new_cols:
            update_sql = f'''
            UPDATE {table}
            SET {col} = (
                SELECT dev.{col}
                FROM dev.{table} AS dev
                WHERE dev.id = {table}.id
            )
            WHERE EXISTS (SELECT 1 FROM dev.{table} AS dev WHERE dev.id = {table}.id);
            '''
            try:
                prod_conn.execute(update_sql)
                print(f"  🔄 Colonne '{col}' mise à jour pour les lignes existantes.")
            except Exception as e:
                print(f"  ⚠️ Erreur lors de la mise à jour de '{col}': {e}")
    prod_conn.commit()

def main():
    print("🚀 Migration DEV → PROD")
    with sqlite3.connect(DEV_DB) as dev_conn, sqlite3.connect(PROD_DB) as prod_conn:
        dev_conn.row_factory = sqlite3.Row
        prod_conn.row_factory = sqlite3.Row

        # 🔗 ATTACH de la base DEV dans PROD pour accès croisé
        prod_conn.execute(f"ATTACH DATABASE '{DEV_DB}' AS dev")

        dev_tables = get_table_names(dev_conn)
        prod_tables = get_table_names(prod_conn)

        for table in dev_tables:
            if table not in prod_tables:
                print(f"🆕 Table '{table}' absente en PROD : création...")
                schema = dev_conn.execute(
                    f"SELECT sql FROM sqlite_master WHERE name = ?", (table,)
                ).fetchone()[0]
                prod_conn.execute(schema)
                copy_table_data(dev_conn, prod_conn, table)
            else:
                sync_columns(dev_conn, prod_conn, table)

        print("✅ Migration terminée.")

if __name__ == "__main__":
    main()
