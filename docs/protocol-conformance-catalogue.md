# Protocol Conformance Catalogue

## Status and scope

- **Authority:** Normative conformance plan for T-004 and the Microphone Protocol
- **Audience:** Implementers and reviewers of the Conversation bridge, websocket binding, microphone mapping, and driver protocol
- **Read when:** Implementing T-004, adding protocol tests, reviewing coverage, or closing the migration
- **Approval state:** T-004 sections approved by Captain on 2026-07-19

This catalogue maps stable requirements to implementation owners and required
evidence. T-004 implementation is present; a row becomes a conformance claim
only when its referenced test or recorded manual check exists and passes.
Existing `MP-` entries continue to describe the normative Microphone Protocol.

Equivalent focused filenames are permitted only when this catalogue is updated
in the same change.

## Conversation Bridge Protocol

### Ownership, context, and creation

| Requirement | Summary | Implementation owner | Required automated evidence |
|---|---|---|---|
| `CP-OWNER-001` | One bridge owns all cross-side state | conversation bridge | state ownership/illegal-call tests in `tests/test_conversation_bridge.py` |
| `CP-OWNER-002` | One active InputConversation per InputSession | input supervision | sequential and second-accept rejection tests |
| `CP-OWNER-003` | One fixed input and AgentConversation per bridge | bridge | factory cardinality and no-replacement tests |
| `CP-OWNER-004` | Concurrent AgentConversations isolate mutable state | Agent factories | barrier-based concurrent isolation test |
| `CP-OWNER-005` | Core uses sealed directional abstractions only | interfaces and bridge | fake-only core test plus architecture import check |
| `CP-CONTEXT-001` | Context is typed, immutable, and scope-correct | contexts | mutation/type tests and absence of generic state/attributes |
| `CP-CONTEXT-002` | Context resolution is synchronous and has closed outcomes | context provider | resolved/rejected/unavailable/malformed/exception tests |
| `CP-CREATION-001` | Core starts only with accepted non-whitespace initial text | InputSession adapters | empty/whitespace rejection and atomic creation tests |
| `CP-SESSION-001` | Acceptance returns one fully usable InputConversation | InputSession implementations | accepting-to-active readiness/capability test |
| `CP-SESSION-002` | Close wins acceptance race unless active creation committed | InputSession implementations | barrier tests on both sides of commit |
| `CP-SESSION-003` | Close commits before suspension and context exit gates reuse | InputSession implementations | idempotent close and cleanup-before-readiness tests |
| `CP-STARTUP-001` | Agent entry is raced, joined, and partial entry self-cleans | bridge and Agent factory | terminal-input entry races and partial-`__aenter__()` cleanup |
| `CP-HANDOFF-001` | User-message acceptance commit survives cancellation correctly | bridge and AgentConversation | before/simultaneous/after-commit handoff races |

### Event order, flow control, and follow-up

| Requirement | Summary | Implementation owner | Required automated evidence |
|---|---|---|---|
| `CP-INPUT-001` | Input exposes one serialized follow-up outcome | input adapters | duplicate/outcome arbitration tests |
| `CP-AGENT-001` | Successful turn has exactly one explicit disposition | AgentConversation and bridge | zero-stream/one-stream/end/follow-up/failure cases |
| `CP-SINK-001` | Assistant completion and abort have one definitive commit | input sink | before/at/after commit race tests |
| `CP-BACKPRESSURE-001` | Agent output is a zero-capacity rendezvous | AgentConversation | blocked producer proof while bridge/input is blocked |
| `CP-BACKPRESSURE-002` | Bridge has no assistant output queue | bridge | slow sink test and task/queue inspection |
| `CP-FOLLOWUP-001` | Token gates outcomes until bridge acknowledgement | input adapter and bridge | early outcome, token mismatch, duplicate, terminal bypass tests |
| `CP-FOLLOWUP-002` | Presenter owns semantic follow-up timing | input adapters | prove no core timer; websocket and voice timing tests |
| `CP-RACE-001` | Complete ready set uses normative priority | bridge state machine | pairwise and multi-ready deterministic race table |
| `CP-CANCEL-001` | Input terminal receive is continuously pending | bridge | cancellation in every non-terminal state and handoff race |
| `CP-CANCEL-002` | Agent entry/cancel deadline is explicit and fatal | bridge/top-level termination | injected hook unit tests plus subprocess non-zero exit |
| `CP-LIFETIME-001` | All conversation-owned work is joined or contained | bridge/AgentConversation | task-leak tests across every exit path |

### Terminal behavior, shutdown, logging, and cutover

| Requirement | Summary | Implementation owner | Required automated evidence |
|---|---|---|---|
| `CP-TERMINAL-001` | Terminal reason/code/detail invariants are structural | typed messages and bindings | constructor and serialization matrix |
| `CP-FAILURE-001` | Each failure has ratified isolation/reuse behavior | bridge and supervisors | parameterized failure/reuse cases |
| `CP-SHUTDOWN-001` | First signal atomically closes admission and sessions within one deadline | application lifecycle registry | idle/accepting/active fake-session unit tests and subprocess zero exit |
| `CP-SHUTDOWN-002` | Deadline or second signal hard-exits non-zero | top-level lifecycle | two subprocess escalation tests |
| `CP-OBS-001` | Every InputSession transition has stable context | InputSession implementations | `caplog` transition coverage |
| `CP-OBS-002` | Every bridge transition/race has stable context | bridge | `caplog` state and selected-ready-set coverage |
| `CP-COMPAT-001` | Legacy in-process surfaces are absent after cutover | repository | import/symbol/reference absence checks |

Additional exhaustive core coverage:

- Every cell in the InputSession operation matrix and bridge
  source/event/state matrix.
- Agent entry and initial/follow-up input delivery raced with cancellation,
  recoverable failure, and session close before and simultaneous with commit.
- Progress before streaming and rejection during streaming.
- Agent failure before output, during progress, and during streaming.
- Sink send and completion blocked while terminal input arrives.
- Repeated sequential Conversations and multiple concurrent InputSessions.
- Exact-once `__aexit__()` for partial startup, normal end, cancellation, and
  fatal containment paths.
- Startup rejection for missing, invalid, non-positive, or non-finite
  Conversation/shutdown deadline settings.

## Websocket Conversation Protocol

### Schema, state, and transport

| Requirement | Summary | Implementation owner | Required automated evidence |
|---|---|---|---|
| `WS-OWNER-001` | One websocket maps to one InputSession and one active Conversation | websocket adapter | sequential/overlapping start tests |
| `WS-OWNER-002` | Background reader remains live during Agent/writer blocking | websocket adapter | delayed Agent and blocked-send disconnect/liveness tests |
| `WS-SCHEMA-001` | One strict JSON object per UTF-8 text frame | message parser | round trips plus unknown/null/duplicate/non-text/wrong-type cases |
| `WS-CREATION-001` | Start event includes complete initial text | websocket InputSession | empty/whitespace rejection and one-step creation test |
| `WS-STREAM-001` | External assistant stream IDs are fresh and ordered | websocket sink | start/chunk/complete/abort ID matrix |
| `WS-TERMINAL-001` | Context rejection code is conditional and typed | websocket serializer | every reason/code/detail shape |
| `WS-BACKPRESSURE-001` | Writes serialize without unbounded output queue | writer/sink | concurrent send and blocked drain tests |
| `WS-COMMIT-001` | Transport handoff commit survives cancellation | writer/sink | abort before handoff and cancellation after handoff tests |
| `WS-ERROR-001` | External violation rejects then closes | websocket adapter | every rejection code and close-order test |
| `WS-OBS-001` | Connection logs carry peer and stable InputSession ID | websocket adapter | `caplog` admission/state/gate/close coverage |
| `WS-COMPAT-001` | Old vocabulary is rejected | parser/adapter | parameterized legacy event list |

### Capacity, configuration, and follow-up gate

| Requirement | Summary | Implementation owner | Required automated evidence |
|---|---|---|---|
| `WS-CAPACITY-001` | Every pre-upgrade slot has one owner and exact release | admission controller | handshake/rejection/failure/shutdown release paths |
| `WS-CAPACITY-002` | Full capacity returns `503` plus configured `Retry-After` | HTTP admission | below/at/above-capacity concurrent tests |
| `WS-CONFIG-001` | Required bounds have no hidden defaults | config parser | missing/Boolean/zero/negative/non-integer/non-finite matrix |
| `WS-FOLLOWUP-001` | Gate retains at most one outcome; terminal input bypasses | websocket InputConversation | early/duplicate/cancel/close cases |
| `WS-FOLLOWUP-002` | Outcome exposure follows bridge state acknowledgement | adapter and bridge | before handoff, during drain, before ack, after ack cases |
| `WS-FOLLOWUP-003` | Repository clients default semantic timeout to 15 seconds and allow an explicit override | client configuration/CLI | default, override, and invalid policy tests |
| `WS-FOLLOWUP-004` | Client submission at/before expiry wins and sends one event | shared client state | controllable-clock before/equal/after tests |
| `WS-LEASE-001` | Server lease closes `1013` without forging timeout | websocket adapter | start/cancel/heartbeat/equal-boundary/expiry cases |

Additional websocket coverage:

- Handshake timeout, maximum frame bound, bounded ingress overflow, heartbeat,
  invalid state, duplicate outcome, and every documented close code/reason.
- Slot release after HTTP handler exception, websocket preparation failure,
  invalid handshake, protocol rejection, ordinary close, lease expiry, and
  application shutdown.
- Follow-up send failure before handoff and drain failure after handoff.
- Lease expiry during drain; retained input at the exact lease deadline;
  suppression of input after committed expiry.
- Interactive and batch client normal, follow-up, timeout, cancellation,
  disconnect, and terminal flows.
- Real server/client delayed-response liveness check using the repository
  websocket test workflow.

## Microphone Protocol

These requirements are already normative and are not redesigned by T-004.

| Requirement | Summary | Implementation owner | Required conformance evidence |
|---|---|---|---|
| `MP-OWNER-001` | Driver does not own STT, Conversation, or re-arm | abstract interface and drivers | reusable black-box driver harness; architecture review |
| `MP-ID-001` | Every listening generation has a new ID | manager | `tests/test_microphones.py`; `tests/test_microphone_protocol.py` |
| `MP-ID-002` | Every segment has a new associated utterance ID | drivers and manager | multiple open-mic segment cases |
| `MP-ID-003` | Stale events cannot mutate state | manager and drivers | stale listen/playback/cue and ID-reuse cases |
| `MP-EVENT-001` | Capture and playback vocabularies are separate | messages and interfaces | microphone protocol and Box3 tests |
| `MP-STATE-001` | Driver transition table is enforced | every driver | reusable conformance suite and Box3 contract |
| `MP-STOP-001` | Stop actively ends device capture and awaits its notification | streaming drivers and firmware | explicit-stop ordering, timeout recovery, ESPHome generated-source inspection |
| `MP-VISUAL-001` | `ERROR` is firmware-owned, not commandable | messages, drivers, firmware | reject command plus disconnect firmware validation |
| `MP-VISUAL-002` | Visual commands are state-independent and idempotent | drivers | duplicate command in every connected driver state |
| `MP-VISUAL-003` | Manager explicitly commands connected visuals | manager | normal-sequence assertions |
| `MP-VISUAL-004` | Driver/firmware does not infer main visual | drivers and firmware | callback tests |
| `MP-VISUAL-005` | Reconnect remains error until first command | driver and firmware | disconnect/reconnect/first-command sequence |
| `MP-VISUAL-006` | Local indicators do not replace main visual | firmware | generated-config and hardware checks |
| `MP-VISUAL-007` | Processing remains through playback | manager and drivers | visual sequence around playback |
| `MP-OPENMIC-001` | First partial candidate immediately shows listening | manager partial-STT path | controlled timing before final text |
| `MP-OPENMIC-002` | Rejection resets candidate and idle without re-arm | manager and driver | rejection ordering and unchanged `listen_id` |
| `MP-OPENMIC-003` | Acceptance follows final validation | manager | final result before processing/cue/forwarding |
| `MP-AUDIO-001` | Half-duplex output starts only when disarmed | manager and drivers | cue/playback rejected while active capture |
| `MP-TIMEOUT-001` | Idle open mic has no segment timeout | manager | asynchronous idle generation remains armed |
| `MP-ERROR-001` | Untrusted state closes/recreates boundary | manager | unavailable boundary recovery test |
| `MP-OBS-001` | Commands/events include state and correlation logs | manager and drivers | `caplog` command/event context |
| `MP-OBS-002` | Visual transitions include old/new/cause | manager and drivers | `caplog` visual transition |
| `MP-OBS-003` | Transcript content is opt-in DEBUG only | STT config/implementation | config and STT opt-in/default-off tests |

Existing additional Microphone Protocol coverage remains required: every driver
state/command/event combination; all listening modes; correlation failures;
timeouts and recovery; reusable suites for every driver; ESPHome validation,
compilation, and generated-source inspection when firmware is affected.

## Microphone-Conversation Mapping

| Requirement | Summary | Implementation owner | Required automated evidence |
|---|---|---|---|
| `MAP-OWNER-001` | Driver does not consume core events | voice adapter boundary | fake-driver/fake-bridge import and interaction tests |
| `MAP-OWNER-002` | Mapping does not reproduce bridge lifecycle | adapter/bridge boundary | state/source responsibility tests |
| `MAP-INPUT-001` | Partial STT and candidate state remain private | manager | partial-positive/final-reject cases |
| `MAP-INPUT-002` | Only closed typed input outcomes cross to bridge | voice InputConversation | accepted/rejected/empty/failure cases |
| `MAP-INVARIANT-001` | One accepted utterance creates exactly one input | manager/adapter | all listening modes and repeated partials |
| `MAP-INVARIANT-002` | Rejected/empty input never reaches Agent | manager/adapter | wake/open-mic/follow-up negative cases |
| `MAP-INVARIANT-003` | Re-arm requires scope exit and finished output | manager | Conversation end before playback drain and cancellation cases |
| `MAP-PROGRESS-001` | Progress remains processing-only and never overlaps output | manager/voice adapter | repeated progress, cue exclusion, and stream-overlap rejection |
| `MAP-BACKPRESSURE-001` | Voice renderer is bounded and bridge queue-free | voice sink | full-buffer producer blocking and no-loss tests |
| `MAP-OUTPUT-001` | Text buffer bound is required and blocking | config and voice sink | invalid config plus slow-TTS bound tests |
| `MAP-OUTPUT-002` | Voice start/chunk/complete/abort commit points are definitive | voice sink | before/at/after synthesis/playback commit races |
| `MAP-FOLLOWUP-001` | Token gates early voice outcome | voice InputConversation | speech/timeout before acknowledgement and terminal bypass |
| `MAP-FOLLOWUP-002` | Timer starts after actual cue/listening presentation | manager | delayed playback/cue/listening tests |
| `MAP-FOLLOWUP-003` | Speech at/before expiry wins and one outcome crosses | manager arbiter | controllable monotonic before/equal/after barriers |
| `MAP-TERMINAL-001` | Typed context rejection survives voice mapping | voice adapter | every code and no-detail-parsing tests |
| `MAP-FAILURE-001` | Recoverable failure differs from session closure | manager/adapter | capture/STT/TTS/playback/driver recovery matrix |
| `MAP-OBS-001` | Mapping logs stable IDs and race outcome | manager/adapter | `caplog` transition/correlation cases |
| `MAP-COMPAT-001` | Old microphone Session adapter is absent at cutover | repository | symbol/reference absence checks |

Additional mapping coverage:

- Atomic InputConversation creation only after final accepted STT, stopped
  capture, processing visual, accepted cue, and optional speaker recognition.
- Wake-word without transcript, ordinary open-mic rejection, repeated partial
  candidate, accepted open-mic, and follow-up no-transcript behavior.
- Chunk batching with non-streaming TTS, ordered playback, empty stream, slow TTS
  pushback, cancellation while blocked, and cleanup before re-arm.
- Follow-up presentation delayed by assistant playback and cue completion.
- T-002 audio-progress correlation and T-003 pre-roll/accepted-stop regressions
  with exact ordering and reason strings.
- Context rejection mapping without parsing `detail`.

## Firmware and hardware acceptance

T-004 does not imply firmware changes, but final closure still verifies that the
unchanged binding preserves these behaviors one device at a time:

| Sequence | Expected main visual |
|---|---|
| Server disconnected | `ERROR` |
| Reconnected before first server command | `ERROR` |
| First connected initialization command | `IDLE` |
| Ordinary open-mic speech | remains `IDLE` |
| Partial wake candidate | immediate `LISTENING` |
| Final candidate rejection | `IDLE` |
| Accepted utterance and Agent work | `PROCESSING` |
| Assistant playback | remains `PROCESSING` |
| Playback complete, no follow-up | `IDLE` after Conversation cleanup/readiness |
| Follow-up is being presented/awaited | `LISTENING` |
| Follow-up timeout cue complete | `IDLE` only after Conversation cleanup/readiness |
| Connection loss from any connected state | `ERROR` |

Voice Preview renders the normative low-light red/off/pulsing blue/pulsing white
states; Box3 renders the corresponding error/idle/listening/processing bitmaps.
Orthogonal mute/setup/timer/volume indicators do not replace the main state.

### Recorded manual hardware results

On 2026-07-19, `voice-pe-03` (office Voice Preview) was exercised against a
real AI-server process with a private single-device config generated from the
operator config. The controlled run produced the following evidence:

- first connected initialization selected `IDLE` and armed open-mic capture;
- ordinary speech without `Ryszardzie` was transcribed, rejected before
  Conversation creation, and left the main visual `IDLE`;
- a partial `Ryszardzie` candidate selected `LISTENING`; wake-only final text
  executed `ResetWakeCandidate`, returned to `IDLE`, played no cue, and kept the
  same open-mic listening generation;
- `Ryszardzie, która godzina?` selected `LISTENING`, then `PROCESSING`, stopped
  capture before the accepted cue, kept `PROCESSING` through assistant
  playback, completed normally, selected `IDLE`, and re-armed with a new
  listening generation;
- a clarification turn presented and awaited follow-up in `LISTENING`; the
  fixed 15-second deadline won, capture stopped, the timeout cue completed,
  terminal reason `follow_up_timeout` crossed the bridge, and only then did the
  device select `IDLE` and re-arm;
- a center-button announcement interruption stopped playback and the adapter
  observed `PlaybackFinished`, completed cleanup, selected `IDLE`, and re-armed;
- controlled server shutdown selected the firmware-owned red `ERROR` visual;
  restart selected `IDLE` on the first server command and created a fresh
  open-mic generation.

The initial pre-run observation was a stale pulsing-blue visual despite no
local AI-server process or local TCP owner. It was not reproduced after the
controlled shutdown/restart sequence, which produced the required red-to-idle
transition. Slow-TTS pushback was not demonstrated by this hardware run.

The run also exposed non-binding Agent/configuration findings outside the
microphone mapping result: the operator config required the new explicit
`conversation`, `shutdown`, websocket-capacity, and
`microphones.assistant_text_buffer_characters` fields before startup; one Home
Assistant imperative was misplanned as `query_state`; and one Wikipedia DSA
turn fetched its sources successfully but returned invalid final JSON. These
findings remain open and prevent treating this record as complete Gate D
evidence.

## Required verification order

Current automated evidence is organized as follows:

| Contract area | Current focused evidence |
|---|---|
| Core context outcomes, bridge state/event matrices, rendezvous, cancellation, race priority, entry/exit deadlines, backpressure, typed terminal results, and observability | `tests/test_conversation_protocol_conformance.py`, `tests/test_conversation_bridge.py`, `tests/test_sessions.py`, `tests/test_interfaces.py`, `tests/test_messages.py` |
| Agent factories, concurrent mutable-state isolation, and migrated Agent/DSA behavior | `tests/test_agent_factory.py`, `tests/test_orchestrator_agent.py`, other Agent and DSA unit modules under `tests/`, `orchestrator_and_dsa_tests/` |
| Websocket schema, state, stream correlation, transport handoff, admission/release, follow-up gate/lease, client policy, observability, and real delayed server/client transport | `tests/test_websocket_server.py`, `tests/test_messages.py`, `tests/test_config.py` |
| Voice mapping, follow-up arbitration, recovery, bounded rendering, and Microphone Protocol regressions | `tests/test_microphones.py`, `tests/test_microphone_protocol.py`, `tests/test_box3_esphome_microphone.py`, `tests/microphone_driver_conformance.py` |
| Application configuration, lifecycle construction, graceful signal shutdown, deadline escalation, second-signal escalation, and logging | `tests/test_config.py`, `tests/test_server_lifecycle.py`, `tests/test_server_logging.py` |

This inventory records test locations, not Gate D by itself. Subprocess hard-exit
proofs run in `tests/test_server_lifecycle.py`; the manual one-device hardware
checks below remain explicit closure evidence and may not be inferred from
ordinary pytest success.

Current automated checkpoint before the final closure re-review:

| Boundary | Exact regression evidence |
|---|---|
| InputSession acceptance/close matrix | `test_voice_input_session_accept_operation_matrix_rejects_every_non_idle_state`, `test_voice_session_close_wins_uncommitted_acceptance_and_is_idempotent`, `test_voice_session_close_after_accept_commit_releases_active_control_and_never_rearms`, `test_websocket_input_session_accept_operation_matrix_rejects_every_non_idle_state`, `test_websocket_close_matrix_quiesces_non_active_session_states`, `test_websocket_active_close_waits_for_conversation_scope_cleanup`, `test_websocket_protocol_closure_remains_closing_until_active_scope_exits`, `test_websocket_reader_failure_remains_closing_until_active_scope_exits` |
| Bridge ready-set and sink races | `test_race_operation_pairwise_and_multi_ready_precedence`, `test_race_operation_deadline_is_selected_only_when_neither_candidate_commits`, `test_terminal_input_preempts_each_inflight_sink_operation`, `test_terminal_input_is_observable_in_every_nonterminal_bridge_stage` |
| Repository client follow-up arbitration and presentation | `test_interactive_submission_before_and_after_expiry_boundary`, `test_interactive_submission_wins_at_exact_expiry_boundary`, `test_interactive_terminal_wins_when_terminal_and_line_are_both_committed`, `test_batch_follow_up_timer_is_cancelled_by_terminal_server_event`, `test_interactive_assistant_response_resets_dim_client_style` |
| Voice committed-media cancellation and timer origin | `test_voice_sink_cancellation_before_playback_commit_cancels_renderer`, `test_voice_sink_cancellation_at_playback_commit_drains_committed_renderer`, `test_processing_update_cancellation_drains_committed_playback`, `test_follow_up_cancellation_drains_committed_cue_without_starting_listening`, `test_follow_up_cancellation_stops_committed_listening_generation`, `test_voice_cleanup_immediately_after_follow_up_commit_stops_listening_generation`, `test_voice_cleanup_during_follow_up_capture_stops_capturing_generation`, `test_follow_up_deadline_is_fixed_at_listening_started_before_collector_scheduling`, `test_voice_follow_up_monotonic_before_equal_after_arbiter` |
| Websocket drain, lease, heartbeat, and capacity | `test_follow_up_drain_failure_after_handoff_closes_typed_committed_interval`, `test_follow_up_lease_expiry_during_writer_drain_closes_and_joins_tasks`, `test_follow_up_resource_lease_closes_without_forging_timeout`, `test_capacity_is_released_when_websocket_preparation_fails`, `test_capacity_is_released_after_invalid_handshake_and_timeout` |
| Full automated checkpoint | 707 passing pytest cases; `git diff --check` clean |
| Post-review behavioral checkpoint | `orchestrator_and_dsa_tests/run.sh --no-transcript`: 45/45 passing with orchestrator and DSA model `qwen3:14b`; 237.11 seconds |

1. Documentation structure, link, requirement-ID, and matrix consistency checks.
2. Focused core bridge, AgentConversation, websocket, client, mapping, and
   existing Microphone Protocol tests.
3. Entire pytest suite.
4. Subprocess fatal-containment and shutdown-signal tests.
5. `orchestrator_and_dsa_tests/run.sh --no-transcript` with the currently used
   model.
6. Real websocket server/client checks including capacity and follow-up lease.
7. Microphone runtime checks one device at a time: Box3, bedroom Voice Preview,
   office Voice Preview.
8. ESPHome validation, compilation, and generated `main.cpp` inspection only if
   firmware changes for a separately approved reason.

## Completion rule

T-004 cannot pass Gate D until every requirement above points to current passing
automated evidence or an explicitly recorded manual hardware result, all legacy
surfaces are absent, and the complete verification order succeeds. Renaming or
deleting a requirement requires updating its governing protocol and this
catalogue in the same change.
