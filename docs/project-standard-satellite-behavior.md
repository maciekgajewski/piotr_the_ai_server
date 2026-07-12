# Project-Standard Satellite Behavior

## Document status

- **Authority:** Normative device behavior subordinate to the Microphone Protocol
- **Audience:** Agents changing shared satellite behavior, ESPHome packages, wake words, cues, or controls
- **Read when:** Changing behavior intended to remain consistent across satellite hardware

All Piotr voice satellites should expose the same behavior to the user and to the AI server unless hardware support makes a behavior impossible.

The abstract server-to-device contract, including visual state, is defined by the [Microphone Protocol](microphone-protocol.md). This document defines shared satellite policy and firmware realization without exposing firmware details through the abstract interface.

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

## Main Visual States

All satellites implement the four semantic states from the Microphone Protocol:

| State | Voice Preview | Box3 |
|---|---|---|
| `ERROR` | Low-light red LEDs | Error bitmap |
| `IDLE` | LEDs off | Idle bitmap |
| `LISTENING` | Pulsing blue LEDs | Listening bitmap |
| `PROCESSING` | Pulsing white LEDs | Processing bitmap |

While connected, the AI server explicitly controls `IDLE`, `LISTENING`, and `PROCESSING`. Firmware controls `ERROR` when disconnected. Voice-assistant callbacks must not infer or overwrite the connected main state. Mute, setup, timer, volume, and other local indicators may be shown orthogonally but must not replace the main visual.

## Firmware Control Services

ESPHome API service names and script IDs are implementation details behind each microphone driver. They are not part of the abstract Microphone Protocol and must not be invoked outside a concrete driver.

The shared firmware package may define common service names for Piotr satellite implementations. Stage 3 will replace the current implicit service contract with explicit commands for:

- starting and stopping each listening mode;
- setting `IDLE`, `LISTENING`, and `PROCESSING`;
- resetting a rejected open-mic wake candidate;
- playing semantic cues;
- acknowledging operation completion where required by the protocol.

Open-mic firmware keeps continuous capture active after a rejected candidate. The server commands `LISTENING` as soon as partial STT finds a wake candidate, then commands `IDLE` after final rejection or `PROCESSING` after final acceptance.

The shared service contract lives in `firmware/esphome/packages/piotr-voice-satellite-api-services.yaml`.
Hardware packages include that file and implement its script IDs. Keep hardware-specific behavior inside the hardware package. For example, Box3 maps visual commands to bitmaps, while Voice Preview maps them to LED animations.

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
- Home Assistant Voice Preview Edition, living room: `firmware/esphome/voice-pe-02.yaml`
- Home Assistant Voice Preview Edition, office: `firmware/esphome/voice-pe-03.yaml`
