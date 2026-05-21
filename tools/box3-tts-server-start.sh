#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source tools/lib/box3-tts-docker-common.sh
box3_require_docker
box3_build_tts_image_if_needed
box3_start_tts_server_if_needed
box3_status_tts_server
