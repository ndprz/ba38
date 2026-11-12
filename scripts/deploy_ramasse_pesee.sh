#!/usr/bin/env bash
# ============================================================================
# Renommage du champ bénévoles prep_pesee -> ramasse_pesee (DEV -> PROD)
# + libellés « Planning Pesée ramasse »
# ============================================================================
# Usage (depuis n'importe où, PAS en root) :
#   bash /srv/ba38/dev/scripts/deploy_ramasse_pesee.sh [--simulation] [version]
#
# Déroulé :
#   1. contrôles (colonne présente en PROD, absente la cible, simulation du
#      déploiement ciblé = syntaxe, version, tâches de fond)
#   2. sauvegarde des 2 bases PROD (ba380 + ba380_test)
#   3. renommage de la colonne (benevoles, benevoles_inactifs) + field_groups
#   4. déploiement ciblé des 7 fichiers + rechargement à chaud
#   5. si le déploiement échoue : colonne remise à l'ancien nom
# Coupure entre 3 et 4 : quelques secondes (pages pesée/VIF en erreur).
# ============================================================================
set -euo pipefail

BASE="/srv/ba38"
DEV="$BASE/dev"
PROD="$BASE/prod"
ANCIEN="prep_pesee"
NOUVEAU="ramasse_pesee"
BASES=("$PROD/instance/ba380.sqlite" "$PROD/instance/ba380_test.sqlite")

SIMU=0
if [ "${1:-}" = "--simulation" ]; then SIMU=1; shift; fi
VERSION="${1:-1.7.290}"
MESSAGE="Champ benevoles prep_pesee renommé ramasse_pesee + libellés Planning Pesée ramasse"
FICHIERS=(
  ba38_planning/pesee.py
  ba38_planning/vif.py
  templates/planning/pesee/gestion_planning_pesee.html
  templates/planning/pesee/maj_modele_planning_pesee.html
  templates/planning/pesee/creation_planning_pesee.html
  templates/planning/pesee/apercu_planning_pesee.html
  templates/planning/pesee/apercu_modele_planning_pesee.html
  templates/planning/planning_main.html
  scripts/add_fonction_tri_entrepot.py
)

renommer() {  # $1 = base, $2 = de, $3 = vers
  sqlite3 "$1" "PRAGMA busy_timeout=30000;
BEGIN IMMEDIATE;
ALTER TABLE benevoles RENAME COLUMN $2 TO $3;
ALTER TABLE benevoles_inactifs RENAME COLUMN $2 TO $3;
UPDATE field_groups SET field_name='$3' WHERE field_name='$2';
COMMIT;" >/dev/null
}
a_colonne() {  # $1 = base, $2 = nom -> nombre
  sqlite3 -readonly "$1" "select count(*) from pragma_table_info('benevoles') where name='$2'"
}

echo "== 1. Contrôles"
for db in "${BASES[@]}"; do
  [ -f "$db" ] || { echo "❌ base introuvable : $db"; exit 1; }
  [ "$(a_colonne "$db" "$ANCIEN")" = "1" ] || { echo "❌ $db : colonne $ANCIEN absente (déjà renommée ?)"; exit 1; }
  [ "$(a_colonne "$db" "$NOUVEAU")" = "0" ] || { echo "❌ $db : colonne $NOUVEAU existe déjà"; exit 1; }
  echo "✅ $(basename "$db") : $ANCIEN présent, $NOUVEAU absent"
done
grep -rIl "$ANCIEN" "$DEV" --include=*.py --include=*.html --exclude-dir=archive --exclude-dir=__pycache__ --exclude-dir=venv 2>/dev/null \
  | grep -v "/scripts/archive/" && { echo "❌ $ANCIEN encore présent dans DEV (fichiers ci-dessus)"; exit 1; } || true
echo "✅ plus aucune référence à $ANCIEN dans DEV"

cd "$DEV"
bash scripts/deploy_fichiers.sh --simulation "$VERSION" "$MESSAGE" "${FICHIERS[@]}"

if [ "$SIMU" = "1" ]; then
  echo "🧪 Simulation terminée : rien n'a été modifié."
  exit 0
fi

echo "== 2. Sauvegarde des bases PROD"
DEST="$BASE/backups/fichiers/ramasse_pesee_$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"
for db in "${BASES[@]}"; do
  sqlite3 "$db" ".backup $DEST/$(basename "$db")"
  echo "💾 $DEST/$(basename "$db")"
done

echo "== 3. Renommage des colonnes PROD"
for db in "${BASES[@]}"; do renommer "$db" "$ANCIEN" "$NOUVEAU"; echo "✅ $(basename "$db")"; done

echo "== 4. Déploiement des fichiers"
if ! bash scripts/deploy_fichiers.sh "$VERSION" "$MESSAGE" "${FICHIERS[@]}"; then
  echo "❌ Déploiement échoué : retour à l'ancien nom de colonne"
  for db in "${BASES[@]}"; do renommer "$db" "$NOUVEAU" "$ANCIEN"; echo "↩️  $(basename "$db")"; done
  echo "⚠️  Vérifier l'état de PROD (sauvegardes dans $DEST)"
  exit 1
fi

echo "== 5. Vérification"
for db in "${BASES[@]}"; do
  echo "$(basename "$db") : $NOUVEAU=$(a_colonne "$db" "$NOUVEAU") $ANCIEN=$(a_colonne "$db" "$ANCIEN")"
done
echo "🎉 Terminé : v$VERSION. Sauvegardes : $DEST"
