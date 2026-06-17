#!/usr/bin/env bash
set -euo pipefail

CALLER_CWD="$PWD"
cd "$(dirname "$0")/.."

usage() {
  echo "Usage: tools/speaker-profile-build.sh <input-samples-dir> <output-profile-dir> [profile-builder args...]" >&2
}

die() {
  echo "speaker-profile-build.sh: $1" >&2
  usage
  exit 2
}

resolve_path() {
  if [[ "$1" = /* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s/%s\n' "$CALLER_CWD" "$1"
  fi
}

if [[ $# -lt 2 ]]; then
  die "input and output directories are required"
fi

INPUT_DIR="$(resolve_path "$1")"
OUTPUT_DIR="$(resolve_path "$2")"
shift 2

if [[ ! -d "$INPUT_DIR" ]]; then
  die "input samples directory does not exist: $INPUT_DIR"
fi

mkdir -p "$OUTPUT_DIR"
mkdir -p .hf-cache

if docker compose version >/dev/null 2>&1; then
  docker compose -f docker-compose.speaker-recognition.yml run --rm \
    --build \
    --user "$(id -u):$(id -g)" \
    --volume "$INPUT_DIR:/input:ro" \
    --volume "$OUTPUT_DIR:/output" \
    speaker-recognition \
    python3 -m ai_server.speaker_recognition.profile_builder /input /output "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose -f docker-compose.speaker-recognition.yml run --rm \
    --build \
    --user "$(id -u):$(id -g)" \
    --volume "$INPUT_DIR:/input:ro" \
    --volume "$OUTPUT_DIR:/output" \
    speaker-recognition \
    python3 -m ai_server.speaker_recognition.profile_builder /input /output "$@"
else
  echo "Docker Compose is required. Install the Docker Compose plugin or docker-compose." >&2
  exit 1
fi
