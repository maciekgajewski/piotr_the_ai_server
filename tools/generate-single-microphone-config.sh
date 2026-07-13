#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -z "${PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi

PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" tools/lib/single_microphone_config.py "$@"
