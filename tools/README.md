# ESP32-S3-BOX-3 Tools

Small Piotr-side utilities for the ESPHome-flashed Box satellite.

Current device:

```text
host: piotr-box3-01-cbfaA8.local
reported name: piotr-box3-01-cbfaa8
api port: 6053
```

Secrets are read from `firmware/esphome/secrets.yaml`.

## Ollama Cloud Model Setup

Signs in the Docker Compose `ollama` service, pulls an Ollama cloud model, and
smoke-tests the local API endpoint used by the AI server.

```bash
tools/ollama-cloud-setup-test.sh --services-config config/services.env
```

The default model is `gpt-oss:20b-cloud`. Override it with:

```bash
tools/ollama-cloud-setup-test.sh --services-config config/services.env --model MODEL-NAME-CLOUD
```

See [../docs/ollama-cloud-models.md](../docs/ollama-cloud-models.md).

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

Current wake word from the ESPHome package:

- Ryszardzie

Capture output is 16 kHz, mono, 16-bit PCM WAV. The default normalization
target is peak `0.89`.

If a failed capture leaves the Box in listening mode, reset the voice-assistant
state:

```bash
.venv/bin/python tools/lib/box3_reset_voice.py
```

## Capture Voice Learning Samples

Captures voice samples through a configured microphone for speaker-recognition
enrollment. The tool prints all behavior-test phrases as prompts, starts the
microphone in follow-up listening mode, removes silent audio using the selected
microphone's `speech_peak_threshold`, and writes 5 second WAV samples.

```bash
tools/capture-voice-samples.sh --config /path/to/ai-server.yaml --mic office --out audio/voice-samples/maciek
```

Press Ctrl-C to stop. On startup, the tool scans the output directory, reports
the existing usable voice seconds, and continues at the next free numbered WAV
file name.

## Build Speaker Profile

Builds a SpeechBrain ECAPA-TDNN speaker profile from a directory of captured
voice samples. SpeechBrain runs inside the `speaker-recognition` Docker image;
the output directory receives `speaker_profile.npz`, `metadata.json`, and
`manifest.json`.

```bash
tools/speaker-profile-build.sh audio/voice-samples/maciek audio/speaker-profiles/maciek
```

Rebuild an existing profile:

```bash
tools/speaker-profile-build.sh audio/voice-samples/maciek audio/speaker-profiles/maciek --overwrite
```

The same image also provides the HTTP recognition service:

```bash
docker compose -f docker-compose.speaker-recognition.yml up speaker-recognition
```

The service listens on port `2140` by default. The AI server sends configured
`users.<name>.voice_profile` paths with each streamed utterance, so the profile
directory must be mounted into the speaker-recognition container. On this host,
`config/services.env` mounts `/home/maciek/user_voice_data` read-only at the
same path.

## Record Wake-Word Training Samples

Records positive training samples through the Box microphone without relying on
the current on-device wake-word model.

```bash
tools/box3-record-wakeword-samples.sh --count 20
```

The tool temporarily switches the Box wake-word engine to `In Home Assistant`,
waits for the Box to stream microphone audio, prompts before each sample, and
restores `On device` mode when done.

Defaults:

- phrase: `Ryszardzie`
- sample length: `1.5s`
- ready delay after pressing Enter: `0.4s`
- normalization target: peak `0.89`
- output directory: `audio/training-samples/ryszardzie/positive/`
- file names: `0001.wav`, `0002.wav`, ...

Tune the capture window:

```bash
tools/box3-record-wakeword-samples.sh --count 50 --seconds 1.8
```

Disable normalization when raw microphone levels are needed:

```bash
tools/box3-record-wakeword-samples.sh --normalize-peak 0
```

## Test Wake-Word Model Locally

Captures short Box microphone clips and runs the local `Ryszardzie` TFLite
model without flashing firmware.

```bash
tools/box3-wakeword-test.sh --count 5
```

The tool prompts before each test capture, records for `1.5s` by default, and
prints the model prediction. Test captures are temporary by default.

Keep test captures for inspection:

```bash
tools/box3-wakeword-test.sh --count 5 --output-dir audio/wakeword-tests/ryszardzie/
```

Run the predictor directly against an existing WAV file:

```bash
tools/box3-wakeword-predict-file.sh audio/training-samples/ryszardzie/positive/0001.wav
```

## Play Audio

Serves a local audio file from Piotr over temporary HTTP and asks the Box media
player to play it.

```bash
tools/box3-play-audio.sh audio/playback/timer_finished.flac
```

Set playback volume before playing:

```bash
tools/box3-play-audio.sh audio/playback/timer_finished.flac --volume 0.8
```

Review a recorded wake-word training sample:

```bash
tools/box3-play-audio.sh audio/training-samples/ryszardzie/positive/0001.wav
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

By default, `tools/box3-tts.sh` autostarts a named Wyoming Piper server
container before synthesis:

```text
piotr-box3-tts-server
```

Manage it directly:

```bash
tools/box3-tts-server-start.sh
tools/box3-tts-server-status.sh
tools/box3-tts-server-logs.sh
tools/box3-tts-server-stop.sh
```

Use the old per-command Piper CLI path explicitly:

```bash
echo "Test syntezy mowy." | tools/box3-tts.sh --engine cli
```

The server runs Wyoming Piper with CUDA enabled through `onnxruntime-gpu`.
The first request after server restart may pay CUDA/model warmup cost; subsequent
requests should be much faster.

Run synthesis without playing on the Box:

```bash
echo "Test syntezy mowy." | tools/box3-tts.sh --self-test
```

Experimental streaming mode asks the Box to play an HTTP WAV stream while the
Wyoming server is still producing audio chunks:

```bash
echo "Test strumieniowania mowy." | tools/box3-tts.sh --stream
```

Streaming mode logs:

- `tts_send_seconds`: time to send the ESPHome media command
- `tts_first_audio_seconds`: time from Box HTTP request to first Wyoming audio chunk
- `tts_first_byte_sent_seconds`: time from Box HTTP request to first HTTP audio bytes
- `tts_stream_seconds`: total time spent writing the HTTP stream
- `tts_stream_bytes`: number of WAV bytes written to the HTTP response

Use the older Piper CLI streaming comparison path with:

```bash
echo "Test strumieniowania mowy." | tools/box3-tts.sh --stream --engine cli
```

Defaults:

- voice: `pl_PL-bass-high`
- engine: `wyoming`
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
