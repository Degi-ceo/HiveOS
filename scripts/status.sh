#!/usr/bin/env bash
# status.sh — instant status snapshot for after-SSH-drop recovery
#
# Usage: bash scripts/status.sh
#
# Shows: current branch + last commit, all worktrees with HEAD, all branches
# with their tip commits, and a quick test summary. Designed to be readable
# at a glance and to fit in a phone screenshot.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "═══════════════════════════════════════════════════════════"
echo " HiveOS STATUS  —  $(date -u +"%Y-%m-%d %H:%M UTC")"
echo "═══════════════════════════════════════════════════════════"
echo

echo "📍 Current branch: $(git rev-parse --abbrev-ref HEAD)"
echo "   HEAD: $(git log -1 --pretty=format:'%h %s')"
echo

echo "🌳 Worktrees:"
git worktree list
echo

echo "🔀 Branches in flight:"
git for-each-ref --format='  %(refname:short) %(objectname:short) %(subject)' refs/heads/ | grep -v "sprint6/" | head -15
echo

echo "📦 Open PRs (via gh if available):"
if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  gh pr list --state open --limit 10 2>/dev/null || echo "  (gh CLI not authed)"
else
  echo "  (gh CLI not available)"
fi
echo

echo "🧪 Test summary:"
if [ -f .venv/bin/pytest ]; then
  cd src/.. && .venv/bin/pytest -q --tb=no 2>/dev/null | tail -2 || echo "  (tests failed or interrupted)"
fi
echo

echo "═══════════════════════════════════════════════════════════"
echo "For detailed release notes: bash scripts/release-notes.sh"
echo "For the full changelog: cat CHANGELOG.md"
echo "═══════════════════════════════════════════════════════════"