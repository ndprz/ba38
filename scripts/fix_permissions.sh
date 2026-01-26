#!/bin/bash
set -euo pipefail

# ============================================================
# 🔧 Correction des permissions des scripts BA38
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

echo "🔧 Correction des permissions dans $SCRIPT_DIR ..."
CORRECTED=0

# ------------------------------------------------------------
# Scripts shell (.sh)
# ------------------------------------------------------------
while IFS= read -r file; do
    if [ ! -x "$file" ]; then
        chmod +x "$file"
        echo "✅ +x $file"
        CORRECTED=$((CORRECTED + 1))
    fi
done < <(find "$SCRIPT_DIR" -type f -name "*.sh")

# ------------------------------------------------------------
# Scripts Python (.py)
# ------------------------------------------------------------
while IFS= read -r file; do
    if [ ! -x "$file" ]; then
        chmod +x "$file"
        echo "✅ +x $file"
        CORRECTED=$((CORRECTED + 1))
    fi
done < <(find "$SCRIPT_DIR" -type f -name "*.py")

# ------------------------------------------------------------
# Résumé
# ------------------------------------------------------------
if [ "$CORRECTED" -eq 0 ]; then
    echo "🟢 Tous les scripts étaient déjà correctement configurés."
else
    echo "🎉 $CORRECTED script(s) corrigé(s)."
fi
