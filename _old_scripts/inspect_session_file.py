import os
import pickle
from flask.sessions import SecureCookieSessionInterface
from ba38 import app

session_interface = SecureCookieSessionInterface()
serializer = session_interface.get_signing_serializer(app)

session_dir = "/home/ndprz/flask_sessions"

print(f"📋 Inspection des sessions dans : {session_dir}")

if not os.path.exists(session_dir):
    print("❌ Dossier introuvable.")
    exit()

for fname in os.listdir(session_dir):
    fpath = os.path.join(session_dir, fname)
    try:
        with open(fpath, "rb") as f:
            raw = f.read()

        session_data = None

        # Test Pickle
        if raw.startswith(b"\x80"):
            session_data = pickle.loads(raw)
        else:
            try:
                text = raw.decode()
                session_data = serializer.loads(text)
            except Exception:
                continue  # format inconnu

        if session_data:
            print(f"\n🔍 Session : {fname}")
            for k, v in session_data.items():
                print(f"• {k} = {v}")


    except Exception as e:
        print(f"❌ Erreur {fname} : {e}")
