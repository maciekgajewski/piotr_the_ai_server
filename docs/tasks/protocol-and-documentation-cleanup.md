# Protocol and Documentation Cleanup

## Status

- **Authority:** Active implementation plan
- **Audience:** Agents and maintainers executing or reviewing the protocol cleanup
- **Read when:** Planning or performing documentation, protocol, session, microphone, websocket, or satellite state changes covered by this migration

This is a design-first migration: the protocol documents will become normative specifications, and the implementation will then be changed to conform to them. Current runtime behavior is evidence to inspect, not the authority for the new design.

The work is divided into three stages:

1. Make project documentation visible and useful to agents.
2. Define coherent, valid, normative protocols and interfaces.
3. Replace the implementation with protocol-conforming code and firmware.

Stage 3 must not begin until the normative documents produced by Stage 2 have been reviewed and approved.

## Progress

- **Stage 1:** Completed on 2026-07-12. Added the documentation index, agent routing, authority labels, and the protocol documentation standard.
- **Stage 2:** Not started.
- **Stage 3:** Not started and blocked on Stage 2 approval.

## Design assumptions

- Backward compatibility is not required unless explicitly introduced as a later decision.
- The Conversation Protocol and Microphone Protocol are separate contracts joined by a small normative mapping document.
- `Session` exclusively owns session and conversation lifecycle.
- `MicrophoneManager` owns desired microphone behavior. Concrete drivers own device-specific execution only.
- STT remains behind its own interface and is not part of the microphone driver protocol.
- Wake-word detection is driver-owned in wake-word mode. Wake-phrase acceptance is manager-owned in open-mic mode.
- Speaker recognition is awaited only for accepted utterances.
- A microphone is half-duplex unless its interface explicitly advertises otherwise.
- Device-specific ESPHome services, bitmaps, and LED implementations remain private to drivers and firmware.
- Protocol violations are not silently repaired. Invalid internal behavior fails an invariant; invalid external behavior produces a protocol rejection and closes the endpoint when possible.

## Stage 1: Documentation form, visibility, and agent usability

### Objective

Ensure that an agent touching sessions, websocket communication, microphones, STT integration, or satellite firmware encounters the applicable normative documentation before planning or editing.

### 1.1 Create a documentation index

Add `docs/README.md` with a compact catalogue containing, for every document:

- title and link;
- authority: normative, operational, plan, or historical;
- intended audience;
- when an agent must read it;
- implementation modules it governs;
- relevant tests;
- related documents.

At minimum, catalogue:

- Conversation Protocol;
- Microphone Protocol;
- Microphone-Conversation Mapping;
- Orchestrator and DSA Architecture;
- project-standard satellite behavior;
- setup and operational documents;
- historical notes.

### 1.2 Route agents from `AGENTS.md`

Add mandatory routing rules:

- Before changing `ai_server/messages.py`, `ai_server/interfaces.py`, `ai_server/sessions.py`, websocket clients, or the websocket server, read the Conversation Protocol.
- Before changing `ai_server/microphones/`, microphone configuration, or satellite microphone firmware, read the Microphone Protocol.
- Before changing microphone-to-session behavior, read both protocols and the Microphone-Conversation Mapping.
- Protocol documents are normative. Implementation drift is a defect.
- A protocol change must update its documentation and conformance tests in the same change.
- Drivers must remain sealed behind the microphone interface.
- Concrete service names and device-specific visual rendering must stay inside drivers and firmware.

### 1.3 Improve README navigation

Replace the generic description of `docs/` with a direct link to `docs/README.md`. Keep the root README concise and use it to route agents rather than duplicate protocol content.

### 1.4 Standardize normative protocol documents

Use the same structure for every protocol:

1. Status and scope
2. Ownership boundaries
3. Terminology
4. Typed event inventory, grouped by direction
5. State inventory
6. Transition table
7. Invariants
8. Timeouts and cancellation
9. Failure and recovery
10. Normal sequences
11. Invalid sequences
12. Observability requirements
13. Implementation and test references
14. Compatibility policy
15. Explicitly unresolved decisions

Use `MUST`, `MUST NOT`, `SHOULD`, and `MAY` consistently. Clearly identify every named item as a protocol event, protocol state, internal implementation state, or illustrative sequence step.

### 1.5 Classify existing documentation

Review all project Markdown files and label their authority and status.

- Keep `notes/setting-up-esp-box.md` explicitly historical and non-normative.
- Mark obsolete plans as historical or update their status.
- Supersede `docs/open-mic-protocol.md`, or convert it into a clearly scoped normative extension of the new Microphone Protocol.
- Prevent setup notes and historical experiments from appearing authoritative.

### Stage 1 acceptance criteria

- An agent starting from `AGENTS.md` can find the applicable protocol without repository search.
- Every normative document is directly indexed.
- Every document declares its authority and audience.
- Normative protocols use a consistent, predictable structure.
- Historical notes cannot reasonably be mistaken for current requirements.
- All local Markdown links resolve.
- No runtime behavior changes during this stage.

## Stage 2: Normative protocol and interface design

### Objective

Define the desired architecture independently of the current buggy implementation. Complete and approve these specifications before implementing them.

## 2.1 Conversation Protocol

Rewrite `docs/ai-server-conversation-protocol.md` as the authoritative Session contract.

### Ownership

- `Session` exclusively owns session and conversation lifecycle.
- `ConversationEndpoint` exposes only active-conversation message behavior.
- Agents cannot create conversations or control input adapters.
- Websocket and microphone adapters translate their transports into Conversation Protocol events.

### Attributes

- `medium` is mandatory and immutable for the Session.
- Valid values are `text` and `voice`.
- `user` and `area` are optional non-empty strings.
- Session defaults and conversation override rules are explicit.
- Examples must satisfy all attribute invariants, including `"medium":"text"` in websocket handshakes.

### Canonical events

Endpoint to Session:

- `SessionAttributes`
- `NewConversation`
- `MessageBegin`
- `MessageFragment`
- `MessageEnd`
- `ConversationEnded`
- endpoint closure

Session to endpoint:

- `ReadyForConversation`
- `FollowUpRequested`
- `ProcessingUpdate`
- `MessageBegin`
- `MessageFragment`
- `MessageEnd`
- `ConversationEnded`
- `SessionRejected`

Remove legacy `WaitForNewMessage`. Replace ambiguous direction-neutral waiting names with direction-specific names. Add `message_id` to every message-stream event so stale or interleaved fragments cannot silently corrupt streams.

### Canonical states

- `HANDSHAKE`
- `IDLE`
- `AWAITING_USER_MESSAGE`
- `RECEIVING_USER_MESSAGE`
- `AGENT_ACTIVE`
- `AWAITING_FOLLOW_UP`
- `ENDING_CONVERSATION`
- `CLOSED`

Provide a complete state/event matrix. Every event in every state must be classified as a valid transition, a valid event without transition, or a protocol violation.

### Required invariants

- A Session has zero or one active Conversation.
- A Conversation has zero or one open user message and zero or one open assistant message.
- Message IDs are unique within a Conversation.
- Exactly one party owns the conversational floor.
- A follow-up can only be requested after a complete user message and while no assistant message is open.
- At most one follow-up request is outstanding.
- Conversation-scoped state is never reused by another Conversation.
- `ProcessingUpdate` is forbidden inside an assistant message stream.
- External protocol violations produce `SessionRejected` before protocol-error closure when transport permits it.

### Termination and serialization

Specify normal agent return, explicit user termination, follow-up timeout, endpoint closure, invariant failure, and malformed external events. Define one cleanup path that closes streams, destroys conversation state, emits `ConversationEnded` when possible, and returns to `IDLE`.

The websocket JSON appendix must cover every canonical event, all required fields, validation rules, rejection behavior, processing updates, follow-up, and complete valid examples.

## 2.2 Microphone Protocol

Create `docs/microphone-protocol.md` as the normative `MicrophoneManager` to driver contract.

### Correlation identifiers

- Every listening generation has a unique `listen_id`.
- Every captured segment has a unique `utterance_id` associated with one `listen_id`.
- Every playback stream has a unique `playback_id`.
- Drivers echo applicable identifiers in all resulting events.
- Events with inactive identifiers are stale and must not mutate current state.
- Re-arming always creates a new `listen_id`.

### Listening modes

- `WAKE_WORD`: wait for local wake detection and capture one utterance.
- `OPEN_MIC`: continuously detect zero or more speech segments; manager performs wake-phrase acceptance.
- `FOLLOW_UP`: capture one speech segment without a wake word.

### Manager-to-driver events

- `StartListening(listen_id, mode)`
- `StopListening(listen_id, reason)`
- `SetVisualState(state)`
- `ResetWakeCandidate(listen_id, utterance_id)`
- `PlayCue(cue_id, cue_type)`
- `PlaybackBegin(playback_id, format, volume)`
- `PlaybackChunk(playback_id, data)`
- `PlaybackEnd(playback_id)`
- `Close`

### Driver-to-manager events

- `ListeningStarted(listen_id, mode)`
- `ListeningStopped(listen_id, reason)`
- `SpeechStarted(listen_id, utterance_id, format, wake_phrase)`
- `AudioChunk(listen_id, utterance_id, data)`
- `AudioProgress(listen_id, utterance_id, chunks, bytes)`
- `SpeechEnded(listen_id, utterance_id, reason)`
- `MicrophoneUnavailable(listen_id, reason)`
- `DriverClosed`

Do not overload `AudioStart` and `AudioEnd` across capture, readiness, speech segmentation, and playback.

### Driver states

- `DISARMED`
- `ARMING`
- `LISTENING`
- `CAPTURING`
- `PLAYING`
- `CLOSED`

Specify legal commands, emitted events, and transitions in every state. Nested speech starts, mismatched identifiers, audio outside a segment, and implicit driver re-arming are protocol violations.

In `WAKE_WORD` and `FOLLOW_UP`, completing one segment disarms the driver. In `OPEN_MIC`, completing or rejecting a segment returns to `LISTENING` under the same `listen_id` until explicitly stopped.

### User-visible visual states

The protocol defines four semantic visual states:

| State | Voice Preview | Box3 |
|---|---|---|
| `ERROR` | Low-light red LEDs | Error bitmap |
| `IDLE` | LEDs off | Idle bitmap |
| `LISTENING` | Pulsing blue LEDs | Listening bitmap |
| `PROCESSING` | Pulsing white LEDs | Processing bitmap |

Ownership rules:

- While connected, the manager explicitly sends `SetVisualState(IDLE | LISTENING | PROCESSING)`.
- Drivers and firmware MUST NOT infer these states from listening, capture, playback, cue, or voice-assistant events.
- `ERROR` is a firmware-owned fail-safe because the server cannot command a disconnected device.
- On reconnection, the first server visual command replaces `ERROR`.
- Duplicate visual commands are idempotent.
- Rendering details stay private to firmware.
- A visual precedence table must define how disconnected state, mute, setup, timers, and server-controlled states interact. Disconnected `ERROR` must override server state.

### Visual transitions

Wake-word mode:

```text
connected -> IDLE
wake word accepted and capture begins -> LISTENING
utterance accepted and capture ends -> PROCESSING
assistant playback or conversation ends -> IDLE
```

Open-mic mode:

```text
open-mic armed -> IDLE
ordinary speech segment -> remain IDLE
partial STT detects wake candidate -> LISTENING immediately
candidate rejected by final transcript -> IDLE
candidate accepted by final transcript -> PROCESSING
assistant playback or conversation ends -> IDLE
```

Follow-up mode:

```text
follow-up requested -> LISTENING
follow-up accepted -> PROCESSING
follow-up timeout -> IDLE
```

### Open-mic acceptance

The manager owns open-mic acceptance:

```text
SpeechStarted
-> private audio and partial STT
-> optional WakeCandidateDetected
-> SpeechEnded
-> FinalTranscriptProduced
-> final wake-phrase validation
-> Accepted or Rejected
```

Requirements:

- The first partial wake-candidate transition immediately sends `SetVisualState(LISTENING)` while the user is still speaking.
- Duplicate partial candidates do not repeat the visual transition.
- Candidate detection does not play an accepted-utterance cue.
- Partial transcript text is never forwarded or content-logged.
- Rejection sends `ResetWakeCandidate`, returns the display to `IDLE`, and keeps the same open-mic listening generation active.
- Acceptance occurs only after final transcription and usable suffix validation.
- Acceptance moves the display to `PROCESSING`, plays the accepted cue, optionally performs speaker recognition, and emits one captured utterance to the mapping layer.
- A rejected or empty segment never creates a Conversation.

### Timeout taxonomy

Define separate owners and behavior for:

- arm acknowledgement timeout;
- follow-up speech-start timeout;
- active-segment liveness timeout;
- playback timeout;
- device connection watchdog.

Open-mic silence in `LISTENING` is normal. It is not an active-segment stall and must not require artificial audio progress to keep the listening generation alive.

### Recovery

- Recovery explicitly stops the old generation and never reuses its identifiers.
- A failed capture never becomes an empty Conversation.
- A follow-up timeout ends the active Conversation.
- Open-mic rejection does not end the listening generation.
- A driver never re-arms itself implicitly.
- Recoverable failures while connected return to an explicitly selected server state; connection loss activates firmware-owned `ERROR`.

## 2.3 Microphone-Conversation Mapping

Create `docs/microphone-conversation-mapping.md` to define the adapter between the two protocols.

An accepted new-conversation utterance maps to exactly:

```text
NewConversation(attributes)
MessageBegin(message_id)
MessageFragment(message_id, final_text)
MessageEnd(message_id)
```

Specify separately:

- wake-word conversation start;
- accepted open-mic conversation start;
- rejected open-mic candidate;
- wake detection with no usable transcript;
- follow-up message;
- follow-up timeout;
- processing update;
- assistant playback;
- conversation termination and re-arming.

Required cross-protocol invariants:

- One accepted microphone utterance produces exactly one user message.
- No Conversation exists for rejected open-mic speech or empty input.
- No driver creates Conversation events.
- No Session knows which concrete microphone driver is active.
- Re-arming is triggered only by an explicit adapter transition.
- Assistant playback must finish or fail before listening is re-armed.
- Stale driver events cannot affect a newer listening generation or Conversation.
- The adapter uses the follow-up deadline supplied by Session; it does not invent a second timeout.

## 2.4 Conformance catalogue

Derive tests before implementation and create a traceable checklist:

```text
requirement ID -> protocol section -> implementation owner -> conformance test
```

Use stable identifiers such as:

- `CP-SESSION-001`
- `CP-MESSAGE-001`
- `MP-LISTEN-001`
- `MP-VISUAL-001`
- `MP-OPENMIC-001`
- `MAP-FOLLOWUP-001`

### Stage 2 acceptance criteria

- Every real event has exactly one definition and direction.
- Conceptual states are never presented as typed events.
- Every state has a complete valid-event table.
- Every timeout has one owner and one meaning.
- Every re-arm and recovery path is explicit.
- Every visual transition has an owner.
- Open-mic candidate detection changes the visual state before final transcription.
- Final transcription precedes acceptance.
- Examples obey all invariants.
- Requirements are traceable to planned tests.
- No unresolved contradiction is silently deferred to implementation.
- The normative documents are approved before Stage 3.

## Stage 3: Protocol-conforming implementation

### Objective

Replace current accidental and buggy behavior with code and firmware that conform to the approved specifications. Do not preserve legacy behavior unless the approved protocols explicitly require it.

## 3.1 Conversation event types and interfaces

Primary files:

- `ai_server/messages.py`
- `ai_server/interfaces.py`
- `tests/test_messages.py`
- `tests/test_interfaces.py`

Work:

- introduce canonical events and enums;
- add required identifiers and fields;
- remove legacy events;
- update directional type aliases;
- validate identifiers and attributes;
- update JSON parsing and serialization;
- omit `None` fields from internal JSON-like dictionaries;
- add exhaustive serialization and validation tests.

## 3.2 Explicit Session state machine

Primary files:

- `ai_server/sessions.py`
- focused Session tests

Work:

- represent Session state explicitly;
- centralize transition validation and floor ownership;
- validate message IDs;
- use one conversation cleanup path;
- reject invalid external events with `SessionRejected`;
- assert invalid internal adapter behavior;
- log every transition with stable Session and Conversation context.

Test every valid transition, every invalid event/state pair, closure in every state, agent return with an open message, duplicate follow-up, and stale or mismatched message IDs.

## 3.3 Websocket transport and clients

Primary files:

- `ai_server/websocket_server.py`
- `ai_server/ws_client_common.py`
- chat and batch clients
- `tests/test_websocket_server.py`

Work:

- implement the complete canonical serialization;
- require `medium=text` during handshake;
- handle `ConversationEnded`, `SessionRejected`, and `ProcessingUpdate`;
- remove legacy wait-state compatibility;
- apply the Session-supplied follow-up deadline;
- update interactive prompts and batch completion behavior;
- test complete scripted conversations and invalid sequences.

## 3.4 Microphone events and interface

Primary files:

- `ai_server/microphones/messages.py`
- `ai_server/microphones/interfaces.py`
- `ai_server/microphones/types.py`

Work:

- add listening, visual, cue, utterance, and playback enums;
- add correlation IDs;
- separate capture events from playback events;
- make `SetVisualState` part of the abstract driver contract;
- make availability and lifecycle events typed;
- keep device-specific details outside the interface.

## 3.5 Restructure `MicrophoneManager`

Primary file:

- `ai_server/microphones/manager.py`

Work:

- separate driver state management from Conversation adaptation;
- introduce explicit per-microphone protocol state;
- generate and validate correlation IDs;
- replace implicit `pending_event` re-arming with explicit transitions;
- separate idle listening from active-capture liveness;
- centralize visual transitions and recovery;
- ensure every accepted utterance creates exactly one message;
- ensure rejected and empty input creates none.

If the manager becomes too large, split reusable state-machine and adapter code into package modules while keeping interfaces in `interfaces.py` and messages in `messages.py`.

## 3.6 Immediate open-mic candidate feedback

Change partial STT handling so the first wake-candidate transition for the current `utterance_id`:

1. records the candidate;
2. immediately sends `SetVisualState(LISTENING)`;
3. continues capture without a chime;
4. ignores duplicate candidate notifications;
5. validates the final transcript;
6. returns to `IDLE` on rejection;
7. moves to `PROCESSING` on acceptance.

Tests must control partial and final transcript timing and prove that `LISTENING` is commanded before `SpeechEnded` and before final transcription completes.

## 3.7 Box3 driver and firmware

Primary files:

- `ai_server/microphones/drivers/box3_esphome.py`
- `firmware/esphome/packages/piotr-voice-satellite-api-services.yaml`
- `firmware/esphome/packages/esp32-s3-box-3-ryszardzie.yaml`
- `tests/test_box3_esphome_microphone.py`

Work:

- map semantic visual states to explicit ESPHome services;
- add or select a proper processing bitmap/page;
- remove visual changes inferred from voice-assistant callbacks;
- retain firmware-owned disconnected `ERROR`;
- apply the server-commanded state after reconnection;
- implement correlated listening and speech events;
- separate listening readiness, speech boundaries, and playback;
- keep service names private to the driver;
- test service discovery, visual mapping, reconnect behavior, stale events, candidate timing, and half-duplex behavior.

Use the repository Box3 build and flash workflow. Perform hardware setup and flashing one step at a time.

## 3.8 Voice Preview firmware

Primary files:

- `firmware/esphome/packages/home-assistant-voice-pe-piotr-standard.yaml`
- `firmware/esphome/packages/piotr-voice-satellite-api-services.yaml`
- Voice Preview entrypoint YAML files

Work:

- implement explicit services for `IDLE`, `LISTENING`, and `PROCESSING`;
- render them as LEDs off, pulsing blue, and pulsing white;
- retain low-light red as the disconnected fail-safe;
- prevent voice-assistant callbacks from overriding server-commanded state;
- implement the approved visual precedence table for disconnected, mute, setup, timer, and server-controlled states.

## 3.9 Conformance tests and migration

Primary suites:

- `tests/test_microphones.py`
- `tests/test_mic_protocol_test.py`
- `tests/test_box3_esphome_microphone.py`
- `tests/test_messages.py`
- `tests/test_websocket_server.py`

Work:

- remove assertions tied to obsolete events;
- associate tests with protocol requirement IDs;
- create reusable driver conformance tests;
- test ordering rather than mere event presence;
- test every failure and recovery path;
- treat visual output as protocol behavior;
- do not retain tests solely to preserve accidental legacy behavior.

## 3.10 Verification

Run verification in this order:

1. Focused protocol and state-machine tests.
2. Entire pytest suite.
3. ESPHome validation and compilation for every affected entrypoint.
4. Generated firmware inspection for required services.
5. Entire `orchestrator_and_dsa_tests/` suite using the currently configured model.
6. Manual websocket smoke test.
7. Hardware validation, one device at a time: Box3, bedroom Voice Preview, then office Voice Preview.
8. Live state-sequence verification using logs and visible device output.

Required hardware sequences:

- disconnected -> `ERROR`;
- connected and inactive -> `IDLE`;
- ordinary open-mic speech -> remain `IDLE`;
- wake candidate during speech -> immediate `LISTENING`;
- rejected candidate -> `IDLE`;
- accepted utterance -> `PROCESSING`;
- assistant playback or conversation completion -> `IDLE`;
- follow-up request -> `LISTENING`;
- follow-up timeout -> `IDLE`;
- connection loss from any state -> `ERROR`.

### Stage 3 acceptance criteria

- Code implements every approved normative requirement.
- No legacy event or implicit state remains without an explicitly documented reason.
- Conversation and microphone conformance suites pass.
- The entire pytest suite passes.
- The entire orchestrator and DSA behavior suite passes with the configured model.
- All affected firmware configurations validate and compile.
- Box3 and Voice Preview expose the same semantic visual states.
- Open-mic wake candidates visibly enter `LISTENING` before the user finishes speaking.
- Rejected speech never reaches an agent.
- Logs contain stable Session, Conversation, listen, utterance, and playback context.
- Documentation and implementation references match the final code.

## Delivery milestones

Keep changes reviewable and preserve the design gate:

1. Documentation routing and index
2. Normative Conversation Protocol
3. Normative Microphone Protocol and visual-state contract
4. Normative Microphone-Conversation Mapping and conformance catalogue
5. Conversation event types and Session state machine
6. Websocket migration
7. Microphone types and manager migration
8. Box3 driver and firmware migration
9. Voice Preview firmware migration
10. Full-system conformance and hardware validation

Milestones 1-4 comprise the documentation design. Milestones 5-10 must not begin until the Stage 2 specifications are approved.
