#!/usr/bin/env bash
set -euo pipefail

DEV_DB="${1:-/home/ndprz/dev/ba380dev.sqlite}"
PROD_DB="${2:-/home/ndprz/ba380/ba380.sqlite}"

get_tables() {
  sqlite3 "$1" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
}

normalize_table() {
  local db="$1" table="$2"
  # colonnes: name|type|notnull|dflt_value|pk (ordre stable par cid)
  sqlite3 "$db" "PRAGMA table_info('$table');" | awk -F'|' '{print $2 "|" $3 "|" $4 "|" $5 "|" $6}'
}

has_autoinc() {
  local db="$1" table="$2"
  if sqlite3 "$db" "SELECT sql FROM sqlite_master WHERE type='table' AND name='$table';" \
     | grep -qi 'autoincrement'; then
    echo "AUTOINCREMENT=YES"
  else
    echo "AUTOINCREMENT=NO"
  fi
}

echo "📦 Comparaison des schémas SQLite DEV vs PROD…"

# Union des tables
DEV_TABLES=$(get_tables "$DEV_DB")
PROD_TABLES=$(get_tables "$PROD_DB")
ALL_TABLES=$(printf "%s\n%s\n" "$DEV_TABLES" "$PROD_TABLES" | sort -u)

diff_found=0

while IFS= read -r t; do
  [ -z "$t" ] && continue

  in_dev=$(printf "%s\n" "$DEV_TABLES" | grep -Fx "$t" || true)
  in_prod=$(printf "%s\n" "$PROD_TABLES" | grep -Fx "$t" || true)

  if [ -z "$in_dev" ]; then
    echo "➕ Table présente en PROD mais absente en DEV : $t"
    diff_found=1
    continue
  fi
  if [ -z "$in_prod" ]; then
    echo "➕ Table présente en DEV mais absente en PROD : $t"
    diff_found=1
    continue
  fi

  tmp_dev=$(mktemp) ; tmp_prod=$(mktemp)
  normalize_table "$DEV_DB" "$t" | sort > "$tmp_dev"
  normalize_table "$PROD_DB" "$t" | sort > "$tmp_prod"

  # Compare colonnes/types/default/PK/NOT NULL (insensible aux espaces/retours ligne d’origine)
  if ! diff -u "$tmp_dev" "$tmp_prod" >/dev/null; then
    echo "❗Différences sur la table $t :"
    diff -u "$tmp_dev" "$tmp_prod" | sed "s/^/  /"
    diff_found=1
  fi
  rm -f "$tmp_dev" "$tmp_prod"

  # Compare AUTOINCREMENT (non couvert par PRAGMA table_info)
  ai_dev=$(has_autoinc "$DEV_DB" "$t")
  ai_prod=$(has_autoinc "$PROD_DB" "$t")
  if [ "$ai_dev" != "$ai_prod" ]; then
    echo "❗$t : $ai_dev vs $ai_prod"
    diff_found=1
  fi
done <<< "$ALL_TABLES"

if [ $diff_found -eq 0 ]; then
  echo "✅ Aucun écart structurel significatif (colonnes/types/default/PK/autoincrement)."
else
  echo "🔎 Fin de comparaison. Des différences réelles sont listées ci‑dessus."
fi


echo ""
echo "📊 Comparaison des valeurs 'type_champ' dans la table parametres..."

sqlite3 "$DEV_DB" "SELECT param_value FROM parametres WHERE param_name = 'type_champ' ORDER BY param_value;" > /tmp/params_dev.txt
sqlite3 "$PROD_DB" "SELECT param_value FROM parametres WHERE param_name = 'type_champ' ORDER BY param_value;" > /tmp/params_prod.txt

diff /tmp/params_dev.txt /tmp/params_prod.txt > /tmp/params_diff.txt

if [ -s /tmp/params_diff.txt ]; then
    echo "❗Différences détectées dans les valeurs 'type_champ' :"
    cat /tmp/params_diff.txt
    echo "💡 Pense à insérer les paramètres manquants dans la PROD."
else
    echo "✅ Paramètres 'type_champ' identiques entre DEV et PROD."
fi