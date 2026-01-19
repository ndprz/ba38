#!/usr/bin/env bash
# ============================================================================
# Déploiement DEV → PROD pour BA38 (serveur Debian)
# Cible : /srv/ba38/prod
# ============================================================================

set -euo pipefail

# === Réglages généraux ========================================================
BASE_DIR="/srv/ba38"
DEV_DIR="$BASE_DIR/dev"
PROD_DIR="$BASE_DIR/prod"
SCRIPTS_DIR="$BASE_DIR/scripts"

DEV_DB="$DEV_DIR/ba380dev.sqlite"
PROD_DB="$PROD_DIR/ba380.sqlite"

DEV_ENV="$DEV_DIR/.env"
PROD_ENV="$PROD_DIR/.env"

LOG_FILE="/srv/ba38/app.log"
exec > >(tee -a "$LOG_FILE") 2>&1

trap 'echo "❌ Échec à la commande : ${BASH_COMMAND}"' ERR

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 Déploiement BA38 DEV → PROD lancé : $(date '+%Y-%m-%d %H:%M:%S')"

# === Vérifications préalables ================================================
if [ ! -d "$DEV_DIR" ]; then
  echo "❌ Répertoire DEV introuvable : $DEV_DIR"
  exit 1
fi

if [ ! -f "$DEV_DB" ]; then
  echo "❌ Base DEV absente : $DEV_DB"
  exit 1
fi

# === Création PROD si nécessaire =============================================
if [ ! -d "$PROD_DIR" ]; then
  echo "📁 Création du répertoire PROD : $PROD_DIR"
  mkdir -p "$PROD_DIR"
fi

# === Fonctions SQLite (identiques à ton script historique) ====================
get_tables() {
  sqlite3 "$1" \
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
}

normalize_table() {
  sqlite3 "$1" "PRAGMA table_info('$2');" |
    awk -F'|' '{print $2 "|" $3 "|" $4 "|" $5 "|" $6}'
}

has_autoinc() {
  sqlite3 "$1" \
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='$2';" |
    grep -qi autoincrement && echo YES || echo NO
}

compare_schemas() {
  local diff=0
  local dev_tables prod_tables all_tables

  dev_tables=$(get_tables "$DEV_DB")
  prod_tables=$(get_tables "$PROD_DB" 2>/dev/null || true)
  all_tables=$(printf "%s\n%s\n" "$dev_tables" "$prod_tables" | sort -u)

  echo "🔍 Comparaison des schémas SQLite…"

  while read -r table; do
    [ -z "$table" ] && continue

    if ! echo "$prod_tables" | grep -Fxq "$table"; then
      diff=1
      continue
    fi

    tmp_dev=$(mktemp)
    tmp_prod=$(mktemp)

    normalize_table "$DEV_DB" "$table" | sort > "$tmp_dev"
    normalize_table "$PROD_DB" "$table" | sort > "$tmp_prod"

    diff -u "$tmp_dev" "$tmp_prod" >/dev/null || diff=1

    rm -f "$tmp_dev" "$tmp_prod"

    if [ "$(has_autoinc "$DEV_DB" "$table")" != "$(has_autoinc "$PROD_DB" "$table")" ]; then
      echo "⚠️ $table : AUTOINCREMENT différent (non bloquant)"
    fi
  done <<< "$all_tables"

  return $diff
}

# === 1) Sauvegarde PROD =======================================================
if [ -f "$PROD_DB" ]; then
  echo "💾 Sauvegarde de la base PROD…"
  "$SCRIPTS_DIR/backup_prod.sh"
  echo "✅ Sauvegarde terminée."
else
  echo "ℹ️ Aucune base PROD existante (premier déploiement)."
fi

# === 2) Comparaison / migration SQLite ========================================
if [ -f "$PROD_DB" ] && compare_schemas; then
  echo "✅ Schémas DEV et PROD identiques."
else
  echo "🔧 Migration schéma / données DEV → PROD…"
  python3 "$SCRIPTS_DIR/migrate_schema_and_data_dev_to_prod.py"

  echo "🔁 Vérification post-migration…"
  compare_schemas || {
    echo "❌ Migration incohérente."
    exit 1
  }
  echo "✅ Migration validée."
fi

# === 3) Rsync du code =========================================================
EXCLUDES=(
  ".env"
  "__pycache__"
  "*.pyc"
  ".git"
  "venv"
  "*.sqlite"
  "static/photos_benevoles"
)

RSYNC_EXCLUDES=()
for e in "${EXCLUDES[@]}"; do
  RSYNC_EXCLUDES+=(--exclude="$e")
done

echo "📁 Synchronisation DEV → PROD…"
rsync -av --delete "${RSYNC_EXCLUDES[@]}" "$DEV_DIR/" "$PROD_DIR/"

# === 4) Mise à jour VERSION ===================================================
if [ ! -f "$DEV_ENV" ]; then
  echo "❌ .env DEV introuvable."
  exit 1
fi

set -a
source "$DEV_ENV"
set +a

echo "📝 VERSION détectée : $VERSION"

if [ -f "$PROD_ENV" ]; then
  sed -i "s/^VERSION=.*/VERSION=\"$VERSION\"/" "$PROD_ENV"
else
  echo "VERSION=\"$VERSION\"" > "$PROD_ENV"
fi

# === 5) Reload application ===================================================
echo "🔄 Reload du service PROD…"
systemctl reload ba38-prod.service || systemctl restart ba38-prod.service
echo "✅ Service rechargé."

echo "🎉 Déploiement PROD terminé avec succès."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
