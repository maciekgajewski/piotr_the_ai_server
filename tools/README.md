# ESP32-S3-BOX-3 Tools

Small Piotr-side utilities for the ESPHome-flashed Box satellite.

Current device:

```text
host: piotr-box3-01-cbfaA8.local
reported name: piotr-box3-01-cbfaa8
api port: 6053
```

Secrets are read from `firmware/esphome/secrets.yaml`.

## Probe API

Checks that Piotr can connect to the Box over the encrypted ESPHome native API
and prints device info plus exposed entities.

```bash
.venv/bin/python tools/box3_api_probe.py
```

## Capture Voice

Waits for an on-device wake word and writes microphone audio from the Box to a
WAV file in `audio/captures/`.

```bash
.venv/bin/python -u tools/box3_capture_voice.py --seconds 4
```

For normal satellite use, keep Piotr subscribed so wake-word detection stays
active:

```bash
.venv/bin/python -u tools/box3_capture_voice.py --continuous --seconds 4
```

By default the captured WAV is normalized after recording. Disable that with:

```bash
.venv/bin/python -u tools/box3_capture_voice.py --seconds 4 --normalize-peak 0
```

Current wake words from the ESPHome package:

- Okay Nabu
- Hey Mycroft
- Hey Jarvis

Capture output is 16 kHz, mono, 16-bit PCM WAV. The default normalization
target is peak `0.89`.

If a failed capture leaves the Box in listening mode, reset the voice-assistant
state:

```bash
.venv/bin/python tools/box3_reset_voice.py
```

## Play Audio

Serves a local audio file from Piotr over temporary HTTP and asks the Box media
player to play it.

```bash
.venv/bin/python -u tools/box3_play_audio.py audio/playback/timer_finished.flac
```

Set playback volume before playing:

```bash
.venv/bin/python -u tools/box3_play_audio.py audio/playback/timer_finished.flac --volume 0.8
```

The upstream ESPHome package remaps API volume `0.0..1.0` into firmware volume
`0.5..0.8`. That means API volume `0.8` becomes effective firmware volume
`0.74`, while API volume `1.0` becomes `0.8`.

The Box currently reports FLAC playback support at 48 kHz, mono. Playback with
`audio/playback/timer_finished.flac` has been verified.
