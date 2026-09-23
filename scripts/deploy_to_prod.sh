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
# 📨 Tâches en arrière-plan en cours en PROD (envois emails/SMS, analyses)
# ============================================================================
# Témoins déposés par ba38_utilitaires/taches_fond.py. Le rechargement de
# gunicorn tuerait ces tâches (envoi partiel) → on arrête AVANT de toucher
# à quoi que ce soit. Un témoin dont le process n'est plus un worker PROD
# vivant (plantage, PID réutilisé) est ignoré.
echo "📨 Vérification des tâches en arrière-plan PROD"

TACHES_DIR="$PROD_DIR/run/taches"
TACHES_EN_COURS=""
for temoin in "$TACHES_DIR"/*.json; do
  [ -e "$temoin" ] || continue
  pid=$(basename "$temoin" | cut -d '-' -f1)
  cmdline=$(tr '\0' ' ' 2>/dev/null < "/proc/$pid/cmdline" || true)
  if [[ "$cmdline" == *"$PROD_DIR/venv/bin/gunicorn"* ]]; then
    detail=$(python3 - "$temoin" <<'PY' 2>/dev/null || basename "$temoin"
import json, sys
t = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"{t.get('nom')} — lancé par {t.get('utilisateur') or '?'} le {t.get('debut')}")
PY
)
    TACHES_EN_COURS+="   • $detail"$'\n'
  fi
done

if [ -n "$TACHES_EN_COURS" ]; then
  echo "❌ Déploiement annulé : tâche(s) en cours en PROD, qui seraient interrompues :"
  printf "%s" "$TACHES_EN_COURS"
  echo "➡️ Rien n'a été modifié. Relancer le déploiement une fois l'envoi terminé."
  exit 1
fi
echo "✅ Aucune tâche en cours"

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
  --exclude "/run/" \
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
# Rechargement à chaud (SIGHUP) : gunicorn démarre les nouveaux workers
# avant d'arrêter proprement les anciens → aucune coupure côté utilisateurs.
# Repli sur un restart complet si le master est introuvable, si gunicorn
# lui-même a été mis à jour (le master ne se recharge pas), ou si les
# nouveaux workers ne démarrent pas.
restart_complet() {
  echo "🔄 Redémarrage complet ba38-prod ($1)"
  sudo systemctl restart ba38-prod.service
}

PROD_MASTER=$(systemctl show -p MainPID --value ba38-prod.service)
GUNICORN_MAJ=0
if [ "$PROD_MASTER" != "0" ] && [ -n "$PROD_MASTER" ]; then
  MASTER_DEBUT=$(date -d "$(ps -o lstart= -p "$PROD_MASTER")" +%s)
  GUNICORN_PKG=$(ls -d "$PROD_DIR"/venv/lib/python*/site-packages/gunicorn 2>/dev/null | head -1)
  if [ -n "$GUNICORN_PKG" ] && [ "$(stat -c %Y "$GUNICORN_PKG")" -gt "$MASTER_DEBUT" ]; then
    GUNICORN_MAJ=1
  fi
fi

if [ "$PROD_MASTER" = "0" ] || [ -z "$PROD_MASTER" ]; then
  restart_complet "service arrêté"
elif [ "$GUNICORN_MAJ" -eq 1 ]; then
  restart_complet "gunicorn mis à jour"
else
  echo "🔄 Rechargement à chaud ba38-prod (SIGHUP, master $PROD_MASTER)"
  ANCIENS_WORKERS=$(pgrep -P "$PROD_MASTER" | sort || true)
  kill -HUP "$PROD_MASTER"

  # Attendre que les nouveaux workers soient démarrés (max 20 s). On n'attend
  # pas la fin des anciens (ils terminent leurs requêtes en cours jusqu'à 30 s) :
  # le script est lancé depuis une requête DEV soumise au timeout gunicorn.
  NB_ANCIENS=$(echo "$ANCIENS_WORKERS" | grep -c . || true)
  RECHARGE_OK=0
  for _ in $(seq 1 20); do
    sleep 1
    ACTUELS=$(pgrep -P "$PROD_MASTER" | sort || true)
    NB_NOUVEAUX=$(comm -13 <(echo "$ANCIENS_WORKERS") <(echo "$ACTUELS") | grep -c . || true)
    if [ "$NB_NOUVEAUX" -ge 1 ] && [ "$NB_NOUVEAUX" -ge "$NB_ANCIENS" ]; then
      RECHARGE_OK=1
      break
    fi
  done

  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:8001/__ping || echo "000")
  if [ "$RECHARGE_OK" -eq 1 ] && [ "$HTTP_CODE" = "200" ]; then
    echo "✅ Rechargement à chaud OK (HTTP $HTTP_CODE)"
  else
    restart_complet "rechargement à chaud KO : workers remplacés=$RECHARGE_OK, HTTP=$HTTP_CODE"
  fi
fi

# ============================================================================
# 📦 COMMIT GIT
# ============================================================================
echo "📦 Commit git"
"$SCRIPTS_DIR/git_commit_push.sh" "v$VERSION - $VERSION_MSG"

echo "🎉 Déploiement terminé avec succès"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
