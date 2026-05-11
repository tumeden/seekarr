#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

echo "=========================================="
echo "Seekarr - Web UI"
echo "=========================================="
echo "Opening local UI at http://127.0.0.1:8788"
echo "Press Ctrl+C to stop."
echo

if ! command -v python >/dev/null 2>&1; then
  echo "[ERROR] Python is not installed or not in PATH."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating Python virtual environment..."
  python -m venv .venv
fi

source ".venv/bin/activate"

if ! python -c "import flask, waitress, requests, cryptography" >/dev/null 2>&1; then
  echo "Installing required Python packages..."
  python -m pip install -r requirements.txt
fi

mkdir -p state

python webui_main.py --db-path state/seekarr.db --host 127.0.0.1 --port 8788
