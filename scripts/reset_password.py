# reset_password.py
"""
Réinitialise le mot de passe d'un utilisateur dans la base ba380dev.sqlite
Usage : à exécuter manuellement en local ou via PythonAnywhere
"""

import sqlite3
from werkzeug.security import generate_password_hash

# Chemin vers la base
db_path = '/home/ndprz/dev/ba380dev.sqlite'

# Informations à modifier
email = 'ba380.informatique2@banquealimentaire.org'
nouveau_mdp = 'ba387938!'

# Génération du hash
hashed = generate_password_hash(nouveau_mdp)

# Connexion et mise à jour
with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET password_hash = ? WHERE email = ?", (hashed, email))
    conn.commit()
    print(f"✅ Mot de passe réinitialisé pour : {email}")
