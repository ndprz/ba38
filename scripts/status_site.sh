#!/bin/bash

echo "🟢 État de l'app www.ba380.org"
echo "-----------------------------------"

# === Test HTTP ===
echo -e "\n🌍 Test HTTP (www.ba380.org) :"
curl -I -s https://www.ba380.org | head -n 1

# === Services systemd ===
echo -e "\n⚙️ Services systemd :"
systemctl is-active ba38-prod.service && echo "✅ ba38-prod.service actif" || echo "❌ ba38-prod.service INACTIF"
systemctl is-active ba38-dev.service && echo "✅ ba38-dev.service actif" || echo "❌ ba38-dev.service INACTIF"

# === Logs PROD ===
echo -e "\n📜 Logs PROD (systemd) :"
systemctl status ba38-prod.service --no-pager -n 20

# === Logs DEV ===
echo -e "\n📜 Logs DEV (systemd) :"
systemctl status ba38-dev.service --no-pager -n 20

# === Base SQLite PROD ===
echo -e "\n📦 Base SQLite réellement utilisée (runtime DEV) :"
curl -s \
  -H "X-Internal-Token: ba38-internal-check" \
  http://127.0.0.1:8000/_runtime/db

