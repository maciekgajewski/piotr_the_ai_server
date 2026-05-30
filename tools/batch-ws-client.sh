#!/usr/bin/env bash
set -euo pipefail
trap 'exit 0' INT

cd "$(dirname "$0")/.."
if [[ -z "${PYTHON:-}" && -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi

"${PYTHON:-python3}" -m ai_server.batch_ws_client "$@"
