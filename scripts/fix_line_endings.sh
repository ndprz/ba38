#!/bin/bash
set -euo pipefail

# ============================================================
# 🔧 Conversion des fins de ligne Windows → Unix (BA38)
# ============================================================

if [ -z "${BA38_BASE_DIR:-}" ]; then
    echo "❌ Variable BA38_BASE_DIR non définie"
    exit 1
fi

SCRIPT_DIR="$BA38_BASE_DIR/scripts"

if [ ! -d "$SCRIPT_DIR" ]; then
    echo "❌ Dossier scripts introuvable : $SCRIPT_DIR"
    exit 1
fi

echo "🔧 Conversion des fins de ligne Windows → Unix dans $SCRIPT_DIR ..."

# Nettoyer ce fichier lui-même
sed -i 's/\r$//' "$0"

# Traite les fichiers .py et .sh
find "$SCRIPT_DIR" -type f \( -name "*.py" -o -name "*.sh" \) | while IFS= read -r file; do
    echo "➡️  Traitement : $file"
    sed -i 's/\r$//' "$file"
done

echo "✅ Tous les fichiers .py et .sh ont été nettoyés."
