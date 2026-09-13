#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Edit .env (DeepSeek key + Telegram), then:"
echo "  ./venv/bin/python scripts/preflight.py"
echo "  ./venv/bin/python main.py"
