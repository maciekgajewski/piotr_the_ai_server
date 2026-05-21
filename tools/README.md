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
.venv/bin/python tools/lib/box3_api_probe.py
```

## Capture Voice

Waits for an on-device wake word and writes microphone audio from the Box to a
WAV file in `audio/captures/`.

```bash
.venv/bin/python -u tools/lib/box3_capture_voice.py --seconds 4
```

For normal satellite use, keep Piotr subscribed so wake-word detection stays
active:

```bash
.venv/bin/python -u tools/lib/box3_capture_voice.py --continuous --seconds 4
```

By default the captured WAV is normalized after recording. Disable that with:

```bash
.venv/bin/python -u tools/lib/box3_capture_voice.py --seconds 4 --normalize-peak 0
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
.venv/bin/python tools/lib/box3_reset_voice.py
```

## Play Audio

Serves a local audio file from Piotr over temporary HTTP and asks the Box media
player to play it.

```bash
.venv/bin/python -u tools/lib/box3_play_audio.py audio/playback/timer_finished.flac
```

Set playback volume before playing:

```bash
.venv/bin/python -u tools/lib/box3_play_audio.py audio/playback/timer_finished.flac --volume 0.8
```

The upstream ESPHome package remaps API volume `0.0..1.0` into firmware volume
`0.5..0.8`. That means API volume `0.8` becomes effective firmware volume
`0.74`, while API volume `1.0` becomes `0.8`.

The Box currently reports FLAC playback support at 48 kHz, mono. Playback with
`audio/playback/timer_finished.flac` has been verified.

## Local Piper TTS

Reads text from standard input, renders it locally with Piper, converts it to
48 kHz mono FLAC, and plays it on the Box.

```bash
echo "Cześć, jestem Piotr." | tools/box3-tts.sh
```

List available Polish Piper voices:

```bash
tools/box3-tts.sh --list-voices
```

Use a specific voice:

```bash
echo "Cześć, jestem Piotr." | tools/box3-tts.sh --voice pl_PL-darkman-medium
```

The wrapper builds `piotr-box3-tts:latest` on first use, runs with Docker
`--gpus all`, and caches the Polish Piper voice under `.piper-cache/`.
By default the wrapper resolves the Box `.local` hostname on the host and passes
the resolved IP into Docker as `BOX3_HOST`.
The TTS tool logs generation time as `tts_generate_seconds` and ESPHome media
command send time as `tts_send_seconds`.

Run synthesis without playing on the Box:

```bash
echo "Test syntezy mowy." | tools/box3-tts.sh --self-test
```

Defaults:

- voice: `pl_PL-bass-high`
- volume: `1.0`
- generated audio: `audio/tts/`

## Local Whisper STT

Uses Docker for the local `faster-whisper` runtime. The wrappers build the STT
image on first use.

Build or rebuild the image manually:

```bash
docker build -f docker/stt.Dockerfile -t piotr-box3-stt:latest .
```

The container uses host networking for ESPHome/mDNS and mounts:

- `.hf-cache/` for Whisper model cache
- `firmware/` read-only for ESPHome API secrets
- `audio/` for optional saved audio
- `tools/lib/` read-only so Python implementation changes do not require image rebuilds

Self-test model loading and CUDA transcription runtime:

```bash
tools/box3-stt-self-test.sh --device cuda
```

List common model presets:

```bash
tools/box3-stt.sh --list-models
```

Wait for one wake-word utterance and print recognized text:

```bash
tools/box3-stt.sh --seconds 5
```

Keep listening and print one line per utterance:

```bash
tools/box3-stt-continuous.sh --seconds 5
```

Set `BOX3_STT_IMAGE` to override the image tag, or `BOX3_STT_GPUS=none` to run
without Docker GPU flags.
By default the wrapper resolves the Box `.local` hostname on the host and passes
the resolved IP into Docker as `BOX3_HOST`.

If `--device cuda` fails with `could not select device driver "" with
capabilities: [[gpu]]`, install/configure NVIDIA Container Toolkit on the host.
Until then, run CPU mode with:

```bash
BOX3_STT_GPUS=none tools/box3-stt-self-test.sh --device cpu --model tiny
```

Defaults:

- model: `base`
- language: `pl`
- device: `auto`
- CUDA compute type: `float16`
- CPU compute type: `int8`
