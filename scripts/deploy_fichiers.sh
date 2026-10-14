#!/usr/bin/env bash
# ============================================================================
# 🎯 Déploiement CIBLÉ DEV → PROD : seulement les fichiers indiqués
# ============================================================================
# Usage :
#   bash /srv/ba38/dev/scripts/deploy_fichiers.sh <version> "<message>" fichier1 [fichier2 ...]
#   bash /srv/ba38/dev/scripts/deploy_fichiers.sh --simulation <version> "<message>" fichier1 ...
#
# Chemins relatifs à /srv/ba38/dev (ex. ba38_fournisseurs/routes.py,
# templates/fournisseurs/fournisseurs.html).
#
# À utiliser quand DEV contient du travail en cours qui ne doit pas partir
# (deploy_to_prod.sh synchronise TOUT l'arbre). Ce script ne touche pas aux
# bases : ni migration, ni sync de la table applications, ni commit git.
# Une évolution qui a besoin d'une migration passe par deploy_to_prod.sh ou
# par une migration PROD faite à part, avant.
# ============================================================================

set -euo pipefail

if [[ "$(pwd)" == *"/prod"* ]]; then
  echo "❌ Ce script ne doit JAMAIS être lancé depuis PROD"
  exit 1
fi

BASE_DIR="/srv/ba38"
DEV_DIR="$BASE_DIR/dev"
PROD_DIR="$BASE_DIR/prod"

SIMULATION=0
if [ "${1:-}" = "--simulation" ]; then
  SIMULATION=1
  shift
fi

VERSION="${1:-}"
VERSION_MSG="${2:-}"
shift 2 || true
FICHIERS=("$@")

if [ -z "$VERSION" ] || [ -z "$VERSION_MSG" ] || [ "${#FICHIERS[@]}" -eq 0 ]; then
  echo "Usage : $0 [--simulation] <version> \"<message>\" fichier1 [fichier2 ...]"
  exit 1
fi

# ============================================================================
# 📝 Journalisation (même fichier que deploy_to_prod.sh)
# ============================================================================
LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"
if [ "$SIMULATION" -eq 0 ]; then
  exec > >(tee -a "$LOG_DIR/deploy.log") 2>&1
fi

trap 'echo "❌ ÉCHEC sur la commande : ${BASH_COMMAND}"' ERR

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎯 Déploiement ciblé BA38 DEV → PROD : $(date '+%Y-%m-%d %H:%M:%S')"
[ "$SIMULATION" -eq 1 ] && echo "🧪 MODE SIMULATION : rien ne sera modifié"
echo "📝 VERSION : $VERSION"
echo "📝 MESSAGE : $VERSION_MSG"

# ============================================================================
# 🔢 Version : x.y.z et strictement supérieure à la version PROD
# ============================================================================
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "❌ Version invalide « $VERSION » (format attendu x.y.z)"
  exit 1
fi

VERSION_PROD=$(grep "^VERSION=" "$PROD_DIR/VERSION" | cut -d '=' -f2)
VERSION_DEV=$(grep "^VERSION=" "$DEV_DIR/VERSION" | cut -d '=' -f2)
for actuelle in "$VERSION_PROD" "$VERSION_DEV"; do
  plus_haute=$(printf '%s\n%s\n' "$actuelle" "$VERSION" | sort -V | tail -1)
  if [ "$VERSION" = "$actuelle" ] || [ "$plus_haute" != "$VERSION" ]; then
    echo "❌ Version $VERSION pas supérieure à l'existant (PROD $VERSION_PROD, DEV $VERSION_DEV)"
    exit 1
  fi
done

# ============================================================================
# 🔎 Contrôle des fichiers demandés
# ============================================================================
# Jamais de données, secrets ou environnement : uniquement du code / gabarits.
INTERDITS='(^|/)(\.env|\.git|instance|venv|logs|run|uploads|exports|__pycache__)(/|$)|\.sqlite$|\.log$|^VERSION$'

echo "🔎 Fichiers à déployer :"
ERREURS=0
for f in "${FICHIERS[@]}"; do
  if [[ "$f" = /* ]] || [[ "$f" == *".."* ]]; then
    echo "   ❌ $f : chemin relatif à $DEV_DIR attendu, sans « .. »"
    ERREURS=1; continue
  fi
  if [[ "$f" =~ $INTERDITS ]]; then
    echo "   ❌ $f : chemin interdit (données, secrets ou environnement)"
    ERREURS=1; continue
  fi
  if [ ! -f "$DEV_DIR/$f" ]; then
    echo "   ❌ $f : fichier absent de DEV (les dossiers ne sont pas acceptés)"
    ERREURS=1; continue
  fi
  if [[ "$f" == *.py ]] && ! "$DEV_DIR/venv/bin/python" -m py_compile "$DEV_DIR/$f" 2>/dev/null; then
    echo "   ❌ $f : erreur de syntaxe Python"
    ERREURS=1; continue
  fi
  if [ ! -f "$PROD_DIR/$f" ]; then
    echo "   ➕ $f (nouveau en PROD)"
  elif cmp -s "$DEV_DIR/$f" "$PROD_DIR/$f"; then
    echo "   ＝ $f (identique, ignoré)"
  else
    echo "   ✏️  $f ($(diff "$PROD_DIR/$f" "$DEV_DIR/$f" | grep -c '^[<>]' || true) lignes modifiées)"
  fi
done
[ "$ERREURS" -eq 0 ] || { echo "❌ Déploiement annulé, rien n'a été modifié"; exit 1; }

# ============================================================================
# 📨 Tâches en arrière-plan en cours en PROD
# ============================================================================
echo "📨 Vérification des tâches en arrière-plan PROD"
TACHES_EN_COURS=""
for temoin in "$PROD_DIR"/run/taches/*.json; do
  [ -e "$temoin" ] || continue
  pid=$(basename "$temoin" | cut -d '-' -f1)
  cmdline=$(tr '\0' ' ' 2>/dev/null < "/proc/$pid/cmdline" || true)
  if [[ "$cmdline" == *"$PROD_DIR/venv/bin/gunicorn"* ]]; then
    TACHES_EN_COURS+="   • $(basename "$temoin")"$'\n'
  fi
done
if [ -n "$TACHES_EN_COURS" ]; then
  echo "❌ Déploiement annulé : tâche(s) en cours en PROD, qui seraient interrompues :"
  printf "%s" "$TACHES_EN_COURS"
  echo "➡️ Rien n'a été modifié. Relancer une fois la tâche terminée."
  exit 1
fi
echo "✅ Aucune tâche en cours"

if [ "$SIMULATION" -eq 1 ]; then
  echo "🧪 Simulation terminée : tout est prêt, rien n'a été modifié"
  exit 0
fi

# ============================================================================
# 💾 Sauvegarde des fichiers PROD remplacés
# ============================================================================
SAUVEGARDE="$BASE_DIR/backups/fichiers/v${VERSION}_$(date '+%Y%m%d-%H%M%S')"
mkdir -p "$SAUVEGARDE"
NOUVEAUX=()
for f in "${FICHIERS[@]}"; do
  if [ -f "$PROD_DIR/$f" ]; then
    mkdir -p "$SAUVEGARDE/$(dirname "$f")"
    cp -a "$PROD_DIR/$f" "$SAUVEGARDE/$f"
  else
    NOUVEAUX+=("$f")
  fi
done
printf '%s\n' "${NOUVEAUX[@]+"${NOUVEAUX[@]}"}" > "$SAUVEGARDE/NOUVEAUX_FICHIERS.txt"
echo "💾 Anciennes versions sauvegardées dans $SAUVEGARDE"

# ============================================================================
# 📁 Copie
# ============================================================================
echo "📁 Copie DEV → PROD"
for f in "${FICHIERS[@]}"; do
  mkdir -p "$PROD_DIR/$(dirname "$f")"
  cp "$DEV_DIR/$f" "$PROD_DIR/$f"
done

# ============================================================================
# 📝 VERSION
# ============================================================================
echo "📝 Mise à jour VERSION"
DATE_NOW=$(date '+%Y-%m-%d %H:%M')
for v in "$PROD_DIR/VERSION" "$DEV_DIR/VERSION"; do
  printf 'VERSION=%s\nMESSAGE=%s\nDATE=%s\n' "$VERSION" "$VERSION_MSG" "$DATE_NOW" > "$v"
done

# ============================================================================
# 🔄 Rechargement à chaud (même logique que deploy_to_prod.sh)
# ============================================================================
restart_complet() {
  echo "🔄 Redémarrage complet ba38-prod ($1)"
  sudo systemctl restart ba38-prod.service
}

PROD_MASTER=$(systemctl show -p MainPID --value ba38-prod.service)
if [ "$PROD_MASTER" = "0" ] || [ -z "$PROD_MASTER" ]; then
  restart_complet "service arrêté"
else
  echo "🔄 Rechargement à chaud ba38-prod (SIGHUP, master $PROD_MASTER)"
  ANCIENS_WORKERS=$(pgrep -P "$PROD_MASTER" | sort || true)
  kill -HUP "$PROD_MASTER"

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

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:8001/__ping || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
  echo "❌ PROD ne répond pas (HTTP $HTTP_CODE). Retour arrière : recopier $SAUVEGARDE vers $PROD_DIR"
  echo "   (supprimer aussi les fichiers listés dans NOUVEAUX_FICHIERS.txt), puis relancer ba38-prod"
  exit 1
fi

echo "🎉 Déploiement ciblé v$VERSION terminé (PROD HTTP $HTTP_CODE)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
