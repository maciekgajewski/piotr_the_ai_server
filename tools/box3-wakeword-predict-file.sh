#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source tools/lib/box3-wakeword-docker-common.sh
box3_require_docker
box3_build_wakeword_image_if_needed
box3_run_wakeword_container -u tools/lib/box3_wakeword_predict_file.py "$@"
