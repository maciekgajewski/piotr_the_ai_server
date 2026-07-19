# Microphone-Conversation Mapping

## Status and scope

- **Authority:** Normative mapping; approved for T-004 implementation
- **Audience:** Maintainers of MicrophoneManager, voice InputSession adaptation, STT/TTS integration, and microphone Conversation tests
- **Read when:** Changing accepted-speech creation, assistant rendering, follow-up, cancellation, recovery, or re-arm behavior
- **Approval state:** Approved by Captain on 2026-07-19

This document maps the normative
[Microphone Protocol](microphone-protocol.md) to the draft
[AI Server Conversation Bridge Protocol](ai-server-conversation-protocol.md).
It preserves the manager-to-driver event vocabulary and all T-002/T-003
ordering, correlation, recovery, reason-string, and re-arm requirements.

This mapping does not authorize firmware or driver-protocol changes. It replaces
the old Session/ConversationEndpoint mapping at T-004 atomic cutover.

Requirement identifiers use the `MAP-` prefix.

## Ownership boundaries

- `MicrophoneManager` owns the persistent voice `InputSession`, input
  acceptance, capture, STT, optional speaker recognition, cues, playback,
  connected visual state, follow-up presentation/timing, and media recovery.
- The per-Conversation voice adapter owns one `InputConversation`, its control
  stream, assistant sink, follow-up commit token, and exact-once cleanup.
- The core bridge owns only cross-side Conversation state. It MUST NOT observe
  listening generations, speech starts, partial transcripts, audio, cues,
  playback IDs, visual state, concrete drivers, or TTS batching.
- A microphone driver knows only the Microphone Protocol and MUST NOT create or
  consume core Conversation events. (`MAP-OWNER-001`)
- The mapping MUST NOT reproduce the bridge state machine or infer Agent state
  from device behavior. (`MAP-OWNER-002`)

## Terminology

- **Voice InputSession:** Persistent manager-owned input scope for one
  microphone instance.
- **Voice acceptance:** Atomic boundary at which final non-whitespace text,
  request context, and a fresh Conversation ID form a ready InputConversation.
- **Pre-Conversation work:** Readiness, listening, capture, STT, acceptance,
  accepted cue, and optional speaker recognition before voice acceptance.
- **Follow-up presentation:** The point at which output has drained, the
  `FOLLOW_UP_READY` cue completed, and matching `FOLLOW_UP` listening is active.
- **Rendering batch:** Adapter-local bounded group of assistant text chunks sent
  to the current TTS implementation.
- **Re-arm:** Explicitly starting a fresh listening generation after media
  cleanup and InputSession return to `IDLE`.
- **Illustrative milestone:** A sequence label such as final transcript accepted;
  it is not a typed event unless it appears in an event inventory.

## Typed event inventory

### Microphone side to voice adapter

These events are defined by the Microphone Protocol. Only the subset that can
affect mapping decisions is listed; other driver events remain manager-local.

| Event/result | Fields used by mapping | Valid mapping states | Mapping effect |
|---|---|---|---|
| `ListeningStarted` | `listen_id`, `mode` | `ACCEPTING`, `PRESENTING_FOLLOW_UP` | Readiness or follow-up presentation may complete |
| `SpeechStarted` | `listen_id`, `utterance_id`, audio format, optional wake word | `ACCEPTING`, `AWAITING_FOLLOW_UP` | Start capture; wins exact follow-up boundary race |
| `AudioChunk`/`AudioProgress` | correlation fields | active capture | Feed/observe private STT path |
| `SpeechEnded` | correlation fields, reason | active capture | Complete STT/acceptance decision |
| `CueFinished` | `cue_id` | matching cue operation | Continue pre-Conversation or follow-up presentation |
| `PlaybackFinished` | `playback_id` | `RENDERING` | Release rendered batch/output drain |
| `MicrophoneUnavailable` | operation correlation, reason | any non-terminal adapter state | Recover locally or fail/close input scope |
| `DriverClosed` | none | every non-terminal state | Close InputSession |

Partial STT snapshots and wake-candidate decisions remain internal manager
results. They MUST NOT cross into the core. (`MAP-INPUT-001`)

### Voice adapter to bridge

| Core field/event | Source condition | Constraint |
|---|---|---|
| `InputConversation.initial_message` | Accepted final STT for new Conversation | Complete non-whitespace text, exposed once |
| `UserMessage` | Accepted final STT during acknowledged follow-up | Complete non-whitespace text |
| `FollowUpTimedOut` | Manager's presenter timer wins | Exactly once for acknowledged interval |
| `ConversationCancelled` | User or input-local cancellation | Every active Conversation state |
| `InputConversationFailed` | Media failure recovered without closing persistent input | Active Conversation only |
| `InputSessionClosed` | Driver/session cannot continue | Every active Conversation state |

Wake detection, `SpeechStarted`, partial text, rejected final text, empty capture,
cue completion, and playback progress are not core events. (`MAP-INPUT-002`)

### Bridge to voice adapter operations

| Operation | Microphone mapping | Commit point |
|---|---|---|
| assistant sink `start()` | Open bounded per-turn renderer and set `PROCESSING` | Renderer accepts stream start |
| `send_text(chunk)` | Feed bounded text batching/synthesis path | Chunk accepted into bounded renderer; external media commit may occur later |
| `complete()` | Flush final batch and drain every correlated playback | Last required playback completes, or empty stream closes |
| `abort(reason, detail)` | Discard uncommitted text, cancel synthesis, stop rendering where supported, clean media | Adapter commits abort before completion commit |
| `request_follow_up()` | Drain assistant output, set `LISTENING`, play `FOLLOW_UP_READY`, arm `FOLLOW_UP` listening and timer | User has actually been presented the offer |
| `acknowledge_follow_up_ready(token)` | Open outcome eligibility for matching local interval | Synchronous matching-token acknowledgement |
| `end_conversation(event)` | Record typed result and complete/abort media cleanup | Adapter accepts terminal result |

The token is created only after presentation commits. Before acknowledgement,
speech/timeout arbitration may retain one adapter-local outcome but MUST NOT
expose it to the bridge. Terminal input bypasses and clears that retained value.
(`MAP-FOLLOWUP-001`)

`ConversationEnded.context_rejection_code` remains a typed enum through this
mapping. The adapter MAY select a configured medium-specific response from the
enum but MUST NOT parse diagnostic `detail`. (`MAP-TERMINAL-001`)

### Voice adapter to Microphone Protocol

The adapter uses only these existing abstract commands:

| Mapping action | Commands |
|---|---|
| Announce new-Conversation readiness | `SetVisualState(IDLE)`, then `StartListening` with `WAKE_WORD` or `OPEN_MIC` |
| Indicate wake/candidate input | `SetVisualState(LISTENING)` |
| Accept utterance | `SetVisualState(PROCESSING)`, optional `StopListening`, `PlayCue(UTTERANCE_ACCEPTED)` |
| Present follow-up | `SetVisualState(LISTENING)`, `PlayCue(FOLLOW_UP_READY)`, `StartListening(FOLLOW_UP)` |
| Follow-up timeout | `StopListening`, `PlayCue(FOLLOW_UP_TIMEOUT)`, `SetVisualState(IDLE)` after cue |
| Render assistant | `SetVisualState(PROCESSING)`, `PlaybackBegin`, `PlaybackChunk`*, `PlaybackEnd` |
| Close | matching stop/cleanup, then `Close` when persistent driver boundary ends |

Exact command legality, correlation, and acknowledgement remain governed by the
Microphone Protocol.

## State inventory

The core InputSession state is authoritative for acceptance cardinality. The
mapping uses these media substates only to serialize microphone work:

| Media substate | Compatible InputSession state | Meaning |
|---|---|---|
| `QUIESCENT` | `IDLE` | No readiness or media operation active |
| `ACCEPTING` | `ACCEPTING` | New-Conversation listening/capture/STT is active |
| `ACTIVE` | `ACTIVE` | InputConversation exists; Agent may be working |
| `RENDERING` | `ACTIVE` | Assistant text is being synthesized or played |
| `PRESENTING_FOLLOW_UP` | `ACTIVE` | Output drain/cue/arm is committing a follow-up offer |
| `AWAITING_FOLLOW_UP` | `ACTIVE` | Follow-up listening/timer arbitration is active |
| `RECOVERING` | `ACCEPTING` or `ACTIVE` | Manager is restoring a trusted microphone boundary |
| `CLOSING` | `CLOSING` | Pending input and media operations are being released |
| `CLOSED` | `CLOSED` | Driver and InputSession are terminal |

Capture-open, cue-active, and playback-active remain Microphone Protocol states,
not additional mapping states.

## Complete transition tables

### New-Conversation acceptance matrix

`V` is valid, `L` is handled locally without exposing a core Conversation, `P`
is a trusted protocol violation requiring boundary recovery, and `I` is
inapplicable after close.

| Trigger/result | `QUIESCENT` | `ACCEPTING` | `ACTIVE` | `RENDERING` | `PRESENTING_FOLLOW_UP` | `AWAITING_FOLLOW_UP` | `RECOVERING` | `CLOSING` | `CLOSED` |
|---|---|---|---|---|---|---|---|---|---|
| core calls `accept_conversation()` | V -> `ACCEPTING` | P | P | P | P | P | P | I | I |
| accepted new final text | P | V -> `ACTIVE` and return InputConversation | P | P | P | P | P | I | I |
| rejected/empty new final text | P | L, remain/re-arm `ACCEPTING` | P | P | P | P | L | I | I |
| recoverable pre-Conversation failure | P | L -> `RECOVERING`/`ACCEPTING` | P | P | P | P | L | I | I |
| unrecoverable/driver closed | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V | I |
| InputConversation context exits | P | P | V -> `QUIESCENT` | V after drain/abort -> `QUIESCENT` | V after abort -> `QUIESCENT` | V after stop -> `QUIESCENT` | V after cleanup -> `QUIESCENT` | V -> `CLOSED` | I |

### Active mapping trigger matrix

| Trigger/result | `ACTIVE` | `RENDERING` | `PRESENTING_FOLLOW_UP` | `AWAITING_FOLLOW_UP` | `RECOVERING` | `CLOSING` | `CLOSED` |
|---|---|---|---|---|---|---|---|
| `ProcessingUpdate` presentation | V, remain | P | P | P | P | I | I |
| assistant sink `start()` | V -> `RENDERING` | P | P | P | P | I | I |
| `send_text()` | P | V, remain | P | P | P | I | I |
| sink `complete()` | P | V -> `ACTIVE` after drain | P | P | P | I | I |
| sink `abort()` | P | V -> `ACTIVE` after abort | P | P | V | V | I |
| `request_follow_up()` | V -> `PRESENTING_FOLLOW_UP` | P | P | P | P | I | I |
| follow-up presentation committed | P | P | V -> `AWAITING_FOLLOW_UP` | P | P | I | I |
| matching token acknowledgement | P | P | P | V/G | P | I | I |
| accepted follow-up final text | P | P | retain until ack | V -> `ACTIVE` and emit `UserMessage` | P | I | I |
| follow-up timeout wins | P | P | retain until ack | V -> `ACTIVE` and emit `FollowUpTimedOut` | P | I | I |
| empty/no-transcript follow-up | P | P | local retry after new presentation | local retry or typed failure | V | I | I |
| Conversation cancellation | V, emit terminal control | V, abort then emit | V, abort then emit | V, stop then emit | V, emit | I | I |
| recoverable media failure | V -> `RECOVERING` and emit `InputConversationFailed` | same | same | same | V | I | I |
| unrecoverable/driver closed | emit `InputSessionClosed` -> `CLOSING` | same | same | same | same | V | I |

`G` means acknowledgement opens the gate and immediately releases one retained
ordinary outcome, if present. Ordinary follow-up outcomes are structurally
impossible outside the acknowledged interval at the core boundary.

## Invariants

1. One accepted microphone utterance creates exactly one ready
   InputConversation or one follow-up `UserMessage`, never both.
   (`MAP-INVARIANT-001`)
2. A fresh Conversation ID is assigned only at voice acceptance. Wake detection,
   capture start, partial text, and rejected final text create no Conversation.
3. Rejected open-mic speech, wake without usable text, and empty follow-up
   capture never reach Agent. (`MAP-INVARIANT-002`)
4. New-Conversation listening uses `WAKE_WORD` or `OPEN_MIC`; follow-up uses
   `FOLLOW_UP`. A follow-up never creates another InputConversation.
5. One microphone InputSession has at most one active bridge. Readiness and
   re-arm never overlap prior output drain or cleanup. (`MAP-INVARIANT-003`)
6. `PROCESSING` covers accepted input, Agent work, synthesis, and assistant
   playback. It does not revert to `IDLE` merely because text or a core terminal
   event arrived.
7. Every listen, utterance, cue, and playback ID follows Microphone Protocol
   correlation; stale events cannot affect a newer Conversation.
8. Concrete service names, images, LEDs, and driver types remain inside drivers
   and firmware.
9. Assistant text flows through an adapter-owned bounded renderer. The bridge
   contains no voice output queue. (`MAP-BACKPRESSURE-001`)
10. T-002/T-003 explicit-stop ordering and exact reason strings are preserved.

## Processing-update mapping

While the adapter is `ACTIVE` and no assistant stream is open, a
`ProcessingUpdate` keeps `PROCESSING` visible and MAY play one configured spoken
processing cue through the ordinary cue/playback exclusion rules. It MUST NOT
overlap assistant rendering, re-arm listening, present follow-up, or end the
Conversation. Repeated updates MAY be throttled by the Agent-side processing
policy. (`MAP-PROGRESS-001`)

## Assistant rendering and bounded backpressure

The voice adapter MUST consume assistant chunks incrementally through a required
positive integer `microphones.assistant_text_buffer_characters` bound. A full
buffer blocks `send_text()` until a rendering worker consumes text; it MUST NOT
drop, overwrite, or create an unbounded secondary queue. (`MAP-OUTPUT-001`)

With a non-streaming TTS engine, the worker MAY form bounded phrase batches from
ordered chunks and synthesize batches sequentially. It MUST preserve text order,
MUST await each correlated playback according to the Microphone Protocol, and
MUST leave room by consuming batches rather than waiting for the complete Agent
message. Empty assistant streams produce no synthesis or playback.

`start()` commits when the bounded renderer opens. `send_text()` returns when
the chunk is accepted into that renderer; acceptance is not external media
commit. Text commits as its batch is handed to irreversible playback.
`complete()` commits only when every accepted batch has synthesized and drained,
or when an empty stream closes. `abort()` discards uncommitted buffered text,
cancels synthesis, and stops future playback; audio already handed to
irreversible playback is not retracted.
(`MAP-OUTPUT-002`)

If the current abstract driver cannot acknowledge mid-playback interruption,
the adapter waits for or closes that driver operation according to the existing
protocol. A future playback-abort command requires a separate Microphone
Protocol decision.

## Follow-up timing and boundary arbitration

Agent follow-up intent and the core event carry no duration. The manager uses its
explicit per-microphone `follow_up_timeout_seconds` policy and starts a monotonic
timer only after previous assistant output drained, `FOLLOW_UP_READY` completed,
and `ListeningStarted(FOLLOW_UP)` confirmed. That instant is the presentation
commit point. (`MAP-FOLLOWUP-002`)

Speech start and expiry commit through one adapter-local atomic arbiter:

1. `SpeechStarted` committed at or before the deadline wins, including exact
   equality; the timer is cancelled permanently.
2. The adapter completes STT and emits `UserMessage` only for accepted complete
   non-whitespace final text.
3. Empty or rejected text begins a newly presented interval when policy permits,
   or emits typed input failure; it never revives the losing prior timer.
4. Expiry committed first emits exactly one `FollowUpTimedOut`; later speech is
   ignored or rejected locally.

Only one outcome may be retained before token acknowledgement and only one may
cross the interface. (`MAP-FOLLOWUP-003`)

On timeout, the manager stops and joins the matching listening generation, plays
and completes `FOLLOW_UP_TIMEOUT`, exposes `FollowUpTimedOut` when the gate is
eligible, and does not re-arm until the Conversation scope exits and a new
`accept_conversation()` exposes readiness.

## Timeouts and cancellation

- Arm, segment-liveness, cue, playback, and connection timeouts remain owned by
  MicrophoneManager under the Microphone Protocol.
- Follow-up semantic timeout is owned by this adapter as described above.
- Cancellation stops input production, suppresses late STT/speaker results,
  aborts uncommitted output, performs correlated media cleanup, and releases the
  bridge through `ConversationCancelled`.
- InputSession `close()` commits `CLOSING` before awaiting microphone teardown;
  pending acceptance raises `InputSessionClosed`, while an active Conversation's
  control receive emits `InputSessionClosed`.
- No timeout or late callback may create an empty message or mutate a newer
  generation.

## Failure and recovery

| Failure boundary | Core result | Persistent InputSession | Required microphone action |
|---|---|---|---|
| Capture/STT failure before voice acceptance | No core event | Reuse if trusted | Stop/discard generation and re-arm with new IDs |
| Empty/rejected new utterance | No core event | Reuse | Preserve open mic or explicitly re-arm |
| Capture/STT failure during active follow-up, recovered | `InputConversationFailed` if Conversation cannot continue | Reuse | Stop, restore trusted disarmed state |
| Cue/TTS/playback failure with trusted recovery | `InputConversationFailed` | Reuse | Abort correlated operation and restore state |
| Driver unavailable but manager can recreate boundary | `InputConversationFailed` for active Conversation | Recreate then reuse | New driver/session correlation |
| Driver closed or recovery cannot restore trust | `InputSessionClosed` | Close | Stop all work and close boundary |
| Invalid driver state/correlation | `InputSessionClosed` if active | Recreate, never guess | Close old driver boundary |

The adapter MUST distinguish recoverable Conversation-local failure from
persistent InputSession closure based on actual recovery, not exception class
names or free-text detail. (`MAP-FAILURE-001`)

## Normal sequences

### Accepted wake-word Conversation

```text
accept_conversation() enters ACCEPTING
SetVisualState(IDLE)
StartListening(WAKE_WORD) -> ListeningStarted
SpeechStarted(wake_word) -> SetVisualState(LISTENING)
AudioChunk* -> SpeechEnded -> final STT accepted
SetVisualState(PROCESSING)
join stopped generation, play accepted cue, resolve optional speaker identity
atomically create InputConversation(initial_message, context, Conversation ID)
bridge runs Conversation
context exit drains/aborts media; InputSession returns IDLE
```

### Open-mic candidate rejection

```text
StartListening(OPEN_MIC) -> ListeningStarted
SpeechStarted -> partial STT detects candidate -> SetVisualState(LISTENING)
final STT rejects candidate
ResetWakeCandidate and SetVisualState(IDLE)
no core Conversation; same open-mic generation remains active
```

### Assistant rendering and follow-up

```text
sink start -> SetVisualState(PROCESSING)
ordered chunks enter bounded renderer and bounded batches play
sink complete drains final PlaybackFinished
request_follow_up -> SetVisualState(LISTENING)
PlayCue(FOLLOW_UP_READY) -> StartListening(FOLLOW_UP) -> ListeningStarted
presentation commits token and timer
bridge acknowledges token after entering WAITING_FOR_FOLLOW_UP
accepted final STT emits one UserMessage
```

## Invalid sequences

- Creating InputConversation on wake detection, `SpeechStarted`, partial text, or
  rejected/empty final text;
- sending raw microphone events to the bridge;
- creating a new InputConversation for follow-up;
- starting follow-up timing before output, cue, and listening presentation
  complete;
- allowing timeout to beat speech committed exactly at the deadline;
- exposing an ordinary outcome before token acknowledgement;
- retaining more than one follow-up outcome;
- starting playback before a half-duplex driver is disarmed;
- re-arming merely because `ConversationEnded` arrived;
- setting `IDLE` while synthesis or playback is outstanding;
- buffering unbounded assistant text or waiting for complete output while a full
  bounded buffer prevents completion;
- parsing terminal diagnostic detail to choose control flow;
- letting stale STT, cue, playback, or driver events affect a newer scope;
- invoking concrete firmware services outside the driver.

## Observability requirements

- Logs MUST use stable
  `MicrophoneInputSession[<microphone>:<input_session_id>]` and
  `MicrophoneInputConversation[<conversation_id>]` prefixes.
  (`MAP-OBS-001`)
- Every cross-protocol translation and mapping substate transition MUST log on
  DEBUG with available Conversation, session, listen, utterance, cue, and
  playback IDs.
- Readiness, accepted utterance, voice acceptance, assistant rendering start and
  drain, follow-up presentation, timeout arbitration, Conversation outcome,
  recovery, and re-arm MUST be logged briefly on INFO.
- Boundary-race logs MUST include monotonic decision values and the winning
  branch without requiring transcript content.
- Raw/partial/rejected transcript content remains governed by `MP-OBS-003` and
  MUST be DEBUG-only with explicit opt-in.

## Implementation and conformance references

Planned implementation owners:

- replacement for `ai_server/microphones/agent_endpoint.py` implementing the
  sealed voice InputSession/InputConversation;
- `ai_server/microphones/manager.py` for acceptance, presentation, timing,
  rendering, cleanup, and recovery;
- existing `ai_server/microphones/interfaces.py`, `messages.py`, `protocol.py`,
  `stt.py`, and `tts.py` behind their current abstract boundaries.

Planned tests are indexed by the
[Protocol Conformance Catalogue](protocol-conformance-catalogue.md), including
accepted-STT creation, exact follow-up boundaries, bounded renderer pushback,
sink commit races, cancellation, recovery, T-002/T-003 regressions, and one-device
hardware checks.

## Compatibility policy

The old microphone `CommunicationEndpoint`/Session adapter and mappings from
`ReadyForConversation`, message begin/fragment/end, timeout-bearing
`FollowUpRequested`, and endpoint `ConversationEnded` are removed at atomic
cutover. There is no compatibility adapter. (`MAP-COMPAT-001`)

The normative Microphone Protocol is not superseded or changed by this mapping.

## Explicitly unresolved decisions

None for T-004. Streaming synthesis, explicit playback-abort acknowledgement,
and incremental hardware-rendering capability negotiation remain future
Microphone Protocol work and do not block this mapping.
