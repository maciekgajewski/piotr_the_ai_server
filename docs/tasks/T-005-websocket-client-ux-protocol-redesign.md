# T-005: Websocket Client UX and Protocol Redesign

## Status

- **Authority:** Active implementation plan; not a normative protocol
- **Audience:** Maintainers of the repository websocket clients, websocket
  protocol helpers, terminal UX, and conformance tests
- **Read when:** Changing `ai_server.chat_client`,
  `ai_server.batch_ws_client`, shared websocket-client state, terminal input,
  client-side follow-up timing, or repository-client exit behavior
- **Created:** 2026-07-19
- **Status:** Ready for implementation
- **Implementation:** Not started
- **Contract review:** The 2026-07-19 independent review was closed, then the
  contract was materially amended on 2026-07-20 with Captain-requested explicit
  terminal-reset behavior; the fresh independent review completed with no
  findings on 2026-07-20
- **Approval:** The architecture decisions in this task were selected by Captain
  on 2026-07-19. Captain approved the independently reviewed normative client
  contract, T-005 ownership reconciliation, and conformance-plan changes on
  2026-07-20. Runtime implementation still requires separate explicit
  authorization.

## Objective

Redesign both repository websocket clients around one typed client-side
protocol engine, while keeping their presentation modes separate:

- the interactive client uses `prompt_toolkit` for correct asynchronous terminal
  input, history, styling, and redraw;
- the batch client remains non-interactive and automation-friendly;
- both clients share one state machine, validation path, timeout arbitration,
  transport lifecycle, and exit classification;
- user input and assistant output use the terminal's normal foreground;
- only prompts, status, client, and system messages use dim gray styling;
- the interactive client exposes input only when the websocket protocol permits
  a user message;
- invalid local input never crosses the websocket boundary;
- invalid or out-of-order server events fail the client session explicitly
  instead of being tolerated or guessed around.

The server binding, JSON vocabulary, and conversation core remain unchanged.
Any websocket client is valid when it conforms to the external binding; it need
not reproduce repository-specific UX, timing, or exit policy. T-005 therefore
defines the interactive and batch client requirements in the separate normative
`docs/websocket-client-behavior.md`, not inside the external websocket protocol.
Captain approved that client contract and ownership split on 2026-07-20. If
implementation discovers that either normative contract must change, stop and
obtain separate Captain approval before continuing.

## Approved design decisions

Captain selected decisions 1-6 one at a time on 2026-07-19 and requested
decision 7 on 2026-07-20:

1. Use `prompt_toolkit` as the interactive terminal foundation.
2. While the server owns the turn, display status but expose no input prompt.
   `Ctrl-C` remains available to terminate the client.
3. Replace duplicated interactive and batch control flow with one shared typed
   client-side protocol engine and separate presentation adapters.
4. Ignore empty or whitespace-only interactive submissions silently and keep
   the same prompt active. Send no websocket event.
5. Require both stdin and stdout to be TTYs for
   `tools/ai-server-chat.sh`. On a non-TTY, exit with a clear diagnostic that
   directs automation to `tools/batch-ws-client.sh`.
6. User input and assistant output use the terminal's normal foreground. Only
   system/client text and prompts use dim gray styling.
7. Every interactive TTY exit, especially `Ctrl-C` from any client state, tears
   down the terminal UI and then writes and flushes an explicit final SGR reset
   `ESC [ 0 m` (`b"\x1b[0m"`) as the last style-affecting output.

## Normative inputs

Read before implementation:

1. root `AGENTS.md` and the architecture decisions in `README.md`;
2. `docs/README.md`;
3. `docs/ai-server-conversation-protocol.md`;
4. `docs/websocket-conversation-protocol.md`;
5. `docs/websocket-client-behavior.md`;
6. `docs/protocol-conformance-catalogue.md`;
7. this task.

Applicable external websocket requirements include:

- `WS-SCHEMA-001`: strict typed JSON without permissive legacy parsing;
- `WS-CREATION-001`: a start event carries one complete non-whitespace
  message;
- `WS-STREAM-001`: assistant stream IDs are fresh, matching, and ordered;
- `WS-TERMINAL-001`: terminal reason and context-rejection fields remain typed;
- `WS-COMPAT-001`: old vocabulary is rejected rather than translated.

Applicable repository-client requirements include:

- `WSC-FOLLOWUP-001`: repository clients default to the shared 15-second
  follow-up timeout and accept only positive finite overrides;
- `WSC-FOLLOWUP-002`: user submission at or before timeout expiry wins and
  exactly one follow-up outcome is sent;
- `WSC-TERMINAL-001`: every interactive TTY exit ends with an explicit flushed
  SGR reset after terminal UI teardown;
- the complete `WSC-` state, presentation, race, UX, batch, lifetime, exit, and
  compatibility requirements catalogued for this task.

This task strengthens client-side enforcement and presentation of the approved
binding. It does not transfer the server's admission, gate, lease, or transport
commit responsibilities into the client.

The applicable target behavior has been promoted into the separate normative
`docs/websocket-client-behavior.md`, its stable `WSC-` requirement IDs are in
`docs/protocol-conformance-catalogue.md`, and Captain approved the reconciled
documents on 2026-07-20. Runtime implementation must conform to those approved
requirements.

## Current baseline

The current implementation is split across:

- `ai_server/chat_client.py`;
- `ai_server/batch_ws_client.py`;
- `ai_server/ws_client_common.py`;
- `tools/ai-server-chat.sh`;
- `tools/batch-ws-client.sh`;
- `tests/test_websocket_server.py`.

The preliminary fixes immediately before T-005 established:

- one shared `DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS = 15.0`;
- an optional `--follow-up-timeout-seconds` override in both clients;
- a terminal-style reset when an assistant stream starts, so assistant output
  no longer inherits the dim system prompt.

These fixes are part of the redesign baseline, not a substitute for it.

## Problems to solve

### Terminal presentation

The current prompt writes the dim-gray ANSI prefix without restoring terminal
style. Terminal-echoed user input consequently inherits gray. A reset after
line submission is too late to affect the text already displayed.

The client prints asynchronous server and status output directly while terminal
input may be active. It has no supported redraw operation for a partially typed
line, so incoming output can split or overwrite the visible prompt and buffer.

Prompt identity is also used as a redraw suppression mechanism. After `/help`
or another client message, asking for the same prompt is treated as a no-op and
may leave no visible prompt.

### Ineffective line editing and history

The client configures GNU Readline history and bracketed paste, but reads stdin
through `os.read()` and an event-loop file-descriptor callback. Readline does not
own that input path, so its editing, history navigation, and paste behavior are
not a reliable part of the UI.

### Input availability and protocol state

The stdin reader remains active while no user event is legal. Text entered under
`waiting for server>` can accumulate in an internal queue and be consumed later
after `conversation_ready` or `follow_up_requested`. This can send text under a
different protocol state from the one visible when the user typed it.

Empty and whitespace-only lines are not rejected locally. They can become
`start_conversation` or `follow_up_message` frames even though the binding
requires non-whitespace text, causing a preventable server rejection and
connection close.

### Duplicated protocol behavior

Interactive and batch clients separately own websocket receive loops,
follow-up timing, terminal handling, and exit behavior. Shared helpers decode
individual messages but do not own a complete client state machine. This makes
it possible for state validation, timeout cancellation, rejection handling, and
exit codes to drift between clients.

### Failure presentation

Invalid follow-up timeout overrides reach an uncaught `ValueError` rather than
an argparse usage error. Protocol rejection, transport loss, local interruption,
normal completion, and local user exit do not yet have one documented exit-code
classification shared by both clients.

## Target architecture

### Package and entrypoint boundary

Create a focused `ai_server/websocket_client/` package. Keep its
`__init__.py` empty. Follow project structure rules:

- `interfaces.py`: sealed transport, presenter, and clock interfaces;
- `messages.py`: internal typed results and commands which are not external JSON
  protocol events;
- `engine.py`: the sole shared client protocol state machine;
- `state.py`: client state and assistant-stream state definitions;
- `terminal_presenter.py`: `prompt_toolkit` interactive presentation;
- `batch_presenter.py`: deterministic scripted-message presentation;
- `options.py`: shared defaults and CLI value validation where useful.

Exact filenames may be adjusted during implementation if module responsibilities
remain equally explicit. Do not put implementation into `__init__.py`.

Retain `ai_server/chat_client.py` and `ai_server/batch_ws_client.py` as thin
command-line entrypoints so the existing `python -m` and shell-wrapper paths
remain stable. Remove `ai_server/ws_client_common.py` after all behavior has a
clear new owner; do not leave a compatibility facade or two competing engines.

### Ownership boundaries

The shared engine owns:

- client protocol state and transition validation;
- exactly-once `session_start`;
- classification of every server event in every client state;
- assistant message-ID correlation and stream ordering;
- selection of `start_conversation` versus `follow_up_message`;
- follow-up submission-versus-timeout arbitration;
- cancellation and joining of receive, input, timeout, and send operations;
- transport-close and exit-result classification;
- rejection of invalid local input before serialization.

The terminal presenter owns:

- the visible prompt and its presentation commit;
- input editing, history, paste, and terminal redraw;
- dim-gray system/client output;
- normal-foreground user echo and assistant output;
- TTY validation, terminal cleanup, and the explicit final ANSI style reset;
- `/help` and `/exit` presentation, after the engine classifies them as local
  commands.

The batch presenter owns:

- the finite ordered list of scripted messages;
- plain output with no client-added styling or ANSI control while preserving
  assistant payload verbatim;
- the choice to exit once all requested messages have completed;
- no terminal input, history, prompt, or redraw behavior.

The aiohttp transport adapter owns only websocket connection, typed frame send
and receive, heartbeat configuration, and bounded close. It does not decide
protocol legality or UI behavior.

Concrete presenter or transport knowledge must remain sealed behind its
interface. The engine must not inspect `prompt_toolkit`, terminal file
descriptors, aiohttp concrete message classes, or batch presenter internals.

### Client states

The engine uses this closed state set:

| State | Meaning | Input prompt |
|---|---|---|
| `CONNECTING` | One connection attempt is active | None |
| `HANDSHAKING` | Transport opened; `session_start` sent; acceptance pending | None |
| `WAITING_FOR_READY` | Session accepted or prior Conversation ended; readiness pending | None |
| `PRESENTING_START_INPUT` | New-Conversation prompt presentation is committing | None until commit |
| `AWAITING_START_INPUT` | `conversation_ready` was presented | New-Conversation prompt |
| `WAITING_FOR_CONVERSATION_START` | `start_conversation` was sent; `conversation_started` is required next | None |
| `WAITING_FOR_AGENT` | Conversation exists and the server owns the active turn | None |
| `PRESENTING_FOLLOW_UP_INPUT` | Follow-up prompt presentation is committing | None until commit |
| `AWAITING_FOLLOW_UP_INPUT` | `follow_up_requested` was presented and its timer is active | Follow-up prompt |
| `WAITING_FOR_CONVERSATION_END` | `follow_up_timed_out` was sent; terminal Conversation result is required | None |
| `CLOSING` | Local exit, rejection, failure, or disconnect committed | None |
| `CLOSED` | Transport and all owned operations are quiescent | None |

Each accepted user turn uses this orthogonal closed output state:

- `BEFORE_ASSISTANT_STREAM`: `processing_update`, zero-output disposition, or
  one `assistant_message_started` remains legal;
- `ASSISTANT_STREAM_OPEN(message_id)`: only matching non-empty chunks followed
  by one matching completion or abort are legal;
- `ASSISTANT_STREAM_FINISHED(message_id)`: no later processing update, second
  stream, chunk, or stream terminal is legal in that turn.

The output state is initialized only after `conversation_started` for an initial
turn and immediately after a committed `follow_up_message` for a follow-up turn.
It is not meaningful in any other client state. The engine separately retains
the active Conversation ID and requires it to be absent before
`conversation_started`, present throughout the Conversation, and cleared only
by `conversation_ended`.

The implementation must define a complete server-event/client-state matrix and
test every legal row plus representative illegal cells. It must not infer
legality from which coroutine happens to be waiting.

### Required transitions

At minimum:

1. A successful connection enters `HANDSHAKING` and sends one `session_start`.
2. `session_accepted` enters `WAITING_FOR_READY`.
3. `conversation_ready` enters `PRESENTING_START_INPUT`. The engine races prompt
   presentation against server rejection, disconnect, and local interruption;
   presentation commit enters `AWAITING_START_INPUT`, while a terminal outcome
   cancels and joins presentation without exposing editable input.
4. A non-whitespace submitted line sends one `start_conversation` and enters
   `WAITING_FOR_CONVERSATION_START`.
5. `conversation_started` is valid only in
   `WAITING_FOR_CONVERSATION_START`; it records a fresh Conversation ID,
   initializes `BEFORE_ASSISTANT_STREAM`, and enters `WAITING_FOR_AGENT` without
   enabling input.
6. `processing_update` is valid only in `WAITING_FOR_AGENT` with
   `BEFORE_ASSISTANT_STREAM`.
7. `assistant_message_started` opens one correlated stream; chunks and terminal
   stream events require the matching ID, and completion or abort enters
   `ASSISTANT_STREAM_FINISHED` without permitting a second stream.
8. `follow_up_requested` is valid only in `WAITING_FOR_AGENT` with output state
   `BEFORE_ASSISTANT_STREAM` or `ASSISTANT_STREAM_FINISHED`. It enters
   `PRESENTING_FOLLOW_UP_INPUT`; the engine races presentation commit against a
   terminal server event, disconnect, and local interruption.
9. A committed follow-up presentation establishes one absolute monotonic
   deadline and enters `AWAITING_FOLLOW_UP_INPUT`. Follow-up text sends exactly
   one `follow_up_message`, initializes a fresh `BEFORE_ASSISTANT_STREAM`, and
   enters `WAITING_FOR_AGENT`. Timeout sends exactly one
   `follow_up_timed_out` and enters `WAITING_FOR_CONVERSATION_END`.
10. `conversation_ended` cancels and joins any active prompt or follow-up timer,
    closes any presentation state, clears Conversation and turn-output state,
    and enters `WAITING_FOR_READY`.
11. `protocol_rejected`, malformed or invalid server behavior, disconnect, and
    local termination enter `CLOSING`, cancel and join all owned operations,
    close the transport once, and produce one typed exit result.

No editable prompt exists outside `AWAITING_START_INPUT` and
`AWAITING_FOLLOW_UP_INPUT`. In particular, a `PRESENTING_*` state may be drawing
the prompt but cannot accept a committed line before presentation commit. Text
typed before or during any non-input state is neither captured nor queued for a
future state.

## Presentation contract

### Styling

Use semantic style roles rather than scattering ANSI escapes:

| Role | Content | Required appearance |
|---|---|---|
| `system` | connection state, processing updates, follow-up timing, errors, help, and client diagnostics | dim gray |
| `prompt` | new-Conversation and follow-up prompt labels | dim gray |
| `user-input` | terminal-echoed submitted text | terminal default foreground |
| `assistant` | streamed assistant text | terminal default foreground |

Prompt styling must end before editable user input begins. Assistant styling
must be normal before the first text chunk. Every exit path after interactive
TTY validation must restore the terminal even after cancellation, rejection,
disconnect, or an exception. After stdout patching and the terminal UI have been
torn down, cleanup writes and flushes an explicit SGR reset `ESC [ 0 m`
(`b"\x1b[0m"`) directly to real terminal stdout. It must be the last
style-affecting output; no dim-gray or other SGR sequence may follow it. Cleanup
is idempotent and does not rely only on `prompt_toolkit` to restore color.

Do not force bright-white ANSI because that can be wrong for light terminal
themes. “Bright” in this task means the terminal's normal foreground, visibly
distinct from dim-gray system text.

The batch presenter emits no ANSI styling.

### Prompt behavior

Use one persistent `prompt_toolkit.PromptSession` with file-backed history.
Use its asynchronous prompt operation rather than a thread around blocking
`input()`. Protect asynchronous output with the library's supported stdout
patching/redraw mechanism so incoming system or transport output does not
destroy an active prompt or edit buffer.

The prompt is created only in an input-accepting engine state. After `/help`, an
unknown local command, or a blank line, the presenter opens a fresh prompt even
when its label is unchanged.

Reopening or redrawing a follow-up prompt does not create a new protocol
interval and does not change its deadline. Blank input, `/help`, and unknown
commands all retain the absolute deadline established by the original
follow-up presentation commit. If the deadline expires while client help or a
diagnostic is being rendered, timeout still wins unless a valid line already
committed at or before the deadline.

History remains at the existing configured location and honors
`PIOTR_CHAT_HISTORY` and `XDG_STATE_HOME`. Preserve a bounded history policy and
ensure the history file is private to the user. Do not record ignored blank
input.

### Commands and local termination

- `/help` prints the command list as dim-gray system text and reopens the same
  prompt.
- `/exit` closes the client cleanly with exit code `0`.
- `Ctrl-D` at an active prompt is equivalent to `/exit`.
- `Ctrl-C` in any state cancels the client, runs the idempotent final terminal
  reset, and exits with code `130`.
- Unknown slash commands do not cross the websocket boundary; print one
  dim-gray diagnostic and reopen the prompt.
- Commands are available only while a prompt is active. There is no hidden
  input collector while the server owns the turn.

## Follow-up timer correctness

The shared engine and presenter must make prompt presentation an explicit
commit boundary:

1. receive and validate `follow_up_requested`;
2. enter `PRESENTING_FOLLOW_UP_INPUT` and race prompt presentation against
   terminal server input, disconnect, and local interruption;
3. receive a typed `InputOfferPresented(presented_at)` result captured at the
   actual presentation commit point;
4. calculate one absolute deadline as
   `presented_at + follow_up_timeout_seconds`;
5. race the active prompt, server receive, local interruption, and deadline;
6. inspect the complete ready set;
7. require the presenter to return typed
   `InputSubmitted(text, submitted_at)`, with `submitted_at` captured atomically
   where the edited line commits; if a non-whitespace submission committed
   before interval closure with `submitted_at` at or before the absolute
   deadline, user input wins, the timer is cancelled and joined, and one
   `follow_up_message` is sent, including when deadline notification is also
   ready;
8. if deadline notification is ready and no eligible submission is committed,
   close the interval, cancel and join the prompt, and send one
   `follow_up_timed_out`; a later submission loses even if the injected clock
   still reads exactly the deadline;
9. if a terminal server event, disconnect, or local interruption commits, it
   cancels both ordinary outcomes and sends neither;
10. never revive a cancelled prompt or timer after leaving the interval.

Blank input and local commands may reopen the editor but must continue racing
the same absolute deadline. Presentation redraw is not a new presentation
commit and never extends the interval.

Tests must use an injected monotonic clock and barriers. Wall-clock sleeps are
not acceptable evidence for before, equal-boundary, and after-boundary cases.

## Concurrent-event priority and outbound commits

At every race boundary, the engine must inspect the complete ready set. First
settle any send which already crossed outbound commit: join success, while an
uncertain-delivery failure selects its non-zero terminal result and cannot be
reclassified as local success or interruption. Among outcomes which have not
already crossed a commit point, priority is:

1. local session termination: `Ctrl-C`, `SIGTERM`, `/exit`, or active-prompt
   EOF;
2. terminal transport or server input, including `protocol_rejected`, legal
   `conversation_ended`, disconnect, connection failure, and a detected client
   protocol error;
3. uncommitted local operational failure, including presentation failure or a
   send which failed before commit;
4. a valid `InputSubmitted` whose commit timestamp is legal for the active
   offer and, for follow-up, is at or before the absolute deadline;
5. follow-up deadline expiry;
6. non-terminal local outcomes such as blank input, `/help`, or an unknown
   command, which redraw without changing protocol state or deadline;
7. batch requested-work completion.

Thus terminal server input beats an equal-ready ordinary line, while a valid
line committed exactly at the follow-up deadline before interval closure beats
timeout. A line committed after the deadline is ineligible even if its task is
ready when the engine inspects the set. Once timeout closes the interval, a
later line cannot reopen it even if its timestamp equals the deadline.

Priority never undoes a committed outbound event. The transport interface must
define a synchronous client-side send commit before its first suspension. Once
the engine invokes that commit for one selected event, the send is shielded and
joined through completion or transport failure; local cancellation cannot cause
the engine to send a competing semantic outcome. A failure after commit closes
the client non-zero with an uncertain-delivery diagnostic. The engine never
retries a send whose delivery status is uncertain.

## Protocol failure and exit classification

Use a typed internal exit result and map it consistently in both entrypoints:

| Outcome | Exit code | Presentation |
|---|---:|---|
| Requested work completed | `0` | Normal output |
| `/exit` or active-prompt EOF | `0` | No error |
| `Ctrl-C`/`SIGTERM` interruption | `130` | Terminal UI restored; final flushed SGR reset is the last style-affecting output |
| Connection attempt failed | `1` | Profile-appropriate connection diagnostic |
| Established connection lost | `1` | Profile-appropriate disconnect diagnostic |
| Server `protocol_rejected` | `1` | Profile-appropriate typed rejection code/detail |
| Invalid server event/order/correlation | `1` | Profile-appropriate client protocol-error diagnostic |
| Local presentation failure | `1` | Profile-appropriate client failure diagnostic |
| Failure before outbound commit | `1` | Profile-appropriate known-unsent diagnostic; no retry |
| Failure after outbound commit | `1` | Profile-appropriate uncertain-delivery diagnostic; no retry |
| Non-TTY interactive invocation | `2` | Plain diagnostic directing use of batch client |
| Invalid timeout or empty/whitespace batch message | argparse usage code `2` | argparse diagnostic without traceback; no connection |

Interactive diagnostics after terminal setup are dim gray. Batch diagnostics
are plain stderr.

The engine makes one connection attempt and never reconnects or retries. This
preserves the current approved single-shot lifecycle.

Client protocol errors fail closed: stop accepting input, cancel and join owned
tasks, close the websocket, restore the terminal, and exit non-zero. Do not send
a server-originated `protocol_rejected` event in the wrong direction and do not
continue by guessing intent.

## Batch-client requirements

The batch client must use the same engine and therefore the same:

- strict server-event/state validation;
- assistant stream correlation;
- 15-second default and positive finite override validation;
- follow-up timer arbitration;
- cancellation and transport cleanup;
- rejection/disconnect exit classification.

Its presenter supplies scripted messages only when the engine requests legal
input. It must not pre-send future messages. Every scripted message must be
validated before the connection attempt; an empty or whitespace-only
`--message` value is an argparse usage error and no connection is made. When no
scripted message remains:

- after ordinary requested work completes, exit `0`;
- during a follow-up interval, wait for the configured semantic timeout and send
  `follow_up_timed_out`;
- on rejection, disconnect, or invalid server behavior, exit `1`.

For batch mode, the offer presentation commit is the synchronous point at which
the engine hands a legal start or follow-up offer to the batch presenter. The
presenter returns `InputOfferPresented(presented_at)` immediately using the
injected monotonic clock. A scripted message, if present, returns a separately
timestamped `InputSubmitted`; if none remains during follow-up, the one deadline
is calculated from that offer commit.

Keep stdout machine-readable. It contains assistant text payloads verbatim in
protocol order and exactly one client-added terminating newline for each
completed or aborted assistant stream. The client adds no submitted-message
echo, prompt, status, diagnostic, styling, or ANSI control sequence. A control
sequence present in assistant payload remains verbatim payload rather than
client-added terminal control. Submitted-message echoes, if retained, and all
system/client diagnostics go to stderr. Tests lock this byte-level contract for
zero, one, multiple, aborted, and payload-control-sequence streams.

## Dependency policy

Add `prompt-toolkit` to `requirements.txt` as a direct runtime dependency. Use
the supported asynchronous `PromptSession.prompt_async()` API and the supported
stdout patch/redraw context. Do not copy private library internals or build a
second raw-terminal editor around it.

The implementation must preserve startup failure clarity if dependencies are
missing. Do not add a silent fallback to the old `os.read()` path.

## Implementation plan

### Stage 1: Ratify the client contract and lock the current defects

1. Draft the T-005 client states, presentation commits, follow-up arbitration,
   exit classification, and TTY/batch behavior in the separate normative
   `docs/websocket-client-behavior.md`.
2. Add stable requirement IDs and planned evidence to
   `docs/protocol-conformance-catalogue.md`.
3. Obtain explicit Captain approval for those normative amendments before any
   runtime implementation.
4. Add focused tests proving the current gray-user-input inheritance, blank
   submission leak, same-label prompt redisplay failure, busy-state input queue,
   invalid CLI traceback, and inconsistent client exit results.
5. Add a PTY harness under `tests/` or `tools/lib/` which can run the real shell
   wrapper, feed keys, observe terminal control output, and enforce timeouts.
6. Keep the tests independent of a production model and external services.

### Stage 2: Build the shared engine behind typed interfaces

1. Add the new `ai_server/websocket_client/` package with empty `__init__.py`.
2. Define typed states, presenter results, exit results, and sealed interfaces.
3. Move connection lifecycle, schema handling, state validation, stream
   correlation, send selection, and cleanup into the engine.
4. Add injected monotonic clock/deadline support for deterministic races.
5. Prove the complete transition matrix with fake transport and presenter
   implementations before wiring either real presenter.

### Stage 3: Implement the terminal presenter

1. Add `prompt-toolkit` and create one asynchronous `PromptSession`.
2. Preserve the existing history path/environment contract.
3. Apply semantic styles and safe redraw around asynchronous output.
4. Expose prompts only for legal input states.
5. Implement blank-line, command, EOF, interrupt, and non-TTY behavior.
6. Implement one idempotent interactive cleanup path which leaves stdout
   patching, tears down `prompt_toolkit`, restores terminal modes, then writes
   and flushes the final explicit `b"\x1b[0m"` reset to real TTY stdout.
7. Delete the raw `os.read()` reader, ineffective GNU Readline setup, manual
   ANSI state ownership, and unused prompt bookkeeping.

### Stage 4: Migrate the batch presenter and entrypoints

1. Adapt scripted input and plain output to the same engine.
2. Keep both shell wrapper names and existing CLI option names stable.
3. Remove the shell wrappers' `SIGINT` traps which currently coerce an
   interrupted client to exit `0`; preserve the Python entrypoint's typed exit
   status, including `130` for `SIGINT`/`Ctrl-C` and `SIGTERM`.
4. Convert timeout validation to argparse-friendly errors.
5. Normalize exit results across interactive and batch entrypoints.
6. Remove `ws_client_common.py` once no caller remains.

### Stage 5: Documentation reconciliation and verification

1. Reconcile the approved normative client contract with the final
   implementation without changing its meaning. Any newly discovered contract
   change requires another Captain approval before the affected code proceeds.
2. Replace planned evidence in `docs/protocol-conformance-catalogue.md` with the
   exact current passing tests.
3. Update T-005 status and evidence without rewriting the historical baseline.
4. Run the focused, complete, PTY, and live checks below.

## Required automated coverage

At minimum:

- every legal shared-engine transition;
- representative invalid server events in every client state;
- duplicate `session_accepted`, readiness, Conversation, follow-up, and terminal
  events;
- rejection of processing or assistant output before `conversation_started`;
- assistant start/chunk/complete/abort ordering and matching message IDs;
- rejection of processing updates and a second assistant stream after stream
  completion or abort in the same turn;
- terminal server input, disconnect, and interruption during both prompt
  presentation commits, with the presentation task cancelled and joined;
- complete ready-set priority covering every modeled terminal failure, local
  exit, ordinary submission, timeout, prompt-redraw outcome, batch completion,
  and already-committed send consequence;
- no prompt and no queued input in every server-owned state;
- new-Conversation and follow-up prompts appear only after the matching server
  event;
- user input and assistant output use normal terminal foreground;
- prompt and every system/client message use dim gray;
- asynchronous output preserves a partially edited prompt and buffer;
- `/help`, unknown command, blank input, and unchanged-label prompt redisplay;
- `/exit`, `Ctrl-D`, interactive and batch `Ctrl-C`/`SIGINT`, interactive and
  batch `SIGTERM`, rejection, disconnect, malformed server event, and terminal
  restoration;
- real PTY proof that every interactive exit after TTY validation, including
  `Ctrl-C` from each client state, ends with a flushed `b"\x1b[0m"` as the last
  style-affecting bytes and emits no later SGR sequence;
- persistent bounded history and configured history-path overrides;
- TTY rejection for redirected stdin or stdout;
- CLI default timeout, valid override, and invalid-value argparse errors;
- empty or whitespace-only batch messages rejected as argparse usage errors
  before connection;
- follow-up before/equal/after boundary arbitration with an injected clock;
- submission timestamps captured at input commit and compared with the one
  absolute deadline;
- blank lines and local commands reopening the follow-up editor without moving
  that deadline;
- terminal server event and disconnect winning over an uncommitted line or
  timeout;
- exact cancellation and joining of prompt, receive, timer, and send tasks;
- cancellation and known-unsent failure before outbound commit, shielding and
  uncertain-delivery failure after commit, and no retry or competing semantic
  outcome;
- both-profile exit code and diagnostic evidence for presentation failure,
  known-unsent send failure, and uncertain-delivery send failure;
- batch messages offered only in legal states and never pre-sent;
- identical protocol and exit classification for both presenters;
- exact batch offer-commit timing, stdout/stderr separation, verbatim assistant
  payload including escape sequences, and absence of client-added ANSI output.

## Required verification order

1. Pure shared-engine and presenter unit tests.
2. `.venv/bin/python -m pytest tests/test_websocket_server.py -q`.
3. The entire pytest suite.
4. PTY tests using `tools/ai-server-chat.sh`, including editable partial input,
   history, redraw, styles, Ctrl-C in every client state, final SGR reset, and
   non-TTY rejection.
5. A real local server with a deterministic delayed fake Agent, proving that the
   client stays connected while busy, exposes no input prompt during the delay,
   renders streamed assistant output normally, and enables the next prompt only
   after protocol readiness.
6. A real batch-client flow against the same server, checking stdout, stderr,
   exit status, follow-up timeout, rejection, and disconnect.
7. `git diff --check` and documentation link/requirement-ID consistency checks.

Do not require Ollama, Home Assistant, microphones, or physical hardware for
T-005 acceptance.

## Expected file impact

Likely additions:

- `docs/websocket-client-behavior.md`;
- `ai_server/websocket_client/__init__.py`;
- `ai_server/websocket_client/interfaces.py`;
- `ai_server/websocket_client/messages.py`;
- `ai_server/websocket_client/state.py`;
- `ai_server/websocket_client/engine.py`;
- `ai_server/websocket_client/terminal_presenter.py`;
- `ai_server/websocket_client/batch_presenter.py`;
- focused engine, presenter, and PTY tests.

Likely modifications:

- `ai_server/chat_client.py`;
- `ai_server/batch_ws_client.py`;
- `tools/ai-server-chat.sh`;
- `tools/batch-ws-client.sh`;
- `requirements.txt`;
- `tests/test_websocket_server.py` or smaller focused client test modules;
- `docs/websocket-conversation-protocol.md` only to preserve the external-wire
  versus repository-client ownership boundary and retired-ID traceability;
- `docs/protocol-conformance-catalogue.md`;
- `docs/README.md`;
- this task.

Likely removal:

- `ai_server/ws_client_common.py`, after the atomic client cutover.

The shell wrappers remain shell scripts with their existing `.sh` names.

## Non-goals

- Changing server websocket JSON event names or schemas;
- changing websocket admission, capacity, heartbeat, ingress, gate, or lease
  policy;
- adding reconnect, retry, offline message delivery, or multi-server failover;
- adding multiline composition, Markdown rendering, syntax highlighting,
  autocompletion, or mouse UI;
- accepting interactive input while the server owns the turn;
- replacing the batch client with piped interactive stdin;
- changing Agent, orchestrator, DSA, microphone, or firmware behavior;
- preserving the raw `os.read()` terminal implementation as a compatibility
  path.

## Acceptance criteria

T-005 is complete only when:

1. Both repository clients run through one typed client protocol engine.
2. The interactive client uses `prompt_toolkit` asynchronous input and no custom
   raw terminal reader.
3. User input and assistant output use normal terminal foreground, while only
   system/client text and prompts use dim gray.
4. Every interactive TTY exit, including `Ctrl-C` in every client state, tears
   down the terminal UI and ends with a flushed explicit `b"\x1b[0m"` reset as
   the last style-affecting output.
5. The prompt is absent whenever user input is invalid in the protocol state,
   and no busy-state text can be queued for later transmission.
6. Empty or whitespace-only interactive submissions are silently ignored
   locally; empty or whitespace-only batch messages fail as argparse usage
   errors before connection.
7. Prompt redraw, editing, history, and asynchronous output work in a real PTY.
8. Follow-up timing begins only after prompt presentation and passes
   deterministic before/equal/after race tests using commit-point submission
   timestamps and one absolute deadline which redraws and commands cannot reset.
9. Client-side state, stream correlation, and server-event ordering are enforced
   explicitly across Conversation-start, pre-stream, open-stream, and
   finished-stream phases and fail closed.
10. Every concurrent ready set follows the documented priority, and outbound
   commit shielding prevents duplicate, competing, or retried uncertain events.
11. Interactive and batch rejection, disconnect, interruption, completion, and
   CLI failures have documented consistent exit results.
12. Interactive non-TTY use fails clearly and points to the batch wrapper.
13. The old duplicated loops, ineffective Readline setup, raw `os.read()` input,
    and `ws_client_common.py` compatibility surface are removed.
14. The separate normative client contract and conformance catalogue match the
    implementation and passing evidence, and their approval predates runtime
    implementation; the external websocket protocol remains client-agnostic.
15. All focused, complete, PTY, and live deterministic server/client checks pass.
16. No temporary process, history fixture, socket, or terminal state remains
    after testing.

## Next action

Await a separate explicit `proceed` or `make it so` authorization before
beginning runtime implementation.
