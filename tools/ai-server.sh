#!/usr/bin/env bash
set -euo pipefail

CALLER_CWD="$PWD"
cd "$(dirname "$0")/.."

AI_SERVER_CONFIG="$PWD/ai_server/config.compose.yaml"
SERVER_ARGS=()
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
      SERVER_ARGS+=("--config" "/config/ai_server.yaml")
      shift 2
      ;;
    --config=*)
      AI_SERVER_CONFIG="$(resolve_config_path "${1#--config=}")"
      SERVER_ARGS+=("--config" "/config/ai_server.yaml")
      shift
      ;;
    *)
      SERVER_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ " ${SERVER_ARGS[*]} " != *" --config "* ]]; then
  SERVER_ARGS=("--config" "/config/ai_server.yaml" "${SERVER_ARGS[@]}")
fi

export AI_SERVER_CONFIG

if docker compose version >/dev/null 2>&1; then
  docker compose run --rm --service-ports ai-server "${SERVER_ARGS[@]}"
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose run --rm --service-ports ai-server "${SERVER_ARGS[@]}"
else
  echo "Docker Compose is required. Install the Docker Compose plugin or docker-compose." >&2
  exit 1
fi
