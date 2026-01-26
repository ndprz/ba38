#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# 🔍 Détermination du contexte d’exécution
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$SCRIPT_DIR" == *"/dev/"* ]]; then
  CONTEXT="DEV"
  BASE_DIR="/srv/ba38/dev"
  PROD_DIR="/srv/ba38/prod"
elif [[ "$SCRIPT_DIR" == *"/prod/"* ]]; then
  CONTEXT="PROD"
  BASE_DIR="/srv/ba38/prod"
  PROD_DIR="/srv/ba38/prod"
else
  echo "❌ Contexte inconnu (ni DEV ni PROD)"
  exit 1
fi

# ============================================================================
# 📦 Chargement .env PROD si présent
# ============================================================================

ENV_FILE="$PROD_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
  source "$ENV_FILE"
else
  echo "⚠️ Aucun .env PROD trouvé ($ENV_FILE)"
  echo "⚠️ Sauvegarde limitée (mode initialisation)"
fi

# ============================================================================
# 📁 Dossiers
# ============================================================================

BACKUP_DIR="/srv/ba38/backups"
LOG_DIR="$PROD_DIR/logs"
LOG_FILE="$LOG_DIR/backup.log"

mkdir -p "$BACKUP_DIR" "$LOG_DIR"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 BACKUP PROD — $(date '+%Y-%m-%d %H:%M:%S')"
echo "Contexte appel : $CONTEXT"
echo "Script : $SCRIPT_DIR"

# ============================================================================
# 🔒 Vérification répertoire PROD
# ============================================================================

if [[ ! -d "$PROD_DIR" ]]; then
  echo "❌ Répertoire PROD introuvable : $PROD_DIR"
  exit 1
fi

# ============================================================================
# 🗄️ Création de l’archive
# ============================================================================

VERSION="$(date '+%Y%m%d-%H%M%S')"
ARCHIVE="$BACKUP_DIR/ba380-v$VERSION.tar.gz"

echo "📁 Source : $PROD_DIR"
echo "📦 Archive : $ARCHIVE"

tar -czf "$ARCHIVE" \
  --exclude="$PROD_DIR/venv" \
  --exclude="$PROD_DIR/__pycache__" \
  --exclude="$PROD_DIR/logs/*.log" \
  -C "$(dirname "$PROD_DIR")" "$(basename "$PROD_DIR")"

echo "✅ Sauvegarde terminée"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
