#!/usr/bin/env bash
# HiveOS one-command installer
# Usage: curl -sSL https://raw.githubusercontent.com/hiveosagent/hiveos/main/install.sh | bash
set -euo pipefail

REPO="https://github.com/hiveosagent/hiveos.git"
INSTALL_DIR="${HIVE_HOME:-$HOME/.hive/hiveos}"

echo "==> Installing HiveOS to $INSTALL_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> Updating existing install..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "==> Cloning repository..."
    git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

echo "==> Setting up Python virtual environment..."
python3 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate

echo "==> Installing HiveOS..."
pip install --upgrade pip -q
pip install -e ".[memory]" -q

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "==> Created .env from .env.example"
    fi
fi

mkdir -p data vault

echo "==> Running health checks..."
hive doctor --fix || true

echo ""
echo "==> HiveOS installed successfully!"
echo "==> Next step: hive init"
echo ""
echo "To add 'hive' to your PATH, add this to your shell profile:"
echo "  export PATH=\"$INSTALL_DIR/.venv/bin:\$PATH\""
