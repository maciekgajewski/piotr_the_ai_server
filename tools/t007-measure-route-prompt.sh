#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec env PYTHONPATH=. .venv/bin/python tools/lib/t007_route_prompt_measurement.py "$@"
