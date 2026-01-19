# fichier : sauvegarde_sqlite_drive_vers_nas.py

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
import os
import shutil

# Configuration
DRIVE_FILE_ID = '1fTXqXtxKoj3swFz6TsGBa2wX7TEiY_pG'
DESTINATION_DIR = r'X:\Sauvegarde SQLITE'
DESTINATION_FILE = os.path.join(DESTINATION_DIR, 'ba380.sqlite')

# Authentification Google Drive
gauth = GoogleAuth()
gauth.LocalWebserverAuth()  # Fenêtre d'auth Google s'ouvrira la première fois
drive = GoogleDrive(gauth)

# Téléchargement du fichier
print("Téléchargement du fichier depuis Google Drive...")
file_drive = drive.CreateFile({'id': DRIVE_FILE_ID})
temp_filename = 'ba380_temp_download.sqlite'
file_drive.GetContentFile(temp_filename)

# Copie vers NAS
print(f"Copie du fichier vers {DESTINATION_FILE}...")
shutil.copy2(temp_filename, DESTINATION_FILE)

# Nettoyage
os.remove(temp_filename)

print("Sauvegarde terminée.")
