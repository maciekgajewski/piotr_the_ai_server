#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
source tools/lib/box3-wakeword-docker-common.sh
box3_require_docker
box3_build_wakeword_image_if_needed
training_config="${BOX3_WAKEWORD_TRAINING_CONFIG:-/app/wakeword/ryszardzie/training_parameters_recorded.yaml}"
restore_checkpoint="${BOX3_WAKEWORD_RESTORE_CHECKPOINT:-0}"
box3_run_wakeword_container -m microwakeword.model_train_eval \
  --training_config="$training_config" \
  --train 1 \
  --restore_checkpoint "$restore_checkpoint" \
  --test_tf_nonstreaming 0 \
  --test_tflite_nonstreaming 0 \
  --test_tflite_nonstreaming_quantized 0 \
  --test_tflite_streaming 0 \
  --test_tflite_streaming_quantized 1 \
  --use_weights best_weights \
  mixednet \
  --pointwise_filters "64,64,64,64" \
  --repeat_in_block "1, 1, 1, 1" \
  --mixconv_kernel_sizes "[5], [7,11], [9,15], [23]" \
  --residual_connection "0,0,0,0" \
  --first_conv_filters 32 \
  --first_conv_kernel_size 5 \
  --stride 3 \
  "$@"
