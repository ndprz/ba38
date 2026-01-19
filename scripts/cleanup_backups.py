#!/usr/bin/env python3
import os
import time
from datetime import datetime

# Dossiers à nettoyer
directories = [
    "/home/ndprz/dev/backup",
    "/home/ndprz/ba380/backup",
    "/home/ndprz/backups"
]

# Nombre de jours de rétention
RETENTION_DAYS = 60
now = time.time()

def delete_old_files(directory, days=RETENTION_DAYS):
    if not os.path.exists(directory):
        print(f"❌ Dossier introuvable : {directory}")
        return

    print(f"📁 Traitement du dossier : {directory}")
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)

        try:
            if os.path.isfile(file_path):
                file_age = now - os.path.getmtime(file_path)
                if file_age > days * 86400:
                    os.remove(file_path)
                    print(f"🗑️ Supprimé : {filename} ({file_path})")
        except Exception as e:
            print(f"⚠️ Erreur lors du traitement de {file_path} : {e}")

if __name__ == "__main__":
    print(f"🕓 Lancement de la purge à {datetime.now().isoformat()}")
    for dir in directories:
        delete_old_files(dir)
    print("✅ Purge terminée.")
