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

create_venv() {
  echo "Creating Python virtual environment..."
  python -m venv --copies .venv
}

if [ ! -d ".venv" ]; then
  create_venv
elif [ ! -x ".venv/bin/python" ] || ! ".venv/bin/python" -c "import sys" >/dev/null 2>&1; then
  echo "Existing Python virtual environment is not usable; recreating it..."
  rm -rf .venv
  create_venv
fi

source ".venv/bin/activate"

if ! python -m pip --version >/dev/null 2>&1; then
  echo "Bootstrapping pip..."
  python -m ensurepip --upgrade
fi

if ! python -c "import flask, waitress, requests, cryptography" >/dev/null 2>&1; then
  echo "Installing required Python packages..."
  python -m pip install -r requirements.txt
fi

mkdir -p state

python webui_main.py --db-path state/seekarr.db --host 127.0.0.1 --port 8788
