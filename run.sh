#!/usr/bin/env bash
# Start the Bookly support agent locally.
# Open http://127.0.0.1:5050 (port 5000 is taken by macOS AirPlay).
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

echo "Bookly Support Agent → http://127.0.0.1:5050"
exec python app.py
