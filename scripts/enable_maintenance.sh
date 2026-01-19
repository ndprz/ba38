#!/usr/bin/env bash

FLAG="/home/ndprz/ba380/maintenance.flag"

if [ -f "$FLAG" ]; then
    echo "⚠️  Le mode maintenance est déjà activé."
else
    touch "$FLAG"
    echo "🛠️  Mode maintenance activé (flag créé dans /home/ndprz/ba380)."
fi
