import sys
import os
from datetime import datetime

# Ajoute le chemin vers le dossier où se trouve utils.py
sys.path.append("/home/ndprz/ba380")

from utils import write_log, get_drive

# Chemin vers la base à sauvegarder
LOCAL_DB_PATH = "/home/ndprz/ba380/ba380.sqlite"
FOLDER_ID_BACKUP = "1tBHcdUcMog7CiMQqM9mX-DEvA0hpI3aa"  # dossier Google Drive

def upload_backup():
    try:
        drive = get_drive()
        today = datetime.now().strftime("%Y-%m-%d")
        filename = f"ba380_backup_{today}.sqlite"
        file_drive = drive.CreateFile({
            'title': filename,
            'parents': [{'id': FOLDER_ID_BACKUP}],
            'supportsAllDrives': True
        })
        file_drive.SetContentFile(LOCAL_DB_PATH)
        file_drive.Upload(param={'supportsAllDrives': True})
        write_log(f"✅ Sauvegarde quotidienne envoyée : {filename}")
    except Exception as e:
        write_log(f"❌ Erreur dans daily_backup : {e}")

if __name__ == "__main__":
    upload_backup()
