#!/usr/bin/env bash
# Hetzner VPS: bare Ubuntu -> HiveOS ready.
set -euo pipefail
sudo apt-get update -y
sudo apt-get install -y python3.11 python3.11-venv python3-pip git
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .                 # the installable `hive` package (deps from pyproject.toml)
# Optional extras: pip install -e ".[memory]"  (Mnemosyne)   ".[voice]"  (faster-whisper)
[ -f .env ] || cp .env.example .env
mkdir -p data vault
echo ">> Done. Edit .env with your keys, then: hive doctor --fix"
