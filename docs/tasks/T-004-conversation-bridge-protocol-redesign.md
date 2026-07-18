# T-004: Conversation Bridge Protocol Redesign

## Status

- **Authority:** Active design and implementation task
- **Status:** Architecture and plan-review resolutions ratified; implementation not started
- **Decision dates:** Initial architecture 2026-07-17; plan-review resolutions 2026-07-18
- **Audience:** Maintainers of agents, sessions, websocket inputs, microphone inputs, clients, and protocol tests
- **Supersedes:** The conversation-core and websocket redesign scope in T-001
- **Does not supersede:** T-002 or T-003; their microphone-driver fixes and verification remain valid

This task records the approved architecture and the work required to make it
normative and implemented. Until the documentation stage below is reviewed and
approved, the existing normative protocol documents continue to describe the
current contract. This task is a plan, not a runtime protocol.

The companion [illustrated design aid](T-004-agent-boundary-options.html) explains
the approved architecture visually. It is informative, not normative.

The seven findings in the [T-004 plan review](T-004-plan-review.md) have candidate
resolutions recorded in this task. They remain open until a fresh independent
review verifies the resolutions and checks that they introduced no new
contradictions.

## Objective

Replace the current loosely coupled `Session`/`Agent`/endpoint protocol with a
typed, enforceable conversation bridge:

- one bridge coroutine owns exactly one input conversation and one agent
  conversation;
- the bridge coroutine is the state holder for that conversation;
- persistent input sessions may exist concurrently, each with at most one active
  conversation;
- one shared `Agent` factory may create many independent agent conversations;
- each side is sealed behind a directional interface;
- every event is legal only from an explicit source and in explicit states;
- cancellation, failure, streaming, follow-up, and cleanup have deterministic
  outcomes.

The migration is a clean break. The server, in-process protocol, websocket JSON
binding, websocket clients, microphone mapping, and conformance tests change
together. There is no compatibility layer for the old event vocabulary.

## Why this task is needed

The current implementation distributes conversation state across the `Session`,
the endpoint, and the agent. Its interfaces permit sequences which the written
protocol does not intend, and some lifecycle events are noticed only after an
agent call returns.

The review found these classes of defect:

1. The documented state model is not fully enforced. An agent can emit output
   before the session has accepted a user message.
2. Input closure and cancellation may be buffered while agent work is running
   instead of promptly cancelling that work.
3. Some failure paths can be reported as ordinary completion.
4. Conversation termination does not carry a sufficiently precise typed reason.
5. Empty-input semantics and follow-up ownership are inconsistent between
   implementations and documentation.
6. The current interfaces expose both directions through one endpoint object,
   making illegal calls representable.
7. The protocol lacks a complete source/event/state conformance matrix and tests
   for each illegal transition.
8. Shared mutable `Conversation.state` obscures ownership and risks accidental
   coupling between agent conversations.

## Current implementation catalogue

This catalogue is the migration baseline, not the target design.

### Core session

- `ai_server/sessions.py`
  - `Session.run()` sequentially waits for an endpoint event and then awaits the
    agent's conversation loop.
  - It does not run a dedicated stateful bridge between two scoped conversation
    interfaces.
  - `SessionManager` supplies the shared agent and creates sessions for inputs.
- `ai_server/messages.py` and `ai_server/interfaces.py`
  - Define the current shared messages and bidirectional endpoint/agent
    abstractions.
  - Their shape does not make event direction or conversation scope structural.

### Agent implementations

- `ai_server/agent/assistant.py`
- `ai_server/agent/echo.py`
- `ai_server/agent/interrogator.py`
- `ai_server/agent/polite_reply.py`
- `ai_server/orchestrator/` and the Domain Specific Agents it coordinates
- agent-facing tools and Home Assistant integrations which currently receive or
  retain the shared conversation/endpoint objects

The current `Agent` is a long-lived object with a `run_conversation(...)` style
entry point. Implementations directly consume input and produce output through a
shared endpoint. The target separates the long-lived factory from one scoped,
stateful `AgentConversation`.

### Websocket input

- `ai_server/websocket_server.py`
  - Maps websocket JSON to the current in-process messages.
  - Uses a background reader so heartbeat and liveness handling are not blocked
    by agent work. That property must be preserved.
- `ai_server/ws_client_common.py`
- `ai_server/chat_client.py`
- `ai_server/batch_ws_client.py`
  - Implement the current client event vocabulary and lifecycle assumptions.
- `tests/test_websocket_server.py`
  - Covers current websocket behavior but not the complete target conformance
    matrix.

### Microphone input

- `ai_server/microphones/manager.py`
  - Owns the persistent microphone runtime, capture, STT, playback, cues, and
    recovery.
  - Currently binds microphone events to a long-lived core `Session`.
- `ai_server/microphones/agent_endpoint.py`
  - Adapts the microphone manager to the current shared endpoint with two queues.
  - It is replaced by the target input-session/input-conversation adapter.
- `ai_server/microphones/interfaces.py` and
  `ai_server/microphones/messages.py`
  - Define the manager-to-driver protocol. That protocol is not redesigned by
    this task.
- `ai_server/microphones/drivers/box3_esphome.py` and other drivers
  - Remain behind the abstract microphone interface.

T-002 and T-003 concern the microphone driver protocol and Box3 lifecycle. Their
behavior, ordering, reason strings, regression tests, and firmware verification
must be preserved.

## Approved architecture

```text
Persistent input session A
    `-- InputConversation A <--> Bridge coroutine A <--> AgentConversation A

Persistent input session B
    `-- InputConversation B <--> Bridge coroutine B <--> AgentConversation B

Shared Agent factory
    |-- creates AgentConversation A with isolated mutable state
    `-- creates AgentConversation B with isolated mutable state
```

The bridge coroutine is the state holder. It does not contain media-specific
policy and it does not inspect concrete input adapter or agent types. Multiple
bridge coroutines run concurrently when multiple input sessions are active.

### Cardinality and lifetime

1. A persistent input session has zero or one active `InputConversation`.
2. A bridge coroutine is created for a fresh conversation and terminates with
   that conversation.
3. A bridge connects exactly one `InputConversation` to exactly one
   `AgentConversation`.
4. A shared `Agent` factory may create independent `AgentConversation` objects
   concurrently.
5. Mutable agent-conversation state is private to its `AgentConversation`.
   Read-only shared resources such as model clients and tool registries may be
   owned by the factory.
6. The next conversation on an input session cannot start until the previous
   conversation's output has drained or been aborted, both scoped interfaces
   have closed, and the input session has returned to `IDLE`.

### Persistent InputSession contract

`InputSession` is a sealed persistent interface owned by an input adapter. It has
an immutable typed `InputSessionContext` and this adapter-local lifecycle:

| State | Meaning |
|---|---|
| `IDLE` | No readiness call or conversation is active |
| `ACCEPTING` | `accept_conversation()` has atomically announced readiness and is waiting for an accepted request or idle closure |
| `RESERVED` | One accepted request has been returned to the supervisor and is reserved for bridge startup |
| `ACTIVE` | The bridge has opened the one permitted `InputConversation` |
| `CLOSED` | The persistent input session cannot accept another conversation |

The per-input supervisor owns only this idle loop:

```python
async with input_adapter.open_session() as input_session:
    while not input_session.closed:
        accepted = await input_session.accept_conversation()
        if isinstance(accepted, InputSessionClosed):
            break
        await bridge_conversation(
            request=accepted,
            input_session=input_session,
            agent=agent,
            context_provider=context_provider,
        )
```

`accept_conversation()` is legal only in `IDLE`. It atomically enters
`ACCEPTING`, exposes readiness in the adapter's own medium, and waits for exactly
one of:

- an immutable typed `ConversationRequest`, containing a complete accepted
  `initial_message` and typed request-scoped context needed to resolve the
  conversation context;
- `InputSessionClosed`, if the persistent input closes while idle.

An accepted request moves the input session to `RESERVED`. It is never queued
behind another request. A second external start while `RESERVED` or `ACTIVE` is
rejected by the input binding according to its protocol; it never becomes a
second internal `ConversationRequest`. For websocket input this is an external
protocol violation and closes the websocket session after a typed rejection
where safe. A microphone remains governed by its media state and cannot create a
new conversation while one is active.

While a request is `RESERVED`, the bridge concurrently awaits the sealed
`InputSession.receive_startup_control(request)` operation. Its only outcomes are
`ConversationCancelled`, `InputConversationFailed`, or `InputSessionClosed`.
These are control outcomes for the one reservation, not queued conversation
starts. The input adapter retains at most the first terminal startup outcome
until the bridge observes it.

Opening the scoped `InputConversation` moves `RESERVED` to `ACTIVE`. Its normal
or recoverable-failure exit returns the session to `IDLE`; input-session closure
moves it to `CLOSED`. The next readiness announcement therefore cannot occur
until all output and scoped cleanup from the previous conversation are complete.
The transition from reservation control to `InputConversation` control is
atomic: a startup outcome is delivered exactly once on one side of the boundary
and cannot be lost during `__aenter__`.

### Startup ownership and scoped interfaces

The bridge owns the complete core conversation lifecycle, including `STARTING`.
It receives the accepted request, persistent input session, Agent factory, and
typed context provider. It watches reservation control throughout startup.
Before opening either scoped conversation, it assigns the conversation ID and
resolves the immutable `ConversationContext`. It then opens both sides itself:

```python
async def bridge_conversation(request, input_session, agent, context_provider):
    state_machine = BridgeStateMachine.starting()
    async with AsyncExitStack() as resources:
        conversation_id = new_conversation_id()
        context = await state_machine.await_startup(
            context_provider.resolve(...), input_session, request
        )
        input_conversation = await state_machine.enter_input_conversation(
            resources, input_session, request, context
        )
        agent_conversation = await state_machine.enter_agent_conversation(
            resources, agent, context
        )
        state_machine.finish_startup()
        return await state_machine.run(
            initial_message=request.initial_message,
            input_conversation=input_conversation,
            agent_conversation=agent_conversation,
        )
```

The pseudocode expresses ownership, not a requirement for those exact function
names. The bridge state machine exists and is in `STARTING` before context
resolution or either `__aenter__` begins. Startup cancellation and failure are
therefore visible to the bridge and use the same typed terminal classification
as failures after startup:

- request/input-conversation startup failure becomes `INPUT_FAILED` or
  `INPUT_SESSION_CLOSED`, as classified by the sealed input adapter;
- agent-conversation startup failure becomes `AGENT_FAILED`;
- cancellation becomes `INPUT_CANCELLED`;
- shutdown becomes `SERVER_SHUTDOWN`;
- a broken bridge/context invariant becomes `INTERNAL_FAILURE` and closes the
  input session.

If input startup succeeds and agent startup fails, exiting the nested context
deterministically closes the input conversation. No agent context is entered
after an input startup failure. Cleanup is exact-once for every context which was
successfully entered.

The final API may use different method names, but these properties are required:

- `InputConversation` exposes only input-to-bridge events and bridge-to-input
  rendering/control operations.
- `AgentConversation` exposes only bridge-to-agent input and agent-to-bridge
  output operations.
- The two interfaces are distinct and sealed. Callers cannot downcast to a
  concrete websocket, microphone, or agent implementation.
- The `AgentConversation` is an active message endpoint running behind its
  interface, not a stateless `run_turn()` function.
- Context-manager exit is the cleanup boundary. The input-owned assistant output
  sink drains on normal completion and aborts on cancellation or failure.

### Conversation creation

An internal bridge conversation is created only after `accept_conversation()`
returns a complete, accepted, non-empty initial user message:

- websocket input combines its external conversation-start operation with the
  submitted initial text;
- microphone input waits for an accepted final STT result;
- whitespace-only input is invalid;
- the core never owns an empty conversation waiting indefinitely for first input.

The bridge assigns one conversation ID while already in `STARTING` and before
constructing either scoped conversation. It is stored once in the immutable
context, scoped conversation objects, and logging context, rather than repeated
on every in-process event.

### Typed context

Replace generic mutable conversation attributes with immutable typed context:

- `InputSessionContext` contains identity and stable capabilities belonging to
  the persistent input session;
- `ConversationContext` contains immutable values resolved before the
  `AgentConversation` is created, including the conversation ID, medium, and
  resolved user settings needed by the agent;
- optional values are explicit typed fields, not an extension dictionary;
- internal JSON-like serialization omits `None` fields unless an external
  binding requires `null`.

The current shared mutable `Conversation.state` is removed. Agent-specific
mutable state belongs inside the `AgentConversation`. User settings are resolved
through a typed provider before conversation creation.

## Message model

Names below are the required clean-break vocabulary for the documentation stage.
Minor Python naming adjustments are permitted only if the normative documents,
all bindings, and all tests use the same final names.

### InputConversation to bridge

| Event | Meaning | Legal use |
|---|---|---|
| `UserMessage` | One complete, non-whitespace follow-up message | Only after follow-up was requested |
| `FollowUpTimedOut` | The input's presented follow-up offer expired | Only while waiting for follow-up |
| `ConversationCancelled` | User/input cancelled the current conversation | Every non-terminal state |
| `InputConversationFailed` | Media-specific operation failed, but the persistent input session recovered | Every non-terminal state; ends only this conversation |
| `InputSessionClosed` | The persistent input session disconnected or cannot recover | Every non-terminal state; ends conversation and session |

The initial `UserMessage` is carried by the accepted `ConversationRequest` and
delivered to the AgentConversation by the bridge after both scoped conversations
open. It is not redundantly re-emitted by `InputConversation`.

There are no core `UserInputStarted` or `UserInputAborted` events. Those are
media-specific details unless and until a future requirement proves they must
cross the abstract interface.

### Bridge to AgentConversation

| Event | Meaning |
|---|---|
| `UserMessage` | A complete accepted user turn |

Cancellation is conveyed through the scoped lifecycle/cancellation operation,
not disguised as a user message.

### AgentConversation to bridge

| Event | Meaning | Constraints |
|---|---|---|
| `ProcessingUpdate` | Optional progress suitable for forwarding to the input | Only before assistant output starts for the turn; never interleaved with assistant text |
| `AssistantMessageStarted` | Opens the turn's sole assistant stream | Zero or one stream per turn |
| `AssistantTextChunk` | Ordered assistant text offered to the input sink | Only inside the open stream |
| `AssistantMessageCompleted` | Closes the stream normally | Exactly once for an opened stream |
| `TurnDisposition` | Explicitly chooses `END_CONVERSATION` or `REQUEST_FOLLOW_UP` | Exactly once after each successfully completed agent turn; no implicit default |

A turn may intentionally produce no assistant stream. The protocol must not
invent an empty assistant reply in that case. It still requires one disposition
if the turn completes successfully. Agent cancellation, typed agent failure, or
an unhandled agent exception is an alternative terminal outcome and produces no
`TurnDisposition`.

`AssistantMessageAborted` is not an AgentConversation event. If the agent cannot
finish an open stream, it terminates with a typed agent failure. The bridge maps
that failure to `AGENT_FAILED` and invokes the input-owned assistant sink's
`abort(AGENT_FAILED)` operation if the input stream was opened.

### Bridge to InputConversation

The bridge forwards validated processing updates and maps validated assistant
events onto one input-owned transactional assistant-output sink. The sink exposes
`start()`, `send_text(chunk)`, `complete()`, and `abort(reason, detail)`; the
adapter serializes these operations and permits exactly one terminal operation.

The bridge also sends:

| Event | Meaning |
|---|---|
| `FollowUpRequested` | Agent explicitly requested another user turn; contains no timeout |
| `ConversationEnded` | The conversation ended with a closed reason and optional diagnostic detail |

`AssistantMessageAborted` is the observable result of the bridge-to-input
`abort(...)` operation, not an agent-to-bridge event. It is emitted only if the
input-side assistant stream opened and did not already complete.

The input adapter decides how to display, speak, cue, or ignore
`ProcessingUpdate`. It decides how a follow-up offer is presented and starts its
timeout only when the user has actually been presented with that offer.

## Follow-up ownership

`REQUEST_FOLLOW_UP` expresses agent intent only. It carries no duration and does
not start a core timer.

After forwarding `FollowUpRequested`, the bridge waits for exactly one of:

- `UserMessage`;
- `FollowUpTimedOut`;
- `ConversationCancelled`;
- `InputConversationFailed`;
- `InputSessionClosed`.

The microphone adapter may base presentation on cues and playback completion.
The websocket adapter may base it on successful delivery to the client. Other
media may use different rules. Only the adapter knows when presentation occurred.

## Assistant streaming and backpressure

1. There is zero or one assistant stream per agent turn.
2. The bridge has no assistant-output queue and no knowledge of input speed,
   buffer capacity, or rendering progress.
3. The bridge validates one agent event and awaits the corresponding input sink
   operation before accepting the next ordinary agent output event. This awaited
   `send` boundary propagates backpressure naturally to agent production.
4. An input adapter may send directly or maintain its own media-specific bounded
   rendering queue. If that queue is full, its `send_text()` blocks. Bounds are
   explicit configuration or named constants and have adapter-level tests.
5. A text chunk becomes irrevocable only when the input actually presents or
   renders it. The adapter alone tracks that boundary. Chunks buffered but not
   rendered may be discarded by abort.
6. `complete()` does not commit input-side completion until preceding accepted
   output has drained according to that adapter's rendering contract. Normal
   conversation exit awaits it.
7. `abort()` is a priority control operation which can run while `send_text()` or
   `complete()` is blocked. If abort wins, the adapter atomically prevents the
   pending operation from later committing, discards unrendered buffered output,
   stops rendering where possible, and commits `AssistantMessageAborted` exactly
   once.
8. If completion committed before cancellation was eligible at the bridge's
   decision point, the stream remains completed and no abort is sent. The
   conversation may still end with `INPUT_CANCELLED` while its already completed
   assistant stream remains valid.
9. The sink operations are cancellation-safe and always expose a definitive
   outcome: committed completion, committed abort, or no commit. There is no
   ambiguous “possibly delivered” result.
10. Already rendered text or speech remains part of the interaction. Abort does
    not retract it.
11. Microphone adapters must be able to render/speak chunks without waiting for
   the complete message in the target design, although concrete streaming-TTS
   engine work may be staged as described under Future microphone work.

The bridge concurrently observes input control events while awaiting agent output
or an input sink operation. A cancellation, input failure, input-session close,
or shutdown can therefore pre-empt a slow or blocked renderer without polling.
The input sink owns completion-versus-abort arbitration because only the input
knows its accepted, buffered, and rendered state.

The input sink enforces this renderer-visible state machine:

| State | Legal operations and transitions |
|---|---|
| `NOT_STARTED` | `start()` commits `OPEN`; conversation termination emits no assistant abort because no stream was visible |
| `OPEN` | `send_text()` remains `OPEN`; `complete()` begins `COMPLETING`; `abort()` commits `ABORTED` |
| `COMPLETING` | The adapter drains preceding output; successful drain commits `COMPLETED`; priority `abort()` commits `ABORTED` and prevents completion |
| `COMPLETED` | Terminal; later abort reports already completed and emits nothing |
| `ABORTED` | Terminal; repeated cleanup is idempotent and emits nothing further |

The adapter's operation result identifies the committed state. A coroutine
cancellation cannot leave a sink operation in an unknown state. The bridge uses
that result, rather than queue contents or timing guesses, when logging and
closing the conversation.

## State machine

The normative protocol must define a complete matrix over source, event, and
state. The minimum conversation states are:

| State | Meaning |
|---|---|
| `STARTING` | Scoped interfaces are being opened around an accepted initial message |
| `WAITING_FOR_AGENT` | User message was delivered; no assistant stream is open |
| `STREAMING_ASSISTANT` | One assistant stream is open |
| `WAITING_FOR_DISPOSITION` | Assistant stream closed; explicit disposition is required |
| `WAITING_FOR_FOLLOW_UP` | Follow-up handling was delegated to the input adapter, which owns presentation and timing |
| `ENDING` | Terminal event is being rendered and scoped resources are closing |
| `CLOSED` | Cleanup is complete; no further events are legal |

Required transitions include:

```text
accepted initial UserMessage
    STARTING -> WAITING_FOR_AGENT

ProcessingUpdate
    WAITING_FOR_AGENT -> WAITING_FOR_AGENT

AssistantMessageStarted
    WAITING_FOR_AGENT -> STREAMING_ASSISTANT

AssistantTextChunk
    STREAMING_ASSISTANT -> STREAMING_ASSISTANT

AssistantMessageCompleted followed by committed input sink complete()
    STREAMING_ASSISTANT -> WAITING_FOR_DISPOSITION

input sink abort() wins while send_text() or complete() is pending
    STREAMING_ASSISTANT -> ENDING -> CLOSED

TurnDisposition(END_CONVERSATION)
    WAITING_FOR_AGENT or WAITING_FOR_DISPOSITION -> ENDING -> CLOSED

TurnDisposition(REQUEST_FOLLOW_UP)
    WAITING_FOR_AGENT or WAITING_FOR_DISPOSITION
        -> WAITING_FOR_FOLLOW_UP

UserMessage
    WAITING_FOR_FOLLOW_UP -> WAITING_FOR_AGENT

FollowUpTimedOut
    WAITING_FOR_FOLLOW_UP -> ENDING -> CLOSED
```

An aborted assistant stream transitions to `ENDING`; an agent does not continue
the same turn after abort and no disposition is required. Agent failure before a
stream opens also transitions directly to `ENDING`. Cancellation and
input-session closure are legal in every non-terminal state and pre-empt agent
work. All other unlisted transitions are protocol violations, not tolerant
no-ops.

### Concurrent-event priority

When multiple awaited operations are ready before the bridge commits its next
transition, it selects exactly one using this precedence:

1. `INTERNAL_FAILURE`;
2. `SERVER_SHUTDOWN`;
3. `INPUT_SESSION_CLOSED`;
4. `INPUT_FAILED`;
5. `INPUT_CANCELLED`;
6. `AGENT_FAILED`;
7. `FOLLOW_UP_TIMEOUT`;
8. ordinary agent output or `TurnDisposition`.

Priority applies only to results already ready at the bridge's decision point.
Once a transition or input-sink terminal operation has committed, a later event
does not retroactively replace it. For example, a cancellation ready alongside
assistant completion wins and invokes abort; cancellation observed only after
input completion committed ends the conversation without changing the completed
stream.

The normative state/event/source matrix and race tests must use this precedence.
The bridge must inspect the complete ready set returned by its concurrency
primitive rather than depending on set iteration or event-loop callback order.

## Validation and trust boundaries

Validation is layered:

- `InputConversation` validates its local framing, closed state, message content,
  transport correlation, and media-specific prerequisites.
- `AgentConversation` validates its local stream framing, closed state, and
  agent-specific production rules.
- the bridge alone validates cross-conversation sequencing, turn order,
  follow-up eligibility, cancellation, and terminal cleanup.

Failure classification is intentional:

| Source | Classification | Required result |
|---|---|---|
| Invalid/unhandled agent output or agent exception | `AGENT_FAILED` | Abort open stream, end only the conversation, keep recovered input session reusable |
| Recoverable input-adapter operation failure | `INPUT_FAILED` | Abort open stream and end only the conversation |
| Input disconnect or unrecoverable adapter failure | `INPUT_SESSION_CLOSED` | Abort conversation and close persistent input session |
| Invalid external websocket message | Binding-level protocol rejection | Send typed rejection where delivery is safe, then close websocket input session |
| Broken bridge invariant | Internal fatal defect | Attempt terminal cleanup, close input session, log with context, and propagate/fail loudly |
| Server shutdown | `SERVER_SHUTDOWN` | Cancel work and close both scoped interfaces deterministically |

There is no automatic retry after an unhandled agent failure.

### Terminal reasons

`ConversationEnded` and `AssistantMessageAborted` use separate closed enums for
machine-actionable reasons plus separate optional diagnostic detail. The
`ConversationEndReason` set is:

- `COMPLETED`
- `INPUT_CANCELLED`
- `FOLLOW_UP_TIMEOUT`
- `AGENT_FAILED`
- `INPUT_FAILED`
- `INPUT_SESSION_CLOSED`
- `INTERNAL_FAILURE`
- `SERVER_SHUTDOWN`

The `AssistantAbortReason` set is:

- `INPUT_CANCELLED`
- `AGENT_FAILED`
- `INPUT_FAILED`
- `INPUT_SESSION_CLOSED`
- `INTERNAL_FAILURE`
- `SERVER_SHUTDOWN`

`COMPLETED` and `FOLLOW_UP_TIMEOUT` are deliberately absent from
`AssistantAbortReason`: normal completion is not an abort, and follow-up timeout
occurs only after the assistant stream has already completed or the successful
turn produced no stream.

Diagnostics are for logs and debugging, not control flow. Bindings must not force
clients to parse free text to determine behavior.

## Websocket binding requirements

Create `docs/websocket-conversation-protocol.md` as the normative external JSON
binding after approval. It must define:

- JSON schemas for every client and server event;
- start-with-initial-message behavior;
- transport-level IDs and correlation where needed externally;
- closed enums and rejection payloads;
- websocket close behavior and close codes;
- maximum message sizes, ingress queue bound, and overflow behavior;
- liveness/heartbeat behavior;
- mapping between external JSON events and the in-process typed protocol;
- examples of one-turn, follow-up, cancellation, failure, and invalid sequences.

The websocket binding is versionless for this migration. Internal communication
is in-process and also has no version handshake. A future network compatibility
requirement may revisit version negotiation.

Preserve the background websocket reader needed for heartbeat and prompt
disconnect detection. It must feed a bounded, validated input path; it must not
be removed in favor of blocking reads tied to agent progress.

The server and all repository websocket clients migrate in the same change. Old
event names are rejected rather than silently interpreted.

## Microphone binding requirements

Rewrite `docs/microphone-conversation-mapping.md` to map the normative microphone
protocol to `InputSession`/`InputConversation`:

- an accepted final STT result creates the conversation and initial
  `UserMessage`;
- microphone-specific listening, STT, cue, playback, and recovery states remain
  inside the adapter/manager;
- the adapter owns follow-up presentation and timeout;
- assistant text is consumed incrementally through a bounded output path;
- cancellation stops agent work and further rendering, then cleans up and rearms
  the persistent microphone session;
- `InputConversationFailed` versus `InputSessionClosed` reflects actual recovery;
- no core component names Box3 services, display assets, LEDs, or concrete driver
  types.

The manager-to-driver [Microphone Protocol](../microphone-protocol.md), drivers,
and firmware are out of scope unless a conformance test proves that the binding
cannot be implemented without changing that abstract contract. Such a finding
requires a separate explicit design decision before changing the protocol.

## Implementation plan

### Stage 1: Write and approve the protocol suite

1. Rewrite `docs/ai-server-conversation-protocol.md` as the typed in-process core
   bridge contract.
2. Add `docs/websocket-conversation-protocol.md` as the external JSON binding.
3. Rewrite `docs/microphone-conversation-mapping.md` for the new core interface
   while preserving the normative microphone driver protocol.
4. Update `docs/protocol-conformance-catalogue.md` with stable requirement IDs,
   owners, bindings, and planned tests.
5. Update `docs/README.md` and applicable `AGENTS.md` reading rules so all three
   contracts are discoverable.
6. Include complete state/event/source tables, lifecycle examples, error tables,
   schemas, bounds, and normative language required by
   `docs/protocol-documentation-standard.md`.
7. Stop for Captain's review. Do not begin implementation until the rewritten
   protocol documents are explicitly approved as normative.

### Stage 2: Add and test the new core without activating it

This is an additive, runnable checkpoint. The current runtime remains entirely on
the old protocol while the new core is built beside it. There is no adapter which
translates or routes between the contracts.

1. Add the typed contexts, requests, enums, messages, and sealed interfaces. A
   focused `ai_server/conversations/` package is preferred; keep its
   `__init__.py` minimal.
2. Implement the `InputSession` lifecycle, per-input supervisor, bridge-owned
   startup, and per-conversation bridge state machine against test factories.
3. Implement the transactional input-owned assistant sink contract, blocking
   pushback, concurrent input-control observation, and deterministic
   completion/abort behavior.
4. Add exhaustive core unit and conformance tests using fake InputSession,
   InputConversation, Agent, and AgentConversation implementations.
5. Prove sequential conversations, multiple concurrent input sessions, startup
   cleanup, priority races, and isolated agent state.
6. Do not remove or modify the old runtime interfaces at this stage. The full
   existing test suite must still pass unchanged, in addition to the new tests.

Gate B reviews this additive core. Passing Gate B does not authorize a partial
production migration.

### Stage 3: Atomic production cutover

Agents, input bindings, clients, runtime construction, and legacy removal form
one coherent cutover change. Work may be organized internally by the groups
below, but no intermediate checkpoint may leave production consumers split
between old and new contracts or require a compatibility facade.

#### 3A: Prepare all agent consumers within the cutover

1. Change `Agent` into a factory exposing
   `async with agent.open_conversation(context)`.
2. Implement active `AgentConversation` endpoints with private mutable state.
3. Migrate Echo, Interrogator, Polite Reply, Assistant, Orchestrator, tools, and
   legacy Home Assistant integration code.
4. Require `TurnDisposition` after every successfully completed turn and no
   disposition after cancellation or failure.
5. Emit processing updates only before assistant streaming starts.

#### 3B: Prepare the websocket binding and clients within the cutover

1. Implement the approved JSON schema and typed InputSession adapter.
2. Preserve independent background reading, heartbeat, and disconnect detection.
3. Bound transport ingress explicitly; let outbound `send` block according to
   websocket transport pushback rather than adding a bridge queue.
4. Migrate interactive and batch clients and shared message helpers.
5. Reject old or invalid external events and close the input session as specified.

#### 3C: Prepare the microphone binding within the cutover

1. Replace `MicrophoneAgentEndpoint` with an `InputSession`/
   `InputConversation` adapter owned by the microphone manager layer.
2. Create a request only after an accepted final STT result.
3. Implement adapter-owned follow-up presentation and timeout.
4. Implement the transactional assistant sink. Any buffer needed by slow TTS is
   bounded and owned by the microphone adapter; a full buffer blocks
   `send_text()` and pushes back naturally.
5. Preserve manager-to-driver ordering, T-002/T-003 fixes, reason strings,
   re-arm behavior, and concrete-driver encapsulation.

#### 3D: Activate and remove in the same cutover

1. Switch runtime construction to the new per-input supervisor and bridge.
2. Switch every concrete agent, websocket path, repository client, and microphone
   path to the new interfaces together.
3. Remove generic mutable `Conversation.state`, the old bidirectional endpoint,
   obsolete messages, legacy Session paths, and superseded tests.
4. Do not retain an old/new translation facade or accept the old websocket event
   vocabulary.
5. Run all focused and complete automated suites before treating the cutover as a
   coherent runnable checkpoint.

### Stage 4: End-to-end verification and closure

1. Complete the conformance catalogue with test evidence for every requirement.
2. Run focused core, agent, websocket, and microphone tests.
3. Run the complete pytest suite.
4. Run `orchestrator_and_dsa_tests/run.sh` with the currently configured model.
5. Exercise the real websocket server with repository clients, including
   disconnect, follow-up, cancellation, slow-send pushback, and invalid-input
   cases.
6. Exercise real microphone conversations one device at a time, including
   streaming, slow-TTS pushback, follow-up timeout, interruption, recovery, and
   re-arm.
7. Verify Box3 and Voice PE paths separately. Firmware changes are not implied.
8. If firmware is changed for a separately approved reason, compile and inspect
   generated `main.cpp` before any flash, then perform post-flash checks.

## Conformance coverage required

At minimum, automated tests must cover:

- every legal transition in the normative state matrix;
- representative illegal events from every source in every state;
- initial empty and whitespace-only messages;
- InputSession readiness, idle closure, reservation, second-start rejection, and
  exact-once reservation-to-active control handoff;
- cancellation, input failure, session close, and context-provider failure during
  every startup operation;
- zero assistant streams and one assistant stream per turn;
- progress before streaming and rejection of progress during streaming;
- stream framing, ordered chunks, completion, and abort;
- explicit end and explicit follow-up dispositions after successful turns, and
  absence of dispositions after cancellation or failure;
- adapter-owned follow-up timing and late-event races;
- cancellation in every non-terminal state;
- simultaneous cancellation, disconnect, timeout, disposition, and stream-end
  races with deterministic outcomes;
- agent failure before output, during processing, and during streaming;
- recoverable input failure versus input-session closure;
- bridge invariant failure and cleanup;
- adapter-owned bounded-buffer backpressure, no bridge queue, and cancellation
  while the input's `send_text()` is blocked;
- definitive input-sink operation outcomes and exactly-one completion or abort;
- normal drain versus abort discard;
- repeated sequential conversations on one persistent input session;
- concurrent conversations on multiple input sessions;
- absence of mutable state leakage between concurrent `AgentConversation`s;
- websocket schema, bounds, heartbeat, rejection, close, and old-vocabulary cases;
- microphone accepted-STT creation, playback, follow-up, cancellation, recovery,
  and T-002/T-003 regressions;
- context-manager cleanup exactly once on all exits.

Use stable conversation/input-session log prefixes in concurrency tests so
failures are diagnosable.

## Expected file impact

The final implementation is expected to touch at least:

- `docs/ai-server-conversation-protocol.md`
- `docs/websocket-conversation-protocol.md` (new)
- `docs/microphone-conversation-mapping.md`
- `docs/protocol-conformance-catalogue.md`
- `docs/README.md` and applicable `AGENTS.md`
- `ai_server/messages.py`, `ai_server/interfaces.py`, `ai_server/sessions.py`
  or their replacements under `ai_server/conversations/`
- `ai_server/websocket_server.py`
- `ai_server/ws_client_common.py`, `ai_server/chat_client.py`, and
  `ai_server/batch_ws_client.py`
- `ai_server/microphones/agent_endpoint.py` (remove/replace)
- `ai_server/microphones/manager.py`
- all concrete agents and any tools coupled to the old conversation objects
- core, agent, websocket, microphone, and orchestrator tests

This is intentionally a systematic migration. Stage 2 permits the inactive new
core to coexist with the complete old runtime solely as additive preparation.
There is no compatibility facade and no runtime path mixing the two contracts.
Stage 3 activates every new consumer and removes every old surface atomically.

## Non-goals

- Redesigning the manager-to-driver microphone protocol.
- Changing Box3 or Voice PE firmware merely because the core protocol changes.
- Adding a websocket or in-process version handshake.
- Supporting old websocket events after migration.
- Adding automatic agent retries.
- Moving media-specific follow-up timers into the bridge.
- Exposing concrete microphone services, cues, LEDs, or display behavior to the
  core.
- Adding speculative generic context extension dictionaries.

## Future microphone work

The target interface deliberately supports text chunks so microphones can begin
speech rendering before a complete assistant message exists. The first core
migration should use the best streaming behavior supported by the current TTS and
driver abstractions.

If truly incremental synthesis or immediate hardware playback interruption needs
new driver operations, document that as follow-up work. Potential future changes
include:

- a streaming TTS synthesis interface;
- explicit playback-abort acknowledgement;
- finer rendering progress/correlation;
- device capability negotiation for incremental audio.

Those possibilities do not authorize changes to the current normative microphone
protocol in T-004.

## Acceptance criteria

T-004 is complete only when:

1. The core, websocket binding, microphone mapping, and conformance catalogue are
   complete, mutually consistent, approved, and marked with correct authority.
2. Every concrete Agent, websocket path, and microphone manager binding uses the
   new scoped interfaces; the old endpoint protocol is gone.
3. Each active input session owns at most one bridge coroutine, and multiple input
   sessions can converse concurrently.
4. The bridge is the sole owner of cross-side conversation state and rejects all
   illegal source/event/state combinations.
5. Agents return an explicit typed disposition after every successfully completed
   turn; aborted and failed turns terminate without one.
6. Follow-up presentation and timeout are owned by each input adapter.
7. The bridge contains no assistant-output queue. Each input applies blocking
   pushback directly and owns any explicitly bounded media buffer, normal drain,
   and deterministic completion-versus-abort arbitration.
8. Terminal reasons and diagnostics are typed and unambiguous.
9. Agent, recoverable input, input-session, external protocol, internal invariant,
   and shutdown failures have the ratified isolation behavior.
10. Concurrent agent conversations have isolated mutable state.
11. The websocket server and all repository clients use only the new binding.
12. Microphone driver protocol and T-002/T-003 behavior remain conformant.
13. Every catalogue requirement links to passing automated evidence or an
    explicitly documented manual hardware check.
14. Focused tests, full pytest, the orchestrator/DSA behavior suite, real
    websocket checks, and required microphone checks pass.

## Plan-review resolution record

These are candidate resolutions for the open findings in
`T-004-plan-review.md`. Finding closure still requires the independent pass
specified by that review.

| Finding | Candidate resolution in this task |
|---|---|
| `T004-PLAN-001` | The bridge receives the accepted request, InputSession, Agent factory, and context provider; it enters `STARTING`, assigns the ID, resolves context, and opens both scoped conversations itself. Startup classification and exact-once cleanup are explicit. |
| `T004-PLAN-002` | `InputSession` now has sealed `IDLE`/`ACCEPTING`/`RESERVED`/`ACTIVE`/`CLOSED` states and pull-gated `accept_conversation()` semantics covering readiness, complete initial input, typed context, idle close, second-start rejection, and return to readiness. |
| `T004-PLAN-003` | The bridge queue is removed. The input owns a transactional assistant sink and any bounded media buffer; blocking `send_text()` provides pushback, `abort()` has control priority, and completion versus abort has a definitive exactly-once outcome. |
| `T004-PLAN-004` | Stage 2 is additive and leaves the old runtime complete. Stage 3 migrates every consumer, activates the new runtime, and removes the old protocol in one coherent atomic cutover without a facade. |
| `T004-PLAN-005` | A disposition is required only after a successfully completed turn. Agent failure is an alternative outcome, bridge-to-input abort is explicit, and `ConversationEndReason` and `AssistantAbortReason` are separate enums. |
| `T004-PLAN-006` | T-001 continuation guidance is limited to its remaining microphone, firmware, and hardware evidence; conversation-core and websocket work routes to T-004 and superseded sections are marked historical. |
| `T004-PLAN-007` | Startup ownership, cancellation handshake, and concurrent terminal-event behavior are now ratified. The exact priority order and commit boundary are specified and required in tests. |

## Review gates

- **Gate A — protocol approval:** Captain approves the rewritten normative
  documents before production code changes begin.
- **Gate B — additive core review:** typed interfaces, bridge state machine,
  transactional sink contract, and exhaustive fake-based core tests are reviewed
  while the old runtime remains complete and active.
- **Gate C — atomic cutover review:** every agent and binding is migrated, the
  runtime switches once, all legacy surfaces are removed, and websocket and
  microphone mappings are checked against the same conformance catalogue.
- **Gate D — closure:** legacy surfaces are absent and fresh verification evidence
  satisfies every acceptance criterion.
