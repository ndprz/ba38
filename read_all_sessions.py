import os
from flask import Flask
from flask.sessions import SecureCookieSessionInterface

# Création d'une app Flask mockée pour décoder les sessions
app = Flask(__name__)

# Charger la clé secrète comme dans ba38.py
app.config.from_pyfile('config.py', silent=True)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'dev-secret-key'

# Interface de session Flask
session_interface = SecureCookieSessionInterface()
serializer = session_interface.get_signing_serializer(app)

session_dir = "/home/ndprz/flask_sessions"

print(f"📦 Lecture des fichiers de session dans : {session_dir}\n")

if not os.path.exists(session_dir):
    print("❌ Dossier de session introuvable.")
    exit()

nb_total = 0
nb_valides = 0

for fname in os.listdir(session_dir):
    path = os.path.join(session_dir, fname)
    try:
        with open(path, "rb") as f:
            raw = f.read()

        raw_str = raw.decode('utf-8')  # maintenant c'est une chaîne signée
        session_data = serializer.loads(raw_str)
        nb_total += 1

        if "user_id" in session_data:
            nb_valides += 1
            print(f"✅ {fname} : user_id={session_data.get('user_id')}, username={session_data.get('username')}, role={session_data.get('user_role')}")
        else:
            print(f"ℹ️ {fname} : pas de user_id → {list(session_data.keys())}")

    except Exception as e:
        print(f"❌ {fname} : {e}")

print(f"\n🔍 {nb_valides} sessions avec user_id sur {nb_total} fichiers analysés.")
