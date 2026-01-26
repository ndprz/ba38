from pydrive2.auth import GoogleAuth
from pydrive2.auth import ServiceAccountCredentials
from pydrive2.drive import GoogleDrive
import sqlite3
import json
from utils import write_log, get_db_path
write_log("🔥 auth_gdrive.py IMPORTÉ")


# 🔹 Charger les informations du fichier service_account.json
with open("service_account.json") as f:
    creds_data = json.load(f)

# 🔹 Authentification avec le Compte de Service
gauth = GoogleAuth()
gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(
    "service_account.json",
    scopes=["https://www.googleapis.com/auth/drive"],
)
drive = GoogleDrive(gauth)

# 🔹 Chemin où sauvegarder la base SQLite
db_filename = "partenaires.sqlite"

# 🔹 Télécharger `partenaires.sqlite` depuis Google Drive
def download_database():
    """Télécharge la base de données `partenaires.sqlite` depuis Google Drive."""
    file_list = drive.ListFile({'q': "title='partenaires.sqlite'"}).GetList()

    if file_list:
        file_drive = file_list[0]
        file_drive.GetContentFile(db_filename)  # 📥 Télécharge le fichier en local

        print("✅ Base de données téléchargée depuis Google Drive avec succès !")

        # 🔹 Vérifier que la table `field_groups` existe bien
        conn = sqlite3.connect(get_db_path())
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        print("📌 Tables disponibles:", tables)

        conn.close()
    else:
        print("❌ Fichier `partenaires.sqlite` non trouvé sur Google Drive.")

# 🔹 Envoyer `partenaires.sqlite` vers Google Drive
def upload_database():
    """Téléverse la base de données locale `partenaires.sqlite` vers Google Drive."""
    file_list = drive.ListFile({'q': "title='partenaires.sqlite'"}).GetList()

    if file_list:
        file_drive = file_list[0]  # Récupérer le fichier existant
        file_drive.SetContentFile(db_filename)  # Remplace le contenu
        file_drive.Upload()  # 📤 Téléverser
        print("✅ Base de données mise à jour sur Google Drive !")
    else:
        print("❌ Impossible de trouver `partenaires.sqlite` sur Google Drive.")

# 🔹 Exécuter le téléchargement au lancement
# download_database()
upload_database()
