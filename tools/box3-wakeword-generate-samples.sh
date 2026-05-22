#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source tools/lib/box3-tts-docker-common.sh
box3_require_docker
box3_build_tts_image_if_needed

docker run --rm -i \
  --gpus "${BOX3_TTS_GPUS:-all}" \
  --entrypoint python3 \
  -e PIPER_HOME=/app/.piper-cache \
  -v "$PWD/.piper-cache:/app/.piper-cache" \
  -v "$PWD/wakeword:/app/wakeword" \
  -v "$PWD/tools/lib:/app/tools/lib:ro" \
  "$box3_tts_image" \
  -u tools/lib/box3_wakeword_generate_samples.py "$@"
