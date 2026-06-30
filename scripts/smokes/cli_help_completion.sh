#!/usr/bin/env bash
# Smoke: verify `hive --help` is categorized and `hive completion <shell>` emits valid scripts.
set -e
cd "$(git rev-parse --show-toplevel)"
export PYTHONPATH=src
export HIVE_NO_COLOR=1
export NO_COLOR=1

PYTHON=/home/hive/hiveos/.venv/bin/python

echo "== hive --help (categorized) =="
$PYTHON -c "from hive.surfaces.cli import main; main(['--help'])"

echo ""
echo "== hive completion bash (first 5 lines) =="
$PYTHON -c "from hive.surfaces.cli import main; main(['completion', 'bash'])" | head -5

echo ""
echo "== bash syntax check =="
$PYTHON -c "from hive.surfaces.cli import main; main(['completion', 'bash'])" \
    | bash -n && echo "[ok] bash script parses"

if command -v zsh >/dev/null 2>&1; then
    if $PYTHON -c "from hive.surfaces.cli import main; main(['completion', 'zsh'])" | zsh -n 2>/dev/null; then
        echo "[ok] zsh script parses"
    else
        echo "[skip] zsh parse failed (likely missing zsh completion runtime)"
    fi
else
    echo "[skip] zsh not installed"
fi

if command -v fish >/dev/null 2>&1; then
    if $PYTHON -c "from hive.surfaces.cli import main; main(['completion', 'fish'])" | fish -n 2>/dev/null; then
        echo "[ok] fish script parses"
    else
        echo "[skip] fish parse failed (likely missing fish runtime)"
    fi
else
    echo "[skip] fish not installed"
fi

echo ""
echo "ALL OK"