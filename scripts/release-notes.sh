#!/usr/bin/env bash
# release-notes.sh — generate release notes from git history
#
# Usage:
#   bash scripts/release-notes.sh [SINCE_REF]
#
# Examples:
#   bash scripts/release-notes.sh              # from main
#   bash scripts/release-notes.sh origin/main  # from main explicit
#   bash scripts/release-notes.sh v0.3.0       # from last tag
#
# Output: markdown with sections per commit, links to PRs when detectable.
# Designed for piping into clipboard, Telegram, or RELEASE_NOTES.md update.

set -euo pipefail

SINCE_REF="${1:-main}"
BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)}"

if ! git rev-parse --verify "$SINCE_REF" >/dev/null 2>&1; then
  echo "ref '$SINCE_REF' not found; falling back to last tag" >&2
  SINCE_REF="$(git describe --tags --abbrev=0 2>/dev/null || echo main)"
fi

echo "# Release notes — branch \`$BRANCH\` (since \`$SINCE_REF\`)"
echo
echo "Generated $(date -u +"%Y-%m-%d %H:%M UTC")"
echo
echo "## Commits"
echo
git log --no-merges --pretty=format:'- `%h` %s%n  author: %an · date: %ad' --date=short "$SINCE_REF..$BRANCH" 2>/dev/null || git log --no-merges --pretty=format:'- `%h` %s%n  author: %an · date: %ad' --date=short -20

echo
echo
echo "## Files changed (top 30)"
echo
git diff --stat "$SINCE_REF..$BRANCH" 2>/dev/null | head -30 || git diff --stat HEAD~20..HEAD | head -30

echo
echo "## Test summary"
echo
if [ -f .venv/bin/pytest ]; then
  .venv/bin/pytest -q --tb=no 2>/dev/null | tail -3
else
  echo "(.venv/bin/pytest not present on this branch)"
fi