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
# 📁 Nom archive avec VERSION
# ============================================================================

VERSION_FILE="/srv/ba38/prod/VERSION"

if [ -f "$VERSION_FILE" ]; then
  VERSION=$(grep "^VERSION=" "$VERSION_FILE" | cut -d'=' -f2)
else
  VERSION="unknown"
fi

VERSION="${VERSION:-unknown}"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"

ARCHIVE="$BACKUP_DIR/ba380-v${VERSION}-${TIMESTAMP}.tar.gz"

echo "📦 Version détectée : $VERSION"
echo "📦 Backup VERSION=$VERSION"
echo "📦 Archive : $ARCHIVE"


# ============================================================================
# 📦 Création archive PROPRE (structure OK)
# ============================================================================

# uploads/ (~540 Mo) est exclu : deploy_to_prod.sh n'y touche jamais (rsync
# --exclude "uploads/"), et l'archiver faisait dépasser 30 s au déploiement
# lancé depuis la page admin_scripts (timeout gunicorn) — constaté 2026-11-26.
tar -czf "$ARCHIVE" \
  --exclude="./uploads" \
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
