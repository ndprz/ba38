#!/bin/bash

echo "🟢 État de l'app www.ba380.org"
echo "-----------------------------------"

# Tester la réponse HTTP
echo -e "\n🌍 Test HTTP (www.ba380.org) :"
curl -s -o /dev/null -w "%{http_code}\n" https://www.ba380.org


# --- DEV ---
echo -e "\n📌 DEV : ndprz.pythonanywhere.com"
if [ -f /var/log/ndprz.pythonanywhere.com.error.log ]; then
  echo -e "🔍 5 dernières lignes du error.log (DEV) :"
  tail -n 5 /var/log/ndprz.pythonanywhere.com.error.log
else
  echo "❌ error.log (DEV) introuvable"
fi

if [ -f /var/log/ndprz.pythonanywhere.com.server.log ]; then
  echo -e "\n🔍 5 dernières lignes du server.log (DEV) :"
  tail -n 5 /var/log/ndprz.pythonanywhere.com.server.log
else
  echo "❌ server.log (DEV) introuvable"
fi

# --- PROD ---
echo -e "\n📌 PROD : www.ba380.org"
if [ -f /var/log/www.ba380.org.error.log ]; then
  echo -e "🔍 5 dernières lignes du error.log (PROD) :"
  tail -n 5 /var/log/www.ba380.org.error.log
else
  echo "❌ error.log (PROD) introuvable"
fi

if [ -f /var/log/www.ba380.org.server.log ]; then
  echo -e "\n🔍 5 dernières lignes du server.log (PROD) :"
  tail -n 5 /var/log/www.ba380.org.server.log
else
  echo "❌ server.log (PROD) introuvable"
fi

# Vérifier les tables SQLite
echo -e "\n📦 Tables existantes dans la base utilisée :"
sqlite3 /home/ndprz/ba380/ba380.sqlite "SELECT name FROM sqlite_master WHERE type='table';"

# Dernière modif du WSGI
echo -e "\n🕒 Dernière modif .wsgi :"
ls -l /var/www/www_ba380_org_wsgi.py
