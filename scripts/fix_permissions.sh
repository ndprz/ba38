#!/bin/bash

echo "🔧 Correction des permissions dans /home/ndprz/scripts/ ..."

SCRIPT_DIR="/home/ndprz/scripts"
CORRECTED=0

# Fichiers .sh
while IFS= read -r file; do
    if [ ! -x "$file" ]; then
        chmod +x "$file"
        echo "✅ +x $file"
        CORRECTED=$((CORRECTED + 1))
    fi
done < <(find "$SCRIPT_DIR" -type f -name "*.sh")

# Fichiers .py
while IFS= read -r file; do
    if [ ! -x "$file" ]; then
        chmod +x "$file"
        echo "✅ +x $file"
        CORRECTED=$((CORRECTED + 1))
    fi
done < <(find "$SCRIPT_DIR" -type f -name "*.py")

if [ "$CORRECTED" -eq 0 ]; then
    echo "🟢 Tous les scripts étaient déjà correctement configurés."
else
    echo "🎉 $CORRECTED script(s) corrigé(s)."
fi
