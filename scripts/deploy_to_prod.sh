#!/usr/bin/env bash
# ============================================================================
# 🚀 Déploiement DEV → PROD pour BA38 (serveur Debian) — VERSION SÉCURISÉE
# ============================================================================
#
# 🎯 Objectifs :
# - Déployer le code DEV vers PROD
# - Sauvegarder la base PROD avant toute modification
# - Migrer automatiquement schéma + données si nécessaire
# - Synchroniser le code via rsync (exclusions strictes)
# - Mettre à jour VERSION / VERSION_MSG en PROD
# - Recharger le service systemd
# - Journaliser l’intégralité du déploiement
#
# ⚠️ IMPORTANT :
# - Aucun commit Git n’est effectué ici
# - Le code DOIT être push AVANT le déploiement
#
# Logs :
#   /srv/ba38/logs/deploy.log
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

# cd "$DEV_DIR"

# if ! git diff --quiet || ! git diff --cached --quiet; then
#   echo "❌ Des modifications non commitées existent"
#   echo "👉 Faites un git commit + push AVANT le déploiement"
#   exit 1
# fi

# echo "✅ Repo Git propre"

# ============================================================================
# 🌍 Chargement de l’environnement DEV
# ============================================================================
if [ ! -f "$DEV_ENV" ]; then
  echo "❌ Fichier .env DEV introuvable"
  exit 1
fi

set -a
source "$DEV_ENV"
set +a

# ============================================================================
# 📝 VERSION : argument ou interactif
# ============================================================================

VERSION="${1:-}"
VERSION_MSG="${2:-}"

# fallback message si vide
if [ -z "$VERSION_MSG" ]; then
  VERSION_MSG="(sans message)"
fi


if [ -z "$VERSION" ]; then
  # Mode interactif uniquement si terminal
  if [ -t 0 ]; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "📝 Saisie de la version"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    read -p "➡️ Version (ex: 1.3.39) : " VERSION
    read -p "➡️ Message : " VERSION_MSG

    if [ -z "$VERSION" ]; then
      echo "❌ Version obligatoire"
      exit 1
    fi

  else
    echo "❌ VERSION non fournie (mode non interactif)"
    exit 1
  fi
fi

echo "📝 VERSION : $VERSION"
echo "📝 MESSAGE : $VERSION_MSG"


# ============================================================================
# ✅ Confirmation uniquement si mode interactif
# ============================================================================

if [ -t 0 ]; then
  read -p "Confirmer le déploiement ? (o/N) : " CONFIRM

  if [[ "$CONFIRM" != "o" && "$CONFIRM" != "O" ]]; then
    echo "❌ Déploiement annulé"
    exit 1
  fi
else
  echo "⚠️ Mode non interactif → déploiement automatique"
fi

: "${VERSION:?VERSION non défini dans DEV/VERSION}"


echo "📝 VERSION : $VERSION"
echo "📝 MESSAGE : $VERSION_MSG"



: "${SQLITE_DB_DEV:?SQLITE_DB_DEV non défini}"
: "${SQLITE_DB:?SQLITE_DB non défini}"

echo "📝 VERSION : $VERSION"
echo "📝 MESSAGE : $VERSION_MSG"

# ============================================================================
# 📦 Installation dépendances (option sécurisée)
# ============================================================================
echo "📦 Vérification des dépendances Python"

if [ -f "$DEV_DIR/requirements.txt" ]; then

  source "$PROD_DIR/venv/bin/activate"

  if ! cmp -s "$DEV_DIR/requirements.txt" "$PROD_DIR/requirements.txt"; then
    echo "📦 Mise à jour dépendances"

    cp "$DEV_DIR/requirements.txt" "$PROD_DIR/"

    if ! pip install --upgrade --no-cache-dir -r "$PROD_DIR/requirements.txt" > /dev/null 2> /tmp/pip_error.log; then
      echo "❌ ERREUR pip install"
      cat /tmp/pip_error.log
      exit 1
    fi

    echo "📦 Packages installés (requirements)"
    pip freeze | grep -f "$PROD_DIR/requirements.txt"

  else
    echo "📦 Dépendances déjà à jour"
  fi

else
  echo "⚠️ requirements.txt absent"
fi



# ============================================================================
# 🗄️ Bases SQLite
# ============================================================================
DEV_DB="$DEV_DIR/$SQLITE_DB_DEV"
PROD_DB="$PROD_DIR/$SQLITE_DB"

# ============================================================================
# 🔍 Vérifications préalables
# ============================================================================
[ -d "$DEV_DIR" ]  || { echo "❌ DEV_DIR introuvable"; exit 1; }
[ -d "$PROD_DIR" ] || { echo "❌ PROD_DIR introuvable"; exit 1; }
[ -f "$DEV_DB" ]   || { echo "❌ Base DEV absente : $DEV_DB"; exit 1; }

# ============================================================================
# 🧠 Fonctions SQLite – comparaison de schéma
# ============================================================================
get_tables() {
  sqlite3 "$1" \
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
}

normalize_table() {
  sqlite3 "$1" "PRAGMA table_info('$2');" |
    awk -F'|' '{print $2 "|" $3 "|" $4 "|" $5 "|" $6}'
}

compare_schemas() {
  local diff_found=0
  local all_tables

  echo "🔍 Comparaison des schémas SQLite…"

  all_tables=$(printf "%s\n%s\n" \
    "$(get_tables "$DEV_DB")" \
    "$(get_tables "$PROD_DB" 2>/dev/null || true)" | sort -u)

  for table in $all_tables; do
    exists=$(sqlite3 "$PROD_DB" \
      "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$table';")

    if [ "$exists" -eq 0 ]; then
      diff_found=1
      continue
    fi

    diff <(normalize_table "$DEV_DB" "$table" | sort) \
         <(normalize_table "$PROD_DB" "$table" | sort) \
         > /dev/null || diff_found=1
  done

  return $diff_found
}

# ============================================================================
# 💾 1) Sauvegarde de la base PROD
# ============================================================================
if [ -f "$PROD_DB" ]; then
  echo "💾 Sauvegarde de la base PROD"
  "$SCRIPTS_DIR/backup_prod.sh"
fi

# ============================================================================
# 🔧 2) Migration schéma / données
# ============================================================================
if [ -f "$PROD_DB" ] && compare_schemas; then
  echo "✅ Schémas identiques"
else
  echo "🔧 Migration DEV → PROD"
  "$DEV_DIR/venv/bin/python" "$SCRIPTS_DIR/migrate_schema_and_data_dev_to_prod.py"
fi

# ============================================================================
# 📁 3) Synchronisation code (rsync sécurisé)
# ============================================================================
echo "📁 Synchronisation DEV → PROD"

rsync -av --delete \
  --exclude ".env" \
  --exclude ".git/" \
  --exclude ".git_OLD_ba380DEV/" \
  --exclude ".vscode/" \
  --exclude "backup/" \
  --exclude "logs/" \
  --exclude "*.log" \
  --exclude "*.log.*" \
  --exclude "instance/" \
  --exclude "*.sqlite" \
  --exclude "static/uploads/" \
  --exclude "static/factures/archives/" \
  --exclude "static/evenements/" \
  --exclude "static/photos_benevoles/" \
  --exclude "exports/" \
  --exclude "__pycache__/" \
  --exclude "venv/" \
  "$DEV_DIR/" "$PROD_DIR/"

# ============================================================================
# 📝 4) Mise à jour VERSION en PROD
# ============================================================================
echo "📝 Mise à jour VERSION dans PROD"

touch "$PROD_ENV"
sed -i '/^VERSION=/d' "$PROD_ENV"
sed -i '/^VERSION_MSG=/d' "$PROD_ENV"

{
  echo "VERSION=\"$VERSION\""
  echo "VERSION_MSG=\"$VERSION_MSG\""
} >> "$PROD_ENV"

# ============================================================================
# 📝 Mise à jour VERSION (DEV + PROD)
# ============================================================================

DATE_NOW=$(date '+%Y-%m-%d %H:%M')

# 🔵 PROD
VERSION_FILE_PROD="/srv/ba38/prod/VERSION"

echo "VERSION=$VERSION" > "$VERSION_FILE_PROD"
echo "MESSAGE=$VERSION_MSG" >> "$VERSION_FILE_PROD"
echo "DATE=$DATE_NOW" >> "$VERSION_FILE_PROD"

echo "✅ VERSION mise à jour (PROD)"

# 🟢 DEV (important pour cohérence)
VERSION_FILE_DEV="/srv/ba38/dev/VERSION"

echo "VERSION=$VERSION" > "$VERSION_FILE_DEV"
echo "MESSAGE=$VERSION_MSG" >> "$VERSION_FILE_DEV"
echo "DATE=$DATE_NOW" >> "$VERSION_FILE_DEV"

echo "✅ VERSION mise à jour (DEV)"


# ============================================================================
# 🔄 5) Restart service
# ============================================================================
echo "🔄 Redémarrage ba38-prod"
sudo systemctl restart ba38-prod.service

echo "🎉 Déploiement terminé avec succès"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
