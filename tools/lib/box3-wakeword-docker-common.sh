#!/usr/bin/env bash

box3_wakeword_image="${BOX3_WAKEWORD_IMAGE:-piotr-box3-wakeword:latest}"
box3_default_tensorflow_wheel="third_party/tensorflow-wheels/tensorflow-2.16.1-cp311-cp311-linux_x86_64.whl"

box3_require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed or not on PATH" >&2
    exit 127
  fi
}

box3_build_wakeword_image_if_needed() {
  if [[ "${BOX3_WAKEWORD_REBUILD:-0}" == "1" ]] || ! docker image inspect "$box3_wakeword_image" >/dev/null 2>&1; then
    local -a build_args=()
    if [[ -n "${BOX3_WAKEWORD_TENSORFLOW_WHEEL_PATH:-}" ]]; then
      build_args+=(--build-arg "TENSORFLOW_WHEEL_PATH=${BOX3_WAKEWORD_TENSORFLOW_WHEEL_PATH}")
    elif [[ -f "$box3_default_tensorflow_wheel" ]]; then
      build_args+=(--build-arg "TENSORFLOW_WHEEL_PATH=/tmp/tensorflow-wheels/$(basename "$box3_default_tensorflow_wheel")")
    fi
    if [[ -n "${BOX3_WAKEWORD_TENSORFLOW_WHEEL_URL:-}" ]]; then
      build_args+=(--build-arg "TENSORFLOW_WHEEL_URL=${BOX3_WAKEWORD_TENSORFLOW_WHEEL_URL}")
    fi
    docker build "${build_args[@]}" -f docker/wakeword.Dockerfile -t "$box3_wakeword_image" .
  fi
}

box3_run_wakeword_container() {
  local requested_gpus="${BOX3_WAKEWORD_GPUS:-all}"
  local -a gpu_args=()
  local user_id
  local group_id
  user_id="$(id -u)"
  group_id="$(id -g)"

  if [[ "$requested_gpus" != "none" ]]; then
    gpu_args=(--gpus "$requested_gpus")
  fi

  docker run --rm -i \
    "${gpu_args[@]}" \
    --user "$user_id:$group_id" \
    -e HF_HOME=/app/.hf-cache \
    -v "$PWD/.hf-cache:/app/.hf-cache" \
    -v "$PWD/audio:/app/audio" \
    -v "$PWD/wakeword:/app/wakeword" \
    -v "$PWD/tools/lib:/app/tools/lib:ro" \
    "$box3_wakeword_image" "$@"
}
