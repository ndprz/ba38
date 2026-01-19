#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# 🔐 Chargement des variables d’environnement
# ============================================================

# Le script est dans /srv/ba38/scripts
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="$BASE_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ Fichier .env introuvable : $ENV_FILE"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

# ============================================================
# 🛑 Sécurité : uniquement PROD
# ============================================================

if [[ "${ENVIRONMENT:-}" != "PROD" ]]; then
  echo "⛔ Ce script est réservé à l’environnement PROD"
  echo "ENVIRONMENT=${ENVIRONMENT:-non défini}"
  exit 1
fi

# ============================================================
# 📦 Paramètres sauvegarde
# ============================================================

DATE="$(date +'%Y%m%d-%H%M')"
VERSION="${VERSION:-unknown}"

BA38_ROOT="$(cd "$BASE_DIR/.." && pwd)"   # /srv/ba38
BACKUPS_DIR="${BACKUPS_DIR:-$BA38_ROOT/backups}"
SOURCE_DIR="${APP_ROOT:-$BASE_DIR}"

mkdir -p "$BACKUPS_DIR"

DEST="$BACKUPS_DIR/ba38-prod-v${VERSION}-${DATE}.tar.gz"

# ============================================================
# 🚀 Sauvegarde
# ============================================================

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔄 Sauvegarde PROD en cours"
echo "📂 Source  : $SOURCE_DIR"
echo "📦 Archive : $DEST"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

tar -czf "$DEST" "$SOURCE_DIR"

echo "✅ Sauvegarde terminée avec succès"
