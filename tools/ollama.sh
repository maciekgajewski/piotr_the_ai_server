#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICES_CONFIG="config/services.env"

if [[ ! -f "$SERVICES_CONFIG" ]]; then
  echo "Services config file does not exist: $SERVICES_CONFIG" >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker Compose is required. Install the Docker Compose plugin or docker-compose." >&2
  exit 1
fi

EXEC_ARGS=()
if [[ ! -t 0 || ! -t 1 ]]; then
  EXEC_ARGS=(-T)
fi

"${COMPOSE[@]}" --env-file "$SERVICES_CONFIG" exec "${EXEC_ARGS[@]}" ollama ollama "$@"
