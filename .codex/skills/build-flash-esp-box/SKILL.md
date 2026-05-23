---
name: build-flash-esp-box
description: Build, configure, verify, flash, and troubleshoot the Piotr project ESP32-S3-BOX-3 ESPHome satellite firmware. Use when working on firmware/esphome/box3-satellite.yaml or packages for wake words, voice assistant behavior, GT911 red touch button, audio, Wi-Fi/API/OTA settings, ESPHome config/compile/upload, generated main.cpp verification, or post-flash checks for the Box.
---

# Build/Flash ESP Box

## Core Rules

- Read `README.md` "Architecture decisions" before architecture-related changes.
- Follow `AGENTS.md`: no `sudo`; ask the user to run exact sudo commands manually.
- Prefer one step at a time for ESP32-S3-BOX-3 setup work.
- Do not flash until config validation, compile, and generated-source checks pass.
- Use escalated execution for USB flashing because `/dev/ttyACM0` is outside the sandbox.
- Preserve unrelated dirty worktree changes.
- Record decisions and performed steps in `notes/setting-up-esp-box.md` with UTC timestamps.

## Important Paths

- Main ESPHome entrypoint: `firmware/esphome/box3-satellite.yaml`
- Box package: `firmware/esphome/packages/esp32-s3-box-3-ryszardzie.yaml`
- Secrets: `firmware/esphome/secrets.yaml`
- Setup journal: `notes/setting-up-esp-box.md`
- Build tree: `/tmp/piotr-esphome-build/piotr-box3-01`
- Generated source: `/tmp/piotr-esphome-build/piotr-box3-01/src/main.cpp`
- USB serial device: `/dev/ttyACM0`
- Current Box IP: `192.168.0.180`

## Standard Commands

Use these environment variables for ESPHome commands to keep generated files and caches out of the repo:

```bash
PLATFORMIO_CORE_DIR=/tmp/piotr-platformio \
UV_CACHE_DIR=/tmp/piotr-uv \
XDG_CACHE_HOME=/tmp/piotr-cache \
ESPHOME_BUILD_PATH=/tmp/piotr-esphome-build \
.venv/bin/esphome config firmware/esphome/box3-satellite.yaml
```

Compile:

```bash
PLATFORMIO_CORE_DIR=/tmp/piotr-platformio \
UV_CACHE_DIR=/tmp/piotr-uv \
XDG_CACHE_HOME=/tmp/piotr-cache \
ESPHOME_BUILD_PATH=/tmp/piotr-esphome-build \
.venv/bin/esphome compile firmware/esphome/box3-satellite.yaml
```

Upload over USB, with escalated execution:

```bash
PLATFORMIO_CORE_DIR=/tmp/piotr-platformio \
UV_CACHE_DIR=/tmp/piotr-uv \
XDG_CACHE_HOME=/tmp/piotr-cache \
ESPHOME_BUILD_PATH=/tmp/piotr-esphome-build \
.venv/bin/esphome upload firmware/esphome/box3-satellite.yaml --device /dev/ttyACM0
```

Post-flash checks:

```bash
ls -l /dev/ttyACM0
ping -c 2 -W 3 192.168.0.180
```

## Verification Checklist

After compile and before flash, inspect generated source with `rg`. Verify the specific feature being changed is present in `main.cpp`.

For dual wake words and red touch button, check:

```bash
rg -n "ryszardzie_wake_word|okay_nabu_wake_word|red_touch_button|box_touchscreen|start_button_voice_assistant|EnableModelAction|VoiceAssistantStartAction|WakeWordModel\\(|Ryszardzie|Okay Nabu" /tmp/piotr-esphome-build/piotr-box3-01/src/main.cpp
```

Expected current firmware behavior:

- Both wake-word models have stable IDs and are explicitly enabled on boot:
  - `ryszardzie_wake_word`
  - `okay_nabu_wake_word`
- `Ryszardzie` uses the local model manifest at `wakeword/ryszardzie/model/ryszardzie.json`.
- `Okay Nabu` uses ESPHome built-in `okay_nabu`.
- GT911 touchscreen is configured on the existing I2C bus.
- Red circle below the display is GT911 button index `0`.
- A single tap on the red touch button starts `voice_assistant` with synthetic wake word `button`, unless muted or already running.
- A tap still stops `timer_ringing` when a timer is ringing.

## Firmware Editing Notes

- Put Box-specific changes in `firmware/esphome/packages/esp32-s3-box-3-ryszardzie.yaml` unless the top-level file is clearly the right owner.
- For wake-word model enabled-state issues, remember ESPHome persists model enabled/disabled state in flash. Explicit `micro_wake_word.enable_model` on boot avoids stale disabled state.
- For the red touch area, use ESPHome `gt911` binary sensor support, not a generic GPIO button. ESPHome documents ESP32-S3-BOX-3 red circle as GT911 button index `0`.
- Keep GPIO0 long press factory reset behavior intact.
- Treat GPIO3 strapping-pin warnings for GT911 interrupt as expected, but do not ignore new validation errors.

## Flashing Workflow

1. Explain the intended firmware change briefly.
2. Edit the YAML.
3. Run ESPHome `config`.
4. Run ESPHome `compile`.
5. Inspect generated `main.cpp` for the intended behavior.
6. Flash with escalated USB upload only after verification passes.
7. Check `/dev/ttyACM0` and Wi-Fi ping after reboot.
8. Append a timestamped journal entry to `notes/setting-up-esp-box.md`.
9. Final response should state what changed, validation, flash status, and what the user should test.
