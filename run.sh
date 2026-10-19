#!/usr/bin/env bash
# First run creates .venv and installs dependencies, then starts the service.
#   ./run.sh [--host 0.0.0.0] [--port 8780] [--token SECRET]
# TORCH_INDEX picks the torch build (default CUDA 12.8; CPU only: https://download.pytorch.org/whl/cpu)
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q torch torchaudio --index-url "${TORCH_INDEX:-https://download.pytorch.org/whl/cu128}"
  .venv/bin/python -m pip install -q -r requirements.txt
fi
exec .venv/bin/python run.py "$@"
