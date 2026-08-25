#!/usr/bin/env bash
# ============================================================================
# 🚀 Déploiement DEV → PROD pour BA38 (serveur Debian) — VERSION SÉCURISÉE
# ============================================================================

set -euo pipefail

# ============================================================================
# 🛡️ Protection anti-mauvais contexte
# ============================================================================
if [[ "$(pwd)" == *"/prod"* ]]; then
  echo "❌ Ce script ne doit JAMAIS être lancé depuis PROD"
  exit 1
fi

# ============================================================================
# 📁 Répertoires principaux
# ============================================================================
BASE_DIR="/srv/ba38"
DEV_DIR="$BASE_DIR/dev"
PROD_DIR="$BASE_DIR/prod"
SCRIPTS_DIR="$DEV_DIR/scripts"

DEV_ENV="$DEV_DIR/.env"
PROD_ENV="$PROD_DIR/.env"

# ============================================================================
# 📝 Journalisation globale
# ============================================================================
LOG_DIR="$BASE_DIR/logs"
LOG_FILE="$LOG_DIR/deploy.log"

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

trap 'echo "❌ ÉCHEC sur la commande : ${BASH_COMMAND}"' ERR

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 Déploiement BA38 DEV → PROD : $(date '+%Y-%m-%d %H:%M:%S')"

# ============================================================================
# 🔎 Vérification état Git (sécurité)
# ============================================================================
echo "🔎 Vérification état Git (doit être clean)"

# (désactivé volontairement)
# cd "$DEV_DIR"
# if ! git diff --quiet || ! git diff --cached --quiet; then
#   echo "❌ Des modifications non commitées existent"
#   exit 1
# fi

# ============================================================================
# 🌍 Chargement ENV DEV
# ============================================================================
if [ ! -f "$DEV_ENV" ]; then
  echo "❌ Fichier .env DEV introuvable"
  exit 1
fi

set -a
source "$DEV_ENV"
set +a

# ============================================================================
# 🌍 Chargement ENV PROD (CORRECTION CRITIQUE)
# ============================================================================
if [ ! -f "$PROD_ENV" ]; then
  echo "❌ Fichier .env PROD introuvable"
  exit 1
fi

# On lit UNIQUEMENT SQLITE_DB de PROD (ne pas polluer les autres variables)
SQLITE_DB_PROD=$(grep "^SQLITE_DB=" "$PROD_ENV" | cut -d '=' -f2)

# ============================================================================
# 📝 VERSION
# ============================================================================
VERSION="${1:-}"
VERSION_MSG="${2:-}"

[ -z "$VERSION_MSG" ] && VERSION_MSG="(sans message)"

if [ -z "$VERSION" ]; then
  if [ -t 0 ]; then
    read -p "➡️ Version : " VERSION
    read -p "➡️ Message : " VERSION_MSG
    [ -z "$VERSION" ] && { echo "❌ Version obligatoire"; exit 1; }
  else
    echo "❌ VERSION non fournie"
    exit 1
  fi
fi

echo "📝 VERSION : $VERSION"
echo "📝 MESSAGE : $VERSION_MSG"

# ============================================================================
# 🗄️ Bases SQLite (CORRECTION ICI)
# ============================================================================
DEV_DB="$DEV_DIR/$SQLITE_DB_DEV"
PROD_DB="$PROD_DIR/$SQLITE_DB_PROD"

echo "🧪 DEV_DB = $DEV_DB"
echo "🧪 PROD_DB = $PROD_DB"

# ============================================================================
# 🔐 Sécurité forte (évite mauvaise DB)
# ============================================================================
[ -f "$DEV_DB" ] || { echo "❌ Base DEV absente"; exit 1; }
[ -f "$PROD_DB" ] || { echo "❌ Base PROD absente"; exit 1; }

tables_count=$(sqlite3 "$PROD_DB" \
  "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")

if [ "$tables_count" -eq 0 ]; then
  echo "❌ Base PROD vide → STOP"
  exit 1
fi

# ============================================================================
# 📦 Installation dépendances
# ============================================================================
echo "📦 Vérification des dépendances Python"

if [ -f "$DEV_DIR/requirements.txt" ]; then

  source "$PROD_DIR/venv/bin/activate"

  if ! cmp -s "$DEV_DIR/requirements.txt" "$PROD_DIR/requirements.txt"; then
    echo "📦 Mise à jour dépendances"
    cp "$DEV_DIR/requirements.txt" "$PROD_DIR/"
    pip install --upgrade --no-cache-dir -r "$PROD_DIR/requirements.txt"
  else
    echo "📦 Dépendances déjà à jour"
  fi

fi

# ============================================================================
# 💾 BACKUP
# ============================================================================
echo "💾 Sauvegarde de la base PROD"
"$SCRIPTS_DIR/backup_prod.sh"

# ============================================================================
# 🔧 MIGRATION
# ============================================================================
echo "🔧 Migration schéma DEV → PROD"
"$DEV_DIR/venv/bin/python" "$SCRIPTS_DIR/migrate_schema_and_data_dev_to_prod.py"

# ============================================================================
# 🔁 SYNC TABLES METIER (CORRECTION IMPORT)
# ============================================================================
echo "🔁 Synchronisation tables métier"

SYNC_TABLES=("applications")

for table in "${SYNC_TABLES[@]}"; do
  echo "🔁 Sync table : $table"

  exists_dev=$(sqlite3 "$DEV_DB" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$table';")

  [ "$exists_dev" -eq 0 ] && continue

  exists_prod=$(sqlite3 "$PROD_DB" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$table';")

  if [ "$exists_prod" -eq 1 ]; then
    echo "♻️ Reset table"
    sqlite3 "$PROD_DB" "DELETE FROM $table;" || echo "⚠️ DELETE KO"
  fi

  echo "📥 Import données"

  sqlite3 "$DEV_DB" ".dump $table" \
    | sed '/^CREATE TABLE/,/);/d' \
    | sed '/^BEGIN TRANSACTION/d' \
    | sed '/^COMMIT/d' \
    | sqlite3 "$PROD_DB" || echo "⚠️ IMPORT KO"

  count=$(sqlite3 "$PROD_DB" "SELECT COUNT(*) FROM $table;" 2>/dev/null || echo "0")
  echo "📊 $table : $count lignes"
done

# ============================================================================
# 📁 RSYNC CODE (INCHANGÉ)
# ============================================================================
echo "📁 Synchronisation DEV → PROD"

rsync -av --delete \
  --exclude ".env" \
  --exclude ".git/" \
  --exclude ".git_OLD_ba380DEV/" \
  --exclude ".vscode/" \
  --exclude "logs/" \
  --exclude "*.log" \
  --exclude "*.log.*" \
  --exclude "instance/" \
  --exclude "*.sqlite" \
  --exclude "static/uploads/" \
  --exclude "static/factures/archives/" \
  --exclude "static/evenements/" \
  --exclude "static/photos_benevoles/" \
  --exclude "uploads/" \
  --exclude "exports/" \
  --exclude "__pycache__/" \
  --exclude "venv/" \
  "$DEV_DIR/" "$PROD_DIR/"

# ============================================================================
# 📝 VERSION
# ============================================================================
echo "📝 Mise à jour VERSION"

DATE_NOW=$(date '+%Y-%m-%d %H:%M')

echo "VERSION=$VERSION" > "$PROD_DIR/VERSION"
echo "MESSAGE=$VERSION_MSG" >> "$PROD_DIR/VERSION"
echo "DATE=$DATE_NOW" >> "$PROD_DIR/VERSION"

echo "VERSION=$VERSION" > "$DEV_DIR/VERSION"
echo "MESSAGE=$VERSION_MSG" >> "$DEV_DIR/VERSION"
echo "DATE=$DATE_NOW" >> "$DEV_DIR/VERSION"

# ============================================================================
# 🔄 RESTART
# ============================================================================
echo "🔄 Redémarrage ba38-prod"
sudo systemctl restart ba38-prod.service

# ============================================================================
# 📦 COMMIT GIT
# ============================================================================
echo "📦 Commit git"
"$SCRIPTS_DIR/git_commit_push.sh" "v$VERSION - $VERSION_MSG"

echo "🎉 Déploiement terminé avec succès"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
