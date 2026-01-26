#!/usr/bin/env python3
from pathlib import Path

# ============================================================
# 📁 Détermination automatique de la racine BA38
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent      # /srv/ba38/dev ou /srv/ba38/prod
ENV_FILE = BASE_DIR / ".env"


def read_env(path: Path):
    try:
        if not path.exists():
            print(f"❌ Fichier .env introuvable : {path}")
            return

        print(path.read_text(encoding="utf-8"))

    except Exception as e:
        print(f"❌ Erreur lecture fichier : {e}")


if __name__ == "__main__":
    read_env(ENV_FILE)
