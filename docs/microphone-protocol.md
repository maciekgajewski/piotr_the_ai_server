# Microphone Protocol

## Status and scope

- **Authority:** Normative
- **Audience:** Agents changing microphone management, drivers, capture, playback, cues, visual output, or satellite firmware
- **Read when:** Working in `ai_server/microphones/`, microphone configuration, microphone tests, or satellite microphone firmware

This document defines the abstract protocol between `MicrophoneManager` and a microphone driver. It governs listening, captured speech segments, assistant playback, cues, visual state, correlation, failure, and recovery.

It does not govern Conversation lifecycle, STT internals, speaker recognition internals, or device-specific ESPHome services. Translation to Conversation events is defined by [Microphone-Conversation Mapping](microphone-conversation-mapping.md).

Requirement identifiers use the `MP-` prefix.

## Ownership boundaries

- `MicrophoneManager` MUST own desired listening mode, playback sequencing, cue sequencing, visual state while connected, retries, and recovery.
- A driver MUST implement commands only through the abstract `Microphone` interface.
- A driver MUST own device-specific connections, callbacks, audio transport, service names, bitmaps, and LED rendering.
- A driver MUST NOT create Conversation events, perform STT, decide whether open-mic text is accepted, or re-arm itself implicitly. (`MP-OWNER-001`)
- Other server components MUST NOT branch on a concrete driver type or invoke concrete firmware services.
- Firmware MAY implement connection-loss fail-safe behavior that cannot be commanded by a disconnected server.

## Terminology

- **Listening generation:** One explicitly armed interval identified by `listen_id`.
- **Speech segment:** One bounded sequence of captured speech identified by `utterance_id`.
- **Playback stream:** One assistant audio stream identified by `playback_id`.
- **Cue:** One short semantic sound identified by `cue_id`.
- **Main visual state:** The device-wide state controlled by this protocol while connected.
- **Orthogonal indicator:** A hardware-local indication, such as mute or timer status, that does not replace the main visual state.
- **Stale event:** An event whose correlation identifier is not active.

Names in event tables are protocol events. Uppercase names in state tables are protocol states or enum values as explicitly stated.

## Correlation identifiers

- Every `StartListening` command MUST contain a newly generated non-empty `listen_id`. (`MP-ID-001`)
- Every `SpeechStarted` MUST contain a newly generated non-empty `utterance_id` associated with the active `listen_id`. (`MP-ID-002`)
- Every playback stream MUST use a newly generated non-empty `playback_id`.
- Every cue MUST use a newly generated non-empty `cue_id`.
- Driver events MUST echo all applicable identifiers.
- An identifier MUST NOT be reused during the process lifetime.
- A stale event MUST NOT mutate current state. A trusted driver emitting a stale event has violated the protocol. (`MP-ID-003`)

Identifiers make delayed callbacks observable and prevent an old ESPHome run from changing a newer listening generation.

## Enum values

### Listening mode

| Value | Meaning |
|---|---|
| `WAKE_WORD` | Driver waits for local wake-word detection and captures one following speech segment |
| `OPEN_MIC` | Driver detects zero or more speech segments; manager performs server-side wake-phrase acceptance |
| `FOLLOW_UP` | Driver captures one speech segment without requiring a wake word |

### Main visual state

| Value | User meaning | Voice Preview | Box3 |
|---|---|---|---|
| `ERROR` | Device is not connected to the controlling server | Low-light red LEDs | Error bitmap |
| `IDLE` | Connected; no acknowledged user request is active | LEDs off | Idle bitmap |
| `LISTENING` | The system is actively accepting acknowledged user input | Pulsing blue LEDs | Listening bitmap |
| `PROCESSING` | An accepted request is being processed or played back | Pulsing white LEDs | Processing bitmap |

`ERROR` is a normative visual state but is not a valid argument to `SetVisualState`; it is firmware-owned while disconnected. (`MP-VISUAL-001`)

### Cue type

| Value | Meaning |
|---|---|
| `UTTERANCE_ACCEPTED` | Full utterance was accepted and will be processed |
| `FOLLOW_UP_READY` | Device is ready for follow-up speech |
| `FOLLOW_UP_TIMEOUT` | Follow-up input deadline expired |

## Manager-to-driver events

| Event | Fields | Valid use |
|---|---|---|
| `StartListening` | `listen_id`, `mode` | Arm one new listening generation while disarmed |
| `StopListening` | `listen_id`, `reason` | Stop the matching generation |
| `SetVisualState` | `state` | Set `IDLE`, `LISTENING`, or `PROCESSING` while connected |
| `ResetWakeCandidate` | `listen_id`, `utterance_id` | Clear device candidate UI after final rejection without stopping open-mic listening |
| `PlayCue` | `cue_id`, `cue_type` | Play one semantic cue while disarmed |
| `PlaybackBegin` | `playback_id`, `rate`, `width`, `channels`, optional `volume` | Begin one assistant playback stream while disarmed |
| `PlaybackChunk` | `playback_id`, `data` | Append bytes to the matching playback stream |
| `PlaybackEnd` | `playback_id` | Declare that no more bytes will be sent |
| `Close` | none | Permanently release the driver |

`SetVisualState` is independent of audio state and is valid in every connected, non-closed driver state. Duplicate commands are idempotent. (`MP-VISUAL-002`)

## Driver-to-manager events

| Event | Fields | Meaning |
|---|---|---|
| `ListeningStarted` | `listen_id`, `mode` | Device has entered the requested listening generation |
| `ListeningStopped` | `listen_id`, `reason` | Matching generation is no longer active |
| `SpeechStarted` | `listen_id`, `utterance_id`, `rate`, `width`, `channels`, optional `wake_word` | One captured segment has begun |
| `AudioChunk` | `listen_id`, `utterance_id`, `data` | Captured audio for the matching segment |
| `AudioProgress` | `listen_id`, `utterance_id`, `chunks`, `bytes` | Segment liveness without an audio payload |
| `SpeechEnded` | `listen_id`, `utterance_id`, `reason` | Matching captured segment is complete |
| `CueFinished` | `cue_id` | Matching cue has finished |
| `PlaybackFinished` | `playback_id` | Matching playback stream has drained and finished |
| `MicrophoneUnavailable` | operation correlation fields, `reason` | Current requested operation cannot continue temporarily |
| `DriverClosed` | none | Driver is permanently closed |

Capture events use `SpeechStarted` and `SpeechEnded`. Playback uses `PlaybackBegin`, `PlaybackChunk`, `PlaybackEnd`, and `PlaybackFinished`. The protocol MUST NOT overload `AudioStart` or `AudioEnd` across these boundaries. (`MP-EVENT-001`)

## Driver protocol states

### `DISARMED`

No listening, capture, cue, or playback operation is active.

### `ARMING`

The driver is executing `StartListening` and has not yet emitted `ListeningStarted`.

### `LISTENING`

The requested generation is active and no speech segment is currently open.

### `CAPTURING`

Exactly one speech segment is active.

### `STOPPING`

The driver is executing `StopListening` and has not yet emitted `ListeningStopped`.

### `PLAYING_CUE`

Exactly one cue is active.

### `PLAYING_AUDIO`

Exactly one assistant playback stream is active or draining.

### `CLOSED`

Terminal state.

## Driver transition table

The table covers audio-state events. `SetVisualState` is independently valid in every connected state except `CLOSED`.

| Current state | Command or driver result | Action | Next state |
|---|---|---|---|
| `DISARMED` | `StartListening` | Begin requested generation | `ARMING` |
| `DISARMED` | `PlayCue` | Begin semantic cue | `PLAYING_CUE` |
| `DISARMED` | `PlaybackBegin` | Open playback stream | `PLAYING_AUDIO` |
| `DISARMED` | `Close` | Release resources | `CLOSED` |
| `ARMING` | matching `ListeningStarted` | Record active generation | `LISTENING` |
| `ARMING` | matching `MicrophoneUnavailable` | Report failure | `DISARMED` |
| `ARMING` | matching `StopListening` | Cancel activation | `STOPPING` |
| `LISTENING` | `SpeechStarted` | Open one segment | `CAPTURING` |
| `LISTENING` | matching `StopListening` | Stop generation | `STOPPING` |
| `LISTENING` | matching `MicrophoneUnavailable` | Report failure | `DISARMED` |
| `CAPTURING` | matching `AudioChunk` | Forward audio | `CAPTURING` |
| `CAPTURING` | matching `AudioProgress` | Record liveness | `CAPTURING` |
| `CAPTURING` | matching `SpeechEnded` in `OPEN_MIC` | Close segment | `LISTENING` |
| `CAPTURING` | matching `SpeechEnded` in `WAKE_WORD` or `FOLLOW_UP` | Close segment and generation | `DISARMED` |
| `CAPTURING` | matching `MicrophoneUnavailable` | Abort segment and generation | `DISARMED` |
| `STOPPING` | matching `ListeningStopped` | Clear generation | `DISARMED` |
| `STOPPING` | matching `MicrophoneUnavailable` | Clear failed generation | `DISARMED` |
| `PLAYING_CUE` | matching `CueFinished` | Complete cue | `DISARMED` |
| `PLAYING_CUE` | matching `MicrophoneUnavailable` | Abort cue | `DISARMED` |
| `PLAYING_AUDIO` | matching `PlaybackChunk` | Append audio | `PLAYING_AUDIO` |
| `PLAYING_AUDIO` | matching `PlaybackEnd` | Finish input and drain device | `PLAYING_AUDIO` |
| `PLAYING_AUDIO` | matching `PlaybackFinished` after `PlaybackEnd` | Complete playback | `DISARMED` |
| `PLAYING_AUDIO` | matching `MicrophoneUnavailable` | Abort playback | `DISARMED` |
| any non-closed state | `Close` | Abort operation and release resources | `CLOSED` |
| `CLOSED` | any command or event | Inapplicable | `CLOSED` |

Any command or event not allowed by this table is an internal protocol violation. A nested `SpeechStarted`, mismatched identifier, audio outside `CAPTURING`, or implicit driver re-arm MUST fail an invariant. (`MP-STATE-001`)

## Listening-mode requirements

### Wake-word mode

- The driver MUST perform local wake-word detection.
- `SpeechStarted.wake_word` MUST identify the detected wake word.
- The segment MUST contain the post-wake utterance, not wake-word-only audio unless the device transport makes separation impossible and documents it.
- One completed or failed segment ends the listening generation.

### Follow-up mode

- The driver MUST capture the next speech segment without requiring a wake word.
- `SpeechStarted.wake_word` MUST be absent.
- One completed or failed segment ends the listening generation.

### Open-mic mode

- The driver MUST perform local speech gating but MUST NOT perform final wake-phrase acceptance.
- One listening generation MAY produce zero or more sequential speech segments.
- Ordinary silence while `LISTENING` is normal.
- Completing or rejecting one segment MUST NOT end the listening generation.
- The manager MUST explicitly stop the generation before cue or assistant playback.

## Main visual-state protocol

### Ownership and precedence

- When connected, the manager MUST explicitly command the main visual state. (`MP-VISUAL-003`)
- Drivers and firmware MUST NOT infer `IDLE`, `LISTENING`, or `PROCESSING` from listening, speech, cue, playback, or voice-assistant callbacks. (`MP-VISUAL-004`)
- Firmware MUST enter `ERROR` when its server-connection watchdog determines that the controlling server is unavailable.
- `ERROR` MUST override the last server-commanded state while disconnected.
- On reconnection, the device MUST retain `ERROR` until it receives the manager's first `SetVisualState`; the manager MUST send that command as part of connection initialization. (`MP-VISUAL-005`)
- Mute, setup, timer, volume, and other hardware-local indicators MAY be shown orthogonally, but MUST NOT replace the main connected visual state. (`MP-VISUAL-006`)
- This protocol defines no `MUTED`, `SPEAKING`, setup, or timer main visual state.

### Required transitions

| Situation | Required state |
|---|---|
| Connected and no accepted request active | `IDLE` |
| Wake-word segment begins | `LISTENING` |
| Follow-up is requested, including while its ready cue plays | `LISTENING` |
| First server-side open-mic partial detects a wake candidate | `LISTENING` immediately |
| Open-mic candidate is rejected by final transcript | `IDLE` |
| Complete utterance is accepted | `PROCESSING` |
| Agent is generating a response | `PROCESSING` |
| Assistant audio is playing | `PROCESSING` |
| Playback finishes and no follow-up is requested | `IDLE` |
| Playback finishes and follow-up is requested | `LISTENING` |
| Follow-up times out | `IDLE` after the timeout cue completes |
| Server connection is lost | Firmware-owned `ERROR` |

`PROCESSING` MUST remain active throughout assistant playback. (`MP-VISUAL-007`)

## Open-mic acceptance protocol

Open-mic acceptance is manager-owned and has the following ordered internal milestones, which are not driver protocol events:

```text
SpeechStarted
-> private audio and partial STT
-> optional internal WakeCandidateDetected
-> SpeechEnded
-> internal FinalTranscriptProduced
-> final wake-phrase validation
-> Accepted or Rejected
```

### Partial candidate

- Partial transcript snapshots MUST remain private to the manager/STT boundary.
- Partial transcript text MUST NOT be sent to an agent or content-logged.
- The first partial containing the configured wake phrase MUST mark the current `utterance_id` as a candidate and immediately send `SetVisualState(LISTENING)`. (`MP-OPENMIC-001`)
- Candidate visual feedback MUST occur while capture continues; it MUST NOT wait for `SpeechEnded` or final transcription.
- Duplicate positive partials for the same utterance MUST NOT repeat the transition or cue.
- Candidate detection MUST NOT play `UTTERANCE_ACCEPTED`.

### Final rejection

After `SpeechEnded`, the final transcript determines acceptance.

- If the final transcript lacks the wake phrase or usable non-empty text after it, the segment MUST be rejected.
- If a partial candidate had been displayed, rejection MUST send `ResetWakeCandidate` and `SetVisualState(IDLE)`. (`MP-OPENMIC-002`)
- Rejection MUST discard text and buffered speaker-recognition work.
- Rejection MUST NOT create a Conversation, play an accepted cue, or stop the open-mic listening generation.

### Final acceptance

- Acceptance MUST occur only after final transcript production, wake-phrase validation, and usable suffix extraction. (`MP-OPENMIC-003`)
- Acceptance MUST send `SetVisualState(PROCESSING)`.
- The manager MUST stop the open-mic generation and receive `ListeningStopped` before playing `UTTERANCE_ACCEPTED` or assistant audio.
- Speaker recognition MAY use buffered audio but MUST be awaited only after acceptance.
- One accepted segment MUST be forwarded exactly once to the mapping layer.

## Cue and playback sequencing

- A half-duplex driver MUST be `DISARMED` before `PlayCue` or `PlaybackBegin`. (`MP-AUDIO-001`)
- The manager MUST await `CueFinished` before starting a conflicting listening or playback operation.
- `PlaybackEnd` means no more bytes will be sent; `PlaybackFinished` means the device has drained the audio and playback is complete.
- The manager MUST NOT re-arm listening before `PlaybackFinished`.
- Visual `PROCESSING` remains active while assistant playback drains.
- A driver MAY advertise full-duplex capability in a future interface revision, but no current behavior may assume it.

## Timeouts and cancellation

| Timeout | Starts | Successful completion | Failure meaning | Owner |
|---|---|---|---|---|
| Arm acknowledgement | `StartListening` | `ListeningStarted` | Device did not enter requested mode | Manager |
| Follow-up speech start | follow-up `ListeningStarted` | `SpeechStarted` | User did not begin follow-up | Mapping/manager using Session deadline |
| Segment liveness | `SpeechStarted` or last segment event | next `AudioChunk`, `AudioProgress`, or `SpeechEnded` | Active capture stalled | Manager |
| Cue completion | `PlayCue` | `CueFinished` | Cue operation stalled | Manager |
| Playback completion | `PlaybackBegin` or last playback activity | `PlaybackFinished` | Playback stalled | Manager |
| Connection watchdog | last valid server connectivity evidence | server connectivity restored | Server unavailable | Firmware |

- Idle open-mic `LISTENING` has no speech-segment liveness timeout. (`MP-TIMEOUT-001`)
- A timeout MUST name the active operation and correlation identifier in logs.
- Timeout recovery MUST stop or discard the failed generation before creating another identifier.

## Failure and recovery

- A failed capture MUST NOT be converted into an empty accepted utterance.
- Recovery MUST NOT reuse a `listen_id`, `utterance_id`, `cue_id`, or `playback_id`.
- The manager MUST explicitly select the next visual state after a recoverable failure while connected.
- Open-mic rejection is a normal outcome, not device unavailability.
- Follow-up timeout is a normal Conversation outcome, not device unavailability.
- Connection loss activates firmware-owned `ERROR`; server reconnection begins with an explicit visual-state command.
- If an internal sequence violates state or correlation invariants, the manager MUST close and recreate the driver/session boundary rather than guessing recovery. (`MP-ERROR-001`)

## Normal sequences

### Wake-word capture

```text
SetVisualState(IDLE)
StartListening(listen-1, WAKE_WORD)
ListeningStarted(listen-1, WAKE_WORD)
SpeechStarted(listen-1, utterance-1, wake_word="Ryszardzie")
SetVisualState(LISTENING)
AudioChunk(...)*
SpeechEnded(listen-1, utterance-1)
final transcript accepted
SetVisualState(PROCESSING)
PlayCue(UTTERANCE_ACCEPTED)
CueFinished
```

### Open-mic rejection after an early candidate

```text
SetVisualState(IDLE)
StartListening(listen-2, OPEN_MIC)
ListeningStarted(listen-2, OPEN_MIC)
SpeechStarted(listen-2, utterance-2)
AudioChunk(...)*
partial STT detects wake candidate
SetVisualState(LISTENING)
AudioChunk(...)*
SpeechEnded(listen-2, utterance-2)
final transcript rejects candidate
ResetWakeCandidate(listen-2, utterance-2)
SetVisualState(IDLE)
```

### Accepted response and playback

```text
accepted utterance
SetVisualState(PROCESSING)
StopListening(active-listen-id, "utterance_accepted") if still armed
ListeningStopped(active-listen-id, "utterance_accepted")
PlayCue(UTTERANCE_ACCEPTED)
CueFinished
PlaybackBegin(playback-1, ...)
PlaybackChunk(playback-1, ...)*
PlaybackEnd(playback-1)
PlaybackFinished(playback-1)
SetVisualState(IDLE) or SetVisualState(LISTENING) for follow-up
```

## Invalid sequences

- `AudioChunk` before `SpeechStarted`;
- a second `SpeechStarted` while capturing;
- event identifiers that do not match the active operation;
- `PlaybackBegin` while listening is active on a half-duplex driver;
- implicit re-arm after capture or playback;
- treating open-mic idle silence as an active segment timeout;
- showing `LISTENING` for ordinary open-mic speech before a wake candidate exists;
- waiting until final transcription to show a detected candidate;
- accepting before final transcript validation;
- switching from `PROCESSING` to `IDLE` when playback begins;
- inferring the connected main visual from firmware voice-assistant callbacks;
- using `ERROR` to represent mute or a recoverable processing failure.

## Observability requirements

- Every command and driver event MUST be logged on DEBUG with microphone instance, protocol state, and all correlation identifiers. (`MP-OBS-001`)
- Listening, accepted utterance, cue, playback, unavailability, reconnection, and recovery are crucial events and MUST be logged briefly on INFO.
- Every main visual transition MUST be logged on DEBUG with old state, new state, and cause. (`MP-OBS-002`)
- Open-mic partial and rejected final transcript text MUST NOT be content-logged.
- Accepted transcript text follows the project's transcript logging policy and MUST be logged only after acceptance.
- Stale events and protocol violations MUST be logged with enough context to identify the old and active generations.

## Implementation and conformance references

Stage 3 implementation targets:

- `ai_server/microphones/messages.py`
- `ai_server/microphones/interfaces.py`
- `ai_server/microphones/types.py`
- `ai_server/microphones/manager.py`
- `ai_server/microphones/drivers/box3_esphome.py`
- shared ESPHome satellite packages

Conformance targets:

- `tests/test_microphones.py`
- `tests/test_mic_protocol_test.py`
- `tests/test_box3_esphome_microphone.py`
- reusable driver conformance tests
- [Protocol Conformance Catalogue](protocol-conformance-catalogue.md)

## Compatibility policy

This protocol intentionally replaces the current overloaded audio events and implicit firmware state coupling. Stage 3 MUST migrate all in-repository drivers, managers, tests, and firmware together. Compatibility aliases are not required.

## Unresolved decisions

None. New listening modes, main visual states, full-duplex behavior, or device-inferred visual transitions require a normative protocol change before implementation.
