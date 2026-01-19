#!/bin/bash

# ✅ Charger les variables d'environnement
set -a
source /home/ndprz/ba380/.env
set +a

# 🕒 Date et nom du fichier
DATE=$(date +"%Y%m%d-%H%M")
VERSION=${VERSION:-"unknown"}
DEST="/home/ndprz/backups/ba380-v$VERSION-$DATE.tar.gz"
SOURCE="/home/ndprz/ba380"

echo "🔄 Sauvegarde de la prod en cours (version $VERSION)..."
tar -czf "$DEST" "$SOURCE"
echo "✅ Sauvegarde créée : $DEST"


