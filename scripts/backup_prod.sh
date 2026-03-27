#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# 📦 BACKUP PROD — VERSION PRO
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BASE_DIR="/srv/ba38"
PROD_DIR="$BASE_DIR/prod"
BACKUP_DIR="$BASE_DIR/backups"
LOG_DIR="$PROD_DIR/logs"
LOG_FILE="$LOG_DIR/backup.log"

mkdir -p "$BACKUP_DIR" "$LOG_DIR"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 BACKUP PROD — $(date '+%Y-%m-%d %H:%M:%S')"

# ============================================================================
# 🔍 Vérifications
# ============================================================================

[ -d "$PROD_DIR" ] || { echo "❌ PROD introuvable"; exit 1; }

# ============================================================================
# 📁 Nom archive
# ============================================================================

VERSION="$(date '+%Y%m%d-%H%M%S')"
ARCHIVE="$BACKUP_DIR/ba380-v$VERSION.tar.gz"

echo "📁 Source : $PROD_DIR"
echo "📦 Archive : $ARCHIVE"

# ============================================================================
# 📦 Création archive PROPRE (structure OK)
# ============================================================================

tar -czf "$ARCHIVE" \
  --exclude="venv" \
  --exclude="__pycache__" \
  --exclude="logs/*.log" \
  --exclude="static/evenements" \
  --exclude="static/photos_benevoles" \
  -C "$PROD_DIR" .

# ============================================================================
# 🧪 Vérification archive
# ============================================================================

echo "🔍 Vérification archive"

if tar -tzf "$ARCHIVE" > /dev/null; then
  echo "✅ Archive valide"
else
  echo "❌ Archive corrompue"
  exit 1
fi

# ============================================================================
# 🧹 Rotation (garde 10 backups)
# ============================================================================

echo "🧹 Rotation backups (garde 10)"

ls -1t "$BACKUP_DIR"/ba380-v*.tar.gz | tail -n +11 | xargs -r rm -f

echo "✅ Sauvegarde terminée"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
