# T-004 Plan Review

## Status

- **Authority:** Review record
- **Review date:** 2026-07-18
- **Subject:** Uncommitted T-004 plan and related documentation changes
- **Re-review date:** 2026-07-18
- **Second re-review:** 2026-07-18 against commit `565aeaf`
- **Third independent re-review:** 2026-07-18 against the current working tree
- **Fourth independent closure pass:** 2026-07-18 against the current working tree
- **Fifth independent re-review:** 2026-07-18 against the current working tree
- **Sixth independent re-review:** 2026-07-18 against the current working tree
- **Outcome:** Review open; T-004 is not yet implementation-ready
- **Finding status:** Sixteen findings closed; one finding open

This document records a read-only review of the T-004 plan. It is evidence and
does not change the authority of T-004 or the current normative protocols.

## Scope

The original review covered every uncommitted file present at the time:

- `docs/README.md`;
- `docs/tasks/T-001-protocol-and-documentation-cleanup.md`;
- `docs/tasks/T-004-agent-boundary-options.html`;
- `docs/tasks/T-004-conversation-bridge-protocol-redesign.md`.

The current interfaces, session implementation, documentation index, and
protocol documentation standard were inspected where needed to verify claims in
the plan.

The sixth re-review covered every currently uncommitted file:

- `docs/tasks/T-004-agent-boundary-options.html`;
- `docs/tasks/T-004-conversation-bridge-protocol-redesign.md`;
- `docs/tasks/T-004-plan-review.md`.

## Summary

The sixth independent pass closes T004-PLAN-014 through T004-PLAN-016. Agent
input delivery now remains under input-control observation, application shutdown
has a bounded top-level path through sealed InputSession closure, and websocket
admission plus follow-up waiting have explicit resource bounds.

The whole-design rescan found one new race at the boundary between websocket
output commitment and bridge state. `FollowUpRequested` commits externally and
starts the lease at transport handoff, but the bridge is only documented as
entering `WAITING_FOR_FOLLOW_UP` after forwarding completes. A fast client reply
can therefore commit while flow-control drain is still pending, without a rule
that makes it legal or defers it. T-004 remains not implementation-ready until
that commit/state transition is made atomic or the early reply is safely held.

## Findings

### T004-PLAN-001 — The bridge cannot own `STARTING` or assign the conversation ID with the illustrated lifecycle

- **Severity:** High
- **Status:** Closed by third independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 166-173,
  199-200, 320-332, and 360-362 at review time

The illustrated API opens both scoped conversations before it calls
`bridge_conversation()`. The plan nevertheless assigns the bridge ownership of
the `STARTING` state, cancellation during every non-terminal state, and creation
of the conversation ID stored in the scoped objects.

Consequently, failure or cancellation inside either context manager's
`__aenter__` occurs before the bridge state machine exists. The bridge also
cannot assign an ID which the already-created scoped conversation objects were
supposed to receive at construction.

**Required resolution:** Select one lifecycle owner. Either the bridge receives
the factories or context managers and opens both scoped conversations itself, or
the supervisor owns ID assignment and a separately specified startup state
machine. Startup cancellation, failure classification, and cleanup must belong
to that owner.

**Verified resolution:** T-004 now creates the bridge state machine before
context resolution or either scoped `__aenter__`, assigns the ID before scoped
conversation construction, and makes the bridge responsible for entering and
cleaning both contexts. The original ownership contradiction is gone. Remaining
reservation-finalization problems are tracked separately under T004-PLAN-002.

**Subsequent candidate simplification:** T-004 no longer exposes a reservation or
an unentered InputConversation to the bridge. The input adapter owns acceptance,
assigns the ID from the common factory, and returns a ready InputConversation;
only then does the bridge enter `STARTING`, adopt the ID, resolve agent context,
and open AgentConversation. This preserves one owner per phase but changes the
mechanism verified above, so the next independent pass must reconfirm this
finding remains closed.

**Second re-review result:** The new phase split fixes ID and input-context
ownership, but the shown bridge directly awaits context resolution and then
`AgentConversation.__aenter__()`. Neither operation is raced against
`InputConversation` control events. A cancellation, recoverable input failure,
or input-session close during either potentially long startup operation can
therefore remain buffered until startup finishes, recreating one of the defects
T-004 is intended to remove. The state table says cancellation pre-empts every
non-terminal state, but the pseudocode and interface requirements do not provide
that behavior.

Cancellation of provider resolution or a partially completed AgentConversation
`__aenter__()` also needs an exact cleanup contract; Python does not invoke
`__aexit__()` when `__aenter__()` fails or is cancelled before committing.

**Remaining required resolution:** Require the bridge to concurrently observe
InputConversation control throughout context resolution and agent startup. Define
cancellation-safe provider resolution and Agent factory entry, including cleanup
of partially acquired resources and deterministic priority when startup result
and input terminal control become ready together.

**Third candidate resolution applied:** Context resolution is now a synchronous,
non-blocking operation which performs no I/O, waiting, or task creation. The
bridge starts its InputConversation control receive before resolving context,
checks the complete ready set after resolution, and races that same receive
against AgentConversation entry with input control winning simultaneous
readiness. Successful entry is registered in an `AsyncExitStack`; cancelled or
failed `__aenter__()` must clean its own partial acquisition. Entry cancellation
uses the same acknowledged deadline and process-fatal containment policy as an
active AgentConversation. Verify independently before changing this finding's
status.

**Third independent re-review result:** The ready InputConversation now exists
before `STARTING`, so ID assignment and input cleanup have one owner. Context
resolution cannot buffer asynchronous cancellation because it is explicitly
synchronous, non-blocking, and barred from I/O or task creation. The bridge
creates the input-control watcher before resolution, evaluates ready input before
committing the context outcome, and races the same watcher against owned Agent
entry. Input control wins simultaneous readiness. Successful entry is registered
for exact-once exit, while failed or cancelled `__aenter__()` owns partial-resource
cleanup and is bounded by the process-fatal entry deadline. These requirements
resolve the startup ownership and cancellation defect. The separately missing
server-shutdown signal is tracked under T004-PLAN-012.

### T004-PLAN-002 — The persistent `InputSession` contract is missing

- **Severity:** High
- **Status:** Closed by second fresh verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 148-162,
  188-217, 225-237, and 478-489 at review time

T-004 defines the active `InputConversation` event vocabulary, but it does not
define the persistent input-session interface which the proposed supervisor
needs. In particular, the plan does not say how the supervisor:

- waits for a complete accepted initial message;
- obtains the per-conversation context needed before agent creation;
- handles an input session closing while idle;
- announces or exposes readiness for the next conversation;
- rejects or queues a second start while the previous conversation drains and
  closes.

The current runtime has explicit handshake, readiness, and new-conversation
operations. Removing those operations without a typed replacement leaves the
new input-session supervisor and sequential-conversation lifecycle undefined.

**Required resolution:** Define the sealed `InputSession` API, its idle and
startup lifecycle, how an accepted conversation request carries initial input
and context, and how readiness and idle closure cross the adapter boundary.

**Re-review result:** The new `IDLE`/`ACCEPTING`/`RESERVED`/`ACTIVE`/`CLOSED`
lifecycle, pull-gated `accept_conversation()`, second-start rejection, and atomic
reservation-to-active handoff resolve most of the finding. One required boundary
is still absent.

If cancellation, recoverable input failure, context resolution failure, or
shutdown ends startup before `InputConversation.__aenter__` succeeds, there is no
scoped input conversation through which the bridge can send `ConversationEnded`.
T-004 also defines no `InputSession` operation which releases the reservation,
reports its typed result, and moves `RESERVED` to `IDLE` or `CLOSED`. Lines
222-225 describe return from an `InputConversation` exit only; they do not cover
a reservation which never became `ACTIVE`.

**Remaining required resolution:** Add a sealed reservation-finalization
operation or equivalent context-managed reservation contract. It must define
typed terminal delivery where possible, exact-once release, and the resulting
InputSession state for every pre-`InputConversation` startup outcome.

**Second candidate resolution applied:** T-004 removes `RESERVED` rather than
finalizing it. `accept_conversation()` is now an asynchronous context manager
whose `__aenter__()` performs all input acceptance and setup and returns only a
fully active InputConversation. Pre-return failures are input-session-local and
expose no core conversation; post-return outcomes use the ready conversation;
`__aexit__()` provides exact-once cleanup and returns the session to `IDLE` or
`CLOSED`. Verify independently before changing this finding's status.

**Verified resolution:** The direct `IDLE -> ACCEPTING -> ACTIVE` contract now
covers readiness, complete initial input, typed input context, idle closure,
second-start rejection, exact-once active cleanup, and return to readiness. By
placing all pre-return work inside the input-owned `__aenter__()`, it removes the
unfinalized reservation boundary which kept this finding open. Bridge-startup
cancellation after the ready conversation exists is tracked under
T004-PLAN-001.

### T004-PLAN-003 — Cancellation can leave the input with an unterminated assistant stream

- **Severity:** High
- **Status:** Closed by second fresh verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 248-270
  and 293-311 at review time

The bounded output design distinguishes queued output from output accepted by
the input, but it defines no acknowledgement or separate delivery state. A
possible race is:

1. the input accepts `AssistantMessageStarted` and one or more chunks;
2. `AssistantMessageCompleted` remains queued;
3. cancellation occurs;
4. the bridge discards the unrendered queue contents.

The agent-side stream is then logically complete while the input-side stream is
still open. Sending `AssistantMessageAborted` after a delivered completion would
be illegal, while failing to send it in the sequence above leaves the renderer
unterminated.

The directional inventory also defines `AssistantMessageAborted` as an
agent-to-bridge event, while cancellation requires the bridge to synthesize and
deliver an abort after it has cancelled agent work.

**Required resolution:** Define the input-acceptance or delivery handshake,
track the renderer-visible stream state, define bridge-originated abort as an
explicit direction, and specify deterministic completion-versus-abort ordering.

**Re-review result:** Removing the bridge queue, moving buffering into the input,
and making bridge-originated abort explicit fixes the original untracked-queue
race. The replacement sink contract nevertheless promises a stronger atomicity
than the websocket adapter can provide.

T-004 says that priority `abort()` can win while `send_text()` or `complete()` is
blocked, prevent that pending operation from committing, and avoid any
"possibly delivered" result. With the repository's aiohttp 3.13.5 transport,
`WebSocketWriter.send_frame()` writes or buffers the complete websocket frame
before it awaits flow-control drain. Cancellation can therefore arrive while
`send_str()` is still blocked even though the frame is already irrevocably in the
transport. In particular, an assistant-completion frame may already have been
sent while the abstract `complete()` operation still appears pending.

The HTML repeats the unsupported claim that blocked send alone gives the bridge a
definitive producer-side boundary.

**Remaining required resolution:** Define adapter-specific commit points. For a
websocket, the commit must occur when the frame is handed to the transport, not
when the awaited drain returns; abort cannot override that commit. If actual
client receipt or presentation is required, add an explicit client
acknowledgement. Update terminal priority so it arbitrates only operations which
have not already crossed their adapter's declared commit point.

**Second candidate resolution applied:** T-004 now separates adapter acceptance,
medium commitment, and drain/presentation. Each binding declares its commit
point; websocket frames commit at transport handoff before aiohttp flow-control
drain returns, no client receipt is claimed, and abort wins only while completion
is still uncommitted. An adapter-owned serialized websocket writer is not
cancelled after handoff may have begun: the write commits and drains or fails and
closes the session, so it cannot be reinterpreted as unsent. Typed sink results
preserve committed state across coroutine cancellation. The HTML uses the same
distinction. Verify independently before changing this finding's status.

**Verified resolution:** The revised contract matches aiohttp's actual write
boundary: a websocket frame commits at transport handoff, drain is a later
pushback milestone, and cancellation cannot reinterpret an in-flight write as
unsent. Adapter-specific typed results preserve whether completion or abort
committed. The original dangling-stream race and the replacement websocket
atomicity contradiction are both resolved.

### T004-PLAN-004 — The migration stages remove the old contract before its consumers are migrated

- **Severity:** High
- **Status:** Closed by fresh verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 478-527
  and 652-660 at review time

Stage 2 removes generic `Conversation.state` and the old bidirectional endpoint
abstractions. Concrete agents are not migrated until Stage 3, websocket adapters
until Stage 4, and the microphone adapter until Stage 5. Stage 6 then instructs
the implementer to remove obsolete legacy surfaces again.

This cannot leave a coherent runnable tree at the Stage 2 review gate unless a
temporary compatibility mechanism exists. The plan does not define one and also
discourages parallel old and new protocol stacks.

**Required resolution:** Move legacy removal to the final atomic cutover, or
define a narrowly bounded migration facade with its owner, lifetime, tests, and
mandatory removal point. Clarify which stages are expected to be runnable and
reviewable checkpoints.

**Verified resolution:** Stage 2 is now explicitly additive and runnable, with
the old production runtime untouched. Stage 3 groups all production consumers,
activation, and legacy removal into one atomic cutover without a translation
facade. Stage 4 performs end-to-end closure. The stage contradiction is gone.

### T004-PLAN-005 — Mandatory disposition contradicts abort and failure paths

- **Severity:** Medium
- **Status:** Closed by fresh verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 248-260,
  360-363, 393-406, and 625-643; `T-004-agent-boundary-options.html` lines
  352-357 at review time

`TurnDisposition` is required exactly once after every agent turn, but an
`AssistantMessageAborted` transitions directly to `ENDING`. An agent exception
also cannot emit a disposition. These requirements cannot all hold for aborted
or failed turns.

The terminal-reason section also associates `ConversationEnded` and
`AssistantMessageAborted` with closed enums and then defines only the conversation
terminal reason set. If interpreted as one shared enum, it would permit
nonsensical combinations such as an aborted assistant stream with `COMPLETED`.

**Required resolution:** Require one disposition after every successfully
completed agent turn. Define abort and failure as alternative terminal outcomes,
and define a separate assistant-abort reason enum or an explicit valid subset of
conversation terminal reasons.

**Verified resolution:** T-004 now requires a disposition only after a
successfully completed turn. Cancellation and failure are explicit alternatives,
agent-originated abort has been removed, bridge-to-input abort is explicit, and
the conversation-end and assistant-abort enums are separate. The HTML uses the
same successful-turn wording.

### T004-PLAN-006 — T-001 still directs fresh sessions into the superseded architecture

- **Severity:** Medium
- **Status:** Closed by fresh verification on 2026-07-18
- **Evidence:** `T-001-protocol-and-documentation-cleanup.md` lines 31-46,
  214-259, 346-360, and 651-713 at review time

The new status entry says T-004 supersedes T-001's conversation-core and
websocket redesign. The fresh-session handoff nevertheless tells maintainers to
continue from T-001 and the old normative documents. Later sections still state
that `Session` exclusively owns the lifecycle and present the superseded
conversation and websocket implementation plan as active work.

This can cause a fresh agent to continue the old design even though the new
top-level status note is present.

**Required resolution:** Route all conversation-core and websocket design or
implementation work explicitly to T-004. Limit T-001's continuation instructions
and remaining work to the microphone, firmware, and hardware verification which
T-004 does not supersede. Mark superseded design sections as historical rather
than executable instructions.

**Verified resolution:** T-001 now routes conversation and websocket work to
T-004 in its status, audience, fresh-session handoff, remaining criteria, next
steps, assumptions, historical stage headings, and verification checklist. Its
remaining executable scope is explicitly limited to device evidence.

### T004-PLAN-007 — “Design decisions ratified” overstates the current decision status

- **Severity:** Medium
- **Status:** Closed by second fresh verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 3-15,
  310-311, and 365-367; `T-004-agent-boundary-options.html` lines 240-247 at
  review time

The status says the design decisions are ratified, while the plan explicitly
defers the exact cancellation handshake and whether simultaneous terminal events
use first-observed ordering or explicit priority. These are central lifecycle
decisions with observable failure behavior.

**Required resolution:** Ratify these decisions before retaining the current
status, or change the status to distinguish the selected architecture from the
protocol decisions still pending Gate A.

**Re-review result:** T-004 now specifies the decisions which were previously
deferred: startup ownership, absence of a bridge queue, input-side
completion/abort arbitration, and terminal precedence. That part of the original
finding is resolved.

The replacement status says "Architecture and plan-review resolutions ratified"
while the same document says those resolutions remain candidate and open pending
independent review. The fresh pass has now rejected parts of the candidate
resolutions for T004-PLAN-002 and T004-PLAN-003. The status therefore presents
open, partially unsuccessful review work as ratified resolution.

**Remaining required resolution:** Change the status to distinguish ratified
architecture decisions from candidate review fixes which still have open
findings. Do not claim that the plan-review resolutions are ratified or closed
until this ledger has no open finding.

**Second candidate resolution applied:** T-004 and its HTML now say
"Architecture decisions ratified; review fixes in progress" and no longer claim
that plan-review resolutions are ratified. Verify independently before changing
this finding's status.

**Verified resolution:** Both artifacts now distinguish ratified architecture
from review fixes still awaiting closure. The status accurately describes the
current authority and no longer claims that open review findings are resolved.

### T004-PLAN-008 — AgentConversation flow control and prompt cancellation are not structural

- **Severity:** High
- **Status:** Closed by third independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 279-290,
  349-377, 420-425, and 703-719; `T-004-agent-boundary-options.html` lines
  569-575 at re-review time

T-004 describes `AgentConversation` as an active endpoint and claims that the
bridge's refusal to accept another event propagates backpressure to agent
production. That claim holds only if the agent-to-bridge channel is itself a
rendezvous or explicitly bounded channel whose producer blocks. An active agent
can otherwise continue producing into an unbounded private queue while the
bridge is blocked on the input sink, merely moving the unbounded buffer to the
agent side.

The plan likewise says cancellation uses a scoped lifecycle/cancellation
operation, but it does not require a cancellation method, acknowledgement,
background-task ownership rule, or deadline. Cancelling a bridge-side receive
coroutine does not necessarily cancel model or tool work running behind an
active AgentConversation. This leaves the original requirement for prompt input
cancellation unenforceable through the sealed interface.

**Required resolution:** Specify the AgentConversation input/output operations,
the output channel capacity or rendezvous guarantee, how producer backpressure is
enforced, and a typed cancellation operation with acknowledgement and exact-once
context cleanup. Add tests for cancellation while model/tool work is active and
while agent output is blocked by input pushback.

**Candidate resolution applied:** T-004 now requires a zero-capacity
AgentConversation output rendezvous: producer `emit()` blocks until bridge
`receive_event()` accepts the event. It defines blocking user-message delivery,
typed idempotent `cancel(reason)`, acknowledgement only after all model/tool/
producer/background work is quiescent, exact-once context cleanup, and a named
cancellation deadline whose violation is fatal `INTERNAL_FAILURE`. Conformance
cases cover active work and output blocked by input pushback. The HTML shows the
rendezvous. Verify independently before changing this finding's status.

**Second re-review result:** The zero-capacity rendezvous and acknowledged
`cancel(reason)` structurally resolve output backpressure and normal prompt
cancellation. The missed-deadline path is contradictory, however.

T-004 says context exit cannot complete until every owned task is quiescent. It
also says that when the cancellation deadline expires, the bridge closes only the
input session and "fails loudly" so escaped work cannot survive. If owned work
does not stop, context exit cannot complete; if the bridge abandons it and raises,
the escaped task still exists. Closing one input session does not stop that task
or protect other conversations using the shared Agent factory. An exception in a
websocket/session task is not process termination and does not provide
containment.

**Remaining required resolution:** Choose an enforceable fatal policy for missed
acknowledgement: terminate the server process, quarantine and permanently disable
the shared Agent factory until all work is proven stopped, or another mechanism
which actually contains escaped work. Specify how cleanup completes or why the
process must terminate; do not claim that closing only the input session prevents
survival.

**Third candidate resolution applied:** Missing the explicit Agent entry or
cancellation deadline now invokes an application-wide fatal termination
controller. It stops new work, makes only a separately bounded best-effort
terminal notification and log flush, and terminates the complete server process
non-zero without waiting indefinitely for escaped work. A local exception or
input-session close is explicitly insufficient. Unit tests inject the fatal hook,
and a subprocess integration test must prove the real top-level exit policy.
Verify independently before changing this finding's status.

**Third independent re-review result:** The output channel is structurally a
zero-capacity rendezvous, all AgentConversation-owned producers use its awaited
path, and any unavoidable library adapter buffer must be bounded and blocking.
Typed idempotent cancellation acknowledges only after all owned work is quiescent
and no future output is possible. If Agent entry or cancellation misses its
explicit deadline, T-004 now rejects local failure as containment and requires
the complete server process to exit non-zero after only bounded best-effort
notification and logging. The unit-hook and real subprocess exit tests make that
fatal policy enforceable.

### T004-PLAN-009 — Context-provider failures have no explicit classification

- **Severity:** Medium
- **Status:** Closed by second fresh verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 232-277,
  556-578, and 777-780 at re-review time

The bridge now resolves context during `STARTING`, and the conformance list
requires context-provider failure tests. The startup classification enumerates
input startup failure, agent startup failure, cancellation, shutdown, and broken
bridge/context invariants, but it does not say how expected provider outcomes are
classified. Examples include an unknown user, unavailable settings provider, or
a known provider data error.

Treating every provider exception as a broken invariant would classify an
external or recoverable dependency failure as `INTERNAL_FAILURE` and close the
input session. Treating it as agent or input failure assigns it to the wrong
trust boundary.

**Required resolution:** Define the context provider's typed success and failure
contract, distinguish rejected request data from provider unavailability and
internal invariant failure, assign each outcome a terminal reason and session
reuse policy, and add those cases to the startup state matrix.

**Candidate resolution applied:** T-004 now defines typed `ContextResolved`,
`ContextRejected`, and `ContextUnavailable` outcomes. Rejection and temporary
unavailability have distinct conversation-local end reasons and permit input
session reuse without automatic retry; malformed results and unexpected provider
exceptions are session-fatal `INTERNAL_FAILURE`. The startup priority, terminal
enum, conformance list, and HTML are aligned. Verify independently before
changing this finding's status.

**Verified resolution:** Expected provider outcomes are now typed values with
explicit terminal reasons and session-reuse behavior. Invalid results and
unexpected exceptions are assigned to the provider/bridge invariant boundary.
The startup transitions, failure table, terminal enums, conformance cases, and
HTML agree.

**Third candidate clarification:** Invalid provider results and unexpected
exceptions now follow the plan's application-wide process-fatal invariant policy
rather than ending only the input session. Expected rejection and unavailability
remain conversation-local. The classification stays explicit, but the fresh pass
should reconfirm this changed containment rule.

### T004-PLAN-010 — The typed context-rejection code is lost at the output boundary

- **Severity:** Medium
- **Status:** Closed by third independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 415-427,
  632-669, and 680-695 at second re-review time

`ContextRejected(code, detail)` introduces a closed machine-actionable rejection
code, and the failure table requires the conversation to end with typed
code/detail. The only bridge-to-input terminal event is nevertheless documented
as `ConversationEnded` with a `ConversationEndReason` plus optional diagnostic
detail. `ConversationEndReason.CONTEXT_REJECTED` identifies only the broad class;
no field carries the closed rejection `code`, and `detail` is explicitly
forbidden as control flow.

The typed code is therefore discarded before the input binding or client can act
on it, contradicting the stated machine-actionable contract.

**Required resolution:** Add an explicit optional typed context-rejection code to
the applicable terminal payload, define its presence invariant for
`CONTEXT_REJECTED`, and propagate it through the websocket/microphone mappings and
conformance cases. Alternatively, make each actionable rejection a distinct
closed conversation-end reason and remove the separate code.

**Candidate resolution applied:** `ConversationEnded` now has an optional closed
`context_rejection_code`. It is required exactly when the reason is
`CONTEXT_REJECTED` and forbidden otherwise. The websocket and microphone
bindings preserve the stable typed value, and conformance tests cover both the
presence invariant and both mappings. Verify independently before changing this
finding's status.

**Third independent re-review result:** `ConversationEnded` now carries
`context_rejection_code` as the same closed machine-actionable type produced by
`ContextRejected`. Its required-if-and-only-if invariant is explicit, diagnostic
detail remains non-actionable, both bindings must preserve the stable value, and
the conformance list tests presence, absence, and mapping. The typed rejection is
no longer lost at the output boundary.

### T004-PLAN-011 — Websocket follow-up timing requires presentation knowledge the binding explicitly lacks

- **Severity:** High
- **Status:** Closed by third independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 426-452,
  476-485, 715-740, and 742-755; `T-004-agent-boundary-options.html` lines
  420-436 and 458-471 at second re-review time

T-004 requires the input adapter to start the follow-up timeout only after the
user has actually been presented with the offer. For websocket output, the same
plan explicitly defines commitment only as transport handoff and states that it
does not know client receipt or presentation because there is no client ACK.
Transport handoff therefore cannot establish the event which is supposed to
start the timer.

The external websocket requirements do not decide whether the client owns the
timer and sends a typed timeout, or whether the server requires a presentation
acknowledgement. Starting a server timer at transport handoff would silently
violate the ratified presentation rule when output is buffered, delayed, or never
displayed.

**Required resolution:** Put websocket follow-up presentation and timing on a
boundary which has the fact. Either make the repository websocket client own the
timer and send `FollowUpTimedOut`, or add a typed client
`FollowUpPresented` acknowledgement which starts a server-side timer. Specify
disconnect/race behavior and add binding conformance tests; transport handoff
alone must not be called presentation.

**Candidate resolution applied:** The external websocket client which renders
the prompt now owns an explicitly configured local timeout and sends
`FollowUpTimedOut` if expiry wins. It starts the timer only after UI presentation,
serializes expiry against user submission so exactly one event is sent, and
cancels the timer on submission, cancellation, terminal output, or disconnect.
The server starts no timer and uses no presentation acknowledgement. A
nonresponding client leaves the conversation waiting until disconnect; duplicate
or late timeout is an external protocol violation. Binding tests cover those
races and closure paths. Verify independently before changing this finding's
status.

**Third independent re-review result:** Presentation knowledge and the clock now
belong to the external websocket client which renders the prompt. The server
adapter and bridge explicitly start no timer and claim no presentation from
transport handoff. Client configuration is explicit; rendering precedes timer
start; timeout and submission are serialized; terminal output and disconnect
cancel the timer; and late or duplicate timeout is rejected. The implementation
stage and conformance list cover repository clients and these race paths, so the
original ownership contradiction is resolved.

### T004-PLAN-012 — `SERVER_SHUTDOWN` has no source or observable cancellation path

- **Severity:** High
- **Status:** Closed by fourth independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 297-310,
  691-717, 779, and 1023-1025 at third re-review time

T-004 says shutdown can win while AgentConversation entry is pending, assigns
`SERVER_SHUTDOWN` second-highest concurrent priority, and requires deterministic
shutdown cleanup. The startup pseudocode and contract race only Agent entry
against `InputConversation.receive_control()`, however. `SERVER_SHUTDOWN` is not
an InputConversation event, no bridge-level shutdown event or operation is
defined, and task cancellation is not mapped to a typed shutdown outcome.

The same gap applies after startup: the plan says cancellation and input-session
closure pre-empt every non-terminal state, but never names how application
shutdown is concurrently observed while the bridge is awaiting agent output, a
blocked input sink, follow-up input, or context exit. A priority entry cannot be
implemented or tested without a source which can become ready.

**Required resolution:** Define the sealed application-lifecycle signal or typed
bridge operation which requests shutdown, its owner and lifetime, and how each
bridge observes it from `STARTING` through every blocking non-terminal operation.
Specify how it invokes acknowledged Agent cancellation, input-sink abort, typed
`SERVER_SHUTDOWN` terminal delivery, and exact-once context cleanup. Add race
tests for shutdown during Agent entry, agent work, blocked output, follow-up wait,
and simultaneous terminal readiness.

**Candidate resolution applied:** Application shutdown is removed from the
conversation protocol rather than given a signal path. T-004 now states that the
process simply dies and introduces no graceful-shutdown code. It removes
`SERVER_SHUTDOWN` from terminal and abort reasons, concurrent priority, failure
classification, acceptance criteria, and conformance expectations. Fatal exit
for a broken invariant remains a separate containment policy. Verify
independently before changing this finding's status.

**Fourth independent closure result:** T-004 contains no application-shutdown
event, reason, bridge signal, priority entry, failure-table row, acceptance
criterion, or conformance race. Its non-goals explicitly state that stopping the
process simply terminates it without conversation-protocol handling, and the
HTML says the same. The remaining process-exit requirements apply only to fatal
internal invariant and Agent lifetime-containment failures, require non-zero
exit, and cannot be mistaken for graceful conversation shutdown. The missing
signal defect is resolved by removing the unsupported lifecycle from scope.

### T004-PLAN-013 — Follow-up response-versus-timeout arbitration is undefined for microphone input

- **Severity:** Medium
- **Status:** Closed by fourth independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 516-550,
  677-705, 860, and 1021-1025 at third re-review time

The websocket client is explicitly required to serialize local timeout and user
submission so exactly one event crosses the binding. The microphone adapter also
owns presentation and the timeout, but has no corresponding arbitration rule. At
the timeout boundary it can observe accepted follow-up speech and timer expiry
together and expose both `UserMessage` and `FollowUpTimedOut`.

The bridge priority table ranks `FOLLOW_UP_TIMEOUT` but does not rank the valid
`UserMessage` transition. If both events cross the interface, whichever is
observed second becomes illegal after the first changes state. The plan therefore
does not assign the winner or require the loser to be suppressed, despite its
deterministic race objective.

**Required resolution:** Require every presenter-owned timer implementation,
including the microphone adapter, to atomically commit exactly one of accepted
`UserMessage` or `FollowUpTimedOut` and suppress or handle the losing local event
without exposing it to the bridge. Alternatively, define bridge-level priority
for both events if both may cross the interface. Specify the exact timeout-boundary
rule, late-speech behavior, and focused microphone mapping tests.

**Candidate resolution applied:** The microphone adapter now atomically
arbitrates speech start against timer expiry. Speech detected at or before the
boundary wins, cancels and permanently suppresses that timeout, and allows STT
to finish before an accepted `UserMessage` is emitted. Expiry committed first
emits exactly one `FollowUpTimedOut` and suppresses later speech. A no-transcript
speech attempt may start a new local presentation/listening cycle or emit a typed
input failure, but cannot revive the losing timeout. Focused tests cover before,
exact-boundary, after-boundary, and no-transcript behavior. Verify independently
before changing this finding's status.

**Fourth independent closure result:** The presenter-owned microphone adapter
now has the facts and owns one atomic local transition between speech start and
timer expiry. Speech detected at or before the boundary wins, including
simultaneous readiness; it permanently suppresses that timer while STT finishes.
Expiry committed first emits one timeout and keeps later speech local. Only an
accepted complete non-whitespace transcript becomes `UserMessage`; a
no-transcript attempt can start a fresh local cycle or emit typed input failure
without reviving the old timer. The bridge therefore receives at most one of the
competing outcomes from a cycle. The mapping requirements, implementation stage,
acceptance criteria, HTML, and controllable-clock/barrier tests all preserve the
same ownership and priority.

### T004-PLAN-014 — Blocking user-message delivery is outside the input-control race

- **Severity:** High
- **Status:** Closed by sixth independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 237-243,
  276-281, 343-354, 620-625, and 696-708 at fifth re-review time

`AgentConversation.send_user_message()` explicitly blocks until the active Agent
accepts the message. The startup pseudocode passes the pending input-control task
into `run(...)`, but the plan says only that the bridge observes input control
while awaiting agent output or an input-sink operation. It never requires the
initial `send_user_message()` to be raced against that watcher.

The same gap recurs after follow-up. Once the bridge consumes `UserMessage`, it
must hand that message to the Agent before returning to `WAITING_FOR_AGENT`.
During that blocking handoff, a later cancellation, input failure, or input-session
close needs a new concurrent receive. Without an explicit race, the event can
remain buffered indefinitely if the Agent endpoint stops accepting input. This
contradicts the requirements that input terminal control pre-empt Agent work in
every non-terminal state and that the HTML's watcher observes input continuously.

**Required resolution:** Make each `send_user_message()` an owned operation
raced against a continuously pending input-control receive, for both the initial
and every follow-up turn. Define ready-set priority, cancellation and joining of
the losing handoff, acknowledged Agent cancellation, and the state retained until
delivery commits. Add tests for cancellation, input failure, and session close
before acceptance, including simultaneous acceptance and input termination.

**Candidate resolution applied:** T-004 now adds
`DELIVERING_USER_MESSAGE` for both initial and follow-up handoffs. Each
`send_user_message()` uses a zero-capacity Agent-input rendezvous and returns
typed `AgentInputAccepted` at its declared commit point while the same
continuously pending input-control receive is raced against it. Input terminal
control wins simultaneous readiness; an uncommitted handoff is cancelled and
joined, a committed acceptance is preserved, and accepted work is stopped
through acknowledged Agent cancellation. Follow-up handling starts the next
input-control receive before delivery. Conformance cases cover cancellation,
input failure, and session close before and simultaneous with acceptance. Verify
independently before changing this finding's status.

**Sixth independent closure result:** Both the initial and every follow-up
handoff remain in the explicit `DELIVERING_USER_MESSAGE` state while a
continuously pending input-control receive is raced against typed
`AgentInputAccepted`. Acceptance has an atomic commit boundary; input terminal
control wins simultaneous readiness without erasing an already committed
acceptance. The bridge starts the next control receive before follow-up delivery,
joins every losing task, and uses acknowledged Agent cancellation after accepted
work. The state table, priority order, acceptance criteria, implementation steps,
conformance cases, and HTML preserve the same rule. The original cancellation
gap is closed.

### T004-PLAN-015 — Removing `SERVER_SHUTDOWN` does not define an implementable process-shutdown path

- **Severity:** High
- **Status:** Closed by sixth independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 361-372
  and 1102-1112, `ai_server/server.py` lines 118-135, and
  `ai_server/websocket_server.py` lines 147-159 at fifth re-review time

Removing the unsupported typed `SERVER_SHUTDOWN` outcome resolved the original
source/event/state contradiction. The replacement statement that the process
"simply dies" does not match the executable server, however. `SIGINT` and
`SIGTERM` set a stop event; `run_server()` then awaits microphone-manager close,
aiohttp runner cleanup, websocket close, Agent close, and Home Assistant close.
That is a graceful and potentially blocking application cleanup path, not direct
process termination.

T-004 neither says that this top-level behavior will be replaced nor defines how
the retained cleanup causes target InputSessions and bridges to finish. Stage 3
does not include `server.py` shutdown integration, a cleanup bound, or escalation
when a bridge or adapter does not close. Consequently an implementation can
follow the conversation plan exactly yet hang on deployment stop, or it can
silently discard the existing resource-cleanup behavior to make "simply dies"
literal.

**Required resolution:** Keep ordinary shutdown out of the conversation event
vocabulary, but choose and specify the application policy. Either retain bounded
graceful cleanup by closing persistent input adapters so bridges observe their
ordinary `InputSessionClosed` path, then escalate to hard process exit, or
explicitly replace the current signal handler with immediate process termination
and record the lost cleanup guarantees. Add the chosen `server.py` work and
subprocess signal tests to the migration plan. This does not require restoring a
`SERVER_SHUTDOWN` conversation reason.

**Candidate resolution applied:** T-004 reverses the immediate-death policy and
specifies bounded graceful application shutdown without restoring a conversation
shutdown event. `InputSession.close()` commits `CLOSING` before waiting and
releases accepting or active operations through `InputSessionClosed`. The first
signal atomically closes admission and snapshots the application-owned
InputSession/supervisor registry, concurrently closes every input session, joins
bridges, then closes shared resources within required
`shutdown.grace_period_seconds`; success exits zero. Deadline expiry logs open
owners and hard-exits non-zero, while a second signal hard-exits non-zero
immediately. Stage 3 includes `server.py` and configuration integration, and
subprocess tests cover success, deadline escalation, and second-signal
escalation. Verify independently before changing this finding's status.

**Sixth independent closure result:** The target application lifecycle now has
an implementable owner and ordering. InputSessions register before readiness;
the first signal atomically closes admission and snapshots that registry; each
idempotent `close()` commits `CLOSING` before suspension and releases accepting
or active operations through ordinary `InputSessionClosed`. One configured
global deadline covers bridge, InputSession, Agent factory, aiohttp, microphone,
and Home Assistant cleanup. Success exits zero, while deadline expiry or a second
signal invokes bounded non-zero hard exit. Configuration, `server.py` migration,
unit coverage, subprocess signal coverage, acceptance criteria, and HTML are all
included without reintroducing a conversation-level shutdown event. The finding
is closed.

### T004-PLAN-016 — Client-owned follow-up timing permits unbounded server-side retention

- **Severity:** Medium
- **Status:** Closed by sixth independent verification on 2026-07-18
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 515-569
  and 1144-1154 at fifth re-review time

The external websocket client correctly owns the semantic follow-up timer because
only it knows when presentation occurred. T-004 also explicitly allows a client
which sends neither a response nor a timeout to keep its bridge,
InputConversation, AgentConversation, and connection waiting until disconnect.
A faulty or hostile client can continue satisfying websocket heartbeat while
retaining those resources indefinitely. With concurrent persistent input
sessions and no documented active-session cap or non-semantic idle lease, enough
clients can exhaust server capacity without sending an invalid protocol event.

**Required resolution:** Preserve client ownership of `FOLLOW_UP_TIMEOUT`, but
define a separate server resource-lifetime policy. For example, bound active
websocket sessions and apply an explicit maximum idle/session lease whose expiry
closes the InputSession rather than pretending the prompt was presented. Specify
configuration, close classification, admission behavior, and tests. If
indefinite retention is intentionally accepted, document the trusted-client
assumption and the capacity bound which makes it safe.

**Candidate resolution applied:** T-004 now requires explicit positive
`websocket.max_connections`, `websocket.capacity_retry_after_seconds`, and
`websocket.follow_up_idle_lease_seconds` configuration with no hidden defaults.
The server atomically reserves capacity before upgrade; saturation returns HTTP
`503` with configured `Retry-After`, without waiting or queueing, and every slot
is released exactly once. A non-semantic lease starts when
`FollowUpRequested` commits to transport, ignores heartbeats, and is cancelled
on every valid exit from `WAITING_FOR_FOLLOW_UP`. A complete frame commits to the
lease arbiter only after validation; a valid event committed at or before the
monotonic deadline wins the atomic boundary race, while input after committed
expiry stays local. Expiry closes with `1013 Try Again Later` and
stable reason `follow-up resource lease expired`, producing ordinary
`InputSessionClosed` without emitting `FollowUpTimedOut`. Focused tests cover
admission boundaries, release paths, lease lifecycle and boundary races, and the
absence of a forged semantic timeout. Verify independently before changing this
finding's status.

**Sixth independent closure result:** Every websocket connection now consumes an
atomically reserved pre-upgrade slot, saturation is rejected without queueing,
and a single owner releases the slot on every exit. Each websocket follow-up wait
has a fresh non-semantic monotonic lease which heartbeats cannot extend. Lease
expiry closes the InputSession with stable `1013` policy diagnostics and never
forges the client-owned semantic timeout. Required configuration, admission and
lease races, migration work, acceptance criteria, and HTML agree. The bounded
retention defect is closed; the separate reply-versus-output-commit race exposed
by this lease is tracked under T004-PLAN-017.

### T004-PLAN-017 — A websocket reply can commit before the bridge enters `WAITING_FOR_FOLLOW_UP`

- **Severity:** High
- **Status:** Open
- **Evidence:** `T-004-conversation-bridge-protocol-redesign.md` lines 623-629,
  671-715, 740-749, and 842-847; `T-004-agent-boundary-options.html` lines
  662-666 and 700-711 at sixth re-review time

The websocket binding makes `FollowUpRequested` externally committed when the
complete frame is handed to the transport, even though flow-control drain may
still block. The new resource lease starts at exactly that commit and the
adapter allows a validated client reply to commit to its arbiter immediately.
The HTML likewise moves directly from transport handoff to waiting for the
client outcome.

The bridge contract, however, says it waits for `UserMessage` only *after*
forwarding `FollowUpRequested`, and the state table transitions from
`WAITING_FOR_DISPOSITION` to `WAITING_FOR_FOLLOW_UP` as one undivided action. If
the client receives, renders, and answers while the server writer is still
awaiting drain, the continuously pending input-control receive may expose a
valid `UserMessage` while the bridge still considers that event illegal outside
`WAITING_FOR_FOLLOW_UP`. Rejecting it would punish a conforming fast client;
accepting it without a specified state transition bypasses the bridge's
source/event/state invariant.

**Required resolution:** Make follow-up output commitment, lease activation, and
bridge response eligibility one coherent transaction. For example, let the
adapter publish a typed `FollowUpRequestCommitted` result at transport handoff so
the bridge atomically enters `WAITING_FOR_FOLLOW_UP` before drain continues, or
have the adapter retain exactly one bounded validated response until the bridge
acknowledges that state. Define failure before and after commit, input terminal
priority, drain completion, lease expiry during drain, and exact cleanup of any
retained event. Add focused tests where the reply arrives before handoff, at
handoff, after handoff while drain is blocked, and after drain.

## Files without independent findings

No independent defect was found in `docs/README.md` or the revised T-001 scope.

The illustrated HTML design aid agrees with the resolved ready-input,
transactional-commit, rendezvous, status, and context-provider changes. It also
agrees that a missed Agent deadline exits the server process, the external
websocket client owns its presentation and timing, and the microphone adapter
gives speech start priority at the timeout boundary while exposing only one
outcome. Its claims of continuous startup input observation and direct ordinary
process death originally shared T004-PLAN-014 and T004-PLAN-015 rather than
constituting separate findings. It now illustrates the candidate Agent-input
handoff race, bounded graceful shutdown, websocket admission cap, and follow-up
resource lease, which the sixth pass verified. Its handoff-to-client-outcome
sequence shares the unresolved transport-commit/state race in T004-PLAN-017
rather than constituting a separate finding.

## Validation performed

- inspected `git status`, the complete tracked diff, and both untracked T-004
  files;
- inspected the current session, messages, interfaces, agents, websocket, and
  microphone adapter touchpoints where needed;
- compared the plan with `docs/README.md` and
  `docs/protocol-documentation-standard.md`;
- ran `git diff --check` for tracked changes;
- checked all four changed files for trailing whitespace;
- checked the new document and HTML local link targets.

The fresh closure pass additionally:

- inspected all candidate-resolution sections and the complete revised T-004;
- inspected the staged diff for all five changed files;
- compared the transactional websocket claim with the installed aiohttp 3.13.5
  `WebSocketResponse.send_str()` and `WebSocketWriter.send_frame()` source;
- rechecked the current active-agent and websocket runtime shapes for the
  migration assumptions.

The second fresh closure pass additionally:

- reviewed committed revision `565aeaf` with a clean worktree;
- revalidated the ready-InputConversation ownership split, adapter commit points,
  AgentConversation rendezvous, typed cancellation, and context outcomes;
- traced startup cancellation through context resolution and Agent factory entry;
- compared follow-up presentation ownership with the explicit no-client-ACK
  websocket contract;
- checked machine-actionable context rejection data through the terminal event
  boundary.

The third independent closure pass additionally:

- reviewed the complete current working-tree diff and full T-004 and HTML, not
  only the newest candidate-resolution paragraphs;
- verified startup ownership and partial-entry cleanup against Python asynchronous
  context-manager semantics;
- traced the process-fatal policy from missed Agent entry/cancellation deadline
  through its required unit hook and subprocess exit proof;
- traced `ContextRejectionCode` from provider result through `ConversationEnded`,
  websocket JSON, microphone mapping, and conformance requirements;
- traced websocket follow-up presentation, timer start, submission race,
  terminal cancellation, late event, and disconnect ownership;
- rescanned every blocking lifecycle point and presenter-owned timeout for newly
  introduced or previously missed race gaps;
- inspected the current Session, endpoint, websocket server, repository clients,
  and microphone adapter where needed to verify migration assumptions;
- ran `git diff --check`, trailing-whitespace checks, HTML parsing, local-link
  target checks, and HTML fragment-target checks.

The fourth independent closure pass additionally:

- reread the complete T-004 plan, illustrated HTML, review ledger,
  documentation index, protocol documentation standard, and repository
  architecture decisions;
- inspected the complete current working-tree diff and relevant microphone
  event/interface ownership;
- searched every T-004 and HTML protocol surface for shutdown events, reasons,
  priorities, failure classifications, acceptance criteria, and tests;
- distinguished ordinary process death from the separately bounded fatal
  invariant-containment policy and its required non-zero exit proof;
- traced microphone follow-up presentation through speech start, exact-boundary
  arbitration, STT completion, no-transcript handling, expiry, late speech,
  interface exposure, mapping, implementation, acceptance, and conformance
  requirements;
- rescanned the complete plan for new inconsistencies or design regressions;
- ran `git diff --check`, trailing-whitespace checks, HTML parsing, local-link
  target checks, and HTML fragment-target checks.

The fifth independent re-review additionally:

- re-read the complete current T-004 plan and HTML rather than checking only the
  most recent finding resolutions;
- traced input-control observation across context resolution, Agent entry,
  initial-message acceptance, follow-up-message acceptance, Agent output, and
  input-sink blocking;
- compared the declared ordinary process-death policy with the actual SIGINT/
  SIGTERM, microphone-manager, aiohttp, websocket, Agent, and Home Assistant
  cleanup path in `ai_server/server.py` and `ai_server/websocket_server.py`;
- traced client-owned follow-up timing through a conforming but non-progressing
  websocket connection and checked the plan for admission, active-session, idle,
  and resource-lifetime bounds;
- reconfirmed all thirteen earlier closures and scanned every current uncommitted
  file for regressions.

The sixth independent re-review additionally:

- traced initial and follow-up Agent input delivery through rendezvous acceptance,
  simultaneous input termination, task joining, and acknowledged cancellation;
- traced the first and second signal paths from the application admission gate
  through InputSession registry closure, bridge cleanup, shared-resource ordering,
  the single global deadline, and hard-exit escalation;
- traced websocket slot ownership from pre-upgrade reservation through every
  handshake, rejection, disconnect, and shutdown exit;
- traced the non-semantic follow-up lease from transport handoff through validated
  client input, deadline arbitration, policy close, and `InputSessionClosed`;
- compared the lease's transport-handoff boundary with the bridge's
  `WAITING_FOR_FOLLOW_UP` transition and the existing websocket drain contract;
- reconfirmed the thirteen earlier closures and rescanned the complete T-004,
  HTML, review ledger, current server lifecycle, and all uncommitted changes.

No runtime tests were run because the reviewed changes contain no executable
code.

## Review assumptions

1. T-004 is intended to become an implementation-ready design plan after its
   findings are resolved; exact external JSON schemas may still be produced in
   Stage 1.
2. The numbered stages and review gates are intended to produce coherent,
   reviewable checkpoints. If they are only conceptual headings inside one
   atomic migration, T004-PLAN-004 is less severe, but Gate B currently implies
   a real checkpoint before adapter migration.

## Closure requirement

Not satisfied. T004-PLAN-017 remains open. After a candidate resolution is
applied, a fresh independent pass must verify it, reconfirm the sixteen previous
closures, and rescan the complete plan and illustrated aid for regressions. Gate
A still requires the resulting normative protocol suite to be reviewed and
explicitly approved before implementation begins.
