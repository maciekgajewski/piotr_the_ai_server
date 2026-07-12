# Wake Word Training

## Document status

- **Authority:** Component reference and operational guide
- **Audience:** Agents training or evaluating custom wake words
- **Read when:** Changing wake-word datasets, generation, training, models, or local evaluation

Custom on-device wake words for ESPHome must be `micro_wake_word` TensorFlow Lite Micro models.

Target phrase:

```text
Ryszardzie
```

Generate positive Polish Piper samples:

```bash
tools/box3-wakeword-generate-samples.sh
```

Generated audio is ignored by Git under:

```text
wakeword/ryszardzie/samples/positive/
```

Current recorded-sample training inputs:

- positive samples recorded through the ESP32-S3-BOX-3 microphone;
- negative/background spectrogram features from the microWakeWord dataset;
- a `training_parameters.yaml` matching the microWakeWord notebook.

The earlier synthetic Piper positives are kept on disk for comparison, but the
current retraining path does not use them.

Prepare recorded positive spectrograms and the training config:

```bash
tools/box3-wakeword-prepare-training.sh --force-features
```

Download the upstream pre-generated negative features:

```bash
tools/box3-wakeword-download-negatives.sh
```

Train and export the quantized streaming TFLite model:

```bash
tools/box3-wakeword-train.sh
```

The recorded-sample training config defaults to 1500 steps. On the first real
sample run, validation had already saturated by 1000-1500 steps, so 10k steps
would mostly overfit this small positive set.

Test the local model without flashing firmware:

```bash
tools/box3-wakeword-test.sh --count 5
```

This temporarily switches the Box wake-word engine to `In Home Assistant`,
records a short clip after each Enter keypress, runs the local TFLite model, and
restores `On device` mode when done.

The wake-word Docker image automatically uses a locally cached no-AVX
TensorFlow wheel when this file exists:

```text
third_party/tensorflow-wheels/tensorflow-2.16.1-cp311-cp311-linux_x86_64.whl
```

That wheel is intentionally ignored by Git. It is a community build, not an
official TensorFlow release. It is needed on the current host because the CPU is
an Intel Core i7-930 without AVX, and stock TensorFlow exits with an illegal
instruction before it can use CUDA.

Current ESPHome artifacts:

```text
wakeword/ryszardzie/model/ryszardzie.json
wakeword/ryszardzie/model/ryszardzie.tflite
```

The first model was trained only from synthetic Piper positives. The current
local model artifact has been replaced with the recorded-sample retrain, but
the manifest `probability_cutoff` is still only a starting point. The latest ROC
summary suggested cutoff `0.12` may be less noisy than `0.01`; real Box tests
should decide before flashing.
