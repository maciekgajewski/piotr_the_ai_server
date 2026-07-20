# Websocket Client Behavior

## Status and scope

- **Authority:** Normative repository-client contract
- **Audience:** Maintainers of the repository interactive and batch websocket
  clients, their behavioral core, presentations, and conformance tests
- **Read when:** Changing `ai_server.chat_client`,
  `ai_server.batch_ws_client`, repository-client presentation, client-side
  websocket ordering, follow-up timing, or client exit behavior
- **Approval state:** Amended on 2026-07-20 with Captain-requested explicit
  terminal-reset behavior; fresh independent documentation review completed
  with no findings on 2026-07-20; approved by Captain on 2026-07-20

This document defines the required behavior of the websocket clients shipped in
this repository. It governs the interactive client launched by
`tools/ai-server-chat.sh` and the batch client launched by
`tools/batch-ws-client.sh`.

The external JSON binding remains the separate normative
[Websocket Conversation Protocol](websocket-conversation-protocol.md). Any
external websocket client is valid when it conforms to that protocol; it is not
required to reproduce the repository clients' prompts, styling, commands,
timeouts, exit codes, or internal organization. This document neither adds nor
changes a websocket JSON event, server state, admission rule, resource lease,
heartbeat rule, or close code.

Requirement identifiers use the `WSC-` prefix. State, operation, and result
names in this document form a conformance model; an implementation need not
expose types with the same names if its observable behavior and correctness
boundaries are equivalent.

## Ownership boundaries

- The repository clients MUST apply one consistent state, validation, race,
  outbound-commit, cleanup, and exit policy in interactive and batch modes.
  Presentation differences described here are intentional. (`WSC-OWNER-001`)
- Client behavior owns local protocol-state validation, user-input eligibility,
  assistant-stream correlation, follow-up timeout policy, concurrent-event
  arbitration, and exit classification.
- The interactive presentation owns terminal editing, history, prompt
  presentation, redraw, styling, local-command presentation, TTY validation,
  terminal restoration, and the explicit final ANSI style reset.
- The batch presentation owns the finite ordered scripted input and the
  stdout/stderr contract.
- The websocket transport owns connection, typed frame send and receive,
  heartbeat configuration, and bounded close. Transport details MUST NOT decide
  whether an application event is legal in the current client state.
- The monotonic clock supplies timestamps and deadline notification; it owns no
  protocol state or arbitration decision.
- This contract does not prescribe a terminal library, package layout, class
  hierarchy, or other internal implementation architecture.

## Terminology

- **Input offer:** One opportunity to provide either a new-Conversation message
  or one follow-up outcome.
- **Presentation commit:** The monotonic instant at which an input offer becomes
  usable by its presentation. Merely receiving a server event is not a
  presentation commit.
- **Submission commit:** The monotonic instant captured atomically when an
  edited or scripted line becomes an immutable submitted value.
- **Outbound event commit:** A synchronous client-side transport boundary,
  reached before the first suspension in a send operation, after which the
  event cannot be reclassified as unsent.
- **Terminal server input:** `protocol_rejected`, a legal
  `conversation_ended`, or transport disconnect.
- **Client protocol error:** A malformed, unknown, duplicate, out-of-order, or
  incorrectly correlated server event under the external binding and the
  client state model below.
- **Presentation profile:** The interactive or batch realization of one input
  offer and server output. It is not a separate protocol state machine.

Presentation, submission, and deadline timestamps MUST use one injected
monotonic clock domain. Wall time MUST NOT decide event ordering.

## Typed event and operation inventory

### Websocket server to client events

The external JSON fields and constraints are defined only by the
[Websocket Conversation Protocol](websocket-conversation-protocol.md). This
table binds those existing events to the repository-client conformance model.

| Event | Fields and constraints | Sender | Receiver | Valid client states |
|---|---|---|---|---|
| `session_accepted` | No fields; exactly once | Websocket server | Client behavior | `HANDSHAKING` |
| `conversation_ready` | No fields; one readiness offer | Websocket server | Client behavior | `WAITING_FOR_READY` |
| `conversation_started` | Fresh non-empty `conversation_id` | Websocket server | Client behavior | `WAITING_FOR_CONVERSATION_START` |
| `processing_update` | No fields | Websocket server | Client behavior | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` |
| `assistant_message_started` | Fresh non-empty `message_id` | Websocket server | Client behavior | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` |
| `assistant_text_chunk` | Matching `message_id`; non-empty `text` | Websocket server | Client behavior | `WAITING_FOR_AGENT` plus matching `ASSISTANT_STREAM_OPEN` |
| `assistant_message_completed` | Matching `message_id` | Websocket server | Client behavior | `WAITING_FOR_AGENT` plus matching `ASSISTANT_STREAM_OPEN` |
| `assistant_message_aborted` | Matching `message_id`; typed reason and optional detail | Websocket server | Client behavior | `WAITING_FOR_AGENT` plus matching `ASSISTANT_STREAM_OPEN` |
| `follow_up_requested` | No fields | Websocket server | Client behavior | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` or `ASSISTANT_STREAM_FINISHED` |
| `conversation_ended` | Typed reason, conditional context code, optional detail | Websocket server | Client behavior | Active Conversation states listed in the server-event matrix |
| `protocol_rejected` | Typed code and optional detail | Websocket server | Client behavior | Every connected non-closed state |

### Client to websocket server events

| Event | Fields and constraints | Sender | Receiver | Valid client states at commit |
|---|---|---|---|---|
| `session_start` | Optional non-empty `user` and `area` | Client behavior | Websocket server | Connection success while `CONNECTING` |
| `start_conversation` | Non-whitespace `message` | Client behavior | Websocket server | `AWAITING_START_INPUT` |
| `follow_up_message` | Non-whitespace `message` | Client behavior | Websocket server | `AWAITING_FOLLOW_UP_INPUT` before interval closure |
| `follow_up_timed_out` | No fields | Client behavior | Websocket server | `AWAITING_FOLLOW_UP_INPUT` after timeout selection |

The repository clients expose no local command which sends
`cancel_conversation`. Support for that valid external event is not required by
this repository-client contract.

### Transport and clock to client behavior

These are typed in-process results, not websocket JSON events.

| Result | Fields and constraints | Sender | Receiver | Valid client states |
|---|---|---|---|---|
| `ConnectionOpened` | Live transport | Transport | Client behavior | `CONNECTING` |
| `ConnectionFailed` | Diagnostic detail | Transport | Client behavior | `CONNECTING` |
| `TransportDisconnected` | Diagnostic detail | Transport | Client behavior | Every connected non-closed state |
| `OutboundSendCompleted` | Committed external event | Transport | Client behavior | State entered by that event's commit |
| `OutboundSendFailed` | Event, `UNCOMMITTED` or `DELIVERY_UNCERTAIN`, diagnostic detail | Transport | Client behavior | For `UNCOMMITTED`, the originating state before its outbound transition; for `DELIVERY_UNCERTAIN`, the state entered by that event's commit |
| `FollowUpDeadlineReached` | Fixed absolute monotonic `deadline` | Clock | Client behavior | `AWAITING_FOLLOW_UP_INPUT` |

### Presentation to client behavior

These are typed in-process results, not websocket JSON events.

| Result | Fields and constraints | Sender | Receiver | Valid client states |
|---|---|---|---|---|
| `InputOfferPresented` | `START` or `FOLLOW_UP`; monotonic `presented_at` captured at commit | Presentation | Client behavior | Matching `PRESENTING_*` state |
| `InputSubmitted` | Non-whitespace immutable `text`; monotonic `submitted_at` captured at commit | Presentation | Client behavior | Matching `AWAITING_*_INPUT` state |
| `EmptyInputSubmitted` | Monotonic `submitted_at`; interactive only | Interactive presentation | Client behavior | Either `AWAITING_*_INPUT` state |
| `LocalCommandSubmitted` | `HELP`, `EXIT`, or `UNKNOWN`; monotonic `submitted_at`; interactive only | Interactive presentation | Client behavior | Either `AWAITING_*_INPUT` state |
| `InputEnded` | Active-prompt EOF; interactive only | Interactive presentation | Client behavior | Either `AWAITING_*_INPUT` state |
| `LocalInterrupted` | `SIGINT` or `SIGTERM`; interactive `Ctrl-C` produces `SIGINT` | Repository entrypoint | Client behavior | Every non-closed state in either profile |
| `PresentationFailed` | Diagnostic detail | Presentation | Client behavior | Every non-closed state after presentation initialization |
| `BatchWorkCompleted` | No scripted initial message remains | Batch presentation | Client behavior | `AWAITING_START_INPUT` |
| `CleanupCompleted` | Typed exit result | Presentation and transport cleanup | Client behavior | `CLOSING` |

### Client behavior to presentation operations

These operations affect presentation but do not add websocket events.

| Operation | Fields and constraints | Sender | Receiver | Valid client states |
|---|---|---|---|---|
| `present_input_offer` | `START` or `FOLLOW_UP` | Client behavior | Presentation | Matching `PRESENTING_*` state |
| `show_processing_status` | No response content | Client behavior | Presentation | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` |
| `start_assistant_output` | Fresh `message_id` | Client behavior | Presentation | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` |
| `append_assistant_text` | Matching `message_id`; non-empty `text` | Client behavior | Presentation | `WAITING_FOR_AGENT` plus matching `ASSISTANT_STREAM_OPEN` |
| `finish_assistant_output` | Matching `message_id`; completed or aborted | Client behavior | Presentation | `WAITING_FOR_AGENT` plus matching `ASSISTANT_STREAM_OPEN` |
| `show_system_message` | Typed status or diagnostic; no control-flow parsing | Client behavior | Presentation | Every non-closed state |
| `close_presentation` | Typed exit result; idempotent teardown ending in the explicit final ANSI style reset when interactive stdout is a TTY | Client behavior | Presentation | `CLOSING` |

## State inventory

The closed client state set is:

| State | Meaning | Editable input |
|---|---|---|
| `CONNECTING` | One connection attempt is active | None |
| `HANDSHAKING` | The transport opened and exactly one `session_start` was committed | None |
| `WAITING_FOR_READY` | The session was accepted or a prior Conversation ended | None |
| `PRESENTING_START_INPUT` | A new-Conversation offer is committing | None before commit |
| `AWAITING_START_INPUT` | A new-Conversation offer was presented | Interactive or scripted initial message |
| `WAITING_FOR_CONVERSATION_START` | `start_conversation` was committed | None |
| `WAITING_FOR_AGENT` | A Conversation exists and the server owns the active turn | None |
| `PRESENTING_FOLLOW_UP_INPUT` | A follow-up offer is committing | None before commit |
| `AWAITING_FOLLOW_UP_INPUT` | A follow-up offer and its fixed deadline are active | Interactive or scripted follow-up message |
| `WAITING_FOR_CONVERSATION_END` | `follow_up_timed_out` was committed | None |
| `CLOSING` | Local exit or a terminal failure was committed | None |
| `CLOSED` | Transport and client-owned operations are quiescent | None |

Exactly one `session_start` MUST be committed after connection and before any
other client event. The client MUST retain no active Conversation ID before
`conversation_started`, MUST record its fresh non-empty ID when that event is
accepted, and MUST clear it only when `conversation_ended` is accepted.
(`WSC-STATE-001`)

Every accepted user turn has one orthogonal closed output state:

| Output state | Meaning |
|---|---|
| `BEFORE_ASSISTANT_STREAM` | Processing, zero-output disposition, or one stream start remains legal |
| `ASSISTANT_STREAM_OPEN(message_id)` | Only matching non-empty chunks and one matching terminal remain legal |
| `ASSISTANT_STREAM_FINISHED(message_id)` | No later processing or assistant stream event is legal in this turn |

The output state is initialized after `conversation_started` for the initial
turn and after a committed `follow_up_message` for every follow-up turn. It
exists only while the client is `WAITING_FOR_AGENT`. A message ID MUST be fresh
for the connection, and every chunk, completion, and abort MUST match the open
ID. (`WSC-STREAM-001`)

## Complete transition tables

`V` is a valid transition or effect, `P` is a client protocol violation or
invalid local operation, and `I` is inapplicable because termination already
committed. Every combination not explicitly valid below is `P` in a
non-terminal state and `I` in `CLOSING` or `CLOSED`, except cleanup results
which are explicitly valid in `CLOSING`.

### Server event and transport-input matrix

| Event/result | Valid state and output state | Valid effect |
|---|---|---|
| `ConnectionOpened` | `CONNECTING` | Commit one `session_start`; enter `HANDSHAKING` |
| `ConnectionFailed` | `CONNECTING` | Enter `CLOSING` with connection-failure result |
| `session_accepted` | `HANDSHAKING` | Enter `WAITING_FOR_READY` |
| `conversation_ready` | `WAITING_FOR_READY` | Enter `PRESENTING_START_INPUT`; invoke `present_input_offer(START)` |
| `conversation_started` | `WAITING_FOR_CONVERSATION_START` | Record ID, initialize `BEFORE_ASSISTANT_STREAM`, enter `WAITING_FOR_AGENT` |
| `processing_update` | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` | Interactive profile MUST invoke `show_processing_status`; batch profile emits no stdout; no state transition |
| `assistant_message_started` | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` | Invoke `start_assistant_output`; enter `ASSISTANT_STREAM_OPEN` |
| matching `assistant_text_chunk` | `WAITING_FOR_AGENT` plus matching `ASSISTANT_STREAM_OPEN` | Invoke `append_assistant_text`; no state transition |
| matching `assistant_message_completed` | `WAITING_FOR_AGENT` plus matching `ASSISTANT_STREAM_OPEN` | Invoke `finish_assistant_output`; enter `ASSISTANT_STREAM_FINISHED` |
| matching `assistant_message_aborted` | `WAITING_FOR_AGENT` plus matching `ASSISTANT_STREAM_OPEN` | Invoke `finish_assistant_output`; enter `ASSISTANT_STREAM_FINISHED` |
| `follow_up_requested` | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` or `ASSISTANT_STREAM_FINISHED` | Clear turn-output state; enter `PRESENTING_FOLLOW_UP_INPUT`; invoke `present_input_offer(FOLLOW_UP)` |
| `conversation_ended` | `WAITING_FOR_AGENT` plus `BEFORE_ASSISTANT_STREAM` or `ASSISTANT_STREAM_FINISHED`; either follow-up presentation/input state; or `WAITING_FOR_CONVERSATION_END` | Cancel and join active presentation/input/timer; clear Conversation ID and output state; enter `WAITING_FOR_READY` |
| `protocol_rejected` | Every connected non-terminal state | Enter `CLOSING` with typed rejection result |
| malformed, unknown, duplicate, out-of-order, or mismatched server event | Every connected non-terminal state in which it is not listed as valid above | Enter `CLOSING` with client-protocol-error result |
| `TransportDisconnected` | Every connected non-terminal state | Enter `CLOSING` with disconnect result |

`conversation_ended` while an assistant stream is open is invalid because the
matching stream completion or abort must be observed first. Duplicate
acceptance, readiness, Conversation, follow-up, stream-terminal, and
Conversation-terminal events are invalid. The client MUST fail closed rather
than ignore an event, infer a missing event, or guess the server's intent.
(`WSC-STATE-002`)

### Presentation, clock, send, and cleanup matrix

| Result | Valid state | Valid effect | Other non-terminal states |
|---|---|---|---|
| Matching `InputOfferPresented(START, presented_at)` | `PRESENTING_START_INPUT` | Enter `AWAITING_START_INPUT` | `P` |
| Matching `InputOfferPresented(FOLLOW_UP, presented_at)` | `PRESENTING_FOLLOW_UP_INPUT` | Establish one fixed deadline; enter `AWAITING_FOLLOW_UP_INPUT` | `P` |
| Valid `InputSubmitted` for initial input | `AWAITING_START_INPUT` | Commit `start_conversation`; enter `WAITING_FOR_CONVERSATION_START` | `P` |
| Eligible `InputSubmitted` for follow-up | `AWAITING_FOLLOW_UP_INPUT` | Commit `follow_up_message`; initialize `BEFORE_ASSISTANT_STREAM`; enter `WAITING_FOR_AGENT` | `P` |
| Ineligible post-deadline `InputSubmitted` | `AWAITING_FOLLOW_UP_INPUT` | Discard submission; select deadline outcome and commit `follow_up_timed_out`; enter `WAITING_FOR_CONVERSATION_END` | `P` |
| `EmptyInputSubmitted` | Either `AWAITING_*_INPUT`, interactive only | Silently reopen same offer; no state or deadline change | `P` |
| `LocalCommandSubmitted(HELP)` | Either `AWAITING_*_INPUT`, interactive only | Show command list and reopen same offer; no state or deadline change | `P` |
| `LocalCommandSubmitted(UNKNOWN)` | Either `AWAITING_*_INPUT`, interactive only | Show one diagnostic and reopen same offer; no state or deadline change | `P` |
| `LocalCommandSubmitted(EXIT)` | Either `AWAITING_*_INPUT`, interactive only | Enter `CLOSING` with clean local-exit result | `P` |
| `InputEnded` | Either `AWAITING_*_INPUT`, interactive only | Enter `CLOSING` with clean local-exit result | `P` |
| `BatchWorkCompleted` | `AWAITING_START_INPUT`, batch only | Enter `CLOSING` with requested-work-completed result | `P` |
| `FollowUpDeadlineReached` | `AWAITING_FOLLOW_UP_INPUT` | Select deadline outcome, commit `follow_up_timed_out`, enter `WAITING_FOR_CONVERSATION_END` | `P` |
| `PresentationFailed` | Every state except `CLOSING` and `CLOSED` after presentation initialization | Enter `CLOSING` with local-failure result | `I` after termination commit |
| `LocalInterrupted` | Every state except `CLOSING` and `CLOSED` | Enter `CLOSING` with interrupted result | `I` after termination commit |
| `OutboundSendCompleted` | State entered by the matching outbound commit | No state transition | `P` for a non-matching send |
| `OutboundSendFailed(UNCOMMITTED session_start)` | `CONNECTING` before the outbound commit | Enter `CLOSING` with known-unsent local failure | `P` |
| `OutboundSendFailed(UNCOMMITTED start_conversation)` | `AWAITING_START_INPUT` before the outbound commit | Enter `CLOSING` with known-unsent local failure | `P` |
| `OutboundSendFailed(UNCOMMITTED follow_up_message or follow_up_timed_out)` | `AWAITING_FOLLOW_UP_INPUT` before the outbound commit | Enter `CLOSING` with known-unsent local failure | `P` |
| `OutboundSendFailed(DELIVERY_UNCERTAIN)` | State entered by the matching outbound commit | Enter `CLOSING` with uncertain-delivery result; never retry | `P` |
| `CleanupCompleted` | `CLOSING` | Enter `CLOSED` with the previously selected exit result | `I` |

Presentation results which lose a race are cancelled and joined; they are not
processed later as new results in another state. Send completion or failure is
always joined even when a higher-priority terminal outcome has been selected.

## Invariants

1. No editable or scripted offer exists outside `AWAITING_START_INPUT` and
   `AWAITING_FOLLOW_UP_INPUT`. Text typed while the server owns the turn is not
   captured, buffered, or applied to a future offer. A `PRESENTING_*` state
   cannot accept a committed line. (`WSC-INPUT-001`)
2. A submitted message is non-whitespace before serialization. Interactive
   whitespace-only input is silently ignored. Every batch message is validated
   before connection; an empty or whitespace-only batch argument is a command-
   line usage error and no connection is attempted. (`WSC-INPUT-002`)
3. Presentation commit is raced against rejection, disconnect, legal
   Conversation termination, and local interruption. A terminal winner cancels
   and joins presentation without exposing input. Redraw is not a new
   presentation commit. (`WSC-PRESENTATION-001`)
4. Every assistant stream ID is fresh for the connection, every stream frame is
   correlated, and a turn produces zero or one stream. (`WSC-STREAM-001`)
5. Both profiles use the same client-state, event-validation, race,
   outbound-commit, cleanup, and exit rules. (`WSC-OWNER-001`)
6. The client makes one connection attempt and never reconnects or retries.
7. Client-owned receive, presentation, input, deadline, send, and cleanup work
   is joined or contained before `CLOSED`. (`WSC-LIFETIME-001`)
8. After interactive TTY validation, every exit path, including `Ctrl-C` in
   every client state, MUST tear down the terminal UI and then write and flush
   an explicit SGR reset `ESC [ 0 m` (`b"\x1b[0m"`) to the real terminal
   stdout. This reset is the last style-affecting sequence emitted by the
   client; no dim-gray or other SGR sequence may follow it. Cleanup is
   idempotent and MUST NOT rely only on implicit terminal-library restoration.
   (`WSC-TERMINAL-001`)

### Interactive presentation profile

The interactive client MUST:

- use one persistent asynchronous editing session with bounded file-backed
  history;
- create an active prompt only when the state permits input;
- preserve a partially edited prompt and buffer when asynchronous server or
  status output is rendered;
- open a fresh prompt after blank input, `/help`, or an unknown command even
  when its label is unchanged;
- display every `processing_update` as dim-gray system status while the server
  owns the turn;
- keep terminal-echoed user input and assistant output in the terminal's normal
  foreground;
- render prompts and all system/client messages in dim gray;
- restore normal style before the first assistant chunk;
- on every exit after TTY validation, leave stdout patching and tear down the
  terminal UI before directly writing and flushing the final explicit
  `b"\x1b[0m"` reset to real terminal stdout; and
- avoid forcing bright white or another theme-specific foreground.

Prompt styling ends before editable user input begins. Asynchronous output uses
a supported redraw boundary rather than writing through an active edit buffer.
(`WSC-UX-001`)

The history location MUST honor `PIOTR_CHAT_HISTORY`. Otherwise it is
`$XDG_STATE_HOME/piotr/chat_client_history` when `XDG_STATE_HOME` is set and
`~/.local/state/piotr/chat_client_history` when it is not. The history length
MUST remain bounded at 1000 entries, the file MUST be private to the user, and
ignored blank input MUST NOT be recorded. (`WSC-HISTORY-001`)

- `/help` displays the supported commands as a dim-gray system message and
  reopens the same offer.
- `/exit` commits clean local client closure.
- `Ctrl-D` at an active prompt is equivalent to `/exit`.
- `Ctrl-C` in any state commits interrupted client closure; closure still runs
  the idempotent final-reset sequence required by `WSC-TERMINAL-001` before
  returning exit code 130.
- `SIGTERM` commits the same interrupted closure as `Ctrl-C`.
- An unknown slash command displays one dim-gray diagnostic, never crosses the
  websocket boundary, and reopens the same offer.
- Commands are recognized only while an input offer is active.

Commands and blank input during follow-up do not establish a new offer or
deadline. (`WSC-COMMAND-001`)

The interactive shell-wrapper path requires both stdin and stdout to be TTYs.
If either is not a TTY, it starts no websocket connection, prints a plain
diagnostic directing automation to `tools/batch-ws-client.sh`, and exits with
code 2. (`WSC-TTY-001`)

### Batch presentation profile

The batch client supplies its finite ordered scripted messages only when the
state model exposes a legal offer. It MUST NOT pre-send a later message. Its
offer presentation commits synchronously when the legal offer is handed to the
batch presentation, which immediately captures
`InputOfferPresented(presented_at)`. Any available validated scripted message
commits separately as `InputSubmitted(text, submitted_at)`. (`WSC-BATCH-001`)

When no scripted message remains, a new-Conversation offer completes requested
work successfully. During a follow-up offer, the batch client waits for the
fixed semantic deadline and commits `follow_up_timed_out`. Rejection,
disconnect, invalid server behavior, or local presentation failure is terminal.

Batch stdout MUST contain assistant text payloads verbatim in protocol order and
exactly one client-added terminating newline for every completed or aborted
assistant stream. The client MUST add no submitted-message echo, prompt, status,
diagnostic, styling, or ANSI control sequence to stdout. Because the external
protocol permits arbitrary non-empty text, a payload-provided escape sequence
is preserved verbatim and is not client-added terminal control. Optional
submitted-message echoes and every system/client diagnostic go to stderr.
(`WSC-BATCH-002`)

## Timeouts and cancellation

Both repository clients MUST default `follow_up_timeout_seconds` to 15 seconds
when no command-line override is supplied. An override MUST be a positive
finite number. Invalid values are command-line usage errors without a
traceback. (`WSC-FOLLOWUP-001`)

One follow-up interval proceeds as follows:

1. Accept `follow_up_requested` in a legal state.
2. Commit presentation and capture `presented_at`.
3. Establish one absolute deadline at
   `presented_at + follow_up_timeout_seconds`.
4. Race server receive, local termination, submitted input, and deadline
   notification.
5. Inspect the complete ready set.
6. Select a valid submission only if it committed before interval closure and
   its `submitted_at` is less than or equal to the deadline.
7. If an eligible submission and deadline notification are both ready, the
   eligible submission wins regardless of callback scheduling order.
8. If no eligible submission is committed when the arbiter selects the ready
   deadline, timeout closes the interval. A later submission loses even if its
   clock reading equals the deadline.
9. Cancel and join every losing input or deadline operation, and commit exactly
   one `follow_up_message` or `follow_up_timed_out`.
10. If higher-priority terminal input wins before either outbound commit, send
    neither ordinary outcome.

Thus the equal-boundary rule depends on submission commitment plus
`submitted_at <= deadline`, inspected before the interval is closed; it does
not depend on which ready task a scheduler returns first. Blank input, commands,
help rendering, diagnostics, redraw, and batch scheduling never move the
deadline. (`WSC-FOLLOWUP-002`)

At every race boundary, the client MUST inspect the complete ready set. A send
which already crossed outbound commit is settled first: completion is joined,
while `OutboundSendFailed(DELIVERY_UNCERTAIN)` selects the non-zero uncertain-
delivery result and cannot be reclassified by a concurrent local exit. Among
outcomes which have not already crossed a commit point, priority is:

1. local termination: `SIGINT`/`Ctrl-C`, `SIGTERM`, `/exit`, or active-prompt
   EOF;
2. terminal server or transport input: `protocol_rejected`, a legal
   `conversation_ended`, disconnect, connection failure, or a detected client
   protocol error;
3. an uncommitted local operational failure: `PresentationFailed` or
   `OutboundSendFailed(UNCOMMITTED)`;
4. an eligible submission;
5. follow-up deadline;
6. blank input, `/help`, or an unknown command;
7. `BatchWorkCompleted`.

Thus protocol rejection or disconnect beats a simultaneously ready
presentation failure or batch success; local interruption beats connection
failure; terminal server input beats an ordinary submission; and an eligible
equal-deadline submission beats deadline notification. Priority never undoes
an outbound event which already committed. (`WSC-RACE-001`)

The transport boundary MUST define outbound event commitment synchronously
before the first suspension of a send. Once selected and committed, the send is
shielded and joined through completion or transport failure. Cancellation
cannot cause a competing semantic event. Failure after commit has uncertain
delivery and the event is never retried. (`WSC-COMMIT-001`)

## Failure and recovery

A client protocol error, `protocol_rejected`, transport loss, local
interruption, local exit, or local presentation failure enters `CLOSING`. The
client stops offering input, cancels and joins every owned operation subject to
outbound commitment, closes the transport at most once, restores interactive
terminal state, emits the explicit final style reset when required, and produces
exactly one exit result. There is no recovery,
reconnect, or retry within the process. (`WSC-LIFETIME-001`)

Both repository entrypoints use this classification:

| Outcome | Exit code | Required presentation |
|---|---:|---|
| Requested work completed | `0` | Normal output |
| `/exit` or active-prompt EOF | `0` | No error |
| `Ctrl-C` or `SIGTERM` | `130` | Terminal UI restored; final flushed SGR reset is the last style-affecting output |
| Connection attempt failed | `1` | Connection diagnostic |
| Established connection lost | `1` | Disconnect diagnostic |
| Server `protocol_rejected` | `1` | Typed rejection code and optional detail |
| Invalid server event, order, or correlation | `1` | Client protocol-error diagnostic |
| Local presentation failure | `1` | Client failure diagnostic |
| Failure before outbound commit | `1` | Known-unsent send diagnostic; no retry |
| Failure after outbound commit | `1` | Uncertain-delivery diagnostic; no retry |
| Non-TTY interactive invocation | `2` | Plain batch-client direction |
| Invalid timeout or empty/whitespace batch message | argparse usage code `2` | Argparse diagnostic without traceback; no connection |

Interactive diagnostics after terminal setup use dim gray. Batch diagnostics
use plain stderr. (`WSC-EXIT-001`)

## Normal sequences

### Interactive one-turn Conversation

```text
ConnectionOpened
client commits session_start
session_accepted
conversation_ready
InputOfferPresented(START, presented_at)
InputSubmitted(message, submitted_at)
client commits start_conversation
conversation_started(conversation_id)
processing_update -> interactive status
assistant_message_started(message_id)
assistant_text_chunk(message_id, text)*
assistant_message_completed(message_id)
conversation_ended(completed)
conversation_ready
```

### Follow-up submission at the deadline

```text
follow_up_requested
InputOfferPresented(FOLLOW_UP, presented_at)
deadline = presented_at + follow_up_timeout_seconds
InputSubmitted(message, submitted_at == deadline) and deadline notification ready
complete-ready-set arbitration selects the committed eligible submission
client commits follow_up_message
```

### Batch follow-up timeout

```text
follow_up_requested with no scripted message remaining
InputOfferPresented(FOLLOW_UP, presented_at)
deadline notification becomes ready with no eligible committed submission
client commits follow_up_timed_out
conversation_ended(follow_up_timeout)
conversation_ready
InputOfferPresented(START, presented_at)
BatchWorkCompleted
client closes successfully
```

## Invalid sequences

- Accepting any malformed, unknown, duplicate, out-of-order, or incorrectly
  correlated server event;
- accepting processing or assistant output before `conversation_started`;
- accepting a chunk or stream terminal without one matching open stream;
- accepting a second stream or processing update after stream completion or
  abort in the same turn;
- accepting `conversation_ended` while an assistant stream remains open;
- exposing or collecting input before matching presentation commit or while the
  server owns the turn;
- serializing whitespace-only input or any local command;
- moving a follow-up deadline after blank input, a command, help, diagnostic,
  or redraw;
- sending both `follow_up_message` and `follow_up_timed_out` for one interval;
- retrying an outbound event whose delivery is uncertain;
- reconnecting after connection loss or rejection; or
- adding prompt, status, diagnostic, submitted-message echo, styling, or ANSI
  control output to batch stdout; payload-provided control bytes remain
  verbatim assistant text.

## Observability requirements

- The interactive client MUST display every `processing_update` as dim-gray
  system status and MUST display connection, rejection, disconnect, protocol,
  timeout, and local failure diagnostics in the same semantic style.
- A client protocol-error diagnostic MUST identify the event type and client
  state or identify malformed transport input without exposing submitted or
  assistant message content.
- Batch diagnostics MUST use stderr and MUST leave stdout byte-clean as required
  by `WSC-BATCH-002`.
- Interactive terminal cleanup MUST make the final explicit SGR reset observable
  on real TTY stdout for every exit classification after TTY validation,
  especially `Ctrl-C` from each client state.
- The selected exit classification and whether a failed send was uncommitted or
  delivery-uncertain MUST be reconstructable from the presented diagnostic.

## Implementation and test references

Current related external message definitions and baseline clients:

- [`ai_server/websocket_messages.py`](../ai_server/websocket_messages.py);
- [`ai_server/chat_client.py`](../ai_server/chat_client.py);
- [`ai_server/batch_ws_client.py`](../ai_server/batch_ws_client.py);
- [`ai_server/ws_client_common.py`](../ai_server/ws_client_common.py);
- [`tests/test_websocket_server.py`](../tests/test_websocket_server.py).

T-005 will replace the client behavior with sealed interfaces and typed results
under `ai_server/websocket_client/`, remove `ai_server/ws_client_common.py`, and
add focused state, presentation, PTY, subprocess, and deterministic live-server
tests. Planned ownership and required evidence are tracked by the
[Protocol Conformance Catalogue](protocol-conformance-catalogue.md) and
[T-005](tasks/T-005-websocket-client-ux-protocol-redesign.md). Until that work
passes, the current files above are baseline references, not conformance claims
for this approved contract.

## Compatibility policy

Repository clients parse and emit only the current versionless vocabulary from
the Websocket Conversation Protocol. They reject old aliases and unknown server
events rather than translate or ignore them. (`WSC-COMPAT-001`)

The shell-wrapper names, Python module entrypoints, existing option names,
`PIOTR_CHAT_HISTORY`, and `XDG_STATE_HOME` behavior remain stable across T-005.
This policy does not preserve the raw terminal reader, duplicated client loops,
or `ai_server.ws_client_common` as compatibility surfaces.

## Explicitly unresolved decisions

None. Implementation evidence remains tracked by
[T-005 Websocket Client UX and Protocol Redesign](tasks/T-005-websocket-client-ux-protocol-redesign.md).
