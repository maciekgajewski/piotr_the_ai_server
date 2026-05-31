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

The AI server uses these service names to keep follow-up conversations and end-of-message feedback consistent across satellite models.

## Button Behavior

A physical push-to-talk or touch-to-talk control should start a normal voice assistant run using synthetic wake word `button`, unless the satellite is muted, already running, or handling a timer/alarm state.

## Current Satellite Firmware Entrypoints

- ESP32-S3-BOX-3: `firmware/esphome/box3-satellite.yaml`
- Home Assistant Voice Preview Edition, bedroom: `firmware/esphome/voice-pe-bedroom.yaml`
