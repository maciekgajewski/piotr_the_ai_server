# T-004 Plan Review

## Status

- **Authority:** Review record
- **Review date:** 2026-07-18
- **Subject:** Uncommitted T-004 plan and related documentation changes
- **Outcome:** Changes requested before T-004 is treated as implementation-ready
- **Finding status:** Candidate resolutions applied on 2026-07-18; all findings remain open pending fresh independent verification

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

T-004 is not implementation-ready. The review found four high-severity
architectural gaps and three medium-severity documentation or contract
inconsistencies. The most important unresolved areas are startup ownership, the
persistent input-session interface, cancellation of partially delivered output,
and a migration sequence that can preserve a coherent runnable tree.

## Findings

### T004-PLAN-001 — The bridge cannot own `STARTING` or assign the conversation ID with the illustrated lifecycle

- **Severity:** High
- **Status:** Open
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

**Candidate resolution applied:** T-004 now makes the bridge the startup owner.
It receives the accepted request, InputSession, Agent factory, and context
provider; enters `STARTING`; assigns the ID; resolves context; and opens both
scoped conversations with explicit startup failure classification and exact-once
cleanup. Verify independently before changing this finding's status.

### T004-PLAN-002 — The persistent `InputSession` contract is missing

- **Severity:** High
- **Status:** Open
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

**Candidate resolution applied:** T-004 now specifies sealed InputSession states
and pull-gated `accept_conversation()`. It defines atomic readiness, typed request
contents, idle closure, reservation, second-start rejection without queuing, the
ACTIVE transition, and return to IDLE/CLOSED. Verify independently before
changing this finding's status.

### T004-PLAN-003 — Cancellation can leave the input with an unterminated assistant stream

- **Severity:** High
- **Status:** Open
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

**Candidate resolution applied:** T-004 removes the bridge output queue. The
input owns a transactional assistant sink and any bounded media buffer;
`send_text()` blocks for pushback, and priority `abort()` serializes against
completion with a definitive exactly-once result. Bridge-originated abort is now
an explicit bridge-to-input operation. Verify independently before changing this
finding's status.

### T004-PLAN-004 — The migration stages remove the old contract before its consumers are migrated

- **Severity:** High
- **Status:** Open
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

**Candidate resolution applied:** T-004 Stage 2 is now an additive runnable
checkpoint with the old runtime left complete and the new core tested only with
fakes. Stage 3 migrates all consumers, activates the new runtime, and removes the
old contract atomically without a compatibility facade. Verify independently
before changing this finding's status.

### T004-PLAN-005 — Mandatory disposition contradicts abort and failure paths

- **Severity:** Medium
- **Status:** Open
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

**Candidate resolution applied:** T-004 requires a disposition exactly once only
after a successful turn. Cancellation and failure are alternative terminal
outcomes. Agent-originated abort was removed, bridge-to-input abort was made
explicit, and separate `ConversationEndReason` and `AssistantAbortReason` enums
are listed. Verify independently before changing this finding's status.

### T004-PLAN-006 — T-001 still directs fresh sessions into the superseded architecture

- **Severity:** Medium
- **Status:** Open
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

**Candidate resolution applied:** T-001's audience, reading route, continuation
handoff, remaining criteria, next steps, assumptions, and conversation/websocket
stage headings now limit executable work to remaining microphone/firmware
hardware evidence and route replacement conversation/websocket work to T-004.
Verify independently before changing this finding's status.

### T004-PLAN-007 — “Design decisions ratified” overstates the current decision status

- **Severity:** Medium
- **Status:** Open
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

**Candidate resolution applied:** Startup ownership, input-side transactional
abort, absence of a bridge queue, blocking adapter pushback, and explicit
terminal-event precedence were ratified on 2026-07-18 and specified in T-004.
Gate A still controls normative protocol approval and implementation. Verify
independently before changing this finding's status.

## Files without independent findings

No independent defect was found in `docs/README.md`; its description of T-001
does, however, increase the inconsistency documented in T004-PLAN-006 because it
describes only outstanding hardware verification while T-001 still lists
websocket work.

The illustrated HTML design aid generally agrees with T-004. It repeats the
unconditional disposition requirement recorded in T004-PLAN-005.

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

Do not close this review merely because the plan text changes. Each finding must
record its resolution in T-004 or an applicable normative document, and a fresh
independent pass must verify that the seven defects no longer exist and that no
new contradiction was introduced.
