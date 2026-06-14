#!/usr/bin/env bash
set -euo pipefail

CALLER_CWD="$PWD"
cd "$(dirname "$0")/.."

AI_SERVER_CONFIG=""
SERVICES_CONFIG=""
SERVER_ARGS=()

usage() {
  echo "Usage: tools/ai-server.sh --services-config <path> --config <path> [server args...]" >&2
}

die() {
  echo "ai-server.sh: $1" >&2
  usage
  exit 2
}

resolve_config_path() {
  if [[ "$1" = /* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s/%s\n' "$CALLER_CWD" "$1"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --services-config)
      if [[ $# -lt 2 || -z "$2" ]]; then
        die "--services-config requires a path"
      fi
      SERVICES_CONFIG="$(resolve_config_path "$2")"
      shift 2
      ;;
    --services-config=*)
      if [[ -z "${1#--services-config=}" ]]; then
        die "--services-config requires a path"
      fi
      SERVICES_CONFIG="$(resolve_config_path "${1#--services-config=}")"
      shift
      ;;
    --config)
      if [[ $# -lt 2 || -z "$2" ]]; then
        die "--config requires a path"
      fi
      AI_SERVER_CONFIG="$(resolve_config_path "$2")"
      SERVER_ARGS+=("--config" "/config/ai_server.yaml")
      shift 2
      ;;
    --config=*)
      if [[ -z "${1#--config=}" ]]; then
        die "--config requires a path"
      fi
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

if [[ -z "$SERVICES_CONFIG" ]]; then
  die "--services-config is required"
fi

if [[ ! -f "$SERVICES_CONFIG" ]]; then
  die "services config file does not exist: $SERVICES_CONFIG"
fi

if [[ -z "$AI_SERVER_CONFIG" ]]; then
  die "--config is required"
fi

if [[ ! -f "$AI_SERVER_CONFIG" ]]; then
  die "config file does not exist: $AI_SERVER_CONFIG"
fi

export AI_SERVER_CONFIG

if docker compose version >/dev/null 2>&1; then
  docker compose --env-file "$SERVICES_CONFIG" -f docker-compose.yml -f docker-compose.ai-server.yml run --rm ai-server "${SERVER_ARGS[@]}"
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose --env-file "$SERVICES_CONFIG" -f docker-compose.yml -f docker-compose.ai-server.yml run --rm ai-server "${SERVER_ARGS[@]}"
else
  echo "Docker Compose is required. Install the Docker Compose plugin or docker-compose." >&2
  exit 1
fi
