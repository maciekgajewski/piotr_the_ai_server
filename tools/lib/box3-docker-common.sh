#!/usr/bin/env bash

box3_stt_image="${BOX3_STT_IMAGE:-piotr-box3-stt:latest}"
box3_default_host="${BOX3_HOST:-piotr-box3-01-cbfaA8.local}"

box3_require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed or not on PATH" >&2
    exit 127
  fi
}

box3_build_stt_image_if_needed() {
  if ! docker image inspect "$box3_stt_image" >/dev/null 2>&1; then
    docker build -f docker/stt.Dockerfile -t "$box3_stt_image" .
  fi
}

box3_run_stt_container() {
  local -a gpu_args=()
  local requested_gpus="${BOX3_STT_GPUS:-auto}"
  local resolved_host="$box3_default_host"

  if [[ "$requested_gpus" == "auto" ]]; then
    requested_gpus="all"
    local previous_arg=""
    for arg in "$@"; do
      if [[ "$arg" == "--list-models" || "$arg" == "--device=cpu" || ( "$previous_arg" == "--device" && "$arg" == "cpu" ) ]]; then
        requested_gpus="none"
        break
      fi
      previous_arg="$arg"
    done
  fi

  if [[ "$requested_gpus" != "none" ]]; then
    gpu_args=(--gpus "$requested_gpus")
  fi

  if command -v getent >/dev/null 2>&1; then
    resolved_host="$(getent ahostsv4 "$box3_default_host" | sed -n '1s/[[:space:]].*//p')"
    resolved_host="${resolved_host:-$box3_default_host}"
  fi

  docker run --rm \
    "${gpu_args[@]}" \
    --network host \
    -e BOX3_HOST="$resolved_host" \
    -e HF_HOME=/app/.hf-cache \
    -e BOX3_WHISPER_MODEL="${BOX3_WHISPER_MODEL:-base}" \
    -e BOX3_WHISPER_LANGUAGE="${BOX3_WHISPER_LANGUAGE:-pl}" \
    -v "$PWD/.hf-cache:/app/.hf-cache" \
    -v "$PWD/firmware:/app/firmware:ro" \
    -v "$PWD/audio:/app/audio" \
    -v "$PWD/tools/lib:/app/tools/lib:ro" \
    "$box3_stt_image" "$@"
}
