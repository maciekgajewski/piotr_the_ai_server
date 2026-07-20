# Websocket Conversation Protocol

## Status and scope

- **Authority:** Normative external binding implemented by T-004; T-005
  repository-client ownership reconciliation approved
- **Audience:** Maintainers of the websocket server, any websocket client,
  configuration, and transport conformance tests
- **Read when:** Changing websocket admission, JSON events, client wire
  behavior, heartbeat, follow-up binding, or websocket shutdown
- **Approval state:** T-004 external binding approved by Captain on 2026-07-19;
  T-005 removal of repository-specific client policy and retired-ID mapping
  approved with `docs/websocket-client-behavior.md` on 2026-07-20

This document binds one websocket connection to one persistent text
`InputSession` from the normative
[AI Server Conversation Bridge Protocol](ai-server-conversation-protocol.md).
It defines external JSON only. Core Conversation ordering and Agent behavior are
not duplicated here. The repository's interactive and batch client behavior is
defined separately by
[Websocket Client Behavior](websocket-client-behavior.md); external clients are
not bound by that repository-client contract.

The binding is versionless and is a clean break from the old
`session_attributes`, `new_conversation`, `message_begin`, `message_fragment`,
`message_end`, and endpoint-originated `conversation_ended` vocabulary.

Requirement identifiers use the `WS-` prefix.

## Ownership boundaries

- The websocket adapter owns HTTP admission, websocket framing, schema
  validation, background reading, transport writes, close codes, connection
  capacity, the follow-up resource lease, and the early-outcome gate.
- The external client owns any presentation of `follow_up_requested`, its choice
  of whether and when to produce a semantic timeout, and local arbitration
  between timeout and user submission.
- The core bridge owns Conversation state and MUST NOT inspect websocket frames,
  peer objects, queue sizes, heartbeats, or close codes.
- A websocket connection maps to one `InputSession` and has at most one active
  `InputConversation`. (`WS-OWNER-001`)
- The server MUST keep a background reader active independently of Agent work or
  output pushback so heartbeats, disconnect, and cancellation remain observable.
  (`WS-OWNER-002`)

## Terminology

- **Admission slot:** Capacity reserved atomically before HTTP upgrade.
- **Transport handoff:** The point at which a complete text frame is passed to
  the websocket transport; it may precede flow-control drain.
- **Application event commit:** Successful receipt and schema/state validation
  of one complete client text frame.
- **Follow-up gate:** Adapter-owned single-use gate which retains at most one
  validated ordinary follow-up outcome until bridge acknowledgement.
- **Resource lease:** Server-side non-semantic deadline limiting how long a
  committed follow-up interval holds connection capacity.
- **Protocol rejection:** A typed server event followed by binding closure.
- **Illustrative step:** A named sequence step which is not a typed event.

Every frame is UTF-8 text containing exactly one JSON object. Event names and
enum values use lowercase snake case. Unknown fields, explicit unexpected
`null`, duplicate JSON keys, non-text frames, and unknown event types are
invalid. (`WS-SCHEMA-001`)

## Typed event inventory

### Client to server

| Event | Fields | Constraints | Valid binding states | Core mapping |
|---|---|---|---|---|
| `session_start` | optional `user`, optional `area` | Each present value is a non-empty string | `HANDSHAKE` | Construct text `InputSessionContext` |
| `start_conversation` | `message` | Non-whitespace string | `ACCEPTING` after readiness | Accepted initial message |
| `follow_up_message` | `message` | Non-whitespace string | Committed follow-up interval | `UserMessage` |
| `follow_up_timed_out` | none | Client timer won local arbitration | Committed follow-up interval | `FollowUpTimedOut` |
| `cancel_conversation` | none | Idempotence is not implied; one active Conversation required | Any active Conversation state | `ConversationCancelled` |

`start_conversation` combines external start and complete initial text in one
event. No empty core Conversation is created. (`WS-CREATION-001`)

### Server to client

| Event | Fields | Constraints | Valid binding states | Core mapping |
|---|---|---|---|---|
| `session_accepted` | none | Exactly once after valid handshake | `HANDSHAKE` | InputSession opened |
| `conversation_ready` | none | Exactly once for each adapter `ACCEPTING` entry | `IDLE` | Readiness presentation |
| `conversation_started` | `conversation_id` | Fresh non-empty server ID | after accepted `start_conversation` | InputConversation returned |
| `processing_update` | none | No assistant message open | `ACTIVE` | `ProcessingUpdate` |
| `assistant_message_started` | `message_id` | Fresh non-empty server ID | `ACTIVE` | Sink `start()` commit |
| `assistant_text_chunk` | `message_id`, `text` | Matching open ID; non-empty text | `ACTIVE` | Sink `send_text()` commit |
| `assistant_message_completed` | `message_id` | Matching open ID | `ACTIVE` | Sink `complete()` commit |
| `assistant_message_aborted` | `message_id`, `reason`, optional `detail` | Matching open ID; closed abort enum | `ACTIVE`/`CLOSING` | Sink `abort()` commit |
| `follow_up_requested` | none | Zero or one outstanding interval | `ACTIVE` | Transactional follow-up request |
| `conversation_ended` | `reason`, optional `context_rejection_code`, optional `detail` | Conditional code invariant below | `ACTIVE`/`CLOSING` | Core terminal event |
| `protocol_rejected` | `code`, optional `detail` | Closed rejection code | Any non-closed state when safe | Binding failure |

`message_id` is binding-local because the in-process assistant stream is
unambiguous without it. It correlates external stream frames and MUST NOT be
reused during the connection lifetime. (`WS-STREAM-001`)

`conversation_ended.context_rejection_code` is required exactly when `reason`
is `context_rejected` and omitted otherwise. Its values are `unknown_user`,
`not_authorized`, and `unsupported_input_context`. Optional `detail` is
diagnostic and clients MUST NOT parse it for control flow. (`WS-TERMINAL-001`)

`assistant_message_aborted.reason` is one of `input_cancelled`, `agent_failed`,
`input_failed`, `input_session_closed`, or `internal_failure`.

`protocol_rejected.code` is one of `invalid_json`, `invalid_event`,
`invalid_state`, `message_too_large`, `duplicate_follow_up_outcome`, or
`ingress_overflow`.

## JSON schemas

The tables above are normative. These examples show every field shape; spaces
are insignificant and omitted optional fields are not serialized as `null`.

```json
{"type":"session_start","user":"Maciek","area":"office"}
{"type":"start_conversation","message":"Cześć"}
{"type":"follow_up_message","message":"Tak"}
{"type":"follow_up_timed_out"}
{"type":"cancel_conversation"}
```

```json
{"type":"session_accepted"}
{"type":"conversation_ready"}
{"type":"conversation_started","conversation_id":"c-123"}
{"type":"processing_update"}
{"type":"assistant_message_started","message_id":"a-123"}
{"type":"assistant_text_chunk","message_id":"a-123","text":"Cześć"}
{"type":"assistant_message_completed","message_id":"a-123"}
{"type":"assistant_message_aborted","message_id":"a-123","reason":"input_cancelled"}
{"type":"follow_up_requested"}
{"type":"conversation_ended","reason":"completed"}
{"type":"conversation_ended","reason":"context_rejected","context_rejection_code":"unknown_user","detail":"configured identity is unknown"}
{"type":"protocol_rejected","code":"invalid_state","detail":"follow_up_message without an active follow-up interval"}
```

## State inventory

| State | Meaning |
|---|---|
| `HANDSHAKE` | Upgrade succeeded; `session_start` is required |
| `IDLE` | Session accepted; no readiness operation or Conversation is active |
| `ACCEPTING` | `conversation_ready` was handed off and one complete initial message may be accepted |
| `ACTIVE` | One InputConversation exists and no follow-up transport commit is pending |
| `FOLLOW_UP_COMMITTING` | `follow_up_requested` crossed handoff; bridge acknowledgement is pending |
| `AWAITING_FOLLOW_UP` | Matching token was acknowledged after bridge entered its follow-up state |
| `CLOSING` | Rejection, shutdown, lease expiry, or transport failure committed |
| `CLOSED` | Slot released and no further operation is legal |

Client UI states are not server protocol states. Every client remains
responsible for sending events only in the legal binding states. The repository
clients use the separate state model in
[Websocket Client Behavior](websocket-client-behavior.md).

## Complete transition tables

### Client-event matrix

`V` is valid, `R` means reject and close, `G` means accept into the one-value
follow-up gate, and `I` is inapplicable because the binding is terminal.

| Client event | `HANDSHAKE` | `IDLE` | `ACCEPTING` | `ACTIVE` | `FOLLOW_UP_COMMITTING` | `AWAITING_FOLLOW_UP` | `CLOSING` | `CLOSED` |
|---|---|---|---|---|---|---|---|---|
| `session_start` | V -> `IDLE` | R | R | R | R | R | I | I |
| `start_conversation` | R | R | V -> `ACTIVE` | R | R | R | I | I |
| `follow_up_message` | R | R | R | R | G, at most one | V -> `ACTIVE` | I | I |
| `follow_up_timed_out` | R | R | R | R | G, at most one | V -> `ACTIVE` | I | I |
| `cancel_conversation` | R | R | R | V, remain active until core end | V, bypass gate | V, bypass gate | I | I |
| malformed/unknown/oversized event | R | R | R | R | R | R | I | I |
| transport close | V -> `CLOSED` | V -> `CLOSED` | V -> `CLOSED` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSED` | I |

A second ordinary outcome in either follow-up state is rejected as
`duplicate_follow_up_outcome`; it is never queued. `cancel_conversation` and
transport closure bypass and clear the ordinary outcome register.
(`WS-FOLLOWUP-001`)

### Server/core-result matrix

| Server event/result | `HANDSHAKE` | `IDLE` | `ACCEPTING` | `ACTIVE` | `FOLLOW_UP_COMMITTING` | `AWAITING_FOLLOW_UP` | `CLOSING` | `CLOSED` |
|---|---|---|---|---|---|---|---|---|
| handshake accepted | V -> `IDLE` | P | P | P | P | P | P | I |
| readiness exposed | P | V -> `ACCEPTING` | P | P | P | P | P | I |
| Conversation accepted | P | P | V -> `ACTIVE` | P | P | P | P | I |
| processing/assistant stream frame | P | P | P | V, no binding transition | P | P | P | I |
| `follow_up_requested` handoff | P | P | P | V -> `FOLLOW_UP_COMMITTING` | P | P | P | I |
| matching bridge acknowledgement | P | P | P | P | V -> `AWAITING_FOLLOW_UP` or release retained outcome | P | P | I |
| Conversation terminal outcome | P | P | P | V -> `IDLE` | V -> `IDLE` | V -> `IDLE` | delivery best effort | I |
| protocol rejection | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | no duplicate | I |
| shutdown/lease/transport failure | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V | I |

`P` identifies an internal adapter/core integration violation. It invokes the
core internal-failure containment policy rather than sending a misleading client
rejection.

## Invariants

1. Admission capacity is reserved before HTTP upgrade and released exactly once
   by the connection handler. (`WS-CAPACITY-001`)
2. The reader validates complete events into a bounded ingress path; it never
   waits for Agent or writer progress.
3. At most one Conversation is active. The bounded ingress path does not
   authorize input for a future state or Conversation; schema and binding-state
   validation happen before enqueue.
4. Server writes are serialized. There is no unbounded output queue between the
   bridge and websocket transport. (`WS-BACKPRESSURE-001`)
5. Start, chunk, completion, abort, terminal, and follow-up operations commit at
   transport handoff. Flow-control drain is later and remains awaited.
6. Once transport handoff may have begun, the writer operation is shielded from
   cancellation until it drains or fails. Abort may win only before handoff.
   (`WS-COMMIT-001`)
7. A committed follow-up frame atomically creates its token, opens its gate, and
   starts its resource lease.
8. The bridge state change precedes synchronous acknowledgement. Only then may a
   retained outcome be exposed to the bridge. (`WS-FOLLOWUP-002`)
9. Heartbeats, ping, pong, and TCP activity do not extend the follow-up resource
   lease.
10. Old vocabulary is rejected, never translated. (`WS-COMPAT-001`)

## Required configuration, bounds, and admission

The following fields are required with no built-in defaults:

| Field | Constraint | Purpose |
|---|---|---|
| `websocket.max_connections` | positive integer | Pre-upgrade capacity |
| `websocket.capacity_retry_after_seconds` | positive integer | HTTP retry guidance |
| `websocket.follow_up_idle_lease_seconds` | positive finite number | Non-semantic resource lease |
| `websocket.max_frame_bytes` | positive integer | Complete UTF-8 frame bound |
| `websocket.ingress_queue_capacity` | positive integer | Validated reader-to-adapter bound |
| `websocket.heartbeat_seconds` | positive finite number | Transport heartbeat policy |
| `websocket.handshake_timeout_seconds` | positive finite number | Initial `session_start` deadline |

Missing, Boolean, zero, negative, non-integer integer fields, or non-finite
numeric fields MUST fail server startup. (`WS-CONFIG-001`)

The server atomically reserves a slot before upgrading. When full, it MUST return
HTTP `503 Service Unavailable`, MUST include `Retry-After` equal to the configured
integer, and MUST NOT upgrade, wait, or queue the request. (`WS-CAPACITY-002`)

An event whose encoded frame exceeds `max_frame_bytes` is rejected with close
code `1009`. If the validated ingress queue is full, the adapter sends
`protocol_rejected(code="ingress_overflow")` when safe and closes with `1008`.
It MUST NOT drop or overwrite events.

## Follow-up outcomes and resource lease

The server sends no semantic timeout and imposes no client presentation or
local timer policy. After receiving `follow_up_requested`, a client may send at
most one ordinary outcome: either `follow_up_message` or
`follow_up_timed_out`. It may instead cancel the Conversation or close the
connection. The binding does not require a client to implement interactive
input, a local timeout, or any particular arbitration policy.

The repository clients' 15-second default and deterministic local arbitration
are defined by `WSC-FOLLOWUP-001` and `WSC-FOLLOWUP-002` in
[Websocket Client Behavior](websocket-client-behavior.md).

On the server, the resource lease starts at follow-up transport handoff. One
validated outcome committed after handoff may be retained while flow-control
drain or bridge acknowledgement is pending. An outcome validated before handoff
is an external violation.

Application-event validation and lease expiry are serialized by one monotonic
arbiter. Valid input committed at or before the deadline wins, including equal
timestamps. If expiry commits first, the server emits no
`FollowUpTimedOut`; it closes with `1013` and exact reason
`follow-up resource lease expired`. (`WS-LEASE-001`)

Pre-handoff send failure creates no token or interval. Post-handoff drain
failure, lease expiry during drain, cancellation, and connection close clear the
register and expose only terminal input control to the bridge.

## Timeouts and cancellation

- Handshake timeout closes with `1008` and reason `session start timed out`.
- Heartbeat failure and transport disconnect map to `InputSessionClosed`.
- Client `cancel_conversation` maps to `ConversationCancelled`; it does not
  close the persistent websocket session by itself.
- Application shutdown closes admitted websockets with `1001` and reason
  `server shutting down` where transport permits.
- Cancellation of a server coroutine after possible write handoff cannot
  reclassify the frame as unsent.
- There is no server timer that produces semantic `FollowUpTimedOut`.

## Failure, rejection, and recovery

For an external violation, the adapter MUST send `protocol_rejected` when a safe
serialized write remains possible, then close and release the slot. It MUST NOT
continue by guessing intent. (`WS-ERROR-001`)

| Condition | Event when safe | Close code | Exact/derived reason |
|---|---|---|---|
| Invalid JSON/schema/state | `protocol_rejected` | `1002` | rejection code |
| Oversized frame | `protocol_rejected(message_too_large)` | `1009` | `message too large` |
| Ingress overflow | `protocol_rejected(ingress_overflow)` | `1008` | `ingress overflow` |
| Handshake timeout | none required | `1008` | `session start timed out` |
| Follow-up lease expiry | none | `1013` | `follow-up resource lease expired` |
| Normal client closure | none | `1000` | transport-defined |
| Application shutdown | none required | `1001` | `server shutting down` |
| Unexpected adapter/internal failure | best effort terminal event | `1011` | `internal failure` |

After a binding-level rejection or transport failure, the InputSession is not
reused. Conversation-local core outcomes such as context rejection or Agent
failure end the Conversation and may return the connection to `IDLE`.

## Normal sequences

### One-turn Conversation

```text
HTTP upgrade after capacity reservation
C -> S  session_start
S -> C  session_accepted
S -> C  conversation_ready
C -> S  start_conversation(message)
S -> C  conversation_started(conversation_id)
S -> C  assistant_message_started(message_id)
S -> C  assistant_text_chunk(message_id, text)*
S -> C  assistant_message_completed(message_id)
S -> C  conversation_ended(completed)
S -> C  conversation_ready
```

### Early follow-up response while drain blocks

```text
Server hands follow_up_requested to transport, atomically opening token/gate/lease
Client accepts it and sends follow_up_message
Background reader validates and retains that one value
Server writer drain completes
Bridge enters WAITING_FOR_FOLLOW_UP and synchronously acknowledges token
Adapter exposes retained UserMessage to bridge
```

### Capacity rejection

```text
connection count is at websocket.max_connections
new HTTP request receives 503 plus configured Retry-After
no websocket upgrade and no InputSession occurs
```

## Invalid sequences

- Sending any event other than `session_start` during handshake;
- sending old event names;
- starting a Conversation before `conversation_ready` or while one is active;
- sending whitespace-only initial or follow-up text;
- sending follow-up input before follow-up transport handoff;
- sending both timeout and text for one interval;
- sending a late timeout after a submitted response;
- sending client frames while assistant output owns the turn, except cancellation;
- accepting more connections than the configured atomic capacity;
- extending the lease on heartbeat traffic;
- exposing a retained outcome before bridge acknowledgement;
- cancelling a writer after possible handoff and reporting its frame as unsent;
- continuing after a protocol rejection.

## Observability requirements

- Every connection log MUST use
  `WebsocketInputSession[<peer>:<input_session_id>]`. (`WS-OBS-001`)
- Admission reserve/reject/release, upgrade, handshake, every binding transition,
  frame validation without message content, write handoff, drain, gate state,
  lease start/cancel/expiry, rejection, close, and slot release MUST be logged on
  DEBUG.
- Admission rejection, session acceptance, Conversation start/end, protocol
  rejection, lease enforcement, and unexpected transport failure MUST be logged
  briefly at INFO or WARNING as appropriate.
- Logs MUST distinguish raw-frame receipt from validated application-event
  commit, and transport handoff from drain completion.
- Transcript and assistant content MUST follow project privacy policy and is not
  required for state reconstruction.

## Implementation and conformance references

Implementation owners:

- `ai_server/websocket_server.py`;
- `ai_server/websocket_messages.py`;
- websocket and shutdown fields in `ai_server/config.py` and examples.

Repository client implementation and conformance references are listed in
[Websocket Client Behavior](websocket-client-behavior.md).

Conformance tests are listed in the
[Protocol Conformance Catalogue](protocol-conformance-catalogue.md), especially
websocket schema/state, capacity, follow-up gate/lease, transport commit, and
live transport coverage.

## Compatibility policy

There is no legacy parser, alias, protocol version, or negotiation. Server and
all repository clients switch to this vocabulary in the same atomic cutover.
Unknown and old events are external protocol violations.

## Explicitly unresolved decisions

None. Captain approved the T-004 binding on 2026-07-19 and the T-005 ownership
reconciliation and retired-ID mapping with the separate client contract on
2026-07-20. Verification and conformance evidence remain tracked by T-004 and
T-005 respectively.

## Retired requirement identifiers

- `WS-FOLLOWUP-003`, which required the repository-client 15-second default and
  positive finite override, was replaced by strengthened `WSC-FOLLOWUP-001` on
  2026-07-20; the old identifier MUST NOT be reused.
- `WS-FOLLOWUP-004`, which required repository-client submission-versus-timeout
  arbitration, was replaced by strengthened `WSC-FOLLOWUP-002` on 2026-07-20;
  the old identifier MUST NOT be reused.
