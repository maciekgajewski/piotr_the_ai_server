#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source tools/lib/box3-wakeword-docker-common.sh
box3_require_docker
box3_build_wakeword_image_if_needed
box3_run_wakeword_container -u tools/lib/box3_wakeword_prepare_training.py \
  --positive-samples-dir /app/audio/training-samples/ryszardzie/positive \
  --features-dir /app/wakeword/ryszardzie/generated_features_recorded \
  --train-dir /app/wakeword/ryszardzie/trained_models/wakeword_recorded \
  --config-path /app/wakeword/ryszardzie/training_parameters_recorded.yaml \
  "$@"
