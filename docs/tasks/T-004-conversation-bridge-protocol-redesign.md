# T-004: Conversation Bridge Protocol Redesign

## Status

- **Authority:** Active design and implementation task
- **Status:** Architecture reviewed; ready for Stage 1 protocol drafting; implementation not started
- **Decision dates:** Initial architecture 2026-07-17; architecture review closed 2026-07-18
- **Audience:** Maintainers of agents, sessions, websocket inputs, microphone inputs, clients, and protocol tests
- **Supersedes:** The conversation-core and websocket redesign scope in T-001
- **Does not supersede:** T-002 or T-003; their microphone-driver fixes and verification remain valid

This task records the approved architecture and the work required to make it
normative and implemented. Until the documentation stage below is reviewed and
approved, the existing normative protocol documents continue to describe the
current contract. This task is a plan, not a runtime protocol.

The companion [illustrated design aid](T-004-agent-boundary-options.html) explains
the approved architecture visually. It is informative, not normative.

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
an immutable typed `InputSessionContext` and only these adapter-local states:

| State | Meaning |
|---|---|
| `IDLE` | No readiness call or conversation is active |
| `ACCEPTING` | `accept_conversation().__aenter__()` has announced readiness and the adapter is preparing one complete input conversation |
| `ACTIVE` | `__aenter__()` returned one ready-to-use `InputConversation` |
| `CLOSING` | Application or adapter closure committed; no new conversation is accepted and pending operations are being released |
| `CLOSED` | The persistent input session cannot accept another conversation |

There is no `RESERVED` state and no half-created core conversation. The
per-input supervisor owns only this loop:

```python
async with input_adapter.open_session() as input_session:
    while not input_session.closed:
        try:
            async with input_session.accept_conversation() as input_conversation:
                await bridge_conversation(
                    input_conversation=input_conversation,
                    agent=agent,
                    context_provider=context_provider,
                )
        except InputSessionClosed:
            break
```

`accept_conversation()` returns an asynchronous context manager and is legal only
in `IDLE`. Its `__aenter__()` atomically enters `ACCEPTING`, exposes readiness in
the adapter's own medium, and does not return until the adapter has constructed a
fully usable `InputConversation` containing:

- a complete, accepted, non-whitespace `initial_message`;
- an immutable typed `InputConversationContext` with the medium and other
  request-scoped input facts;
- a fresh `ConversationId` assigned from the common ID factory;
- working input-control and assistant-output operations.

Everything before `__aenter__()` returns is input-session work, not a core
conversation. Cancellation, rejected speech, recoverable adapter failure, or
incomplete external input before that boundary is handled entirely by the input
adapter. It either continues accepting or returns to `IDLE`. Idle disconnect or
unrecoverable failure raises typed `InputSessionClosed` and moves to `CLOSED`.
There is no `ConversationEnded` obligation because no core conversation was
exposed.

Successful `__aenter__()` moves directly from `ACCEPTING` to `ACTIVE`. A second
external start while `ACTIVE` is rejected by the input binding and is never
queued as another internal conversation. For websocket input it is an external
protocol violation and closes the websocket session after a typed rejection
where safe. A microphone remains governed by its media state and cannot create a
new conversation while one is active.

The `accept_conversation()` context manager's `__aexit__()` owns exact-once
cleanup of the yielded InputConversation. Normal or recoverable conversation exit
returns the InputSession to `IDLE`; input-session closure moves it to `CLOSED`.
It does not return until assistant output has completed or aborted and media
cleanup is finished. The next readiness announcement therefore cannot overlap
the previous conversation.

If `close()` has already committed `CLOSING`, InputConversation context exit
participates in teardown but cannot return the session to `IDLE`; the close
operation remains the sole owner of the final `CLOSING -> CLOSED` transition.

`InputSession.close()` is an idempotent application-lifecycle operation. Before
its first suspension it commits `IDLE`, `ACCEPTING`, or `ACTIVE` to `CLOSING`,
prevents further acceptance, and releases every pending `accept_conversation()`
or active `receive_control()` with `InputSessionClosed`. It may then await its
input-local transport/media teardown and moves exactly once to `CLOSED`. Calling
it again awaits the same close result. The close path never waits for new user
input and never exposes a half-created core conversation.

Close wins if `ACCEPTING -> ACTIVE` and `CLOSING` are simultaneously eligible.
If active creation already committed, the ready InputConversation exists and its
control receive resolves `InputSessionClosed`; otherwise
`accept_conversation().__aenter__()` raises `InputSessionClosed` and exposes no
core conversation.

### Core startup ownership and scoped interfaces

The input adapter owns acceptance and construction of the ready-to-use
`InputConversation`. The core bridge lifecycle begins only after that object is
returned. At that boundary the bridge:

1. enters `STARTING`;
2. adopts `input_conversation.context.conversation_id` for state and logging;
3. immediately starts one InputConversation control receive;
4. synchronously resolves the immutable agent-facing `ConversationContext` from
   the typed input context and then checks whether input control is already ready;
5. races AgentConversation entry against that same input-control receive;
6. races initial-message delivery against the same input-control receive;
7. enters the ordinary turn state machine only after Agent input acceptance
   commits.

```python
async def bridge_conversation(input_conversation, agent, context_provider):
    state_machine = BridgeStateMachine.starting(
        conversation_id=input_conversation.context.conversation_id
    )
    input_control = create_task(input_conversation.receive_control())
    context_result = context_provider.resolve(input_conversation.context)
    startup = state_machine.select_context_or_ready_input(
        context_result,
        input_control,
    )
    if isinstance(startup, StartupEnds):
        return await state_machine.finish_startup(
            startup,
            input_conversation,
            input_control,
        )

    async with AsyncExitStack() as resources:
        entry = await state_machine.enter_agent_or_input_wins(
            resources=resources,
            agent_context=startup.agent_context,
            agent_factory=agent,
            input_control=input_control,
        )
        if isinstance(entry, StartupEnds):
            return await state_machine.finish_startup(
                entry,
                input_conversation,
                input_control,
            )
        delivery = await state_machine.deliver_user_message_or_input_wins(
            message=input_conversation.initial_message,
            agent_conversation=entry.agent_conversation,
            input_control=input_control,
        )
        if isinstance(delivery, ConversationEnds):
            return await state_machine.finish_delivery(
                delivery,
                input_conversation,
                input_control,
            )
        return await state_machine.run(
            input_conversation=input_conversation,
            agent_conversation=entry.agent_conversation,
            pending_input_control=input_control,
        )
```

The pseudocode expresses ownership, not exact method names. The input side is
already active before the bridge exists, so every bridge-startup outcome can be
reported through `InputConversation`.

Context resolution is a synchronous, non-blocking pure operation: it performs no
I/O, starts no background work, and contains no `await`. It may inspect only the
typed input context and already-available immutable configuration or snapshots.
After it returns, the bridge evaluates the complete ready set; input terminal
control wins over a context result according to the ratified priority.
`finish_startup(...)` consumes an already-ready control event or cancels and
joins the pending receive when a context outcome ends startup; it never leaks the
watcher task.

Agent factory entry is an owned task raced against the already-pending input
control task. If input cancellation, recoverable input failure, input-session
close wins, the bridge cancels and joins Agent entry before continuing terminal
cleanup. If entry and input control are both ready, input control wins. A
successfully entered context is already registered in the bridge's
`AsyncExitStack` and is exited; a cancelled or failed `__aenter__()` must clean
every partially acquired resource before it propagates because Python will not
call `__aexit__()` for an entry which never committed.

Agent entry has the same explicit cancellation deadline and hard-containment
policy as active AgentConversation cancellation. Agent startup failure becomes
`AGENT_FAILED`; cancellation becomes `INPUT_CANCELLED`; and a broken bridge or
startup-cleanup invariant terminates the server process as specified below. The
surrounding InputConversation context still receives a bounded best-effort typed
terminal outcome and performs cleanup when containment permits normal return.

Every initial or follow-up `send_user_message()` uses the same owned-delivery
race. The bridge remains in `DELIVERING_USER_MESSAGE` while the handoff is
pending. Agent acceptance commits when the AgentConversation takes the message
from its zero-capacity input rendezvous and returns typed `AgentInputAccepted`;
commit and publication of that typed result are one atomic actor transition.
Before that boundary, cancellation of the handoff prevents acceptance; after it,
coroutine cancellation cannot erase the accepted result. The bridge evaluates
the complete ready set, and input terminal control wins if it and acceptance are
ready together. If acceptance already committed, its typed result is preserved,
but the bridge still follows the winning terminal input, invokes acknowledged
Agent cancellation, and never enters `WAITING_FOR_AGENT` for that turn.

After consuming a follow-up `UserMessage`, the bridge immediately starts the
next input-control receive before beginning Agent delivery. If input control
wins either delivery race, the bridge cancels and joins the handoff task, invokes
`AgentConversation.cancel(reason)`, and completes ordinary terminal cleanup. A
handoff which ignores cancellation is contained by the same explicit Agent
cancellation deadline and process-fatal policy. No delivery or watcher task may
escape the conversation scope.

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

### AgentConversation flow control and cancellation

`AgentConversation` is active, but it cannot buffer output ahead of the bridge.
Its agent-to-bridge event channel is a zero-capacity rendezvous:

- the bridge awaits `receive_event()`;
- the agent-side producer awaits `emit(event)`;
- an event transfers only when both operations meet;
- `emit()` remains blocked while the bridge is blocked by input pushback;
- no AgentConversation-owned unbounded queue is permitted.

Model, tool, callback, and helper tasks owned by the AgentConversation must use
the same awaited production path. If an external library cannot be paused, its
adapter may use only an explicitly bounded internal buffer with a documented
bound and blocking overflow behavior. It may not silently recreate an unbounded
agent-side queue.

The bridge-to-agent control surface contains:

| Operation | Contract |
|---|---|
| `send_user_message(message)` | Offers one complete accepted user turn through a zero-capacity input rendezvous and returns typed `AgentInputAccepted` only after the AgentConversation commits acceptance |
| `cancel(reason)` | Idempotent typed cancellation; the first committed reason wins and later calls await the same result |

`send_user_message()` is cancellation-safe. Cancellation before its acceptance
commit guarantees that the message cannot later be accepted. Cancellation after
commit preserves `AgentInputAccepted`; the bridge must then use acknowledged
`cancel(reason)` to stop the accepted work. The operation owns no detached task
or hidden input queue.

`cancel(reason)` cancels every model request, tool call, producer, and background
task owned by that AgentConversation. It closes the output rendezvous and returns
`AgentCancellationAcknowledged` only after all owned work is quiescent and no
future event can be emitted. Context exit remains exact-once and cannot complete
before that condition.

The core defines an explicit agent-entry/cancellation deadline. Missing it is a
broken sealed-lifetime invariant, not an ordinary slow response. Closing an input
session or raising from one bridge task is not containment because escaped work
could survive and the shared Agent factory serves other conversations.

On deadline expiry the bridge invokes an application-wide fatal termination
controller. The server stops accepting work, logs `INTERNAL_FAILURE` with stable
conversation context, and attempts terminal notification/log flushing for only a
separate short bounded interval. It then terminates the entire process with a
non-zero exit status without waiting indefinitely for AgentConversation context
exit. A local exception is insufficient. The service supervisor or container may
restart the server; no in-process conversation or Agent factory is reused.

Both deadlines and the fatal-notification bound are explicit named settings or
constants with tests, not hidden asyncio defaults. Unit tests inject a termination
hook; a subprocess integration test proves that the real top-level policy exits
non-zero when acknowledgement never arrives.

### Application lifecycle and bounded graceful shutdown

Ordinary application shutdown is owned above the conversation protocol. It does
not add `SERVER_SHUTDOWN` to either terminal enum and does not require a bridge
shutdown signal. Instead, the existing `SIGINT`/`SIGTERM` lifecycle closes the
sealed persistent InputSessions so every active bridge observes its ordinary
`InputSessionClosed` path.

`shutdown.grace_period_seconds` is a required positive finite configuration
value with no built-in or command-line fallback. If a command-line override is
added later, it takes precedence over the config value. The first `SIGINT` or
`SIGTERM` atomically starts one application-wide deadline and this ordered shutdown:

The application runtime owns an InputSession/supervisor registry. A session is
registered before it can announce readiness or accept work and deregistered only
after both the session and its supervisor are fully closed. Closing the admission
gate and taking the shutdown snapshot are one atomic lifecycle transition, so no
session can appear between those operations.

1. enter application `SHUTTING_DOWN` exactly once and stop websocket admission,
   input readiness announcements, and creation of new InputSessions;
2. concurrently invoke idempotent `close()` on every registered websocket and
   microphone InputSession; each close commits `CLOSING` before waiting;
3. allow active bridges to observe `InputSessionClosed`, abort any open input
   sink, invoke acknowledged Agent cancellation, and exit both scoped contexts;
4. await all InputSession supervisors and bridge tasks, then close the shared
   Agent factory and remaining shared resources such as Home Assistant;
5. complete aiohttp runner and input-manager cleanup and exit with status zero if
   all work finishes before the single global deadline.

Transport closure may make terminal delivery impossible, so graceful shutdown
does not promise `ConversationEnded` to an external client. Websocket clients
receive the binding's ordinary going-away close where possible; microphone
adapters perform their ordinary stop/re-arm teardown. `InputSessionClosed` is the
only conversation-level observation.

If the global deadline expires, the application logs the still-open resource and
stable session/conversation identifiers, then invokes the hard-exit controller
and terminates non-zero. A second `SIGINT` or `SIGTERM` during `SHUTTING_DOWN`
invokes that non-zero hard exit immediately without extending the deadline or
waiting for additional cleanup. This ordinary-shutdown escalation and the fatal
Agent-lifetime invariant controller may share a top-level hard-exit primitive,
but they retain distinct logs and triggers.

Unit tests use an injected clock, closeable fake InputSessions, and hard-exit
hook. Subprocess tests send one signal and prove bounded status-zero cleanup,
hold one close path beyond the deadline and prove non-zero exit, and send a
second signal to prove immediate non-zero escalation.

### Conversation creation

An internal bridge conversation is created only after
`accept_conversation().__aenter__()` returns a ready InputConversation with a
complete, accepted, non-empty initial user message:

- websocket input combines its external conversation-start operation with the
  submitted initial text;
- microphone input waits for an accepted final STT result;
- whitespace-only input is invalid;
- the core never owns an empty conversation waiting indefinitely for first input.

The input adapter assigns one conversation ID from the common ID factory at the
successful `ACCEPTING -> ACTIVE` transition. The bridge adopts it and passes it
into the agent-facing context. It is stored in the scoped contexts and logging
context rather than repeated on every in-process event.

### Typed context

Replace generic mutable conversation attributes with immutable typed context:

- `InputSessionContext` contains identity and stable capabilities belonging to
  the persistent input session;
- `InputConversationContext` contains the conversation ID, medium, input
  identity, and immutable request-scoped facts known when the input is accepted;
- agent-facing `ConversationContext` is resolved from that typed input context
  before the AgentConversation is created and adds resolved user settings needed
  by the agent;
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

The initial `UserMessage` is exposed once as
`InputConversation.initial_message` and delivered to the AgentConversation by the
bridge after agent startup. It is not redundantly emitted on the active input
event stream.

There are no core `UserInputStarted` or `UserInputAborted` events. Those are
media-specific details unless and until a future requirement proves they must
cross the abstract interface.

### Bridge to AgentConversation

| Event | Meaning |
|---|---|
| `UserMessage` | A complete accepted user turn |

Cancellation is conveyed through the typed, acknowledged
`AgentConversation.cancel(reason)` operation, not disguised as a user message or
assumed from cancellation of a bridge-side `receive_event()` coroutine.
`AgentInputAccepted` is the typed result of `send_user_message()`, not an
AgentConversation-to-bridge output event.

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
| `ConversationEnded` | The conversation ended with a closed reason, conditional typed rejection code, and optional diagnostic detail |

`FollowUpRequested` is a transactional control operation rather than a
fire-and-forget event. A successful operation returns a typed
`FollowUpRequestCommitted` token. The bridge uses that token to acknowledge that
it has entered `WAITING_FOR_FOLLOW_UP`; an adapter must not expose a follow-up
outcome before that acknowledgement. The token is scoped to exactly one
follow-up interval and cannot be reused. It is an in-process operation result;
it is not serialized to the external websocket client.

The terminal payload is structurally typed:

```python
@dataclass(frozen=True)
class ConversationEnded:
    reason: ConversationEndReason
    context_rejection_code: ContextRejectionCode | None = None
    detail: str | None = None
```

`context_rejection_code` is required exactly when
`reason == CONTEXT_REJECTED` and forbidden for every other reason. The initial
closed `ContextRejectionCode` set is:

- `UNKNOWN_USER`;
- `NOT_AUTHORIZED`;
- `UNSUPPORTED_INPUT_CONTEXT`.

Bindings serialize the code as its stable wire value. They never infer it from
`detail`.

`AssistantMessageAborted` is the observable result of the bridge-to-input
`abort(...)` operation, not an agent-to-bridge event. It is emitted only if the
input-side assistant stream opened and did not already complete.

The input-side presenter decides how to display, speak, cue, or ignore
`ProcessingUpdate`. It decides how a follow-up offer is presented and starts its
timeout only when the user has actually been presented with that offer. For a
microphone this is the adapter; for websocket input it is the external client.

## Follow-up ownership

`REQUEST_FOLLOW_UP` expresses agent intent only. It carries no duration and does
not start a core timer.

After `FollowUpRequested` commits, its send operation completes, and the bridge
atomically enters `WAITING_FOR_FOLLOW_UP` and acknowledges the commit token, the
bridge waits for exactly one of:

- `UserMessage`;
- `FollowUpTimedOut`;
- `ConversationCancelled`;
- `InputConversationFailed`;
- `InputSessionClosed`.

The bridge-side ordering is equivalent to:

```python
commit = await send_follow_up_or_input_terminal_wins(
    input_conversation,
    pending_input_control,
)
state_machine.follow_up_committed(commit)
input_conversation.acknowledge_follow_up_ready(commit)  # synchronous
```

`send_follow_up_or_input_terminal_wins(...)` owns and joins both operations. If
terminal input wins, it performs terminal cleanup and never acknowledges a
follow-up token. If the send crossed an irreversible medium commit point, its
adapter operation is allowed to drain or fail according to that binding while
terminal cleanup remains the selected conversation outcome. The final two lines
have no intervening suspension point: the state changes before acknowledgement
can expose a retained outcome.

The microphone adapter may base presentation on cues and playback completion.
For websocket input, the external client—not the server-side websocket adapter—
owns presentation and timing. The client starts its configured local timer only
after its UI has displayed the follow-up prompt and sends `FollowUpTimedOut` if
that timer expires. Transport handoff is not presentation. Other media may use
different rules; the component which actually presents the offer owns the clock.

Every presenter-owned timer serializes its response and expiry locally so only
one event crosses `InputConversation`. For a microphone, speech start wins the
boundary race:

1. the adapter uses one atomic local state transition for speech start and timer
   expiry;
2. speech detected at or before the expiry boundary commits the response path,
   cancels that timer, and permanently suppresses its `FollowUpTimedOut`;
3. the adapter completes STT and sends `UserMessage` only after it accepts a
   complete, non-whitespace transcript;
4. if that speech attempt produces no accepted transcript, the adapter may begin
   a newly presented/listening timeout cycle or emit its typed input failure, but
   it never revives the losing timeout from the previous cycle;
5. expiry committed before speech start emits exactly one `FollowUpTimedOut`, and
   later speech is ignored or rejected locally rather than exposed to the bridge.

If speech start and expiry are both ready when the adapter arbitrates, speech
start wins. Focused tests use a controllable monotonic clock and barrier to prove
the before, exact-boundary, and after-boundary cases.

The server-side bridge and websocket adapter start no follow-up timer and require
no `FollowUpPresented` acknowledgement. They wait for `UserMessage`,
`FollowUpTimedOut`, `ConversationCancelled`, input failure, or connection close.
A conforming websocket client:

1. is configured with an explicit `follow_up_timeout_seconds` policy rather than
   a hidden default;
2. starts the timer only after rendering `FollowUpRequested` to the user;
3. serializes local timeout and user submission so exactly one is sent;
4. cancels the timer on user submission, cancellation, `ConversationEnded`, or
   connection closure;
5. sends one typed `FollowUpTimedOut` when expiry wins.

Websocket frames preserve the client's chosen order. A follow-up outcome
validated after committed transport handoff but before bridge acknowledgement
is retained by the gate described below and remains valid. An outcome validated
before that handoff, a duplicate outcome, or a late timeout after the interval
closes is an external protocol violation and follows the binding's
rejection/close rule. A client which neither responds nor times out leaves the
conversation semantically waiting; the separate websocket follow-up resource
lease below eventually closes that InputSession without inventing a presentation
time or emitting `FollowUpTimedOut`. Repository
interactive and batch clients must implement the same semantic state rule, with
their timeout supplied by explicit client configuration or command-line input.

### Websocket capacity and follow-up resource lease

Semantic follow-up timing remains client-owned. Server resource lifetime is a
separate binding policy with required configuration and no hidden defaults:

- positive integer `websocket.max_connections`;
- positive integer `websocket.capacity_retry_after_seconds`;
- positive finite `websocket.follow_up_idle_lease_seconds`.

The websocket server atomically reserves one connection slot before upgrading
an HTTP request. The cap covers every admitted websocket connection, whether
idle, accepting, or active. Reservation never waits or queues. If no slot is
available, the server does not upgrade and returns HTTP `503 Service Unavailable`
with `Retry-After` equal to the configured value. Every successful reservation
has one owner and is released exactly once in the connection handler's final
cleanup, including handshake failure, protocol rejection, and shutdown.

When `FollowUpRequested` commits at websocket transport handoff, the server-side
adapter atomically opens one follow-up-outcome gate and starts one non-semantic
resource lease for that interval. Heartbeats, pings, and pongs do not reset it.
The gate remains closed to the bridge until the bridge acknowledges the matching
`FollowUpRequestCommitted` token after entering `WAITING_FOR_FOLLOW_UP`.
The lease is cancelled when a valid `UserMessage`, `FollowUpTimedOut`,
`ConversationCancelled`, input failure, or connection close leaves the interval.
Every later follow-up interval gets a fresh token, gate, and lease.

Websocket flow-control drain may still block after transport handoff. During
that interval the adapter retains at most one complete, validated
`UserMessage` or `FollowUpTimedOut` in an input-owned single-value register. It
does not expose that outcome through `InputConversation` until the bridge
acknowledges the token. This is not a bridge queue: the register exists only for
the current committed follow-up request, cannot accept a second outcome, and is
cleared on acknowledgement-and-delivery or every terminal path. A second
follow-up outcome is an external protocol violation rather than queued work.

`ConversationCancelled`, `InputConversationFailed`, and `InputSessionClosed`
remain continuously observable and bypass the follow-up gate. If one is ready
together with a retained outcome, the normal input-terminal priority wins and
the adapter discards the retained outcome during exact-once cleanup. The bridge
performs the state transition and token acknowledgement synchronously, with no
`await` or cancellation point between them; acknowledgement makes an already
retained outcome eligible only after the state transition has committed.

Lease expiry and validated client input are serialized by one adapter-owned
atomic decision. Client input commits to that arbiter when the background reader
has received and validated one complete frame; raw byte arrival is not a commit.
An application event committed at or before the monotonic lease deadline wins,
including simultaneous readiness, cancels the lease, and is exposed normally.
Once expiry commits, later client input is not exposed to the bridge.

A follow-up outcome received and validated before transport handoff is illegal,
because no follow-up interval exists. At handoff, creation of the commit token,
opening the gate, and starting the lease are one adapter transition; a validated
outcome ordered after that transition is retained or exposed according to the
gate. If the websocket send fails before handoff, no token, lease, or follow-up
eligibility exists. If drain or transport fails after handoff but before bridge
acknowledgement, the adapter clears any retained outcome, closes the input
session, and exposes only terminal input control. If the lease expires during
drain before an outcome wins, it follows the same terminal cleanup path.

If the lease expires first, the adapter closes the websocket with code `1013`
(`Try Again Later`) and the stable close reason
`follow-up resource lease expired`. It emits no `FollowUpTimedOut` and no other
conversation event; transport closure produces ordinary `InputSessionClosed`,
which cancels Agent work and closes the bridge. Lease expiry is not an external
protocol violation and is logged as server resource-policy enforcement.

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
5. Every input binding declares an adapter-specific commit point for start,
   chunk, completion, and abort. “Accepted into an adapter buffer,” “committed to
   the medium,” and “drained/presented” are distinct milestones.
6. `abort()` has priority only over operations which have not crossed their
   declared commit point. It discards adapter-buffered, uncommitted output and
   stops rendering where possible. It cannot retract a committed chunk or
   completion.
7. If completion commits first, the stream is `COMPLETED` and later abort emits
   nothing. The conversation may still end with `INPUT_CANCELLED`. If abort
   commits before completion's commit point, completion cannot later commit and
   the stream is `ABORTED` exactly once.
8. Websocket start/chunk/completion/abort commits when the complete frame is
   handed to the websocket transport. Aiohttp flow-control drain may still be
   awaited afterward and provides pushback, but it is not the commit point.
   Cancellation after transport handoff cannot override that frame. The binding
   does not claim client receipt or presentation because no client ACK is added.
   The websocket adapter serializes writes in an adapter-owned writer operation
   which is not cancelled once transport handoff may have begun. Abort may win at
   the gate before that write starts; after it starts, the write either commits
   and drains or fails and closes the input session. Thus bridge cancellation can
   never reinterpret an in-flight `send_str()` as definitely unsent.
   `FollowUpRequested` additionally uses the typed commit-token and single-value
   outcome gate defined above. Its send still blocks through flow-control drain;
   an early validated reply is retained by the adapter, not the bridge, until
   the bridge has entered and acknowledged `WAITING_FOR_FOLLOW_UP`.
9. A microphone adapter declares commit in terms of its renderer. Text or audio
   waiting only in its bounded buffer is uncommitted and may be discarded;
   anything already handed to irreversible playback remains committed.
10. Sink operations preserve their commit record across coroutine cancellation.
    `complete()` and `abort()` return a typed terminal result such as
    `COMPLETED`, `ABORTED`, or `INPUT_SESSION_CLOSED`; the bridge never infers the
    result from whether an awaited drain happened to return.
11. Normal conversation exit awaits adapter drain after terminal commitment.
    Transport or renderer failure after a possible external commit follows the
    adapter's declared failure/closure rule and never causes a contradictory
    second terminal event.
12. Already committed or rendered text and speech remains part of the
    interaction. Abort does not retract it.
13. Microphone adapters must be able to render/speak chunks without waiting for
   the complete message in the target design, although concrete streaming-TTS
   engine work may be staged as described under Future microphone work.

The bridge concurrently observes input control events while awaiting agent output
or an input sink operation and requests abort without polling. The input sink
then arbitrates against its actual commit state: uncommitted buffered renderer
work can be pre-empted, while an in-flight websocket write which may have crossed
transport handoff must finish or close the session. Only the input knows its
accepted, committed, buffered, drained, and rendered state.

The input sink enforces this renderer-visible state machine:

| State | Legal operations and transitions |
|---|---|
| `NOT_STARTED` | A committed `start()` enters `OPEN`; termination before start commits emits no assistant abort because no stream became externally visible |
| `OPEN` | Accepted/committed chunks remain `OPEN`; `complete()` begins `COMPLETING`; a committed `abort()` enters `ABORTED` |
| `COMPLETING` | Completion is pending its adapter-specific commit point; abort may win only before that point |
| `COMPLETED` | Completion committed; drain may still be pending, but later abort reports already completed and emits nothing |
| `ABORTED` | Abort committed; drain/stop cleanup may still be pending, and repeated cleanup emits nothing further |

The adapter's operation result identifies the committed state. Coroutine
cancellation cannot erase a commit which already crossed the binding's declared
boundary. The bridge uses the typed terminal result, rather than await-return
timing, queue contents, or delivery guesses, when logging and closing the
conversation.

## State machine

The normative protocol must define a complete matrix over source, event, and
state. The minimum conversation states are:

| State | Meaning |
|---|---|
| `STARTING` | A ready InputConversation exists; agent context is being resolved and AgentConversation opened |
| `DELIVERING_USER_MESSAGE` | The initial or follow-up user message is being offered to the Agent while input terminal control remains observable |
| `WAITING_FOR_AGENT` | User message was delivered; no assistant stream is open |
| `STREAMING_ASSISTANT` | One assistant stream is open |
| `WAITING_FOR_DISPOSITION` | Assistant stream closed; explicit disposition is required |
| `COMMITTING_FOLLOW_UP` | `REQUEST_FOLLOW_UP` was accepted and the input operation is committing/draining `FollowUpRequested`; terminal input remains observable, but follow-up outcomes are adapter-gated |
| `WAITING_FOR_FOLLOW_UP` | Follow-up handling was delegated to the input-side presenter: microphone adapter or external websocket client |
| `ENDING` | Terminal event is being rendered and scoped resources are closing |
| `CLOSED` | Cleanup is complete; no further events are legal |

Required transitions include:

```text
ready InputConversation, resolved context, and opened AgentConversation
    STARTING -> DELIVERING_USER_MESSAGE

initial AgentInputAccepted
    DELIVERING_USER_MESSAGE -> WAITING_FOR_AGENT

ContextRejected
    STARTING -> ENDING(CONTEXT_REJECTED) -> CLOSED

ContextUnavailable
    STARTING -> ENDING(CONTEXT_UNAVAILABLE) -> CLOSED

invalid context-provider result or exception
    STARTING -> FATAL(INTERNAL_FAILURE) -> terminate server process non-zero

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
        -> COMMITTING_FOLLOW_UP

FollowUpRequestCommitted returned after successful input send/drain;
bridge enters state and synchronously acknowledges matching token
    COMMITTING_FOLLOW_UP -> WAITING_FOR_FOLLOW_UP

UserMessage
    WAITING_FOR_FOLLOW_UP -> DELIVERING_USER_MESSAGE

follow-up AgentInputAccepted
    DELIVERING_USER_MESSAGE -> WAITING_FOR_AGENT

Agent input handoff failure
    DELIVERING_USER_MESSAGE -> ENDING(AGENT_FAILED) -> CLOSED

FollowUpTimedOut
    WAITING_FOR_FOLLOW_UP -> ENDING -> CLOSED
```

An aborted assistant stream transitions to `ENDING`; an agent does not continue
the same turn after abort and no disposition is required. Agent failure before a
stream opens also transitions directly to `ENDING`. Cancellation and
input-session closure are legal in every non-terminal state and pre-empt agent
work, including while Agent input delivery is blocked. All other unlisted
transitions are protocol violations, not tolerant no-ops.

`UserMessage` and `FollowUpTimedOut` are never legal bridge-visible events in
`COMMITTING_FOLLOW_UP`; the adapter gate makes that source/state combination
unrepresentable for conforming implementations. Terminal input remains legal in
that state and pre-empts the pending sink operation. A post-handoff websocket
write is joined according to its non-retractable commit rule before cleanup.

### Concurrent-event priority

When multiple awaited operations are ready before the bridge commits its next
transition, it selects exactly one using this precedence:

1. `INTERNAL_FAILURE`;
2. `INPUT_SESSION_CLOSED`;
3. `INPUT_FAILED`;
4. `INPUT_CANCELLED`;
5. `CONTEXT_UNAVAILABLE`;
6. `CONTEXT_REJECTED`;
7. `AGENT_FAILED`;
8. `FOLLOW_UP_TIMEOUT`;
9. ordinary `AgentInputAccepted`, agent output, or `TurnDisposition`.

Priority applies only to results already ready at the bridge's decision point.
Once an Agent-input acceptance or input-sink terminal operation crosses its
declared commit point, a later event does not retroactively replace it. For
example, input termination ready together with `AgentInputAccepted` wins the
transition but uses acknowledged cancellation because acceptance may already
have committed. Likewise, cancellation ready while assistant completion is
still uncommitted wins and invokes abort; cancellation observed after websocket
completion was handed to the transport ends the conversation without changing
the completed stream, even if transport drain is still blocked.

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

### Context-provider contract

The typed context provider is sealed behind:

```python
def resolve(
    input_context: InputConversationContext,
) -> ContextResolved | ContextRejected | ContextUnavailable:
    ...
```

`resolve()` is synchronous and must be fast: no I/O, waiting, task creation, or
mutable external lookup is permitted. `ContextUnavailable` reports that a
required already-maintained snapshot/dependency state is unavailable; resolution
does not wait for it to recover.

Expected outcomes are values, not exceptions:

| Outcome | Meaning | Conversation result | InputSession reuse |
|---|---|---|---|
| `ContextResolved(context)` | Immutable agent-facing context was resolved | Continue agent startup | Yes |
| `ContextRejected(code, detail)` | Known identity/request data is validly rejected | `CONTEXT_REJECTED` | Yes |
| `ContextUnavailable(detail)` | Settings, identity, or another context dependency is temporarily unavailable | `CONTEXT_UNAVAILABLE` | Yes; no automatic retry |

`code` is a closed machine-actionable rejection enum; `detail` is diagnostic and
must not be parsed for control flow. Invalid provider return types, malformed
resolved context, impossible state, or an unhandled provider exception is a
provider/bridge contract violation: classify it as `INTERNAL_FAILURE`, attempt
bounded terminal delivery through the already-ready InputConversation, and invoke
application-wide fatal termination. Closing only the input session or raising
locally is insufficient.

Context rejection and unavailability are conversation-local. After the typed end
event and InputConversation cleanup, the persistent input session may announce
readiness again. The provider performs no automatic retry inside this
conversation.

Failure classification is intentional:

| Source | Classification | Required result |
|---|---|---|
| Valid context rejection | `CONTEXT_REJECTED` | End only the conversation with typed rejection code/detail; keep input session reusable |
| Context dependency unavailable | `CONTEXT_UNAVAILABLE` | End only the conversation; keep input session reusable; no automatic retry |
| Invalid provider result or unhandled provider exception | `INTERNAL_FAILURE` | Attempt bounded terminal delivery and terminate the server process non-zero |
| Invalid/unhandled agent output or agent exception | `AGENT_FAILED` | Abort open stream, end only the conversation, keep recovered input session reusable |
| Recoverable input-adapter operation failure | `INPUT_FAILED` | Abort open stream and end only the conversation |
| Input disconnect or unrecoverable adapter failure | `INPUT_SESSION_CLOSED` | Abort conversation and close persistent input session |
| Invalid external websocket message | Binding-level protocol rejection | Send typed rejection where delivery is safe, then close websocket input session |
| Broken bridge invariant | Internal fatal defect | Attempt bounded terminal notification/logging and terminate the server process non-zero |
| Agent entry/cancellation deadline missed | `INTERNAL_FAILURE` | Invoke application-wide fatal termination; a local exception or input-session close is insufficient |

There is no automatic retry after an unhandled agent failure.

### Terminal reasons

`ConversationEnded` and `AssistantMessageAborted` use separate closed enums for
machine-actionable reasons plus separate optional diagnostic detail. The
`ConversationEndReason` set is:

- `COMPLETED`
- `INPUT_CANCELLED`
- `FOLLOW_UP_TIMEOUT`
- `CONTEXT_REJECTED`
- `CONTEXT_UNAVAILABLE`
- `AGENT_FAILED`
- `INPUT_FAILED`
- `INPUT_SESSION_CLOSED`
- `INTERNAL_FAILURE`

The `AssistantAbortReason` set is:

- `INPUT_CANCELLED`
- `AGENT_FAILED`
- `INPUT_FAILED`
- `INPUT_SESSION_CLOSED`
- `INTERNAL_FAILURE`

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
- `ConversationEnded.context_rejection_code`, required exactly for
  `CONTEXT_REJECTED` and omitted otherwise;
- websocket close behavior and close codes;
- maximum message sizes, ingress queue bound, and overflow behavior;
- liveness/heartbeat behavior;
- the websocket transport-handoff commit point, its distinction from
  flow-control drain, and failure after possible handoff;
- the single-use `FollowUpRequestCommitted` token, adapter-owned one-value early
  outcome register, bridge readiness acknowledgement, and cleanup before and
  after transport handoff;
- mapping between external JSON events and the in-process typed protocol;
- client-owned follow-up presentation/timing, explicit client timeout policy,
  local timeout-versus-submission serialization, late-event rejection, and
  disconnect behavior;
- required websocket capacity and lease configuration, atomic pre-upgrade
  admission, HTTP `503`/`Retry-After` rejection, exact-once slot release, and
  follow-up lease close code/reason;
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
- speech start at or before follow-up expiry atomically wins over that timeout;
  the adapter suppresses the losing event and exposes exactly one outcome to the
  bridge;
- assistant text is consumed incrementally through a bounded output path;
- cancellation stops agent work and further rendering, then cleans up and rearms
  the persistent microphone session;
- `InputConversationFailed` versus `InputSessionClosed` reflects actual recovery;
- `ConversationEnded.context_rejection_code` remains typed through the mapping so
  the microphone adapter may choose a medium-appropriate response without parsing
  diagnostic detail;
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

1. Add the typed contexts, enums, messages, and sealed interfaces. A
   focused `ai_server/conversations/` package is preferred; keep its
   `__init__.py` minimal.
2. Implement the `InputSession` lifecycle, per-input supervisor, bridge-owned
   core startup from a ready InputConversation, and per-conversation bridge state
   machine against test factories.
3. Implement the transactional input-owned assistant sink contract, blocking
   pushback, adapter-specific commit points, concurrent input-control
   observation, and deterministic completion/abort behavior.
   Implement the same owned-operation race and typed commit semantics for every
   initial and follow-up Agent input handoff.
4. Add exhaustive core unit and conformance tests using fake InputSession,
   InputConversation, Agent, and AgentConversation implementations.
5. Implement and test the AgentConversation rendezvous, acknowledged
   cancellation, cancellation-safe factory entry, process-fatal missed deadline,
   and synchronous typed context-provider outcomes.
6. Prove sequential conversations, multiple concurrent input sessions, startup
   cleanup, priority races, isolated agent state, and absence of hidden output
   buildup under slow-input pushback.
7. Do not remove or modify the old runtime interfaces at this stage. The full
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
   Their output uses the zero-capacity rendezvous and all owned work participates
   in acknowledged cancellation.
3. Migrate Echo, Interrogator, Polite Reply, Assistant, Orchestrator, tools, and
   legacy Home Assistant integration code.
4. Require `TurnDisposition` after every successfully completed turn and no
   disposition after cancellation or failure.
5. Emit processing updates only before assistant streaming starts.

#### 3B: Prepare the websocket binding and clients within the cutover

1. Implement the approved JSON schema and typed InputSession adapter.
2. Preserve independent background reading, heartbeat, and disconnect detection.
3. Add required `max_connections`, capacity `Retry-After`, and follow-up idle
   lease configuration. Reserve capacity atomically before upgrade, return HTTP
   `503` without queueing when full, and release each slot exactly once.
4. Enforce the follow-up resource lease from committed transport handoff through
   `COMMITTING_FOLLOW_UP` and `WAITING_FOR_FOLLOW_UP`; on expiry close with
   `1013` and `follow-up resource lease expired`, producing
   `InputSessionClosed` without `FollowUpTimedOut`.
5. Implement the transactional follow-up gate: atomically create the commit
   token, open the outcome register, and start the lease at transport handoff;
   retain at most one validated early outcome while drain blocks; expose it only
   after the bridge's state acknowledgement; and clear it on every terminal
   path.
6. Bound transport ingress explicitly; let outbound `send` block according to
   websocket transport pushback rather than adding a bridge queue.
7. Migrate interactive and batch clients and shared message helpers.
   Each client owns follow-up presentation and an explicitly configured timer and
   serializes timeout versus user submission.
8. Propagate conditional `context_rejection_code` through terminal JSON.
9. Reject old, duplicate, late, or otherwise invalid external events and close
   the input session as specified.

#### 3C: Prepare the microphone binding within the cutover

1. Replace `MicrophoneAgentEndpoint` with an `InputSession`/
   `InputConversation` adapter owned by the microphone manager layer.
2. Return a ready InputConversation only after an accepted final STT result.
3. Implement adapter-owned follow-up presentation and timeout.
   Serialize speech start against expiry locally, with speech winning exact
   simultaneous readiness and the losing event never crossing the interface.
4. Implement the transactional assistant sink. Any buffer needed by slow TTS is
   bounded and owned by the microphone adapter; a full buffer blocks
   `send_text()` and pushes back naturally.
5. Preserve manager-to-driver ordering, T-002/T-003 fixes, reason strings,
   re-arm behavior, and concrete-driver encapsulation.

#### 3D: Activate and remove in the same cutover

1. Switch runtime construction to the new per-input supervisor and bridge.
2. Switch every concrete agent, websocket path, repository client, and microphone
   path to the new interfaces together.
3. Integrate `ai_server/server.py` signal handling with the InputSession
   registry. On the first signal stop admission and concurrently close all input
   sessions; await bridges and shared-resource cleanup within the required
   `shutdown.grace_period_seconds` deadline. A second signal or deadline expiry
   invokes immediate non-zero hard exit; successful cleanup exits zero.
4. Remove generic mutable `Conversation.state`, the old bidirectional endpoint,
   obsolete messages, legacy Session paths, and superseded tests.
5. Do not retain an old/new translation facade or accept the old websocket event
   vocabulary.
6. Run all focused and complete automated suites before treating the cutover as a
   coherent runnable checkpoint.

### Stage 4: End-to-end verification and closure

1. Complete the conformance catalogue with test evidence for every requirement.
2. Run focused core, agent, websocket, and microphone tests.
3. Run the complete pytest suite.
4. Run `orchestrator_and_dsa_tests/run.sh` with the currently configured model.
5. Exercise the real websocket server with repository clients, including
   admission saturation, follow-up lease expiry, disconnect, follow-up,
   cancellation, slow-send pushback, and invalid-input cases.
6. Exercise real microphone conversations one device at a time, including
   streaming, slow-TTS pushback, follow-up timeout, interruption, recovery, and
   re-arm.
7. Verify Box3 and Voice PE paths separately. Firmware changes are not implied.
8. If firmware is changed for a separately approved reason, compile and inspect
   generated `main.cpp` before any flash, then perform post-flash checks.
9. Run subprocess signal tests for successful bounded graceful shutdown,
   deadline escalation, and immediate second-signal escalation.

## Conformance coverage required

At minimum, automated tests must cover:

- every legal transition in the normative state matrix;
- representative illegal events from every source in every state;
- initial empty and whitespace-only messages;
- InputSession readiness, idle closure, second-start rejection, and direct
  `ACCEPTING -> ACTIVE` creation of one ready InputConversation;
- cancellation, input failure, and session close before input acceptance without
  exposing a core conversation;
- context-provider and agent-startup outcomes after InputConversation creation;
- input cancellation/failure/close raced against AgentConversation entry, with
  input priority on simultaneous readiness and exact cleanup of partial entry;
- initial and follow-up Agent input delivery raced against a continuously pending
  input-control receive, including cancellation, input failure, and session close
  before acceptance and simultaneous with an acceptance commit;
- typed `AgentInputAccepted` preservation, exact cancellation/join of the losing
  handoff, and acknowledged Agent cancellation if input termination wins after
  acceptance committed;
- zero assistant streams and one assistant stream per turn;
- progress before streaming and rejection of progress during streaming;
- zero-buffer agent output rendezvous and producer blocking while input send is
  blocked;
- acknowledged agent cancellation while model work, tool work, and rendezvous
  emission are active, including a subprocess proof that missed entry or
  cancellation acknowledgement terminates the server non-zero;
- stream framing, ordered chunks, completion, and abort;
- explicit end and explicit follow-up dispositions after successful turns, and
  absence of dispositions after cancellation or failure;
- presenter-owned follow-up timing and late-event races, using the microphone
  adapter or external websocket client as applicable;
- microphone speech-start versus timeout arbitration before, exactly at, and
  after expiry, including no-transcript re-arm/failure and suppression of late
  speech or the losing timeout;
- cancellation in every non-terminal state;
- simultaneous cancellation, disconnect, timeout, disposition, and stream-end
  races with deterministic outcomes;
- agent failure before output, during processing, and during streaming;
- recoverable input failure versus input-session closure;
- bridge invariant failure and cleanup;
- adapter-owned bounded-buffer backpressure, no bridge queue, and cancellation
  while the input's `send_text()` is blocked;
- definitive input-sink operation outcomes and exactly-one completion or abort;
- adapter-specific commit-point races, including websocket handoff before drain
  and abort before/after completion commit;
- normal drain versus abort discard;
- repeated sequential conversations on one persistent input session;
- concurrent conversations on multiple input sessions;
- absence of mutable state leakage between concurrent `AgentConversation`s;
- websocket schema, bounds, heartbeat, rejection, close, and old-vocabulary cases;
- atomic websocket connection admission at one below/at/above capacity, HTTP
  `503` with configured `Retry-After`, and exact-once slot release on every exit;
- follow-up resource-lease start at transport commit, cancellation on every exit
  from the committed follow-up interval, heartbeat non-extension, `1013` expiry
  close with stable reason, input-at-deadline priority, suppression of input after
  committed expiry, and absence of a server-generated `FollowUpTimedOut`;
- follow-up replies before handoff, at handoff, after handoff while drain is
  blocked, after drain but before acknowledgement, and after acknowledgement;
  typed token matching; exactly-one adapter retention; duplicate rejection;
  terminal-input priority; and exact cleanup on pre-commit failure,
  post-commit drain failure, lease expiry during drain, cancellation, and close;
- microphone accepted-STT creation, playback, follow-up, cancellation, recovery,
  and T-002/T-003 regressions;
- resolved, rejected, unavailable, malformed, and exceptional context-provider
  outcomes with the specified terminal reason and session-reuse behavior;
- `context_rejection_code` required for `CONTEXT_REJECTED`, forbidden otherwise,
  and preserved through websocket and microphone mappings;
- websocket client-owned follow-up timing from actual UI presentation, explicit
  client configuration, timeout-versus-submission races, duplicates, late
  timeout, and disconnect;
- microphone follow-up timing with speech-start priority and exact-one event
  exposure at the expiry boundary;
- application shutdown with idle, accepting, and active InputSessions; concurrent
  close propagation through `InputSessionClosed`; exact-once bridge and shared
  resource cleanup; status-zero completion before the configured deadline;
- simultaneous InputSession acceptance and application close, with close
  priority before `ACTIVE` commits and ordinary active-conversation cleanup after
  it commits;
- shutdown deadline overrun and second-signal escalation to immediate non-zero
  process exit in subprocess tests;
- startup rejection for each missing, zero, negative, non-integer where required,
  or non-finite shutdown/websocket resource setting, proving no hidden default;
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
- `ai_server/config.py` and `ai_server/server.py`
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
- Adding `SERVER_SHUTDOWN` messages or races to the conversation event
  vocabulary. Graceful application cleanup is implemented above the protocol by
  closing InputSessions and using the existing `InputSessionClosed` path.
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
   Input terminal control remains observable during every initial and follow-up
   Agent input handoff; no handoff or watcher task can escape its scope.
5. Agents return an explicit typed disposition after every successfully completed
   turn; aborted and failed turns terminate without one.
6. Follow-up presentation and timeout are owned by each input-side presenter.
   For websocket input, that owner is the external client which actually renders
   the prompt; the server starts no presentation timer. For microphone input,
   speech start wins the local race against expiry and exactly one result crosses
   the adapter boundary.
7. The bridge contains no assistant-output queue. Each input applies blocking
   pushback directly, declares its commit points, and owns any explicitly bounded
   media buffer, normal drain, and deterministic completion-versus-abort
   arbitration.
8. Terminal reasons and diagnostics are typed and unambiguous.
   `context_rejection_code` is present exactly for `CONTEXT_REJECTED` and reaches
   every applicable binding.
9. Agent, recoverable input, input-session, external protocol, internal invariant,
   and context-provider failures have the ratified isolation behavior.
10. Agent output is a zero-buffer rendezvous; acknowledged cancellation stops all
    conversation-owned work within the explicit deadline. Missing Agent entry or
    cancellation acknowledgement terminates the complete server process non-zero.
11. Concurrent agent conversations have isolated mutable state.
12. The websocket server and all repository clients use only the new binding.
13. Websocket connection capacity is bounded before upgrade, and each follow-up
    wait has a non-semantic resource lease which closes with `1013` without
    forging `FollowUpTimedOut`.
14. Follow-up output commitment and response eligibility are transactional. A
    websocket adapter retains at most one validated post-handoff outcome until
    the bridge acknowledges `WAITING_FOR_FOLLOW_UP`; no early outcome can cross
    into an illegal bridge state and no such buffering exists in the bridge.
15. First-signal graceful shutdown closes InputSessions and all scoped/shared
    resources within the required configured deadline and exits zero; deadline
    expiry or a second signal exits non-zero immediately.
16. Microphone driver protocol and T-002/T-003 behavior remain conformant.
17. Every catalogue requirement links to passing automated evidence or an
    explicitly documented manual hardware check.
18. Focused tests, full pytest, the orchestrator/DSA behavior suite, real
    websocket checks, and required microphone checks pass.

## Review gates

- **Gate A — protocol approval:** Captain approves the rewritten normative
  documents before production code changes begin.
- **Gate B — additive core review:** typed interfaces, bridge state machine,
  adapter commit contract, AgentConversation rendezvous/cancellation, typed
  context resolution, and exhaustive fake-based core tests are reviewed while
  the old runtime remains complete and active.
- **Gate C — atomic cutover review:** every agent and binding is migrated, the
  runtime switches once, all legacy surfaces are removed, and websocket and
  microphone mappings are checked against the same conformance catalogue.
- **Gate D — closure:** legacy surfaces are absent and fresh verification evidence
  satisfies every acceptance criterion.
