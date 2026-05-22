#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -z "${PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi

"${PYTHON:-python3}" -m ai_server.server "$@"
