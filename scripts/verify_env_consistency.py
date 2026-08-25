#!/usr/bin/env python3
"""
🔍 Vérification de la cohérence de l'environnement BA38

Objectifs :
- Identifier la base SQLite réellement utilisée
- Vérifier l'existence du fichier
- Afficher clairement les variables d'environnement clés
- Résultat visible dans admin_scripts (zone bleue) + app.log
"""

from pathlib import Path
import os
import sys

# ============================================================
# 📁 Rendre utils.py importable (racine BA38)
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from ba38_utilitaires.core import get_db_path, write_log


# ============================================================
# 🔊 Helper : log + affichage admin_scripts
# ============================================================
def log_and_print(msg: str):
    print(msg)
    write_log(msg)


# ============================================================
# 🔎 Vérification
# ============================================================
def verify_env_consistency():
    log_and_print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log_and_print("🔎 Vérification cohérence environnement BA38")
    log_and_print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        db_path = get_db_path()
        log_and_print(f"📁 Base SQLite détectée : {db_path}")
    except Exception as e:
        log_and_print(f"❌ ERREUR récupération chemin DB : {e}")
        return

    # --- Existence fichier ---
    if not os.path.exists(db_path):
        log_and_print("❌ ERREUR : le fichier SQLite n'existe pas.")
    else:
        size = os.path.getsize(db_path)
        log_and_print(f"✅ Fichier trouvé ({size} octets)")

    # --- Variables clés ---
    log_and_print("")
    log_and_print("🌍 Variables d'environnement :")
    log_and_print(f"   ENVIRONMENT    = {os.getenv('ENVIRONMENT')}")
    log_and_print(f"   TEST_MODE      = {os.getenv('TEST_MODE')}")
    log_and_print(f"   BA38_BASE_DIR  = {os.getenv('BA38_BASE_DIR')}")

    log_and_print("")
    log_and_print("✅ Vérification terminée.")


# ============================================================
# ▶️ Point d’entrée
# ============================================================
if __name__ == "__main__":
    verify_env_consistency()
