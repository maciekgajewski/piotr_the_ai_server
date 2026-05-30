#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

DEFAULT_MODEL="gpt-oss:20b-cloud"
MODEL="$DEFAULT_MODEL"
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
  --model MODEL   Cloud model to pull and test. Default: gpt-oss:20b-cloud
  --no-signin    Skip the interactive `ollama signin` step.
  --no-pull      Skip `ollama pull`.
  --no-start     Do not start the ollama Compose service.
  -h, --help     Show this help.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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
