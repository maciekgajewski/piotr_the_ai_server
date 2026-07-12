# Protocol Conformance Catalogue

## Status and scope

- **Authority:** Normative conformance plan
- **Audience:** Agents implementing or reviewing Conversation, Microphone, and mapping protocols
- **Read when:** Planning Stage 3 implementation, adding conformance tests, or reviewing protocol coverage

This catalogue maps stable normative requirements to their implementation owners and required Stage 3 tests. A requirement is not implemented until its automated test exists and passes.

Test filenames marked **new** are planned Stage 3 targets; implementation MAY choose a comparably focused filename but MUST update this catalogue in the same change.

## Conversation Protocol

| Requirement | Summary | Implementation owner | Required conformance test |
|---|---|---|---|
| `CP-ATTR-001` | `medium` is required and valid | `interfaces.py`, `sessions.py`, JSON parsing | `tests/test_interfaces.py`; websocket handshake cases |
| `CP-ATTR-002` | Session medium is immutable | `sessions.py` | new `tests/test_sessions.py`: reject medium change |
| `CP-ATTR-003` | Conversation cannot override medium | `sessions.py` | new `tests/test_sessions.py`: reject override |
| `CP-SESSION-001` | Emit readiness once on each `IDLE` entry | `sessions.py` | new `tests/test_sessions.py`: initial and repeated idle entry |
| `CP-SESSION-003` | One cleanup path for all orderly endings | `sessions.py` | parameterized termination-reason tests |
| `CP-SESSION-004` | Conversation state never leaks | `sessions.py` | consecutive Conversation isolation test |
| `CP-MESSAGE-001` | Begin/fragments/end ordering | `sessions.py`, endpoint helper | valid zero/one/many-fragment streams |
| `CP-MESSAGE-002` | Message IDs are unique | `sessions.py` | reject reused user and assistant IDs |
| `CP-MESSAGE-003` | At most one message per side is open | `sessions.py` | reject nested begins |
| `CP-MESSAGE-004` | Fragment/end ID must match | `sessions.py` | reject stale and mismatched IDs |
| `CP-MESSAGE-005` | No control/update/return inside assistant stream | `sessions.py`, `ConversationEndpoint` | each forbidden agent action fails invariant |
| `CP-FLOOR-001` | User and assistant streams do not overlap | `sessions.py` | reject endpoint input while output open |
| `CP-FLOOR-002` | Endpoint regains floor only after follow-up | `ConversationEndpoint` | read without follow-up fails/ends as specified; requested read succeeds |
| `CP-FOLLOWUP-001` | `request_follow_up()` only after complete input and closed output | `ConversationEndpoint` | forbidden-state assertions |
| `CP-FOLLOWUP-002` | Only one follow-up is outstanding | `ConversationEndpoint` | duplicate method-call assertion |
| `CP-FOLLOWUP-003` | Session supplies the sole effective deadline | Session and adapters | agents supply no timeout; websocket and voice adapters receive the exact Session value |
| `CP-ERROR-001` | External violation rejects and closes | websocket adapter, Session | `SessionRejected` precedes protocol close |
| `CP-ERROR-002` | Internal violation fails invariant | Session and trusted adapters | representative impossible internal events assert |
| `CP-OBS-001` | Every Session transition is context-logged | `sessions.py` | `caplog` verifies session, old/new state, event |

Additional exhaustive coverage:

- JSON round trips for every event and every optional-field shape in `tests/test_messages.py`.
- Unknown event, unknown field, wrong type, empty ID, invalid number, and explicit unexpected `null` rejection.
- Every state/event pair from the normative transition table.
- Endpoint closure in every non-terminal state.
- Normal single-turn, multi-assistant-message, follow-up, follow-up-timeout, and rejection sequences.

## Microphone Protocol

| Requirement | Summary | Implementation owner | Required conformance test |
|---|---|---|---|
| `MP-OWNER-001` | Driver does not own STT, Conversation, or re-arm | abstract interface and drivers | reusable driver conformance test; architecture review |
| `MP-ID-001` | Every listening generation has a new ID | manager | consecutive and retry generation tests |
| `MP-ID-002` | Every segment has a new associated utterance ID | drivers and manager | multiple open-mic segments have distinct IDs |
| `MP-ID-003` | Stale events cannot mutate state | manager and drivers | stale listen/utterance/playback/cue event tests |
| `MP-EVENT-001` | Capture and playback event vocabularies are separate | messages and interfaces | type-union and driver conformance tests |
| `MP-STATE-001` | Driver transition table is enforced | every driver | reusable state/event matrix tests |
| `MP-VISUAL-001` | `ERROR` is firmware-owned, not commandable | messages, drivers, firmware | reject `SetVisualState(ERROR)`; disconnect firmware validation |
| `MP-VISUAL-002` | Visual commands are state-independent and idempotent | drivers | duplicate commands in each connected driver state |
| `MP-VISUAL-003` | Manager explicitly commands connected visuals | manager | all normal sequences assert visual commands |
| `MP-VISUAL-004` | Driver/firmware does not infer main visual | drivers and firmware | callback tests prove no implicit main-state mutation |
| `MP-VISUAL-005` | Reconnect remains error until initial command | driver and firmware | disconnect/reconnect/first-command sequence |
| `MP-VISUAL-006` | Local indicators do not replace main visual | firmware | ESPHome generated-config checks and hardware acceptance |
| `MP-VISUAL-007` | Processing remains through playback | manager and drivers | visual sequence around begin/end/finished playback |
| `MP-OPENMIC-001` | First partial candidate immediately shows listening | manager partial-STT path | controlled timing test before speech/final end |
| `MP-OPENMIC-002` | Rejection resets candidate and idle, without re-arm | manager and driver | candidate rejection ordering and unchanged `listen_id` |
| `MP-OPENMIC-003` | Acceptance follows final validation | manager | order test: final result before processing/cue/forwarding |
| `MP-AUDIO-001` | Half-duplex output starts only when disarmed | manager and drivers | cue/playback rejected while listening/capturing |
| `MP-TIMEOUT-001` | Idle open mic has no segment timeout | manager | long idle listening remains armed without progress events |
| `MP-ERROR-001` | Untrusted protocol state is closed/recreated | manager | mismatch causes teardown, not guessed recovery |
| `MP-OBS-001` | Commands/events include state and correlation logs | manager and drivers | `caplog` command/event context tests |
| `MP-OBS-002` | Visual transitions include old/new/cause logs | manager and drivers | `caplog` visual transition test |

Additional exhaustive coverage:

- Every driver state/command/event combination from the normative transition table.
- `WAKE_WORD`, `OPEN_MIC`, and `FOLLOW_UP` normal sequences.
- Arm, segment, cue, playback, and connection timeout recovery.
- Nested segment, audio outside segment, wrong ID, implicit re-arm, and output-overlap violations.
- Reusable conformance suite run against Box3 and every Voice Preview driver implementation.
- ESPHome validation and generated-service inspection for all affected firmware entrypoints.

## Microphone-Conversation Mapping

| Requirement | Summary | Implementation owner | Required conformance test |
|---|---|---|---|
| `MAP-OWNER-001` | Adapter translates without duplicating Session lifecycle | manager/agent endpoint | focused boundary tests; no concrete driver branches |
| `MAP-SESSION-001` | Persistent microphone Session has `medium=voice` | manager | Session construction test |
| `MAP-SESSION-002` | Readiness arms only after output/cue/stop completes | manager | queued readiness while playback drains |
| `MAP-INPUT-001` | Accepted new input creates exactly one Conversation/message | manager/agent endpoint | wake and open-mic exact event sequence |
| `MAP-WAKE-001` | Wake without usable text creates no Conversation | manager | no-transcript regression test |
| `MAP-OPENMIC-001` | Accepted segment forwarded once despite many partials | manager | repeated-candidate exact-once test |
| `MAP-FOLLOWUP-001` | Follow-up uses Session-supplied deadline | Session/manager adapter | configured mismatch test uses event value |
| `MAP-FOLLOWUP-002` | Empty follow-up never creates a message | manager | retry within remaining time, then timeout |
| `MAP-OUTPUT-001` | Processing remains through synthesis/playback | manager | full visual and playback sequence |
| `MAP-END-001` | Accepted queued output drains before re-arm | manager | Conversation end before playback finish |
| `MAP-INVARIANT-001` | One accepted utterance equals one user message | manager | property/parameterized mode tests |
| `MAP-INVARIANT-002` | Rejected/empty input never reaches agent | manager | wake/open-mic/follow-up negative cases |
| `MAP-INVARIANT-003` | Re-arm requires Session event and finished output | manager | no autonomous re-arm tests |
| `MAP-OBS-001` | Cross-protocol logs carry all available IDs | manager | `caplog` translation context tests |

Additional exhaustive coverage:

- New wake-word Conversation, accepted open-mic Conversation, rejected candidate, no transcript, follow-up, timeout, processing update, assistant playback, and termination.
- Multiple assistant messages remain ordered and separately played.
- Empty assistant message is valid and produces no playback.
- Driver unavailability at every translation boundary.

## Firmware and hardware acceptance

Automated ESPHome validation and hardware checks MUST cover:

| Sequence | Expected main visual |
|---|---|
| Server disconnected | `ERROR` |
| Reconnected before first server visual command | `ERROR` |
| First connected initialization command | `IDLE` |
| Ordinary open-mic speech | remains `IDLE` |
| Partial wake candidate while speaking | immediate `LISTENING` |
| Final candidate rejection | `IDLE` |
| Accepted utterance and agent work | `PROCESSING` |
| Assistant playback | remains `PROCESSING` |
| Playback complete, no follow-up | `IDLE` |
| Playback complete, follow-up requested | `LISTENING` |
| Follow-up timeout cue complete | `IDLE` |
| Connection loss from any connected state | `ERROR` |

Voice Preview MUST render `ERROR` as low-light red, `IDLE` with LEDs off, `LISTENING` as pulsing blue, and `PROCESSING` as pulsing white. Box3 MUST render the corresponding error, idle, listening, and processing bitmaps.

Mute, setup, timer, volume, and other local indicators MUST be tested not to replace the connected main visual state.

## Required verification order

1. Focused message, Session, microphone, mapping, and driver conformance tests.
2. Entire pytest suite.
3. ESPHome validation and compilation for every affected entrypoint.
4. Generated firmware inspection for the required services and state ownership.
5. Entire `orchestrator_and_dsa_tests/` suite using the currently configured model.
6. Manual websocket protocol smoke test.
7. Hardware validation one device at a time: Box3, bedroom Voice Preview, office Voice Preview.

## Completion rule

Stage 3 is complete only when every requirement in this catalogue points to an existing passing test or an explicitly documented hardware verification result. Deleting or renaming a requirement requires updating its normative protocol and this catalogue in the same change.
