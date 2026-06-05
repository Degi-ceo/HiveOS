#!/usr/bin/env bash
# Hetzner VPS: bare Ubuntu -> HiveOS ready.
set -euo pipefail
sudo apt-get update -y
sudo apt-get install -y python3.12 python3.12-venv python3-pip git
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
mkdir -p data vault
echo ">> Done. Edit .env with your keys, then: python -m scripts.ping"
