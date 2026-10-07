#!/usr/bin/env bash
# Déploiement ciblé du module CERFA (v1.7.66) sans embarquer les
# modifications Collecte en cours dans DEV. Base PROD déjà migrée
# (migrate_cerfa_frais.py --env prod / prod_test, sauvegarde
# ba380-v1.7.65-20261005-160140.tar.gz).
set -euo pipefail

DEV=/srv/ba38/dev
PROD=/srv/ba38/prod

# Tâches de fond PROD en cours → on ne recharge pas (envois interrompus)
for t in "$PROD"/run/taches/*.json; do
  [ -e "$t" ] || continue
  pid=$(basename "$t" | cut -d- -f1)
  if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$PROD/venv/bin/gunicorn"; then
    echo "❌ Tâche de fond en cours en PROD ($t) : relancer plus tard"; exit 1
  fi
done

rsync -a --delete --exclude "__pycache__/" "$DEV/ba38_cerfa/" "$PROD/ba38_cerfa/"
rsync -a --delete "$DEV/templates/cerfa/" "$PROD/templates/cerfa/"
cp "$DEV/ba38.py" "$PROD/ba38.py"
cp "$DEV/scripts/migrate_cerfa_frais.py" "$DEV/scripts/migrate_schema_and_data_dev_to_prod.py" \
   "$DEV/scripts/deploy_cerfa_seul.sh" "$PROD/scripts/"

for f in "$PROD/VERSION" "$DEV/VERSION"; do
  printf 'VERSION=1.7.66\nMESSAGE=Module CERFA abandon de frais bénévoles (groupe Finance) : questionnaire en ligne, suivi, CERFA / remboursements TAG\nDATE=%s\n' \
    "$(date '+%Y-%m-%d %H:%M')" > "$f"
done

MASTER=$(systemctl show -p MainPID --value ba38-prod.service)
echo "🔄 Rechargement à chaud ba38-prod (master $MASTER)"
kill -HUP "$MASTER"
sleep 6

code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/__ping)
cerfa=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/recus-fiscaux/)
echo "ping=$code  /recus-fiscaux/=$cerfa (attendu 200 et 302)"
if [ "$code" = "200" ] && [ "$cerfa" = "302" ]; then
  for db in "$PROD/instance/ba380.sqlite" "$PROD/instance/ba380_test.sqlite"; do
    sqlite3 "$db" "UPDATE applications SET menu_visible = 1 WHERE appli = 'cerfa';"
  done
  echo "✅ CERFA déployé et visible dans le menu Finance"
else
  echo "❌ Problème : le menu CERFA reste masqué. Voir $PROD/logs/app.log"
  exit 1
fi
