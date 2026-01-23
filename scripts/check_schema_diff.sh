#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# 📦 BA38 — Comparaison des schémas SQLite DEV vs PROD
#
# Objectif :
# - Comparer la structure des bases DEV et PROD
#   (tables, colonnes, types, NOT NULL, DEFAULT, PK, AUTOINCREMENT)
# - Comparer les valeurs parametres.type_champ entre DEV et PROD
#
# Principes d’architecture :
# - Les chemins des bases sont définis dans .env (chemins relatifs)
# - Le script reconstruit des chemins ABSOLUS de manière explicite
# - Aucune dépendance au répertoire courant (pwd)
# - Compatible set -euo pipefail
###############################################################################

# ============================================================
# 📁 Localisation du projet et chargement du .env
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$BASE_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ Fichier .env introuvable : $ENV_FILE"
  exit 1
fi

# Charger les variables d’environnement
set -a
source "$ENV_FILE"
set +a

# ============================================================
# 🗄️ Résolution propre des chemins SQLite
# ============================================================
#
# Règle BA38 :
# - SQLITE_DB_* sont des chemins RELATIFS
# - BA38_BASE_DIR pointe vers /srv/ba38/dev ou /srv/ba38/prod
#

if [[ -z "${BA38_BASE_DIR:-}" ]]; then
  echo "❌ BA38_BASE_DIR non défini dans le .env"
  exit 1
fi

if [[ -z "${SQLITE_DB_DEV:-}" || -z "${SQLITE_DB_PROD:-}" ]]; then
  echo "❌ Variables SQLITE_DB_DEV / SQLITE_DB_PROD non définies"
  exit 1
fi

DEV_DB="$BA38_BASE_DIR/$SQLITE_DB_DEV"
PROD_DB="${BA38_BASE_DIR/dev/prod}/$SQLITE_DB_PROD"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 Comparaison des schémas SQLite DEV vs PROD"
echo "DEV  : $DEV_DB"
echo "PROD : $PROD_DB"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Vérification d’existence des fichiers
if [[ ! -f "$DEV_DB" ]]; then
  echo "❌ Base DEV introuvable : $DEV_DB"
  exit 1
fi

if [[ ! -f "$PROD_DB" ]]; then
  echo "❌ Base PROD introuvable : $PROD_DB"
  exit 1
fi

# ============================================================
# 🔍 Fonctions utilitaires SQLite
# ============================================================

# Liste des tables (hors tables système)
get_tables() {
  sqlite3 "$1" \
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
}

# Normalisation de la structure d’une table
# colonnes : name | type | notnull | dflt_value | pk
normalize_table() {
  local db="$1" table="$2"
  sqlite3 "$db" "PRAGMA table_info('$table');" \
    | awk -F'|' '{print $2 "|" $3 "|" $4 "|" $5 "|" $6}'
}

# Détection AUTOINCREMENT (non couvert par PRAGMA table_info)
has_autoinc() {
  local db="$1" table="$2"
  if sqlite3 "$db" \
      "SELECT sql FROM sqlite_master WHERE type='table' AND name='$table';" \
      | grep -qi 'autoincrement'; then
    echo "AUTOINCREMENT=YES"
  else
    echo "AUTOINCREMENT=NO"
  fi
}

# ============================================================
# 📦 Comparaison des schémas
# ============================================================

DEV_TABLES="$(get_tables "$DEV_DB")"
PROD_TABLES="$(get_tables "$PROD_DB")"
ALL_TABLES="$(printf "%s\n%s\n" "$DEV_TABLES" "$PROD_TABLES" | sort -u)"

diff_found=0

while IFS= read -r table; do
  [[ -z "$table" ]] && continue

  in_dev="$(printf "%s\n" "$DEV_TABLES" | grep -Fx "$table" || true)"
  in_prod="$(printf "%s\n" "$PROD_TABLES" | grep -Fx "$table" || true)"

  if [[ -z "$in_dev" ]]; then
    echo "➕ Table présente en PROD mais absente en DEV : $table"
    diff_found=1
    continue
  fi

  if [[ -z "$in_prod" ]]; then
    echo "➕ Table présente en DEV mais absente en PROD : $table"
    diff_found=1
    continue
  fi

  tmp_dev="$(mktemp)"
  tmp_prod="$(mktemp)"

  normalize_table "$DEV_DB" "$table" | sort > "$tmp_dev"
  normalize_table "$PROD_DB" "$table" | sort > "$tmp_prod"

  if ! diff -u "$tmp_dev" "$tmp_prod" >/dev/null; then
    echo "❗Différences sur la table $table :"
    diff -u "$tmp_dev" "$tmp_prod" | sed 's/^/  /'
    diff_found=1
  fi

  rm -f "$tmp_dev" "$tmp_prod"

  ai_dev="$(has_autoinc "$DEV_DB" "$table")"
  ai_prod="$(has_autoinc "$PROD_DB" "$table")"

  if [[ "$ai_dev" != "$ai_prod" ]]; then
    echo "❗$table : $ai_dev vs $ai_prod"
    diff_found=1
  fi

done <<< "$ALL_TABLES"

if [[ $diff_found -eq 0 ]]; then
  echo "✅ Aucun écart structurel significatif."
else
  echo "🔎 Fin de comparaison. Des différences sont listées ci-dessus."
fi

# ============================================================
# 📊 Comparaison parametres.type_champ
# ============================================================

echo ""
echo "📊 Comparaison des valeurs 'type_champ' (table parametres)..."

has_params_dev="$(sqlite3 "$DEV_DB" \
  "SELECT 1 FROM sqlite_master WHERE type='table' AND name='parametres';")"

has_params_prod="$(sqlite3 "$PROD_DB" \
  "SELECT 1 FROM sqlite_master WHERE type='table' AND name='parametres';")"

if [[ -z "$has_params_dev" || -z "$has_params_prod" ]]; then
  echo "⚠️ Table 'parametres' absente dans l'une des bases :"
  [[ -z "$has_params_dev" ]] && echo "  - absente en DEV"
  [[ -z "$has_params_prod" ]] && echo "  - absente en PROD"
  echo "➡️ Comparaison 'type_champ' ignorée."
else
  tmp_dev="$(mktemp)"
  tmp_prod="$(mktemp)"

  sqlite3 "$DEV_DB" \
    "SELECT param_value FROM parametres
     WHERE param_name = 'type_champ'
     ORDER BY param_value;" > "$tmp_dev"

  sqlite3 "$PROD_DB" \
    "SELECT param_value FROM parametres
     WHERE param_name = 'type_champ'
     ORDER BY param_value;" > "$tmp_prod"

  if ! diff -u "$tmp_dev" "$tmp_prod" >/dev/null; then
    echo "❗Différences détectées dans les valeurs 'type_champ' :"
    diff -u "$tmp_dev" "$tmp_prod" | sed 's/^/  /'
    echo "💡 Pense à synchroniser les paramètres manquants en PROD."
  else
    echo "✅ Paramètres 'type_champ' identiques entre DEV et PROD."
  fi

  rm -f "$tmp_dev" "$tmp_prod"
fi
