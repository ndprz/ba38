#!/bin/bash

echo "🟢 État BA38 (Debian)"

echo ""
echo "🌍 Test HTTP DEV :"
curl -I -s http://127.0.0.1:8000 | head -n 1

echo ""
echo "⚙️ Services systemd :"
systemctl is-active ba38-dev.service && echo "✅ DEV actif" || echo "❌ DEV INACTIF"
systemctl is-active ba38-prod.service && echo "✅ PROD actif" || echo "❌ PROD INACTIF"

echo ""
echo "📦 Base DEV :"
ls -lh /srv/ba38/dev/instance/ba380dev.sqlite

echo ""
echo "📦 Base PROD :"
ls -lh /srv/ba38/prod/instance/ba380.sqlite

echo ""
echo "📜 Dernières erreurs DEV :"
journalctl -u ba38-dev.service -n 10 --no-pager

echo ""
echo "📜 Dernières erreurs PROD :"
journalctl -u ba38-prod.service -n 10 --no-pager
