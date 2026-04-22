#!/usr/bin/env bash

set -euo pipefail

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔧 Git Commit + Push"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

MESSAGE="${1:-Auto commit}"

REPO_DIR="/srv/ba38/dev"

cd "$REPO_DIR"

echo "📂 Repo : $REPO_DIR"

# Vérifier git
if [ ! -d ".git" ]; then
  echo "❌ Pas un repo git"
  exit 1
fi

# Vérifier changements
if git diff --quiet && git diff --cached --quiet; then
  echo "ℹ️ Aucun changement à commit"
  exit 0
fi

echo "➕ git add"
git add .

echo "📝 git commit"
git commit -m "$MESSAGE" || echo "ℹ️ Rien à commit"

echo "⬆️ git push"
git push

echo "✅ Terminé"
