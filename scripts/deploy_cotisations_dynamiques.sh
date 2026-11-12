#!/usr/bin/env bash
# ============================================================================
# Cotisations bénévoles dynamiques (table benevoles_cotisations) : DEV -> PROD
# ============================================================================
# Usage (PAS en root) :
#   bash /srv/ba38/dev/scripts/deploy_cotisations_dynamiques.sh [--simulation] [version]
#
# ⚠️ Les données de cotisations de DEV ne sont JAMAIS copiées en PROD : la table
# PROD est créée et remplie depuis les colonnes cotisation_AAAA de PROD
# (migrate_benevoles_cotisations.py --env prod / prod_test). Le script générique
# migrate_schema_and_data_dev_to_prod.py n'est pas lancé, et la table est en
# EXCLUDE_TABLES pour les prochains déploiements complets.
#
# Déroulé :
#   1. contrôles (bases présentes, table absente, colonnes présentes, simulation)
#   2. sauvegarde des 2 bases PROD (ba380 + ba380_test)
#   3. création + remplissage de benevoles_cotisations depuis les colonnes PROD
#   4. déploiement ciblé des fichiers + rechargement à chaud
#   5. retrait des 7 champs cotisation_AAAA de field_groups (juste après le code)
#   6. vérification des comptes (table = colonnes d'origine)
#   Si le déploiement échoue (4) : la table est supprimée (l'ancien code n'en a pas besoin).
# ============================================================================
set -euo pipefail

BASE="/srv/ba38"
DEV="$BASE/dev"
PROD="$BASE/prod"
PY="$DEV/venv/bin/python"
BASES=("$PROD/instance/ba380.sqlite" "$PROD/instance/ba380_test.sqlite")
ENVS=("prod" "prod_test")

SIMU=0
if [ "${1:-}" = "--simulation" ]; then SIMU=1; shift; fi
VERSION="${1:-1.7.292}"
MESSAGE="Cotisations bénévoles par année (table dédiée, date de paiement, suivi) ; retrait Mail groupé et Réinitialiser"
FICHIERS=(
  ba38_benevoles/__init__.py
  ba38_benevoles/cotisations.py
  ba38_benevoles/routes.py
  ba38_benevoles/suivi_cotisations.py
  ba38_billets/routes.py
  ba38_cerfa/frais_admin.py
  ba38_export/routes.py
  scripts/envoyer_questionnaire_benevoles.py
  scripts/migrate_benevoles_cotisations.py
  scripts/migrate_schema_and_data_dev_to_prod.py
  templates/benevoles/_cotisation_date.html
  templates/benevoles/benevoles_tabulator.html
  templates/benevoles/create_benevole.html
  templates/benevoles/edition_tableau_benevoles.html
  templates/benevoles/suivi_cotisations.html
  templates/benevoles/update_benevole.html
  templates/cerfa/frais/suivi.html
  templates/docsHtml/Documentation_Utilisateur_Apercu_Fonctionnalites_BA38.html
  templates/docsHtml/Documentation_Utilisateur_Module_Benevoles_BA38.html
  templates/docsHtml/Documentation_Utilisateur_Module_CreateBenevole_BA38.html
  templates/partials/boutons_nav_benevole.html
)

oui_total() {  # $1 = base -> nombre de 'oui' dans benevoles + benevoles_inactifs, toutes années
  local total=0 t c n
  for t in benevoles benevoles_inactifs; do
    for c in $(sqlite3 -readonly "$1" "select name from pragma_table_info('$t') where name glob 'cotisation_[0-9][0-9][0-9][0-9]'"); do
      n=$(sqlite3 -readonly "$1" "select count(*) from $t where lower(trim(coalesce($c,'')))='oui'")
      total=$((total + n))
    done
  done
  echo "$total"
}
nb_table() { sqlite3 -readonly "$1" "select count(*) from benevoles_cotisations"; }
a_table() { sqlite3 -readonly "$1" "select count(*) from sqlite_master where type='table' and name='benevoles_cotisations'"; }

echo "== 1. Contrôles"
for db in "${BASES[@]}"; do
  [ -f "$db" ] || { echo "❌ base introuvable : $db"; exit 1; }
  [ "$(a_table "$db")" = "0" ] || { echo "❌ $db : benevoles_cotisations existe déjà"; exit 1; }
  echo "✅ $(basename "$db") : table absente, $(oui_total "$db") cotisations 'oui' dans les colonnes d'origine"
done
cd "$DEV"
bash scripts/deploy_fichiers.sh --simulation "$VERSION" "$MESSAGE" "${FICHIERS[@]}"

if [ "$SIMU" = "1" ]; then
  echo "🧪 Simulation terminée : rien n'a été modifié."
  exit 0
fi

echo "== 2. Sauvegarde des bases PROD"
DEST="$BASE/backups/fichiers/cotisations_dynamiques_$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"
for db in "${BASES[@]}"; do
  sqlite3 "$db" ".backup $DEST/$(basename "$db")"
  echo "💾 $DEST/$(basename "$db")"
done

echo "== 3. Création et remplissage de benevoles_cotisations depuis les colonnes PROD"
for e in "${ENVS[@]}"; do
  echo "-- $e"
  "$PY" "$DEV/scripts/migrate_benevoles_cotisations.py" --env "$e" | head -1
done

echo "== 4. Déploiement des fichiers"
if ! bash scripts/deploy_fichiers.sh "$VERSION" "$MESSAGE" "${FICHIERS[@]}"; then
  echo "❌ Déploiement échoué : suppression de la table benevoles_cotisations"
  for db in "${BASES[@]}"; do sqlite3 "$db" "DROP TABLE IF EXISTS benevoles_cotisations"; echo "↩️  $(basename "$db")"; done
  echo "⚠️  Vérifier l'état de PROD (sauvegardes dans $DEST)"
  exit 1
fi

echo "== 5. Retrait des champs cotisation_AAAA de field_groups"
for e in "${ENVS[@]}"; do
  echo "-- $e"
  "$PY" "$DEV/scripts/migrate_benevoles_cotisations.py" --env "$e" --retirer-champs | sed -n 1,2p
done

echo "== 6. Vérification"
for db in "${BASES[@]}"; do
  src=$(oui_total "$db"); dst=$(nb_table "$db")
  restants=$(sqlite3 -readonly "$db" "select count(*) from field_groups where appli='benevoles' and field_name glob 'cotisation_[0-9][0-9][0-9][0-9]'")
  echo "$(basename "$db") : colonnes d'origine=$src table=$dst champs field_groups restants=$restants"
  [ "$src" = "$dst" ] || echo "⚠️  écart de comptage sur $(basename "$db")"
done
echo "🎉 Terminé : v$VERSION. Sauvegardes : $DEST"
