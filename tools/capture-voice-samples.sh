#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -z "${PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi

PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" -u tools/lib/capture_voice_samples.py "$@"
