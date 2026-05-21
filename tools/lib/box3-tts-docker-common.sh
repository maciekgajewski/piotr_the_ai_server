#!/usr/bin/env bash

box3_tts_image="${BOX3_TTS_IMAGE:-piotr-box3-tts:latest}"
box3_tts_container="${BOX3_TTS_CONTAINER:-piotr-box3-tts-server}"
box3_tts_port="${BOX3_TTS_PORT:-10200}"
box3_default_host="${BOX3_HOST:-piotr-box3-01-cbfaA8.local}"

box3_require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed or not on PATH" >&2
    exit 127
  fi
}

box3_build_tts_image_if_needed() {
  if [[ "${BOX3_TTS_REBUILD:-0}" == "1" ]] || ! docker image inspect "$box3_tts_image" >/dev/null 2>&1; then
    docker build -f docker/tts.Dockerfile -t "$box3_tts_image" .
    return
  fi

  if ! docker run --rm --entrypoint python3 "$box3_tts_image" -c "import wyoming, wyoming_piper" >/dev/null 2>&1; then
    docker build -f docker/tts.Dockerfile -t "$box3_tts_image" .
  fi
}

box3_tts_server_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$box3_tts_container" 2>/dev/null || true)" == "true" ]]
}

box3_start_tts_server_if_needed() {
  if box3_tts_server_running; then
    return
  fi

  if docker inspect "$box3_tts_container" >/dev/null 2>&1; then
    docker rm "$box3_tts_container" >/dev/null
  fi

  docker run -d \
    --name "$box3_tts_container" \
    --gpus "${BOX3_TTS_GPUS:-all}" \
    --network host \
    -e PIPER_HOME=/app/.piper-cache \
    -v "$PWD/.piper-cache:/data" \
    --entrypoint wyoming-piper \
    "$box3_tts_image" \
    --voice "${BOX3_PIPER_VOICE:-pl_PL-bass-high}" \
    --uri "tcp://0.0.0.0:${box3_tts_port}" \
    --data-dir /data \
    --download-dir /data \
    --update-voices \
    --use-cuda \
    --samples-per-chunk "${BOX3_TTS_SAMPLES_PER_CHUNK:-1024}"

  for _ in {1..50}; do
    if timeout 0.2 bash -c ":</dev/tcp/127.0.0.1/${box3_tts_port}" >/dev/null 2>&1; then
      return
    fi
    sleep 0.1
  done

  echo "TTS server did not become ready on 127.0.0.1:${box3_tts_port}" >&2
  docker logs "$box3_tts_container" >&2 || true
  exit 1
}

box3_stop_tts_server() {
  if docker inspect "$box3_tts_container" >/dev/null 2>&1; then
    docker rm -f "$box3_tts_container"
  else
    echo "$box3_tts_container is not created"
  fi
}

box3_status_tts_server() {
  if docker inspect "$box3_tts_container" >/dev/null 2>&1; then
    docker ps -a --filter "name=^/${box3_tts_container}$" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"
  else
    echo "$box3_tts_container is not created"
  fi
}

box3_logs_tts_server() {
  docker logs -f "$box3_tts_container"
}

box3_tts_needs_server() {
  local previous_arg=""

  for arg in "$@"; do
    if [[ "$arg" == "--list-voices" || "$arg" == "--engine=cli" || ( "$previous_arg" == "--engine" && "$arg" == "cli" ) ]]; then
      return 1
    fi
    previous_arg="$arg"
  done

  return 0
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
    -e BOX3_TTS_SERVER_HOST="${BOX3_TTS_SERVER_HOST:-127.0.0.1}" \
    -e BOX3_TTS_SERVER_PORT="$box3_tts_port" \
    -e PIPER_HOME=/app/.piper-cache \
    -e BOX3_PIPER_VOICE="${BOX3_PIPER_VOICE:-pl_PL-bass-high}" \
    -v "$PWD/.piper-cache:/app/.piper-cache" \
    -v "$PWD/firmware:/app/firmware:ro" \
    -v "$PWD/audio:/app/audio" \
    -v "$PWD/tools/lib:/app/tools/lib:ro" \
    "$box3_tts_image" "$@"
}
