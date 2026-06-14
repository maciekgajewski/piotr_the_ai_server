#!/usr/bin/env bash
set -euo pipefail

CALLER_CWD="$PWD"
cd "$(dirname "$0")/.."

AI_SERVER_CONFIG="${AI_SERVER_CONFIG:-$PWD/ai_server/test-config.yaml}"
ARGS=()

resolve_config_path() {
  if [[ "$1" = /* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s/%s\n' "$CALLER_CWD" "$1"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      AI_SERVER_CONFIG="$(resolve_config_path "$2")"
      shift 2
      ;;
    --config=*)
      AI_SERVER_CONFIG="$(resolve_config_path "${1#--config=}")"
      shift
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

export AI_SERVER_CONFIG
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

exec .venv/bin/python tools/lib/mic_protocol_test.py --config "$AI_SERVER_CONFIG" "${ARGS[@]}"
