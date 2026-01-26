#!/usr/bin/env python3
"""
🧹 Nettoyage automatique des sauvegardes BA38

- Supprime les fichiers plus anciens que RETENTION_DAYS
- Fonctionne DEV / PROD
- Logs visibles dans admin_scripts + app.log
"""

from pathlib import Path
import os
import sys
import time
from datetime import datetime

# ============================================================
# 📁 Rendre utils.py importable (racine BA38)
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from utils import write_log


# ============================================================
# 🔧 Configuration
# ============================================================

RETENTION_DAYS = 60
RETENTION_SECONDS = RETENTION_DAYS * 86400
NOW = time.time()

# Dossiers de sauvegarde autorisés
BACKUP_DIRECTORIES = [
    "/srv/ba38/dev/backup",
    "/srv/ba38/prod/backup",
    "/srv/ba38/backups",
]


# ============================================================
# 🔊 Helper log
# ============================================================
def log(msg: str):
    print(msg)
    write_log(msg)


# ============================================================
# 🧹 Nettoyage
# ============================================================
def cleanup_directory(directory: str):
    path = Path(directory)

    if not path.exists():
        log(f"❌ Dossier introuvable : {directory}")
        return

    if not path.is_dir():
        log(f"⚠️ Ignoré (non dossier) : {directory}")
        return

    log(f"📁 Analyse du dossier : {directory}")

    deleted = 0

    for item in path.iterdir():
        try:
            if not item.is_file():
                continue

            age = NOW - item.stat().st_mtime
            if age > RETENTION_SECONDS:
                item.unlink()
                deleted += 1
                log(f"🗑️ Supprimé : {item.name}")

        except Exception as e:
            log(f"⚠️ Erreur sur {item} : {e}")

    log(f"✅ {deleted} fichier(s) supprimé(s) dans {directory}")


# ============================================================
# ▶️ Point d’entrée
# ============================================================
if __name__ == "__main__":
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log("🧹 Nettoyage des sauvegardes BA38")
    log(f"🕓 Démarrage : {datetime.now().isoformat()}")
    log(f"📆 Rétention : {RETENTION_DAYS} jours")
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    for directory in BACKUP_DIRECTORIES:
        cleanup_directory(directory)

    log("🎉 Nettoyage terminé.")
