import sqlite3
import shutil

dev_db = "/home/ndprz/dev/ba380dev.sqlite"
prod_db = "/home/ndprz/ba380/ba380.sqlite"

# Sauvegarde de sécurité
shutil.copy2(prod_db, prod_db + ".backup")

# Connexions aux deux bases
conn_dev = sqlite3.connect(dev_db)
conn_prod = sqlite3.connect(prod_db)

# Lecture des données DEV
users = conn_dev.execute("SELECT * FROM users").fetchall()
roles = conn_dev.execute("SELECT * FROM roles_utilisateurs").fetchall()

# Récupération des colonnes pour insertion dynamique
user_columns = [d[1] for d in conn_dev.execute("PRAGMA table_info(users)")]
role_columns = [d[1] for d in conn_dev.execute("PRAGMA table_info(roles_utilisateurs)")]

# Purge des tables PROD
conn_prod.execute("DELETE FROM users")
conn_prod.execute("DELETE FROM roles_utilisateurs")

# Réinsertion des données
conn_prod.executemany(
    f"INSERT INTO users ({', '.join(user_columns)}) VALUES ({', '.join(['?']*len(user_columns))})",
    users
)
conn_prod.executemany(
    f"INSERT INTO roles_utilisateurs ({', '.join(role_columns)}) VALUES ({', '.join(['?']*len(role_columns))})",
    roles
)

conn_prod.commit()
conn_dev.close()
conn_prod.close()

print("✅ Tables users et roles_utilisateurs synchronisées de DEV → PROD.")
