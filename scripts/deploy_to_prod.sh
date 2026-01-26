#!/usr/bin/env bash
# ============================================================================
# 🚀 Déploiement DEV → PROD pour BA38 (serveur Debian)
# ============================================================================
#
# Objectifs :
# - Déployer le code DEV vers PROD
# - Sauvegarder la base PROD avant toute modification
# - Migrer automatiquement schéma + données si nécessaire
# - Synchroniser le code via rsync (exclusions strictes)
# - Mettre à jour VERSION / VERSION_MSG en PROD
# - Recharger le service systemd
# - Journaliser l’intégralité du déploiement dans un log global
#
# Logs :
#   /srv/ba38/logs/deploy.log   (historique global des déploiements)
#
# ⚠️ Ce script DOIT être lancé depuis DEV uniquement
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
# 📝 Journalisation globale des déploiements
# ============================================================================
LOG_DIR="$BASE_DIR/logs"
LOG_FILE="$LOG_DIR/deploy.log"

mkdir -p "$LOG_DIR"

# Redirige stdout + stderr vers le log (et console)
exec > >(tee -a "$LOG_FILE") 2>&1

# Trace toute erreur bloquante
trap 'echo "❌ ÉCHEC sur la commande : ${BASH_COMMAND}"' ERR

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 Déploiement BA38 DEV → PROD : $(date '+%Y-%m-%d %H:%M:%S')"
echo "📝 VERSION détectée : ${VERSION:-inconnue}"
echo "📝 MESSAGE associé : ${VERSION_MSG:-non défini}"


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
# 🗄️ Bases SQLite (définies exclusivement via .env)
# ============================================================================
DEV_DB="$DEV_DIR/$SQLITE_DB_DEV"
PROD_DB="$PROD_DIR/$SQLITE_DB_PROD"

# ============================================================================
# 🔍 Vérifications préalables
# ============================================================================
[ -d "$DEV_DIR" ] || { echo "❌ Dossier DEV introuvable"; exit 1; }
[ -f "$DEV_DB" ]  || { echo "❌ Base DEV absente : $DEV_DB"; exit 1; }

# ============================================================================
# 🧠 Fonctions SQLite (comparaison de schéma)
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
  local diff=0
  local all_tables

  all_tables=$(printf "%s\n%s\n" \
    "$(get_tables "$DEV_DB")" \
    "$(get_tables "$PROD_DB" 2>/dev/null || true)" | sort -u)

  echo "🔍 Comparaison des schémas SQLite…"

  for table in $all_tables; do
    if ! sqlite3 "$PROD_DB" ".tables" | grep -qw "$table"; then
      diff=1
      continue
    fi

    diff <(normalize_table "$DEV_DB" "$table" | sort) \
         <(normalize_table "$PROD_DB" "$table" | sort) || diff=1
  done

  return $diff
}

# ============================================================================
# 💾 1) Sauvegarde de la base PROD
# ============================================================================
if [ -f "$PROD_DB" ]; then
  echo "💾 Sauvegarde de la base PROD…"
  "$SCRIPTS_DIR/backup_prod.sh"
fi

# ============================================================================
# 🔧 2) Migration schéma / données si nécessaire
# ============================================================================
if [ -f "$PROD_DB" ] && compare_schemas; then
  echo "✅ Schémas DEV / PROD identiques"
else
  echo "🔧 Migration schéma et données DEV → PROD"
  python3 "$SCRIPTS_DIR/migrate_schema_and_data_dev_to_prod.py"
  compare_schemas || { echo "❌ Migration invalide"; exit 1; }
fi

# ============================================================================
# 📁 3) Synchronisation du code (rsync)
# ============================================================================
echo "📁 Synchronisation du code DEV → PROD"

rsync -av --delete \
  --exclude ".env" \
  --exclude ".git/" \
  --exclude ".git_OLD_ba380DEV/" \
  --exclude ".vscode/" \
  --exclude "backup/" \
  --exclude "flask_sessions/" \
  --exclude "logs/" \
  --exclude "*.log" \
  --exclude "*.log.*" \
  --exclude "instance/" \
  --exclude "*.sqlite" \
  --exclude "static/uploads/" \
  --exclude "static/factures/archives/" \
  --exclude "__pycache__/" \
  --exclude "venv/" \
  --exclude "sessions/" \
  --exclude "flask_sessions/" \
  "$DEV_DIR/" "$PROD_DIR/"

# ============================================================================
# 📝 4) Mise à jour VERSION et VERSION_MSG en PROD
# ============================================================================
echo "📝 Mise à jour VERSION et VERSION_MSG dans .env PROD"

touch "$PROD_ENV"

# Nettoyage préalable
sed -i '/^VERSION=/d' "$PROD_ENV"
sed -i '/^VERSION_MSG=/d' "$PROD_ENV"

# Écriture propre
{
  echo "VERSION=\"$VERSION\""
  echo "VERSION_MSG=\"$VERSION_MSG\""
} >> "$PROD_ENV"

# ============================================================================
# 🔄 5) Reload du service systemd
# ============================================================================
echo "🔄 Reload du service ba38-prod"
sudo systemctl reload ba38-prod.service || \
sudo systemctl restart ba38-prod.service

echo "🎉 DÉPLOIEMENT PROD TERMINÉ AVEC SUCCÈS"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
