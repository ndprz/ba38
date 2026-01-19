#!/bin/bash

echo "🟢 État de l'app www.ba380.org"

echo -e "\n🔍 Dernière ligne du error.log :"
tail -n 1 /var/log/www.ba380.org.error.log 2>/dev/null

echo -e "\n📦 Base utilisée :"
sqlite3 /home/ndprz/ba380/ba380.sqlite "SELECT name FROM sqlite_master WHERE type='table';"

echo -e "\n👤 Session actuelle :"
ls -l /home/ndprz/ba380/flask_sessions 2>/dev/null | tail -n 5

echo -e "\n🕒 Dernière modif .wsgi :"
ls -l /var/www/www_ba380_org_wsgi.py
