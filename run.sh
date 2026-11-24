#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  exec uv run app.py
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Error: Python 3.11+ not found. Install Python 3.11+ or open public/index.html in a browser." >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating local virtual environment (.venv) and installing dependencies..."
  "$PYTHON_BIN" -m venv .venv
  ".venv/bin/python" -m pip install --upgrade pip >/dev/null
  ".venv/bin/pip" install -r requirements.txt
fi

exec ".venv/bin/python" app.py
