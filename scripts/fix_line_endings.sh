#!/bin/bash

echo "🔧 Conversion des fins de ligne Windows en format Unix dans /home/ndprz/scripts/..."

# Nettoyer ce fichier lui-même
sed -i 's/\r$//' "$0"

# Traite les fichiers .py et .sh
find /home/ndprz/scripts/ -type f \( -name "*.py" -o -name "*.sh" \) | while read -r file; do
    echo "➡️  Traitement : $file"
    sed -i 's/\r$//' "$file"
done

echo "✅ Tous les fichiers .py et .sh ont été nettoyés."
