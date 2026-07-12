# Setting Up ESP32-S3-BOX-3

## Document status

- **Authority:** Historical record
- **Audience:** Agents researching prior Box3 experiments and decisions
- **Read when:** Timestamped historical evidence is needed; verify all commands and behavior against current operational and normative documents

This file is not a current setup guide or protocol specification.

## 2026-05-20T13:31:47Z

Started setup notes for the ESP32-S3-BOX-3 satellite work.

Goal:

- Use the ESP32-S3-BOX-3 as one of many Wi-Fi voice satellites.
- Support a custom wake word.
- Stream the spoken command after the wake word to this computer/server.
- Send arbitrary audio back to the Box for playback.

Observed hardware state:

- The Box is visible over USB as `303a:1001 Espressif USB JTAG/serial debug unit`.
- The serial device is `/dev/ttyACM0`.
- User `maciek` has been added to the `dialout` group, so serial access should work.

Decision:

- Start with ESPHome.

Reason:

- ESPHome is the quickest path to a working Wi-Fi satellite.
- It already has an S3-BOX-3 voice assistant package with microphone, speaker, display, Wi-Fi, media playback, and wake word support wired up.
- ESP-IDF remains the alternative if ESPHome blocks custom behavior later.

Alternative considered:

- ESP-IDF, Espressif's native C/C++ framework.
- It gives more control over audio streaming and protocol design, but requires substantially more firmware work.

Current next step:

- Install or provide `esptool`.
- Run a read-only chip sanity check against `/dev/ttyACM0`, for example:

```bash
esptool.py --port /dev/ttyACM0 chip_id
```

No flashing has been done yet.

## 2026-05-20T13:37:06Z

Decision:

- Install ESPHome locally in this project using a Python virtual environment at `.venv/`.
- Add `.venv/` to `.gitignore`.

Reason:

- Keeps ESPHome and its Python dependencies isolated from the system Python.
- Makes the setup easy to remove or recreate for this project.
- Avoids committing the local virtual environment.

## 2026-05-20T13:41:27Z

Instruction:

- Do not run `sudo` commands from the agent terminal.
- When a task requires `sudo`, ask the user to run the exact command manually.

Recorded this in `AGENTS.md`.

## 2026-05-20T13:44:35Z

Completed:

- Recreated `.venv/` after `python3.14-venv` was installed manually by the user.
- Upgraded pip inside `.venv/`.
- Installed ESPHome locally in `.venv/`.

Installed versions:

- ESPHome `2026.4.5`
- esptool `5.2.0`

Read-only chip check:

```bash
.venv/bin/esptool --port /dev/ttyACM0 chip_id
```

Result:

- Connected successfully.
- Chip type: `ESP32-S3 (QFN56)`, revision `v0.2`.
- Features: Wi-Fi, Bluetooth LE, dual core plus LP core, 240 MHz.
- Embedded PSRAM: 16 MB.
- USB mode: USB-Serial/JTAG.
- MAC: `90:e5:b1:cb:fa:a8`.

No firmware flashing has been done yet.

## 2026-05-20T14:04:13Z

Architecture decision:

- Piotr should receive and generate voice audio directly.
- Home Assistant should be optional: one service/tool Piotr can call, not the primary voice pipeline owner.

Implication:

- The ESP32-S3-BOX-3 can still run ESPHome firmware.
- Piotr should connect to the Box through ESPHome's native API.
- The installed `aioesphomeapi` Python library exposes voice assistant subscription APIs, voice audio callbacks, voice audio send-back, configuration methods, announcement support, and media player commands.

Planned audio flow:

1. Box detects wake word locally.
2. Box opens a voice assistant session over ESPHome native API.
3. Piotr receives microphone audio from the Box.
4. Piotr runs STT, intent/tool routing, and TTS/audio generation.
5. Piotr sends generated audio back to the Box, either through the voice assistant audio path or the media player/announcement path.
6. Piotr may call Home Assistant as one optional tool if the user's command targets home automation.

## 2026-05-20T14:09:38Z

Attempted to compile firmware:

```bash
.venv/bin/esphome compile firmware/esphome/box3-satellite.yaml
```

Result:

- Compile failed before building because PlatformIO tried to create `/home/maciek/.platformio`.
- The sandbox allows writes in the project, not arbitrary writes in the home directory.

Decision:

- Keep PlatformIO state project-local at `.platformio/`.
- Added `.platformio/` to `.gitignore`.

Next compile should be run with:

```bash
PLATFORMIO_CORE_DIR=.platformio .venv/bin/esphome compile firmware/esphome/box3-satellite.yaml
```

## 2026-05-20T14:12:20Z

Second compile attempt got further but failed while creating ESP-IDF Python dependencies:

```text
Failed to initialize cache at `/home/maciek/.cache/uv`
```

Decision:

- Keep uv cache project-local at `.uv-cache/`.
- Added `.uv-cache/` to `.gitignore`.

Next compile should include both local cache variables:

```bash
PLATFORMIO_CORE_DIR=.platformio UV_CACHE_DIR=.uv-cache .venv/bin/esphome compile firmware/esphome/box3-satellite.yaml
```

## 2026-05-20T14:23:17Z

Firmware compile succeeded.

Working compile command:

```bash
PLATFORMIO_CORE_DIR=.platformio UV_CACHE_DIR=.uv-cache GIT_CEILING_DIRECTORIES=/home/maciek/piotr .venv/bin/esphome compile firmware/esphome/box3-satellite.yaml
```

Notes:

- `PLATFORMIO_CORE_DIR=.platformio` keeps PlatformIO state inside the project.
- `UV_CACHE_DIR=.uv-cache` keeps uv cache inside the project.
- `GIT_CEILING_DIRECTORIES=/home/maciek/piotr` avoids CMake detecting the empty project git repo, which has no commits yet.

Build result:

- `firmware.factory.bin` created.
- `firmware.ota.bin` created.
- Reported image size: `4445119` bytes.
- Flash usage: `54.7%`.

Next step:

- Flash over USB to `/dev/ttyACM0`.

## 2026-05-20T15:02:23Z

Flashed firmware over USB:

```bash
PLATFORMIO_CORE_DIR=.platformio UV_CACHE_DIR=.uv-cache GIT_CEILING_DIRECTORIES=/home/maciek/piotr .venv/bin/esphome upload --device /dev/ttyACM0 firmware/esphome/box3-satellite.yaml
```

Result:

- Upload succeeded.
- Device was detected as ESP32-S3 on `/dev/ttyACM0`.
- Application, bootloader, partition table, and OTA data were written and verified.
- Device hard-reset after flashing.

Next step:

- Verify the device joins Wi-Fi and Piotr can connect over the ESPHome API.

## 2026-05-20T13:48:28Z

Updated `firmware/esphome/secrets.yaml` with the user-provided 2.4 GHz Wi-Fi SSID and password.

Security note:

- `firmware/esphome/secrets.yaml` is ignored by git.
- The Wi-Fi password is intentionally not copied into these notes.

Next step:

- Run ESPHome config validation.

## 2026-05-20T13:49:06Z

Ran ESPHome config validation:

```bash
.venv/bin/esphome config firmware/esphome/box3-satellite.yaml
```

Result:

- Configuration is valid.
- ESPHome downloaded/used the upstream S3-BOX-3 voice assistant package.
- ESPHome emitted a warning that multiple ESPHome OTA configs were merged on port `3232`.

Decision:

- Keep the local OTA password block for now.

Reason:

- The warning is caused by the upstream package and our local config both defining ESPHome OTA.
- ESPHome merged them successfully.
- Keeping the local block preserves the per-device OTA password.

No firmware flashing has been done yet.

## 2026-05-20T13:47:11Z

Created ESPHome secrets file:

```text
firmware/esphome/secrets.yaml
```

Updated `.gitignore` so this file is not committed.

Generated per-device secrets for `piotr-box3-01`:

- `box3_01_api_key`
- `box3_01_ota_password`

Still needed:

- Replace the Wi-Fi placeholders in `firmware/esphome/secrets.yaml` with the real 2.4 GHz Wi-Fi SSID and password.

No firmware flashing has been done yet.
## 2026-05-20T15:41:22Z - Playback volume option added

- Added `--volume` to `tools/box3_play_audio.py`.
- The Box package currently clamps speaker media-player volume to:
  - initial: `0.5`
  - min: `0.5`
  - max: `0.8`
- Microphone-related firmware settings currently inherited from the ESPHome package:
  - ES7210 ADC mic gain: `24.0`
  - voice assistant auto gain: `31`
  - voice assistant volume multiplier: `2.0`
- Note:
  - Speaker playback loudness and captured microphone WAV loudness are separate paths.

## 2026-05-20T15:43:20Z - Capture normalization added

- Added post-capture normalization to `tools/box3_capture_voice.py`.
- Default behavior:
  - captured WAV remains 16 kHz mono 16-bit PCM;
  - after capture, samples are scaled so peak amplitude reaches `0.89`;
  - normalization is skipped for silence or empty captures.
- Disable normalization with `--normalize-peak 0`.
- Validation:
  - `python -m py_compile tools/box3_capture_voice.py` passed.

## 2026-05-20T15:46:33Z - Capture voice state cleanup fixed

- Problem:
  - The capture script could leave the Box in listening/awaiting-response state.
  - Cause: Piotr sent `VOICE_ASSISTANT_RUN_END` too soon, before ESPHome had finished stopping the microphone after `VOICE_ASSISTANT_STT_VAD_END`.
- Change:
  - `tools/box3_capture_voice.py` now sends `STT_VAD_END`, waits for the audio stream stop callback or times out, then sends `RUN_END`.
  - Added `tools/box3_reset_voice.py` to send stop/end events if the Box is already stuck in a voice-assistant run.
- Validation:
  - `python -m py_compile tools/box3_capture_voice.py tools/box3_reset_voice.py tools/box3_common.py` passed.
  - Ran `tools/box3_reset_voice.py` once over Wi-Fi.

## 2026-05-20T15:51:04Z - Capture script can run as persistent subscriber

- Finding:
  - ESPHome only keeps on-device wake-word detection running while a voice-assistant API client is subscribed.
  - A one-shot capture script exits after recording, unsubscribes, and the firmware's `on_client_disconnected` automation stops wake-word detection.
- Change:
  - Added `--continuous` to `tools/box3_capture_voice.py`.
  - In continuous mode Piotr remains connected and captures each wake-word run to a timestamped WAV.
- Command:
  - `.venv/bin/python -u tools/box3_capture_voice.py --continuous --seconds 4`
- Validation:
  - `python -m py_compile tools/box3_capture_voice.py` passed.

## 2026-05-20T19:18:23Z - Playback volume mapping documented

- Finding:
  - ESPHome speaker media player remaps API volume `0.0..1.0` into configured firmware limits.
  - Current package limits are `volume_min: 0.5` and `volume_max: 0.8`.
  - Therefore API volume `0.8` becomes effective firmware volume `0.74`; API volume `1.0` becomes `0.8`.
- Change:
  - `tools/box3_play_audio.py` now prints the effective firmware volume when `--volume` is used.
  - `tools/README.md` documents the mapping.

## 2026-05-20T19:47:38Z - Local Whisper STT tool added

- Added separate STT environment `.venv-stt/` and `requirements-stt.txt`.
- Installed:
  - `faster-whisper`
  - `aioesphomeapi`
  - `PyYAML`
- Added `tools/box3_stt_whisper.py`.
  - Waits for Box wake word through ESPHome voice-assistant API.
  - Captures a fixed duration of 16 kHz mono PCM.
  - Runs local `faster-whisper`.
  - Prints recognized text to stdout and status/debug to stderr.
  - Supports `--continuous`, `--device auto|cuda|cpu`, `--model`, `--language`, `--keep-audio`, and `--self-test`.
- CUDA access:
  - Normal sandbox cannot see `/dev/nvidia*`.
  - Escalated STT venv command can load Whisper on CUDA.
  - CUDA self-test passed with model `base`, device `cuda`, compute type `float16`.
- Validation:
  - `python -m py_compile tools/box3_stt_whisper.py tools/box3_common.py` passed.
  - `HF_HOME=.hf-cache .venv-stt/bin/python -c "import faster_whisper, aioesphomeapi"` passed.
  - `nvidia-smi` shows RTX 2060, driver `595.71.05`, CUDA `13.2`.
  - `tools/box3_api_probe.py` still connects to the Box.

## 2026-05-20T19:50:28Z - STT defaults adjusted

- Review comments addressed:
  - default Whisper language is now Polish (`pl`);
  - added `--list-models` to print common faster-whisper model presets.
- Validation:
  - `python -m py_compile tools/box3_stt_whisper.py` passed.
  - `tools/box3_stt_whisper.py --list-models` prints presets.
  - CPU self-test with `tiny` passed.
  - CUDA self-test with `base` passed.

## 2026-05-20T19:55:07Z - STT audio callback fixed

- Problem:
  - `.venv-stt` installed a newer `aioesphomeapi` whose voice assistant audio callback passes `(data, data2)`.
  - `tools/box3_stt_whisper.py` accepted only one audio argument, causing the API connection to crash after wake-word detection.
- Change:
  - STT audio callback now accepts `data2` and appends both payload chunks when present.
- Validation:
  - `python -m py_compile tools/box3_stt_whisper.py` passed.
  - CUDA self-test with `base` passed.

## 2026-05-20T20:00:17Z - Local CUDA runtime libraries added

- Problem:
  - CUDA model loading worked, but first real transcription failed with missing `libcublas.so.12`.
  - This means the NVIDIA driver was available, but CUDA user-space libraries were not.
- Change:
  - Installed `nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, and `nvidia-cuda-nvrtc-cu12` into `.venv-stt`.
  - Added `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` to `requirements-stt.txt`.
  - `tools/box3_stt_whisper.py` now prepends local NVIDIA library directories to `LD_LIBRARY_PATH` and re-execs itself once before importing `faster_whisper`.
  - `--self-test` now runs a tiny transcription, not just model load.
- Validation:
  - CPU self-test with `tiny` passed.
  - CUDA self-test with `base` passed and completed transcription.

## 2026-05-20T20:01:36Z - STT shell wrappers added

- Added wrapper scripts:
  - `tools/box3-stt`
  - `tools/box3-stt-continuous`
  - `tools/box3-stt-self-test`
- Purpose:
  - hide `.venv-stt/bin/python`;
  - set `HF_HOME=.hf-cache` by default;
  - keep the common STT commands short.
- Validation:
  - `tools/box3-stt --list-models` passed.
  - `tools/box3-stt-self-test --device cpu --model tiny` passed.
  - `tools/box3-stt-self-test --device cuda` passed outside the sandbox.

## 2026-05-20T20:05:00Z - Tool implementation files nested

- Moved Python implementation scripts under `tools/lib/`.
- Removed executable flags from the Python implementation scripts.
- User-facing executable commands are now shell wrappers such as:
  - `tools/box3-stt`
  - `tools/box3-stt-continuous`
  - `tools/box3-stt-self-test`
- Updated wrapper targets and current tools README paths.

## 2026-05-21T07:25:02Z - STT wrappers moved to Docker

- Added Docker image definition:
  - `docker/stt.Dockerfile`
  - `.dockerignore`
- STT shell wrappers renamed to `.sh` suffix:
  - `tools/box3-stt.sh`
  - `tools/box3-stt-continuous.sh`
  - `tools/box3-stt-self-test.sh`
- STT wrappers now:
  - build `piotr-box3-stt:latest` on first use;
  - run with host networking;
  - request all GPUs by default with Docker `--gpus all`;
  - mount `.hf-cache`, `firmware`, `audio`, and `tools/lib`.
- Added `tools/lib/box3-docker-common.sh` as non-executable wrapper support.
- Docker was not available in the current command environment, so image build/run was not verified here.

## 2026-05-21T07:51:00Z - STT Docker image built

- Docker access was fixed by adding the user to the `docker` group.
- Built `piotr-box3-stt:latest` through `tools/box3-stt.sh --list-models`.
- Validation:
  - `tools/box3-stt.sh --list-models` passed.
  - `BOX3_STT_GPUS=none tools/box3-stt-self-test.sh --device cpu --model tiny` passed.
- GPU container status:
  - `tools/box3-stt-self-test.sh --device cuda` currently fails with Docker error `could not select device driver "" with capabilities: [[gpu]]`.
  - Docker daemon runtimes list only `runc`; NVIDIA Container Toolkit is not configured yet.

## 2026-05-21T08:18:41Z - NVIDIA Docker runtime configured

- User installed/configured NVIDIA Container Toolkit on the host.
- `docker run --rm --gpus all nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04 nvidia-smi` works.
- GPU-first STT wrappers can now use Docker `--gpus all`.

## 2026-05-21T09:34:18Z - Piper TTS prototype added

- Decision: start TTS with Piper and the Polish `pl_PL-gosia-medium` voice.
- Added `tools/box3-tts.sh`.
- Added `docker/tts.Dockerfile` and `requirements-tts.txt`.
- Added `tools/lib/box3_tts_piper.py`:
  - reads text from stdin;
  - downloads/caches the Piper Polish voice under `.piper-cache`;
  - synthesizes WAV locally;
  - converts it to 48 kHz mono FLAC for Box playback;
  - plays through the existing ESPHome media-player path.
- The TTS container requests Docker `--gpus all` to keep the project GPU-first, even though Piper inference itself is CPU-oriented.

## 2026-05-21T09:42:24Z - Piper TTS verified on Box

- Built `piotr-box3-tts:latest`.
- `tools/box3-tts.sh --list-voices` lists `pl_PL-gosia-medium`.
- `echo ... | tools/box3-tts.sh --self-test` downloaded the Polish voice, synthesized audio, and wrote FLAC under `audio/tts`.
- First playback attempt failed because the container could not resolve the `.local` Box hostname.
- Updated Docker wrappers to resolve the default Box hostname on the host and pass `BOX3_HOST` into containers.
- `echo "Cześć, jestem Piotr." | tools/box3-tts.sh` succeeded and played `box3-tts-20260521T094156Z.flac` on the Box.
- Re-ran `tools/box3-stt-self-test.sh --device cuda`; STT Docker path still works.

## 2026-05-21T09:53:00Z - Piper Polish voice catalog expanded

- Added all current official Polish Piper voices from `rhasspy/piper-voices`:
  - `pl_PL-bass-high`
  - `pl_PL-darkman-medium`
  - `pl_PL-gosia-medium`
  - `pl_PL-mc_speech-medium`
  - `pl_PL-mls_6892-low`
- `tools/box3-tts.sh --list-voices` lists these voices.
- `tools/box3-tts.sh --voice ...` downloads and uses the selected voice.
- TTS logs:
  - `tts_generate_seconds` for Piper synthesis plus FLAC conversion;
  - `tts_send_seconds` for sending the ESPHome media-player command.

## 2026-05-21T09:55:10Z - Default Piper voice changed

- Default TTS voice changed from `pl_PL-gosia-medium` to `pl_PL-bass-high`.

## 2026-05-21T10:26:33Z - Experimental Piper streaming prototype

- Added `tools/box3-tts.sh --stream`.
- Streaming mode:
  - gives the Box an HTTP WAV URL immediately;
  - starts Piper when the Box connects;
  - forwards Piper stdout chunks to the HTTP response;
  - avoids writing a complete generated audio file before playback.
- Added streaming timing logs:
  - `tts_send_seconds`;
  - `tts_first_audio_seconds`;
  - `tts_stream_seconds`;
  - `tts_stream_bytes`.
- Test result with a 126-character Polish phrase:
  - `tts_send_seconds=0.101`;
  - `tts_first_audio_seconds=9.985`;
  - `tts_stream_seconds=10.271`;
  - `tts_stream_bytes=353324`.
- Interpretation: the Box can request the HTTP stream, but Piper stdout does not provide useful early audio for this phrase; first bytes arrived near the end of synthesis.
- Piper `--output-raw` was also checked and did not materially improve first-byte latency for the same phrase.
- Existing pre-rendered FLAC mode remains the default.

## 2026-05-21T10:38:04Z - Wyoming Piper server autostart added

- Decision: use a long-running Wyoming Piper server to avoid paying Piper process startup/model loading cost on every TTS request.
- Added service wrappers:
  - `tools/box3-tts-server-start.sh`
  - `tools/box3-tts-server-stop.sh`
  - `tools/box3-tts-server-status.sh`
  - `tools/box3-tts-server-logs.sh`
- Server container name: `piotr-box3-tts-server`.
- Server port: `10200`.
- `tools/box3-tts.sh` now autostarts the server for default Wyoming-backed synthesis.
- Added `--engine wyoming|cli`; default is `wyoming`.
- `--list-voices`, `--stream`, and `--engine cli` do not autostart the server.
- Validation:
  - first Wyoming-backed playback succeeded with `tts_generate_seconds=5.013` and `tts_send_seconds=0.101`;
  - second warm-server self-test succeeded with `tts_generate_seconds=3.001`.

## 2026-05-21T10:50:14Z - Wyoming chunk streaming prototype

- Changed `tools/box3-tts.sh --stream` to use the Wyoming server by default.
- Streaming mode now:
  - starts an HTTP WAV response for the Box;
  - sends a WAV header when Wyoming `AudioStart` arrives;
  - forwards each Wyoming `AudioChunk` directly to the Box;
  - stops on Wyoming `AudioStop`.
- Added `tts_first_byte_sent_seconds`.
- Previous Piper CLI streaming path remains available with `--stream --engine cli`.
- Validation:
  - 139-character phrase: first audio bytes sent at `2.803s`; full stream `10.377s`; `383020` bytes.
  - 48-character phrase with smaller server chunks: first audio bytes sent at `3.089s`; full stream `3.196s`; `156716` bytes.
- Interpretation: Wyoming chunk streaming removes the full-file buffering delay, but Piper/voice inference still takes roughly 2.8-3.1s before first chunk for tested Polish phrases.

## 2026-05-21T11:10:44Z - TTS choke point found and GPU enabled

- Problem found: the TTS container had `onnxruntime-gpu` installed but CPU `onnxruntime` was shadowing it, so Wyoming Piper exposed only CPU providers.
- Confirmed before fix:
  - providers: `['AzureExecutionProvider', 'CPUExecutionProvider']`;
  - `nvidia-smi` showed no TTS process.
- Fix:
  - added `onnxruntime-gpu` to `requirements-tts.txt`;
  - Docker image now uninstalls CPU `onnxruntime` and force-reinstalls `onnxruntime-gpu`;
  - Wyoming Piper starts with `--use-cuda`;
  - `--stream` now autostarts the Wyoming server.
- Confirmed after fix:
  - providers: `['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']`.
- Direct Wyoming timing:
  - first CUDA warmup request first chunk: `3.737s`;
  - second warm request first chunk: `0.311s`.
- Box streaming timing with warmed GPU server:
  - `tts_first_audio_seconds=0.312`, `tts_first_byte_sent_seconds=0.311`;
  - next run: `tts_first_audio_seconds=0.279`, `tts_first_byte_sent_seconds=0.279`.

## 2026-05-21T13:30:12Z - Ryszardzie wake-word training scaffold

- Confirmed ESPHome BOX-3 package uses on-device `micro_wake_word` with model names:
  - `okay_nabu`
  - `hey_mycroft`
  - `hey_jarvis`
- Custom phrase `Ryszardzie` needs a microWakeWord `.tflite` model plus ESPHome JSON manifest.
- Added local wake-word tooling:
  - `tools/box3-wakeword-generate-samples.sh`
  - `tools/box3-wakeword-prepare-training.sh`
  - `tools/box3-wakeword-download-negatives.sh`
  - `tools/box3-wakeword-train.sh`
  - `docker/wakeword.Dockerfile`
  - `wakeword/README.md`
- Generated 225 positive `Ryszardzie` samples with Polish Piper voices:
  - `pl_PL-bass-high`
  - `pl_PL-darkman-medium`
  - `pl_PL-gosia-medium`
  - `pl_PL-mc_speech-medium`
  - `pl_PL-mls_6892-low`
- Sample format verified inside the TTS container:
  - 16 kHz
  - mono
  - about 1.17 seconds for the first sample
- Training image builds, but local training is blocked:
  - host CPU is Intel Core i7-930;
  - CPU flags do not include AVX;
  - stock TensorFlow 2.x exits with illegal instruction before it can use CUDA.
- Options for training:
  - use an AVX-capable host;
  - build/use a no-AVX TensorFlow 2.16+ wheel;
  - train in a cloud GPU runner.

## 2026-05-21T15:05:00Z - Ryszardzie wake-word model export

- Kept a community no-AVX TensorFlow 2.16.1 wheel locally under `third_party/tensorflow-wheels/`.
- The wheel is ignored by Git; it is used only by the wake-word Docker image on this non-AVX host.
- TensorFlow imports and sees the RTX 2060, although it warns that some GPU kernels may JIT compile for compute capability 7.5.
- Downloaded and extracted the upstream microWakeWord negative datasets locally under ignored `wakeword/ryszardzie/negative_datasets/`.
- Prepared positive spectrogram features from the 225 synthetic Piper samples.
- Patched vendored microWakeWord streaming export for the current TensorFlow/Keras behavior when `tf.control_dependencies` receives no state assignment op.
- Exported a quantized streaming TFLite model:
  - `wakeword/ryszardzie/model/ryszardzie.tflite`
  - size: about 60 KiB
- Added the ESPHome model manifest:
  - `wakeword/ryszardzie/model/ryszardzie.json`
  - wake word: `Ryszardzie`
  - language: Polish
  - initial `probability_cutoff`: `0.01`
  - initial `tensor_arena_size`: `30000`
- Built-in TFLite streaming export test completed, but the validation set is still synthetic-heavy.
- Decision: treat this as a first prototype for on-device testing, not as a tuned production wake word.

## 2026-05-21T15:35:00Z - Ryszardzie firmware image prepared

- Box is visible on USB as `/dev/ttyACM0`.
- Changed firmware to use only the local `Ryszardzie` wake-word model.
- ESPHome list merging kept the upstream built-in wake words when overriding from the top-level config, so the upstream package was copied locally to:
  - `firmware/esphome/packages/esp32-s3-box-3-ryszardzie.yaml`
- In that local package, `micro_wake_word.models` now contains only:
  - `wakeword/ryszardzie/model/ryszardzie.json`
- Corrected the model manifest version to `2` for the current ESPHome model schema.
- `esphome config` validates and resolves exactly one wake-word model.
- Built the firmware image successfully using temporary build/cache paths under `/tmp` because the root filesystem has very little free space.
- Build artifacts:
  - factory image: `/tmp/piotr-esphome-build/piotr-box3-01/.pioenvs/piotr-box3-01/firmware.factory.bin`
  - OTA image: `/tmp/piotr-esphome-build/piotr-box3-01/.pioenvs/piotr-box3-01/firmware.ota.bin`
- After the build, `/dev/ttyACM0` was no longer present; recheck USB before flashing.
- No flash has been performed yet.

## 2026-05-21T15:44:00Z - Ryszardzie firmware flashed

- User confirmed `/dev/ttyACM0` existed on the host after reconnecting the Box.
- The sandbox could not see `/dev/ttyACM0`, so the upload required an escalated command with host device access.
- Flashed via:
  - `.venv/bin/esphome upload firmware/esphome/box3-satellite.yaml --device /dev/ttyACM0`
- Upload wrote and verified:
  - app at `0x10000`
  - bootloader at `0x0`
  - partitions at `0x8000`
  - OTA data at `0x9000`
- ESPHome reported `Successfully uploaded program.`
- Post-flash checks:
  - USB still enumerates as `303a:1001 Espressif USB JTAG/serial debug unit`.
  - Ping to `192.168.0.180` succeeds.

## 2026-05-21T15:55:00Z - Disk cleanup after wake-word prototype

- Confirmed the flashed `Ryszardzie` wake word works on the Box.
- Freed root filesystem space while preserving retraining inputs:
  - removed project-local failed PlatformIO cache: `.platformio/`
  - removed ESPHome build output: `firmware/esphome/.esphome/build/`
  - removed negative dataset ZIP archives after extraction
- Kept:
  - positive training samples: `wakeword/ryszardzie/samples/`
  - extracted negative datasets: `wakeword/ryszardzie/negative_datasets/`
  - generated features: `wakeword/ryszardzie/generated_features/`
  - trained model outputs: `wakeword/ryszardzie/trained_models/`
  - deployed model package: `wakeword/ryszardzie/model/`
- Root filesystem free space improved from about `612M` to about `14G`.

## 2026-05-21T20:15:00Z - Real positive wake-word sample recorder

- Decision: improve the weak `Ryszardzie` model by recording real positive samples instead of relying only on synthetic Piper-generated samples.
- Constraint: the host is headless over SSH, so the ESP32-S3-BOX-3 microphone is the recording device.
- Added a recorder tool:
  - `tools/box3-record-wakeword-samples.sh`
  - implementation: `tools/lib/box3_record_wakeword_samples.py`
- Defaults:
  - phrase: `Ryszardzie`
  - sample length: `1.5s`
  - output directory: `audio/training-samples/ryszardzie/positive/`
  - numbered filenames such as `0001.wav`, `0002.wav`
- The tool temporarily switches the Box wake-word engine to `In Home Assistant` to stream raw microphone audio, prompts before each sample, and restores `On device` mode during cleanup.

## 2026-05-21T20:17:28Z - Generic Box audio playback wrapper

- Added `tools/box3-play-audio.sh` as the shell entrypoint for playing a local sound file on the Box.
- The wrapper delegates to `tools/lib/box3_play_audio.py`, which serves the file over temporary HTTP and asks the Box media player to play it.
- Intended immediate use: verify recorded wake-word samples such as `audio/training-samples/ryszardzie/positive/0001.wav`.

## 2026-05-21T20:20:31Z - Wake-word sample recorder cleanup

- Added a default `0.4s` ready delay after pressing Enter before recording starts, to avoid capturing keyboard noise.
- Added default peak normalization for recorded samples:
  - default target peak: `0.89`
  - disable with `--normalize-peak 0`
- The recorder logs the original peak and applied gain for each saved sample.

## 2026-05-21T20:43:56Z - Ryszardzie retrained from recorded positives

- Retrained the `Ryszardzie` wake-word model using only recorded positive samples from:
  - `audio/training-samples/ryszardzie/positive/`
- Synthetic Piper positives remain on disk for comparison, but were not used in this retraining run.
- Reused the existing extracted negative/background feature datasets under:
  - `wakeword/ryszardzie/negative_datasets/`
- Generated recorded-positive features under:
  - `wakeword/ryszardzie/generated_features_recorded/`
- Trained into a separate recorded-run model directory:
  - `wakeword/ryszardzie/trained_models/wakeword_recorded/`
- Training was stopped after 1500 steps because the small recorded-positive set had already saturated validation metrics by 1000-1500 steps.
- Exported and evaluated the quantized streaming TFLite model:
  - `wakeword/ryszardzie/trained_models/wakeword_recorded/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite`
- Replaced the local deployable model artifact with that recorded-sample retrain:
  - `wakeword/ryszardzie/model/ryszardzie.tflite`
- ROC summary:
  - cutoff `0.12`: `frr=0.0000`, `faph=0.000`
  - cutoff `0.01`: `frr=0.0000`, `faph=0.187`
- Left the ESPHome manifest cutoff unchanged for now; tune it after live no-flash tests.
- Added no-flash model test tools:
  - `tools/box3-wakeword-test.sh`
  - `tools/box3-wakeword-predict-file.sh`

## 2026-05-22T05:30:30Z - Wake-word test captures made temporary by default

- Changed `tools/box3-wakeword-test.sh` so test audio is temporary unless `--output-dir` is provided.
- Default output is now just the model prediction result.
- Use `--output-dir audio/wakeword-tests/ryszardzie/` to preserve captured test WAV files for inspection.

## 2026-05-22T11:12:59Z - Ryszardzie cutoff selected for flashing

- Decision: use the recorded-sample `Ryszardzie` model with ESPHome `probability_cutoff` set to `0.7`.
- Rationale: live no-flash tests showed real positives can spike high while noise can also produce moderate scores; `0.7` is a practical first on-device cutoff.
- Updated `wakeword/ryszardzie/model/ryszardzie.json`.
- Box is connected over USB as `/dev/ttyACM0`; flashing requires running ESPHome upload outside the sandbox for USB device access.

## 2026-05-22T11:14:47Z - Ryszardzie cutoff 0.7 flashed

- Validated ESPHome configuration after changing the model manifest cutoff.
- Flashed the Box over USB `/dev/ttyACM0` using ESPHome upload outside the sandbox.
- Upload wrote and verified:
  - app at `0x10000`
  - bootloader at `0x0`
  - partitions at `0x8000`
  - OTA data at `0x9000`
- ESPHome reported `Successfully uploaded program.`
- Post-flash checks:
  - `/dev/ttyACM0` still exists.
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-05-25T14:25:34Z - Conversation cue timing firmware reflashed

- Manual mic-protocol test showed the wake cue was heard after speech capture, and the test harness sent `MessageEndCue` only after operator questions.
- Fixed the test harness to send `MessageEndCue` immediately after `AudioEnd`, before asking the operator about the captured turn.
- Updated cue scripts so local media playback waits briefly for `media_player.is_announcing` to become true before waiting for playback to finish.
- Removed unbounded pre-cue idle waits from short cue scripts and follow-up start.
- Validated ESPHome configuration.
- Rebuilt firmware and verified generated `main.cpp` contains the cue playback-start waits.
- Flashed the Box over OTA to `192.168.0.180`.
- ESPHome reported `OTA successful` and `Successfully uploaded program.`
- Post-flash check:
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-05-25T11:05:42Z - Conversation cue firmware flashed

- Added local on-device cue sounds for:
  - wake-word recognized
  - user message end detected
  - follow-up microphone open
  - conversation timeout
- Added ESPHome API actions used by the AI server:
  - `play_message_end_cue`
  - `play_conversation_timeout_cue`
  - `start_follow_up_listening`
- `start_follow_up_listening` waits for speaker playback to be idle, plays the follow-up cue, and starts `voice_assistant` with synthetic wake word `follow_up`.
- Validated ESPHome configuration.
- Rebuilt firmware and verified generated `main.cpp` contains:
  - local cue audio files
  - the new API actions
  - `VoiceAssistantStartAction` with wake word `follow_up`
- USB serial was not visible as `/dev/ttyACM*` or `/dev/ttyUSB*`; flashed over OTA to `192.168.0.180`.
- ESPHome reported `OTA successful` and `Successfully uploaded program.`
- Post-flash check:
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-05-22T16:21:31Z - Add Okay Nabu fallback wake word

- Live `Ryszardzie` wake-word behavior is still hit and miss.
- Decision: keep the custom local `Ryszardzie` model and add ESPHome's built-in `okay_nabu` model as a second on-device wake word.
- Chime-after-wake is not included in this flash; it needs a separate test because local media playback can interact with microphone capture and wake-word restart behavior.

## 2026-05-22T16:22:00Z - Dual wake-word firmware flashed

- Validated ESPHome configuration with two on-device wake-word models:
  - local `Ryszardzie`
  - built-in `okay_nabu`
- Flashed the Box over USB `/dev/ttyACM0`.
- ESPHome reported `Successfully uploaded program.`
- Post-flash checks:
  - `/dev/ttyACM0` still exists.
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-05-23T10:04:09Z - Enable Okay Nabu explicitly

- The clean-generated ESPHome `main.cpp` showed both wake-word models, but `Okay Nabu` was generated with `default_enabled=false`.
- ESPHome enables only the first wake-word model by default; additional models need explicit enabling or persisted runtime state.
- Decision: assign `okay_nabu` a stable ESPHome model ID and run `micro_wake_word.enable_model` at boot.

## 2026-05-23T10:14:31Z - Dual wake-word firmware reflashed with Okay Nabu enabled

- Validated ESPHome configuration after adding the boot-time `micro_wake_word.enable_model` action.
- Rebuilt the firmware from a clean build tree and verified generated `main.cpp` contains:
  - local `Ryszardzie` model
  - built-in `Okay Nabu` model
  - boot-time `EnableModelAction` for `okay_nabu_wake_word`
- Flashed the Box over USB `/dev/ttyACM0`.
- ESPHome reported `Successfully uploaded program.`
- Post-flash checks:
  - `/dev/ttyACM0` still exists.
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-05-23T10:31:32Z - Red touch button and explicit dual wake-word enable flashed

- User confirmed `Okay Nabu` works, but `Ryszardzie` does not.
- ESPHome persists wake-word model enabled/disabled state in flash, so relying on default enabled state can leave a model disabled after previous runtime changes.
- Decision: give both wake-word models stable IDs and explicitly enable both at boot:
  - `ryszardzie_wake_word`
  - `okay_nabu_wake_word`
- Added GT911 touchscreen support for the ESP32-S3-BOX-3 red circle below the screen.
- Added internal GT911 button binary sensor `red_touch_button` at index `0`.
- A single tap on the red touch button now:
  - stops the timer if a timer is ringing
  - otherwise starts a normal voice assistant run with synthetic wake word `button`, when not muted and not already running
- Validated ESPHome configuration.
- Rebuilt firmware and verified generated `main.cpp` contains:
  - boot-time `EnableModelAction` for both wake-word models
  - local `Ryszardzie` model
  - built-in `Okay Nabu` model
  - `GT911Touchscreen` on the existing I2C bus
  - `GT911Button` index `0`
  - `VoiceAssistantStartAction` with wake word `button`
- Flashed the Box over USB `/dev/ttyACM0`.
- ESPHome reported `Successfully uploaded program.`
- Post-flash checks:
  - `/dev/ttyACM0` still exists.
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-05-26T09:38:02Z - Follow-up listening chime flashed

- Decision: follow-up capture should be announced with a chime, using a separate firmware media file so the sound can diverge later.
- Replaced `follow_up_listening.wav` with the same sample as `wake_recognized.wav`.
- Added 2s safety timeouts to cue playback completion waits, so a stuck media-player/speaker state cannot block follow-up listening forever.
- Updated the microphone protocol test to ask about the follow-up chime before requesting the message-end cue.
- Updated the conversation protocol document to state that `StartFollowUpListening` includes the follow-up cue.
- Validated ESPHome configuration.
- Rebuilt firmware and verified generated `main.cpp` contains:
  - `follow_up_listening_sound`
  - `play_follow_up_listening_cue`
  - `VoiceAssistantStartAction` with wake word `follow_up`
  - 2s cue wait timeouts
- USB serial was not visible as `/dev/ttyACM0`; flashed over OTA to `192.168.0.180`.
- ESPHome reported `OTA successful` and `Successfully uploaded program.`
- Post-flash check:
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-05-26T10:33:43Z - Guard follow-up cue startup

- User observed intermittent follow-up failures where no follow-up cue was audible, capture opened later, and the stream ended on initial silence before speech.
- User also observed a delayed chime when interrupting the test, suggesting the cue was queued or blocked behind media/voice-assistant state.
- Added `server_controlled_follow_up_starting` firmware state.
- While follow-up startup is active, `media_player.on_idle` no longer restarts wake-word detection.
- `start_follow_up_listening` now waits for media player and speaker idle before playing the follow-up cue.
- Increased cue-start waits from 500ms to 2s.
- Validated ESPHome configuration.
- Rebuilt firmware and verified generated `main.cpp` contains:
  - `server_controlled_follow_up_starting`
  - `start_follow_up_listening` idle wait before `play_follow_up_listening_cue`
  - `VoiceAssistantStartAction` with wake word `follow_up`
- USB serial was not visible as `/dev/ttyACM0`; flashed over OTA to `192.168.0.180`.
- ESPHome reported `OTA successful` and `Successfully uploaded program.`
- Post-flash check:
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-06-27T21:07:47Z - Open-mic control service validated

- Added ESPHome API service `start_open_mic_listening` for server-controlled open-mic startup.
- The service stops wake-word handling, waits for media player and speaker idle, disables wake-word mode on the voice assistant, and starts continuous voice assistant capture.
- Validated ESPHome configuration.
- Rebuilt firmware and verified generated `main.cpp` contains:
  - `start_open_mic_listening`
  - `api::UserServiceTrigger` for `start_open_mic_listening`
  - `voice_assistant::StartContinuousAction`
- Firmware was compiled only; it was not flashed in this step.

## 2026-06-27T21:14:58Z - Open-mic control service flashed OTA

- Revalidated ESPHome configuration before flashing.
- Verified generated `main.cpp` contains:
  - `start_open_mic_listening`
  - `voice_assistant::StartContinuousAction`
- Flashed the office Box over OTA to `192.168.0.180`.
- ESPHome reported `OTA successful` and `Successfully uploaded program.`
- Post-flash check:
  - Ping to `192.168.0.180` succeeded with 2/2 replies.

## 2026-06-28T08:55:00Z - Open-mic display stays idle until server acceptance

- Fixed open-mic satellite UI behavior where any loud VAD segment could switch the Box display into listening/thinking state before the server detected the wake phrase.
- Added `server_controlled_open_mic_active` in `firmware/esphome/packages/esp32-s3-box-3-ryszardzie.yaml`.
- While server-controlled open mic is active, `voice_assistant.on_listening`, `on_stt_vad_end`, and `on_stt_end` no longer update the display or request text.
- `play_message_end_cue` now switches to the thinking phase only after the server accepts the open-mic segment and asks for the cue.
- Validated with `esphome config` and `esphome compile`; generated `main.cpp` contains the open-mic guard and accepted-cue transition.

## 2026-06-28T20:34:00Z - Open-mic rejected-candidate reset service

- Added ESPHome API service `reset_open_mic_wake_candidate` for the AI-server `OpenMicWakeCandidateRejected` protocol event.
- The service keeps continuous open-mic capture active, resets `voice_assistant_phase` to listening while `server_controlled_open_mic_active` is true, clears request/response text placeholders, and redraws the display.
- Validated ESPHome configuration.
- Rebuilt firmware and verified generated `main.cpp` contains:
  - `api::UserServiceTrigger` for `reset_open_mic_wake_candidate`
  - `voice_assistant_phase->value() = 2`
  - `text_request` and `text_response` reset to `"..."`
- Flashed the office Box over OTA to `192.168.0.180`.
- ESPHome reported `OTA successful` and `Successfully uploaded program.`

## 2026-06-29T18:31:01Z - Shared satellite microphone service contract

- Added `firmware/esphome/packages/piotr-voice-satellite-api-services.yaml` as the common ESPHome API service contract for Piotr voice satellites.
- Box and Voice PE firmware packages now include the common service contract instead of duplicating `api.services`.
- The contract exposes:
  - `play_message_end_cue`
  - `play_conversation_timeout_cue`
  - `start_follow_up_listening`
  - `start_open_mic_listening`
  - `reset_open_mic_wake_candidate`
- Voice PE firmware now implements the open-mic backend scripts so it has the same server-facing microphone control surface as the office Box, while keeping Voice PE-specific LED and audio playback behavior local to its package.
- Validated ESPHome configuration for:
  - `firmware/esphome/box3-satellite.yaml`
  - `firmware/esphome/voice-pe-bedroom.yaml`
  - `firmware/esphome/voice-pe-02.yaml`
- Compiled all three firmware entrypoints successfully.
- Verified generated `main.cpp` for all three builds contains:
  - all five shared API service triggers
  - `server_controlled_open_mic_active`
  - `voice_assistant::StartContinuousAction`
- Flashed the office Box over OTA to `192.168.0.180`; ESPHome reported `OTA successful`.
- Flashed the bedroom Voice PE over OTA to `192.168.0.13`; ESPHome reported `OTA successful`.
- Flashed the new living-room Voice PE over USB on `/dev/ttyACM0`; esptool verified the written hashes and ESPHome reported `Successfully uploaded program`.
- Post-flash name resolution:
  - `piotr-box3-01-cbfaA8.local` -> `192.168.0.180`
  - `piotr-voice-pe-bedroom-01.local` -> `192.168.0.13`
  - `piotr-voice-pe-02.local` -> `192.168.0.167`

## 2026-06-29T19:27:42Z - Added office Voice PE 03

- Added `firmware/esphome/voice-pe-03.yaml` for `piotr-voice-pe-03`.
- Added matching ESPHome API and OTA secrets in `firmware/esphome/secrets.yaml` and placeholders in `firmware/esphome/secrets.example.yaml`.
- Added AI-server microphone config for `voice-pe-03` in area `office`.
- Moved the existing Box 3 AI-server area from `office` to `Hanna's Den` while keeping its existing config name stable.
- Updated `docs/project-standard-satellite-behavior.md` to list `voice-pe-03` as the office Voice PE.
- Validated ESPHome configuration for `firmware/esphome/voice-pe-03.yaml`.
- Compiled `firmware/esphome/voice-pe-03.yaml` successfully.
- Verified generated `main.cpp` contains:
  - all five shared API service triggers
  - `server_controlled_open_mic_active`
  - `voice_assistant::StartContinuousAction`
- Flashed the new Voice PE over USB on `/dev/ttyACM0`; esptool verified written hashes and ESPHome reported `Successfully uploaded program`.
- Post-flash name resolution:
  - `piotr-voice-pe-03.local` -> `192.168.0.153`
