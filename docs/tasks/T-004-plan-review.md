# T-004 Plan Review

## Status

- **Authority:** Review record
- **Review date:** 2026-07-18
- **Subject:** Uncommitted T-004 plan and related documentation changes
- **Re-review date:** 2026-07-18
- **Outcome:** Changes still requested before T-004 is treated as implementation-ready
- **Finding status:** Four findings closed; five findings open

This document records a read-only review of the T-004 plan. It is evidence and
does not change the authority of T-004 or the current normative protocols.

## Scope

The review covered every uncommitted file present at the time:

- `docs/README.md`;
- `docs/tasks/T-001-protocol-and-documentation-cleanup.md`;
- `docs/tasks/T-004-agent-boundary-options.html`;
- `docs/tasks/T-004-conversation-bridge-protocol-redesign.md`.

The current interfaces, session implementation, documentation index, and
protocol documentation standard were inspected where needed to verify claims in
the plan.

## Summary

The candidate resolutions substantially improve T-004 and close four of the
seven original findings. T-004 is still not implementation-ready. Three original
findings remain open, and the fresh pass found two additional defects. The open
set is three high-severity architectural gaps and two medium-severity contract
or status inconsistencies.

## Findings

### T004-PLAN-001 — The bridge cannot own `STARTING` or assign the conversation ID with the illustrated lifecycle

- **Severity:** High
- **Status:** Closed by fresh verification on 2026-07-18
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

### T004-PLAN-002 — The persistent `InputSession` contract is missing

- **Severity:** High
- **Status:** Open — partially resolved
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

### T004-PLAN-003 — Cancellation can leave the input with an unterminated assistant stream

- **Severity:** High
- **Status:** Open — original queue race removed, replacement contract is inconsistent with websocket writes
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
- **Status:** Open — the previously deferred decisions are specified, but the new status overstates review closure
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

### T004-PLAN-008 — AgentConversation flow control and prompt cancellation are not structural

- **Severity:** High
- **Status:** Open
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

### T004-PLAN-009 — Context-provider failures have no explicit classification

- **Severity:** Medium
- **Status:** Open
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

## Files without independent findings

No independent defect was found in `docs/README.md` or the revised T-001 scope.

The illustrated HTML design aid generally agrees with T-004. Its backpressure
diagram repeats the unresolved producer-flow-control assumption in
T004-PLAN-008, and its transactional sink description inherits the websocket
commit-boundary problem in T004-PLAN-003.

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

The review remains required while T004-PLAN-002, T004-PLAN-003,
T004-PLAN-007, T004-PLAN-008, or T004-PLAN-009 is open. Candidate fixes must
update T-004 and the HTML where applicable, after which another fresh independent
pass must verify the changed contracts and scan for regressions before this
review can be removed.
