#!/usr/bin/env bash
set -euo pipefail

CALLER_CWD="$PWD"
cd "$(dirname "$0")/.."

DEFAULT_MODEL="gpt-oss:20b-cloud"
MODEL="$DEFAULT_MODEL"
SERVICES_CONFIG=""
SIGNIN=1
PULL=1
START=1
PROMPT="Reply with exactly: piotr cloud ok"

usage() {
  cat <<'USAGE'
Usage: tools/ollama-cloud-setup-test.sh [options]

Sign in the Docker Compose ollama service, pull an Ollama cloud model, and
smoke-test local cloud offload through http://127.0.0.1:11434/api/chat.

Options:
  --services-config PATH
                  Compose services env file.
  --model MODEL   Cloud model to pull and test. Default: gpt-oss:20b-cloud
  --no-signin    Skip the interactive `ollama signin` step.
  --no-pull      Skip `ollama pull`.
  --no-start     Do not start the ollama Compose service.
  -h, --help     Show this help.
USAGE
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
        echo "--services-config requires a value." >&2
        usage >&2
        exit 2
      fi
      SERVICES_CONFIG="$(resolve_config_path "$2")"
      shift 2
      ;;
    --services-config=*)
      if [[ -z "${1#--services-config=}" ]]; then
        echo "--services-config requires a value." >&2
        usage >&2
        exit 2
      fi
      SERVICES_CONFIG="$(resolve_config_path "${1#--services-config=}")"
      shift
      ;;
    --model)
      if [[ $# -lt 2 ]]; then
        echo "--model requires a value." >&2
        usage >&2
        exit 2
      fi
      MODEL="$2"
      shift 2
      ;;
    --model=*)
      MODEL="${1#--model=}"
      shift
      ;;
    --no-signin)
      SIGNIN=0
      shift
      ;;
    --no-pull)
      PULL=0
      shift
      ;;
    --no-start)
      START=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$SERVICES_CONFIG" ]]; then
  echo "--services-config is required." >&2
  usage >&2
  exit 2
fi

if [[ ! -f "$SERVICES_CONFIG" ]]; then
  echo "Services config file does not exist: $SERVICES_CONFIG" >&2
  usage >&2
  exit 2
fi

if [[ ! "$MODEL" =~ ^[[:alnum:]_.:/-]+$ ]]; then
  echo "Model name contains unsupported characters: $MODEL" >&2
  echo "Use only letters, numbers, '_', '.', ':', '/', and '-'." >&2
  exit 2
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker Compose is required. Install the Docker Compose plugin or docker-compose." >&2
  exit 1
fi

COMPOSE+=("--env-file" "$SERVICES_CONFIG")

if [[ "$START" -eq 1 ]]; then
  "${COMPOSE[@]}" up -d ollama
fi

if [[ "$SIGNIN" -eq 1 ]]; then
  if [[ -t 0 && -t 1 ]]; then
    "${COMPOSE[@]}" exec ollama ollama signin
  else
    echo "Skipping interactive sign-in because stdin/stdout is not a TTY." >&2
    echo "Run this manually first:" >&2
    echo "  ${COMPOSE[*]} exec ollama ollama signin" >&2
  fi
fi

if [[ "$PULL" -eq 1 ]]; then
  "${COMPOSE[@]}" exec -T ollama ollama pull "$MODEL"
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required for the local /api/chat smoke test." >&2
  exit 1
fi

REQUEST_BODY=$(printf '{"model":"%s","messages":[{"role":"user","content":"%s"}],"stream":false}' "$MODEL" "$PROMPT")

echo "Testing local Ollama cloud offload with model: $MODEL"
curl --fail-with-body -sS \
  -H "Content-Type: application/json" \
  -d "$REQUEST_BODY" \
  http://127.0.0.1:11434/api/chat
printf '\n'
