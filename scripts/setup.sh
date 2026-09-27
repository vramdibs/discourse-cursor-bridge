#!/usr/bin/env bash
# Bootstrap a virtualenv and install dependencies.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example - edit WEBHOOK_SECRET and ALLOWED_USERNAMES before starting."
fi

echo "Done. Run: .venv/bin/python server.py"
