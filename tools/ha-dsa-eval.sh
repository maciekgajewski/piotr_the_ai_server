#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD:$PWD/tools/lib${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -u tools/lib/ha_dsa_eval.py "$@"
