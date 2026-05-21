#!/usr/bin/env bash

box3_tts_image="${BOX3_TTS_IMAGE:-piotr-box3-tts:latest}"
box3_default_host="${BOX3_HOST:-piotr-box3-01-cbfaA8.local}"

box3_require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed or not on PATH" >&2
    exit 127
  fi
}

box3_build_tts_image_if_needed() {
  if ! docker image inspect "$box3_tts_image" >/dev/null 2>&1; then
    docker build -f docker/tts.Dockerfile -t "$box3_tts_image" .
  fi
}

box3_run_tts_container() {
  local requested_gpus="${BOX3_TTS_GPUS:-all}"
  local -a gpu_args=()
  local resolved_host="$box3_default_host"

  if [[ "$requested_gpus" != "none" ]]; then
    gpu_args=(--gpus "$requested_gpus")
  fi

  if command -v getent >/dev/null 2>&1; then
    resolved_host="$(getent ahostsv4 "$box3_default_host" | sed -n '1s/[[:space:]].*//p')"
    resolved_host="${resolved_host:-$box3_default_host}"
  fi

  docker run --rm -i \
    "${gpu_args[@]}" \
    --network host \
    -e BOX3_HOST="$resolved_host" \
    -e PIPER_HOME=/app/.piper-cache \
    -e BOX3_PIPER_VOICE="${BOX3_PIPER_VOICE:-pl_PL-bass-high}" \
    -v "$PWD/.piper-cache:/app/.piper-cache" \
    -v "$PWD/firmware:/app/firmware:ro" \
    -v "$PWD/audio:/app/audio" \
    -v "$PWD/tools/lib:/app/tools/lib:ro" \
    "$box3_tts_image" "$@"
}
