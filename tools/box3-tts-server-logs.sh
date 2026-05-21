#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source tools/lib/box3-tts-docker-common.sh
box3_require_docker
box3_logs_tts_server
