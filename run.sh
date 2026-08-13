#!/usr/bin/env bash
# Start the Bookly support agent locally.
# Open http://127.0.0.1:5000 in Chrome for full voice support.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "Bookly Support Agent → http://127.0.0.1:5000"
echo "Use Chrome for mic (Web Speech API). Toggle voice replies in the UI."
exec python app.py
