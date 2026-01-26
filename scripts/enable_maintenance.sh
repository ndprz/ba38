#!/usr/bin/env bash

FLAG="/srv/ba38/prod/maintenance.flag"

if [ -f "$FLAG" ]; then
    echo "⚠️  Le mode maintenance est déjà activé."
else
    touch "$FLAG"
    echo "🛠️  Mode maintenance activé (flag créé dans /srv/ba38/prod)."
fi
