#!/bin/bash

FLAG="/srv/ba38/prod/maintenance.flag"

if [ -f "$FLAG" ]; then
    rm "$FLAG"
    echo "✅ Mode maintenance désactivé."
else
    echo "ℹ️  Le mode maintenance n'était pas activé."
fi
