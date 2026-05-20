# ESP32-S3-BOX-3 Satellite Plan

Goal: make each ESP32-S3-BOX-3 a Wi-Fi satellite for the Piotr server.

## Target Behavior

1. The Box listens locally for a wake word.
2. After wake detection, only the following spoken command is streamed over Wi-Fi.
3. The server processes the command and can send arbitrary audio back to the Box speaker.

## Recommended First Implementation

Use ESPHome firmware on the Box and run the voice pipeline on this computer.
This gives us a working satellite quickly because the current ESPHome S3-BOX-3
package already contains the correct mic, speaker, display, media player, and
micro wake word configuration.

The local template is:

```text
firmware/esphome/box3-satellite.yaml
```

Create a real secrets file next to it:

```bash
cp firmware/esphome/secrets.example.yaml firmware/esphome/secrets.yaml
```

Edit `secrets.yaml` with the 2.4 GHz Wi-Fi credentials and generated API/OTA
secrets.

## Custom Wake Word

ESPHome uses `micro_wake_word` for on-device wake detection. Built-in upstream
models include `okay_nabu`, `hey_mycroft`, and `hey_jarvis` in the current
S3-BOX-3 package.

For our own wake phrase, train or obtain a microWakeWord model. ESPHome accepts:

```yaml
micro_wake_word:
  models:
    - model: https://server.local/models/piotr_wake_word.json
```

The JSON manifest must point to the matching model files. Once the custom model
is ready, copy the upstream package locally and replace its `micro_wake_word`
model list, or maintain a local package that owns the whole voice section.

## Audio Back To The Satellite

The official S3-BOX-3 ESPHome package exposes a speaker-backed media player.
That is the path for arbitrary audio playback:

1. Server generates or selects an audio file.
2. Server exposes it via HTTP on the LAN.
3. Server asks the satellite media player to play that URL.

In a Home Assistant based setup this is `media_player.play_media`. In a custom
Piotr server, we either speak ESPHome's native API directly or replace this with
a small custom firmware protocol.

## Physical Connection Status

The device was not visible on USB when checked from this computer:

```bash
lsusb
ls -l /dev/ttyACM* /dev/ttyUSB*
```

For first flash, connect USB-C directly to the Box itself, not the blue dock.
Use a data-capable cable. If it still does not appear, hold the upper left BOOT
button and tap the lower left RESET button to enter flash mode.

After the initial firmware flash and Wi-Fi provisioning, the device should be
manageable over Wi-Fi with OTA updates.

## Scaling To Many Satellites

Use one YAML file per physical device:

```text
firmware/esphome/box3-kitchen.yaml
firmware/esphome/box3-office.yaml
firmware/esphome/box3-bedroom.yaml
```

Each satellite should have a unique `name`, friendly name, API key, OTA password,
and static DHCP reservation on the router if we want predictable addressing.

## Sources

- Home Assistant ESP32-S3-BOX voice assistant guide: https://www.home-assistant.io/voice_control/s3_box_voice_assistant/
- ESPHome voice assistant component: https://esphome.io/components/voice_assistant/
- ESPHome micro wake word component: https://esphome.io/components/micro_wake_word/
- ESPHome S3-BOX-3 package: https://github.com/esphome/wake-word-voice-assistants/blob/main/esp32-s3-box-3/esp32-s3-box-3.yaml
