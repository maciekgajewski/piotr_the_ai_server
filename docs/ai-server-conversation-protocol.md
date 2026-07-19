# AI Server Conversation Bridge Protocol

## Status and scope

- **Authority:** Normative runtime contract; implemented by T-004
- **Audience:** Maintainers of conversation state, agents, input adapters, application lifecycle, and conformance tests
- **Read when:** Changing conversation messages or interfaces, agent construction, input supervision, websocket or microphone bindings, or shutdown behavior
- **Approval state:** Approved by Captain on 2026-07-19

This document defines the typed, in-process protocol between one persistent
`InputSession`, one per-conversation bridge coroutine, and one
`AgentConversation`. It replaces the old shared `Session`/`ConversationEndpoint`
contract. T-004 performed the atomic production cutover on 2026-07-19; runtime
drift from this document is now a defect.

Websocket JSON is defined by the
[Websocket Conversation Binding](websocket-conversation-protocol.md). Voice
translation is defined by the
[Microphone-Conversation Mapping](microphone-conversation-mapping.md). The
manager-to-driver contract remains the separate
[Microphone Protocol](microphone-protocol.md).

Requirement identifiers use the `CP-` prefix.

## Ownership boundaries

- One bridge coroutine MUST own all cross-side state for one Conversation.
  (`CP-OWNER-001`)
- One persistent `InputSession` MUST have zero or one active
  `InputConversation`. (`CP-OWNER-002`)
- One bridge MUST connect exactly one `InputConversation` to exactly one
  `AgentConversation`; neither side may be replaced during the Conversation.
  (`CP-OWNER-003`)
- A shared `Agent` factory MAY serve concurrent Conversations, but every opened
  `AgentConversation` MUST own isolated mutable state. (`CP-OWNER-004`)
- Input acceptance, presentation, media cleanup, and follow-up timing belong to
  the input adapter. Agent execution and agent-local work belong to the
  `AgentConversation`. Cross-side sequencing belongs only to the bridge.
- Application admission and graceful shutdown belong above the Conversation
  protocol. Shutdown closes `InputSession` objects; it does not add a shutdown
  event to the Conversation vocabulary.
- Core code MUST use only sealed directional interfaces. It MUST NOT inspect a
  concrete input adapter, transport, microphone, driver, agent, prompt, or
  device service. (`CP-OWNER-005`)

## Terminology and typed context

- **InputSession:** Persistent input-adapter scope which accepts sequential
  Conversations.
- **InputConversation:** Input-owned, per-Conversation control and output scope.
- **Bridge:** The sole cross-side state machine for one Conversation.
- **Agent:** Long-lived factory for isolated Agent Conversations.
- **AgentConversation:** Active per-Conversation agent endpoint.
- **Assistant sink:** Input-owned transactional output interface.
- **Commit point:** Binding-owned boundary after which an operation cannot be
  reclassified as not having happened.
- **Protocol violation:** A source/event/state combination classified `P` in a
  complete matrix below.
- **Internal failure:** A broken sealed-interface or bridge invariant which
  requires application-wide fatal containment.

IDs are non-empty opaque strings from one common process-wide ID factory and
MUST NOT be reused during the process lifetime.

The following contexts are immutable typed values. Optional values are explicit
fields; there is no generic attributes or extension dictionary.

| Context | Required fields | Optional fields | Owner |
|---|---|---|---|
| `InputSessionContext` | `input_session_id`, `medium` | `user`, `area` | Input adapter |
| `InputConversationContext` | `conversation_id`, `input_session_id`, `medium` | `user`, `area` | Input adapter |
| `ConversationContext` | all resolved identity fields plus immutable `user_settings` | `user`, `area` | Context provider |

`medium` is the closed enum `TEXT` or `VOICE`. Context fields MUST remain
unchanged for their scope. Mutable agent state MUST NOT be stored in any context
or shared with another `AgentConversation`. (`CP-CONTEXT-001`)

The synchronous context provider has this closed result set:

| Result | Fields | Meaning |
|---|---|---|
| `ContextResolved` | `context` | Continue Agent entry |
| `ContextRejected` | `code`, optional `detail` | Valid request is rejected |
| `ContextUnavailable` | optional `detail` | Required maintained snapshot is temporarily unavailable |

`ContextRejectionCode` is the closed enum `UNKNOWN_USER`, `NOT_AUTHORIZED`, or
`UNSUPPORTED_INPUT_CONTEXT`. `resolve()` MUST perform no I/O, await, retry, task
creation, or mutable external lookup. (`CP-CONTEXT-002`)

## Typed event and operation inventory

### InputConversation to bridge events

`initial_message` is a required immutable field on `InputConversation`, not an
event on the active control stream.

| Event | Fields and constraints | Sender | Receiver | Valid states |
|---|---|---|---|---|
| `UserMessage` | `text`: non-whitespace string | InputConversation | Bridge | `WAITING_FOR_FOLLOW_UP` |
| `FollowUpTimedOut` | none | InputConversation | Bridge | `WAITING_FOR_FOLLOW_UP` |
| `ConversationCancelled` | none | InputConversation | Bridge | Every non-terminal bridge state |
| `InputConversationFailed` | optional diagnostic `detail` | InputConversation | Bridge | Every non-terminal bridge state |
| `InputSessionClosed` | optional diagnostic `detail` | InputConversation | Bridge | Every non-terminal bridge state |

The adapter MUST serialize follow-up response and timeout locally so at most one
ordinary follow-up outcome reaches the bridge. (`CP-INPUT-001`)

### Bridge to InputConversation operations

| Operation | Arguments/result | Valid states |
|---|---|---|
| `assistant_output.start()` | `AssistantSinkStarted` or `InputSessionClosed` | `WAITING_FOR_AGENT` |
| `assistant_output.send_text(chunk)` | non-empty text; `AssistantTextAccepted` or `InputSessionClosed` | `STREAMING_ASSISTANT` |
| `assistant_output.complete()` | `COMPLETED`, `ABORTED`, or `INPUT_SESSION_CLOSED` | `STREAMING_ASSISTANT` |
| `assistant_output.abort(reason, detail)` | `ABORTED`, `COMPLETED`, or `INPUT_SESSION_CLOSED` | Any non-terminal state after sink start |
| `request_follow_up()` | `FollowUpRequestCommitted` or `InputSessionClosed` | `COMMITTING_FOLLOW_UP` |
| `acknowledge_follow_up_ready(token)` | synchronous, no return value | Transition to `WAITING_FOR_FOLLOW_UP` only |
| `end_conversation(event)` | bounded best effort | `ENDING` |

`FollowUpRequestCommitted` is a single-use token scoped to one follow-up
interval. The adapter MUST NOT expose an ordinary follow-up outcome before the
matching token is acknowledged. (`CP-FOLLOWUP-001`)

The assistant sink is a transactional state machine owned by the input adapter:

| Sink state | Legal operations |
|---|---|
| `NOT_STARTED` | `start()` |
| `OPEN` | `send_text()`, `complete()`, `abort()` |
| `COMPLETING` | completion may commit; `abort()` may win only before the binding commit point |
| `COMPLETED` | cleanup and repeated terminal inspection only |
| `ABORTED` | cleanup and repeated terminal inspection only |

Exactly one of completion or abort commits. Cancellation of an awaiting bridge
task MUST NOT erase a binding commit that already occurred. (`CP-SINK-001`)

### Bridge to AgentConversation operations

| Operation | Arguments/result | Valid states |
|---|---|---|
| `send_user_message(message)` | complete `UserMessage`; returns `AgentInputAccepted` at acceptance commit | `DELIVERING_USER_MESSAGE` |
| `cancel(reason)` | `AgentCancellationReason`; returns `AgentCancellationAcknowledged` after all owned work is quiescent | Every non-terminal state after Agent entry starts |

Both operations are idempotent only where explicitly stated: `cancel()` is
idempotent and the first reason wins; `send_user_message()` is one offer for one
turn and MUST NOT be retried after its acceptance status is uncertain.

### AgentConversation to bridge events

| Event | Fields and constraints | Sender | Receiver | Valid states |
|---|---|---|---|---|
| `ProcessingUpdate` | no response content | AgentConversation | Bridge | `WAITING_FOR_AGENT` before a stream starts |
| `AssistantMessageStarted` | none | AgentConversation | Bridge | `WAITING_FOR_AGENT`; zero or one per turn |
| `AssistantTextChunk` | non-empty `text` | AgentConversation | Bridge | `STREAMING_ASSISTANT` |
| `AssistantMessageCompleted` | none | AgentConversation | Bridge | `STREAMING_ASSISTANT`; exactly once for an opened stream |
| `TurnDisposition` | `END_CONVERSATION` or `REQUEST_FOLLOW_UP` | AgentConversation | Bridge | `WAITING_FOR_AGENT` or `WAITING_FOR_DISPOSITION`; exactly once after successful turn |
| `AgentConversationFailed` | optional diagnostic `detail` | AgentConversation | Bridge | Any active Agent state |

An Agent turn may produce no assistant stream, but successful completion always
requires one explicit `TurnDisposition`. Agent failure and cancellation are
terminal alternatives and produce no disposition. (`CP-AGENT-001`)

Agent output MUST use a zero-capacity rendezvous. Production blocks until the
bridge receives the event; no Agent-owned unbounded output queue is permitted.
(`CP-BACKPRESSURE-001`)

### Bridge to input terminal event

```python
ConversationEnded(
    reason: ConversationEndReason,
    context_rejection_code: ContextRejectionCode | None = None,
    detail: str | None = None,
)
```

`ConversationEndReason` is the closed enum `COMPLETED`, `INPUT_CANCELLED`,
`FOLLOW_UP_TIMEOUT`, `CONTEXT_REJECTED`, `CONTEXT_UNAVAILABLE`, `AGENT_FAILED`,
`INPUT_FAILED`, `INPUT_SESSION_CLOSED`, or `INTERNAL_FAILURE`.

`context_rejection_code` is required exactly for `CONTEXT_REJECTED` and
forbidden for every other reason. `detail` is diagnostic and MUST NOT be parsed
for control flow. (`CP-TERMINAL-001`)

The separate `AssistantAbortReason` enum is `INPUT_CANCELLED`, `AGENT_FAILED`,
`INPUT_FAILED`, `INPUT_SESSION_CLOSED`, or `INTERNAL_FAILURE`.

## State inventory

### InputSession states

| State | Meaning |
|---|---|
| `IDLE` | No readiness operation or Conversation is active |
| `ACCEPTING` | `accept_conversation().__aenter__()` is presenting readiness and preparing complete accepted input |
| `ACTIVE` | One ready `InputConversation` was returned |
| `CLOSING` | Closure committed and pending operations are being released |
| `CLOSED` | Terminal persistent-session state |

### Bridge states

| State | Meaning |
|---|---|
| `STARTING` | Context resolution and Agent entry are in progress |
| `DELIVERING_USER_MESSAGE` | One complete user turn is being offered to Agent |
| `WAITING_FOR_AGENT` | User input was accepted; no assistant stream is open |
| `STREAMING_ASSISTANT` | One assistant stream is open |
| `WAITING_FOR_DISPOSITION` | Assistant stream completed; disposition is required |
| `COMMITTING_FOLLOW_UP` | Follow-up presentation is committing or draining |
| `WAITING_FOR_FOLLOW_UP` | Input-side presenter owns the follow-up interval |
| `ENDING` | Terminal notification and scoped cleanup are in progress |
| `CLOSED` | All scoped work is quiescent |

## Complete transition tables

### InputSession operation matrix

`V` is valid with the stated transition; `P` is a protocol violation; `I` is
inapplicable because the session is terminal.

| Operation/result | `IDLE` | `ACCEPTING` | `ACTIVE` | `CLOSING` | `CLOSED` |
|---|---|---|---|---|---|
| call `accept_conversation()` | V -> `ACCEPTING` | P | P | P | I |
| accepted complete input | P | V -> `ACTIVE` | P | P | I |
| recoverable pre-Conversation rejection/failure | P | V -> `IDLE` | P | P | I |
| InputSession becomes unavailable | V -> `CLOSED` | V -> `CLOSED` | V -> `CLOSING` | V -> `CLOSED` | I |
| InputConversation context exits normally | P | P | V -> `IDLE` | V -> `CLOSED` | I |
| call `close()` | V -> `CLOSING` | V -> `CLOSING` | V -> `CLOSING` | V, await same close | I |

`accept_conversation().__aenter__()` MUST return only after it has atomically
created a ready `InputConversation` with a complete non-whitespace
`initial_message`, immutable context, a fresh Conversation ID, live control
receive, and live output operations. (`CP-SESSION-001`)

Close wins if acceptance and close are simultaneously eligible. If active
creation already committed, the active control receive resolves
`InputSessionClosed`; otherwise no core Conversation is exposed.
(`CP-SESSION-002`)

The first `close()` call MUST commit `CLOSING` before its first suspension,
release pending acceptance or control receive, and own the one close result.
Repeated calls await that same result. InputConversation context exit MUST finish
or abort assistant output and media cleanup before `ACTIVE` can return to
`IDLE`; it cannot restore `IDLE` after close has committed.
(`CP-SESSION-003`)

### Bridge source/event/state matrix

Codes are: `V` valid; `P` protocol violation; `T` terminal input valid and
pre-emptive; `F` internal fatal result; `I` inapplicable in terminal states.
Rows explicitly cover every typed source event and operation result.

| Source event/result | `STARTING` | `DELIVERING` | `WAITING_AGENT` | `STREAMING` | `WAITING_DISP` | `COMMIT_FU` | `WAITING_FU` | `ENDING` | `CLOSED` |
|---|---|---|---|---|---|---|---|---|---|
| `ContextResolved` | V: enter Agent | P | P | P | P | P | P | P | I |
| `ContextRejected` | V -> `ENDING` | P | P | P | P | P | P | P | I |
| `ContextUnavailable` | V -> `ENDING` | P | P | P | P | P | P | P | I |
| malformed/exceptional context result | F | P | P | P | P | P | P | P | I |
| Agent entry success | V -> `DELIVERING` | P | P | P | P | P | P | P | I |
| `AgentInputAccepted` | P | V -> `WAITING_AGENT` | P | P | P | P | P | P | I |
| Agent handoff/entry failure | V -> `ENDING` | V -> `ENDING` | P | P | P | P | P | P | I |
| `ProcessingUpdate` | P | P | V, no transition | P | P | P | P | P | I |
| `AssistantMessageStarted` | P | P | V -> `STREAMING` | P | P | P | P | P | I |
| `AssistantTextChunk` | P | P | P | V, no transition | P | P | P | P | I |
| `AssistantMessageCompleted` | P | P | P | V -> `WAITING_DISP` after sink commit | P | P | P | P | I |
| `TurnDisposition(END)` | P | P | V -> `ENDING` | P | V -> `ENDING` | P | P | P | I |
| `TurnDisposition(FOLLOW_UP)` | P | P | V -> `COMMIT_FU` | P | V -> `COMMIT_FU` | P | P | P | I |
| `AgentConversationFailed` | V -> `ENDING` | V -> `ENDING` | V -> `ENDING` | V -> `ENDING` and abort | V -> `ENDING` | V -> `ENDING` | V -> `ENDING` | P | I |
| `FollowUpRequestCommitted` | P | P | P | P | P | V -> `WAITING_FU` plus synchronous ack | P | P | I |
| follow-up `UserMessage` | P | P | P | P | P | P | V -> `DELIVERING` | P | I |
| `FollowUpTimedOut` | P | P | P | P | P | P | V -> `ENDING` | P | I |
| `ConversationCancelled` | T | T | T | T | T | T | T | P | I |
| `InputConversationFailed` | T | T | T | T | T | T | T | P | I |
| `InputSessionClosed` | T | T | T | T | T | T | T | V, affects delivery | I |
| sink operation failure/inconsistent result | F | F | F | F | F | F | F | F | I |
| bridge invariant failure | F | F | F | F | F | F | F | F | I |

`DELIVERING`, `WAITING_AGENT`, `STREAMING`, `WAITING_DISP`, `COMMIT_FU`, and
`WAITING_FU` abbreviate the full state names only inside this matrix.

The bridge MUST inspect the complete ready set at each race decision and apply
this priority: `INTERNAL_FAILURE`, `INPUT_SESSION_CLOSED`, `INPUT_FAILED`,
`INPUT_CANCELLED`, `CONTEXT_UNAVAILABLE`, `CONTEXT_REJECTED`, `AGENT_FAILED`,
`FOLLOW_UP_TIMEOUT`, then ordinary Agent acceptance/output/disposition.
(`CP-RACE-001`)

Priority does not undo a commit which already occurred. If input termination and
`AgentInputAccepted` are ready together, terminal input wins the state
transition, the accepted result remains true, and the bridge uses acknowledged
Agent cancellation.

## Invariants

1. A Conversation begins only after input acceptance has produced complete,
   non-whitespace initial text. The core never waits for an initial message.
   (`CP-CREATION-001`)
2. The bridge starts one continuously pending input-control receive before
   context resolution and replaces it immediately after every consumed
   non-terminal control event. (`CP-CANCEL-001`)
3. Input terminal control remains observable during Agent entry, every Agent
   input handoff, Agent output, every sink operation, and follow-up commitment.
4. The bridge contains no assistant-output queue. It awaits each input sink
   operation before receiving the next ordinary Agent event.
   (`CP-BACKPRESSURE-002`)
5. There is zero or one assistant stream per turn; chunks are ordered; progress
   cannot interleave with text; successful turns have exactly one disposition.
6. Follow-up intent contains no timeout. Only the component that presents the
   offer owns its semantic timer. (`CP-FOLLOWUP-002`)
7. The state transition to `WAITING_FOR_FOLLOW_UP` and acknowledgement of the
   matching token occur synchronously with no intervening suspension point.
8. Every owned watcher, handoff, model, tool, producer, and renderer operation is
   joined or contained before scoped context exit. (`CP-LIFETIME-001`)
9. Sequential Conversations on one InputSession do not overlap cleanup or
   readiness; concurrent InputSessions may run independent bridges.
10. Internal JSON-like dictionaries omit fields whose values are `None` unless
    an external protocol explicitly requires `null`.
11. Agent context entry is an owned task raced against the already-pending input
    control receive. Input terminal control cancels and joins entry. A committed
    entry is registered for exact-once exit; an uncommitted `__aenter__()` owns
    cleanup of every partially acquired resource because Python will not invoke
    `__aexit__()` for it. (`CP-STARTUP-001`)
12. Each `send_user_message()` is an owned rendezvous raced against input
    control. Cancellation before acceptance commit guarantees the message cannot
    later be accepted. Acceptance at or before cancellation remains committed,
    and the bridge then uses acknowledged Agent cancellation.
    (`CP-HANDOFF-001`)

## Timeouts, cancellation, and application shutdown

The core owns no user follow-up timer. A missing presenter outcome may leave the
bridge in `WAITING_FOR_FOLLOW_UP`; bindings MUST define resource containment.

Agent entry and acknowledged cancellation share a required positive finite
`conversation.agent_cancellation_deadline_seconds`. The value has no built-in
fallback. Deadline expiry is a sealed-lifetime failure and invokes the
application-wide fatal termination controller. (`CP-CANCEL-002`)

Fatal terminal notification uses a separate required positive finite
`conversation.fatal_notification_seconds`. After this bound the process exits
non-zero without waiting for escaped Agent work.

Application shutdown uses required positive finite
`shutdown.grace_period_seconds`, with no built-in fallback. On the first
`SIGINT` or `SIGTERM`, the application MUST atomically close admission, snapshot
the registered InputSessions, call every `close()` concurrently, await their
supervisors and bridges, then close shared Agent and external resources within
the one global deadline. (`CP-SHUTDOWN-001`)

A session is registered before readiness is exposed and deregistered only after
its session and supervisor close. Deadline expiry or a second signal during
shutdown MUST invoke immediate non-zero hard exit. Successful cleanup exits
zero. (`CP-SHUTDOWN-002`)

## Failure and recovery

| Failure | Conversation result | InputSession reusable | Process result |
|---|---|---|---|
| Valid context rejection | `CONTEXT_REJECTED` with code | Yes | Continue |
| Context snapshot unavailable | `CONTEXT_UNAVAILABLE` | Yes | Continue |
| Agent failure or unhandled Agent exception | `AGENT_FAILED`; abort open sink | Yes after input cleanup | Continue |
| Recoverable input failure | `INPUT_FAILED`; abort open sink | Yes | Continue |
| Input disconnect/unrecoverable failure | `INPUT_SESSION_CLOSED`; abort open sink | No | Continue |
| User cancellation | `INPUT_CANCELLED`; abort open sink if needed | Yes | Continue |
| Presenter timeout | `FOLLOW_UP_TIMEOUT` | Yes | Continue |
| Invalid provider result/exception | `INTERNAL_FAILURE` best effort | No guarantee | Fatal non-zero exit |
| Broken bridge/sink invariant | `INTERNAL_FAILURE` best effort | No guarantee | Fatal non-zero exit |
| Agent entry/cancellation deadline missed | `INTERNAL_FAILURE` best effort | No guarantee | Fatal non-zero exit |

External binding violations are rejected and close only that InputSession unless
they expose an internal invariant failure. There is no automatic Agent or
context retry. (`CP-FAILURE-001`)

## Normal sequences

### Single-turn Conversation

```text
InputSession.accept_conversation() returns initial UserMessage
Bridge resolves context and opens AgentConversation
Bridge delivers initial UserMessage -> AgentInputAccepted
Agent emits AssistantMessageStarted
Agent emits AssistantTextChunk one or more times
Agent emits AssistantMessageCompleted
Agent emits TurnDisposition(END_CONVERSATION)
Bridge sends ConversationEnded(COMPLETED)
Both scoped contexts exit; InputSession returns to IDLE
```

### Follow-up Conversation

```text
successful first turn
Agent emits TurnDisposition(REQUEST_FOLLOW_UP)
Input request_follow_up() returns FollowUpRequestCommitted
Bridge enters WAITING_FOR_FOLLOW_UP and synchronously acknowledges token
Input emits UserMessage
Bridge delivers message -> AgentInputAccepted
successful second turn ends explicitly
```

### Cancellation during Agent handoff

```text
Bridge races send_user_message() against pending input control
Input cancellation and AgentInputAccepted become ready together
Bridge selects INPUT_CANCELLED, preserves acceptance commit,
awaits AgentConversation.cancel(INPUT_CANCELLED), aborts open sink if any,
sends ConversationEnded(INPUT_CANCELLED), and closes both scopes
```

## Invalid sequences

- Exposing an empty or whitespace-only initial or follow-up message;
- opening a second InputConversation while one is active;
- receiving Agent output before an accepted user message;
- emitting a text chunk outside an open assistant stream;
- emitting progress after assistant streaming starts;
- completing two assistant streams in one turn;
- completing a successful turn without a disposition;
- emitting a disposition after Agent failure or cancellation;
- exposing a follow-up outcome before token acknowledgement;
- attaching a timeout to Agent follow-up intent;
- buffering Agent output ahead of a blocked bridge receive;
- cancelling a bridge task and leaving its watcher, handoff, or Agent task alive;
- parsing diagnostic detail as a reason or rejection code;
- handling a broken sealed-lifetime invariant by closing only one session.

## Observability requirements

- Every InputSession transition MUST be logged on DEBUG with
  `InputSession[<id>]`, old state, cause, and new state. (`CP-OBS-001`)
- Every bridge transition MUST be logged on DEBUG with
  `ConversationBridge[<conversation_id>]`, old state, source/result, selected
  race outcome, and new state. (`CP-OBS-002`)
- Conversation acceptance and terminal outcome MUST be logged briefly on INFO
  with Conversation ID, InputSession ID, medium, user, area, and typed reason.
- Agent entry, each user-input acceptance, assistant stream start/completion,
  follow-up commitment, cancellation request/acknowledgement, context exit, and
  cleanup MUST be logged.
- LLM requests and replies MUST include model, token count, and duration under
  the project logging rules.
- Diagnostic detail and content logging MUST NOT be required to reconstruct the
  state machine.

## Implementation and conformance references

Implementation owners:

- `ai_server/conversations/` for contexts, messages, interfaces, bridge, state,
  supervision, and lifecycle registry;
- `ai_server/agent/` and `ai_server/orchestrator/` for Agent factories and
  AgentConversation implementations;
- `ai_server/server.py` for fatal containment and bounded shutdown;
- concrete bindings named in the websocket and microphone documents.

Conformance coverage is indexed by the
[Protocol Conformance Catalogue](protocol-conformance-catalogue.md). T-004 is
the authoritative migration plan:
[Conversation Bridge Protocol Redesign](tasks/T-004-conversation-bridge-protocol-redesign.md).

## Compatibility policy

The migration is a clean break. The old `Session`, generic mutable
`Conversation.state`, bidirectional `CommunicationEndpoint` and
`ConversationEndpoint`, old message-stream vocabulary, and old websocket events
are removed at atomic production cutover. No version negotiation, alias,
translation facade, or permissive legacy parser is provided. (`CP-COMPAT-001`)

## Explicitly unresolved decisions

None. Captain approved this protocol on 2026-07-19. Verification and conformance
evidence remain tracked by T-004.
