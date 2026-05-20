# Setting Up ESP32-S3-BOX-3

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
