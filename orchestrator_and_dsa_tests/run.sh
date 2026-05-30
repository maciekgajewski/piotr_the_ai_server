#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD:$PWD/tools/lib${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -u orchestrator_and_dsa_tests/runner.py "$@"
