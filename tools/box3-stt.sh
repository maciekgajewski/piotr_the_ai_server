#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source tools/lib/box3-docker-common.sh
box3_require_docker
box3_build_stt_image_if_needed
box3_run_stt_container "$@"
