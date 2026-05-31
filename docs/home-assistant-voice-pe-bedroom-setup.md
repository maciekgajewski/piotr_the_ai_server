# Home Assistant Voice PE Bedroom Setup

This setup turns a factory-fresh Home Assistant Voice Preview Edition into a Piotr network satellite for the bedroom.

## Firmware

Build and flash:

```bash
PLATFORMIO_CORE_DIR=/tmp/piotr-platformio \
UV_CACHE_DIR=/tmp/piotr-uv \
XDG_CACHE_HOME=/tmp/piotr-cache \
ESPHOME_BUILD_PATH=/tmp/piotr-esphome-build \
.venv/bin/esphome config firmware/esphome/voice-pe-bedroom.yaml
```

```bash
PLATFORMIO_CORE_DIR=/tmp/piotr-platformio \
UV_CACHE_DIR=/tmp/piotr-uv \
XDG_CACHE_HOME=/tmp/piotr-cache \
ESPHOME_BUILD_PATH=/tmp/piotr-esphome-build \
.venv/bin/esphome compile firmware/esphome/voice-pe-bedroom.yaml
```

```bash
PLATFORMIO_CORE_DIR=/tmp/piotr-platformio \
UV_CACHE_DIR=/tmp/piotr-uv \
XDG_CACHE_HOME=/tmp/piotr-cache \
ESPHOME_BUILD_PATH=/tmp/piotr-esphome-build \
.venv/bin/esphome upload firmware/esphome/voice-pe-bedroom.yaml --device /dev/ttyACM0
```

The expected mDNS name after flashing is:

```text
piotr-voice-pe-bedroom-01.local
```

## AI Server Config

Add this microphone device to the active AI server config:

```yaml
  - type: box3_esphome
    name: voice-pe-bedroom
    address: piotr-voice-pe-bedroom-01.local
    api_key: 7hzfuZW1JytUqcwP9Nrn53FfOW5zWmJj10p5CXU8ZDY=
    expected_name: piotr-voice-pe-bedroom-01
    area: bedroom
```

The driver type is currently named `box3_esphome`, but it speaks the generic ESPHome voice assistant API used by both the Box 3 and Voice PE satellites.

## First-Run Checks

After flashing and reboot:

```bash
ping -c 2 -W 3 piotr-voice-pe-bedroom-01.local
```

Restart the AI server after updating its config, then test:

- Say `Ryszardzie` and speak a short command.
- Say `Okay Nabu` and speak a short command.
- Press the center button and speak a short command.
- Confirm the wake, message-end, follow-up, and timeout cues match the Box 3 behavior.
