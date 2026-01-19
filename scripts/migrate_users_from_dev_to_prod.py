import sqlite3

dev_path = "/home/ndprz/dev/ba380dev.sqlite"
prod_path = "/home/ndprz/ba380/ba380.sqlite"

with sqlite3.connect(dev_path) as conn_dev, sqlite3.connect(prod_path) as conn_prod:
    cur_dev = conn_dev.cursor()
    cur_prod = conn_prod.cursor()

    # Supprimer la table users si elle existe dans la base de production
    cur_prod.execute("DROP TABLE IF EXISTS users")

    # Récupérer la structure de la table depuis la base de dev
    table_schema = cur_dev.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if not table_schema:
        raise Exception("La table 'users' n'existe pas dans la base DEV.")

    # Recréer la table dans la base de production
    cur_prod.execute(table_schema[0])

    # Copier les données
    users_data = cur_dev.execute("SELECT * FROM users").fetchall()
    col_names = [description[0] for description in cur_dev.description]
    placeholders = ", ".join(["?"] * len(col_names))
    insert_sql = f"INSERT INTO users ({', '.join(col_names)}) VALUES ({placeholders})"
    cur_prod.executemany(insert_sql, users_data)

    conn_prod.commit()

print("✅ Table 'users' copiée avec succès de ba380dev.sqlite vers ba380.sqlite.")
