# import os
# from datetime import datetime
# import shutil

# # Chemin du dossier principal (à adapter si besoin)
# basedir = os.path.abspath(os.path.dirname(__file__))

# # Nom du fichier de base de données (ici en DEV)
# db_file = os.path.join(basedir, "ba380dev.sqlite")

# # Dossier de sauvegarde
# backup_dir = os.path.join(basedir, "backup")
# os.makedirs(backup_dir, exist_ok=True)

# # Création du nom de fichier horodaté
# timestamp = datetime.now().strftime("%Y%m%d_%H%M")
# backup_file = os.path.join(backup_dir, f"ba380dev.sqlite.{timestamp}")

# # Copie
# try:
#     shutil.copy2(db_file, backup_file)
#     print(f"✅ Sauvegarde terminée : {backup_file}")
# except Exception as e:
#     print(f"❌ Erreur pendant la sauvegarde : {e}")

import os
from datetime import datetime
import shutil
from dotenv import load_dotenv

# Charger les variables d'environnement
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, ".env"))

# Déterminer la base utilisée
env = os.getenv("ENVIRONMENT", "dev")
test_mode = os.getenv("TEST_MODE", "0")

db_names = {
    ("dev", "0"): "ba380dev.sqlite",
    ("dev", "1"): "ba380dev_test.sqlite",
    ("prod", "0"): "ba380.sqlite",
    ("prod", "1"): "ba380_test.sqlite"
}

db_filename = db_names.get((env, test_mode), "ba380dev.sqlite")
db_path = os.path.join(basedir, db_filename)

# Dossier de sauvegarde
backup_dir = os.path.join(basedir, "backup")
os.makedirs(backup_dir, exist_ok=True)

# Fichier horodaté
timestamp = datetime.now().strftime("%Y%m%d_%H%M")
backup_file = os.path.join(backup_dir, f"{db_filename}.{timestamp}")

# Copie
try:
    shutil.copy2(db_path, backup_file)
    print(f"✅ Sauvegarde terminée : {backup_file}")
except Exception as e:
    print(f"❌ Erreur pendant la sauvegarde : {e}")
