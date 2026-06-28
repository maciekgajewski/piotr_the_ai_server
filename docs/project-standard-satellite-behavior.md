# Project-Standard Satellite Behavior

All Piotr voice satellites should expose the same behavior to the user and to the AI server unless hardware support makes a behavior impossible.

## Wake Words

Satellites use on-device wake-word detection with these enabled wake words:

- `Ryszardzie`, from `wakeword/ryszardzie/model/ryszardzie.json`
- `Okay Nabu`, from ESPHome/microWakeWord

Additional general-purpose wake words should not be enabled on Piotr satellites by default. Hardware-specific stop words may be enabled internally while a timer or long response is playing, but they are not conversation start wake words.

## Chimes

Satellites use the shared cue files in `firmware/esphome/sounds/`:

- Wake recognized: `wake_recognized.wav`
- Message end: `message_end.wav`
- Follow-up listening: `follow_up_listening.wav`
- Conversation timeout: `conversation_timeout.wav`

Hardware-specific system sounds, such as factory reset, mute switch, jack plug, and timer sounds, may stay hardware-specific.

## AI Server Control Services

ESPHome satellites must expose these API services when the hardware can play local sounds and start a voice assistant run:

- `play_message_end_cue`
- `play_conversation_timeout_cue`
- `start_follow_up_listening`
- `start_open_mic_listening`

The AI server uses these service names to keep follow-up conversations and end-of-message feedback consistent across satellite models.
The open-mic service is used only for microphones configured with `open_mic: true`; other microphones continue to use local wake-word mode.
In open-mic mode, satellites should not play a wake-recognized cue when the wake phrase is first detected in partial STT. The server plays `play_message_end_cue` only after end-of-speech and wake-phrase acceptance.

## Silence Calibration

The AI server owns utterance end detection for ESPHome satellites. Keep default timing consistent unless a room needs tuning:

- `initial_silence_seconds`: default `3.0`
- `end_silence_seconds`: default `0.9`
- `speech_peak_threshold`: default `500`
- `post_speech_ignore_seconds`: default `1.0`

Set `speech_peak_threshold` per microphone when a device has a different noise floor. Calibrate with:

```bash
tools/mic-silence-calibrate.sh --config /home/maciek/ai_server_config.yaml --microphone voice-pe-bedroom
```

Apply the recommended threshold only to the noisy microphone, not globally, unless all satellites are measured together.

Use `post_speech_ignore_seconds` when follow-up listening hears the satellite's own reply or room echo. The ignored window applies to follow-up streams and discards early audio instead of sending it to STT.

## Button Behavior

A physical push-to-talk or touch-to-talk control should start a normal voice assistant run using synthetic wake word `button`, unless the satellite is muted, already running, or handling a timer/alarm state.

## Current Satellite Firmware Entrypoints

- ESP32-S3-BOX-3: `firmware/esphome/box3-satellite.yaml`
- Home Assistant Voice Preview Edition, bedroom: `firmware/esphome/voice-pe-bedroom.yaml`
