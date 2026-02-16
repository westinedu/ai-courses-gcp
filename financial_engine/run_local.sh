#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
RELOAD="${RELOAD:-1}"
INSTALL_DEPS="${INSTALL_DEPS:-1}"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[run_local] Creating virtualenv: $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

if [[ "$INSTALL_DEPS" == "1" ]]; then
  echo "[run_local] Installing dependencies from requirements.txt"
  pip install -r requirements.txt
fi

if [[ -f ".env.local" ]]; then
  echo "[run_local] Loading env from .env.local"
  set -a
  # shellcheck disable=SC1091
  source ".env.local"
  set +a
fi

CMD=(python -m uvicorn main:app --host "$HOST" --port "$PORT")
if [[ "$RELOAD" == "1" ]]; then
  CMD+=(--reload)
fi

echo "[run_local] Starting service at http://$HOST:$PORT"
echo "[run_local] Docs: http://localhost:$PORT/docs"
exec "${CMD[@]}"
