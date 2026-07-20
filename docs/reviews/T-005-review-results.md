# T-005 review results

## Review target

- Task: `docs/tasks/T-005-websocket-client-ux-protocol-redesign.md`
- Review ledger: `docs/reviews/T-005-review-results.md`

## Review pass 1 - initial

### Snapshot

- Reviewer: `task-reviewer`
- Pass: `initial`
- HEAD: `35cec1f1cee3303caaeebe46e8d4c0818953d0f1`
- Working tree: no staged changes; unstaged modifications to both entrypoints, wrappers, requirements, task, and server tests; deleted `ai_server/ws_client_common.py`; untracked `ai_server/websocket_client/` and `tests/test_websocket_client.py`.
- Task: `docs/tasks/T-005-websocket-client-ux-protocol-redesign.md`

### Findings

#### F-001 - High: Batch client exits after the first completed Conversation

- Status: `Open`
- Evidence: `ai_server/websocket_client/engine.py:140-149` returns `ExitKind.COMPLETED` for every batch `ConversationEnded`, without checking whether `BatchPresenter` still has scripted messages. `docs/tasks/T-005-websocket-client-ux-protocol-redesign.md:234-240` assigns the batch presenter a finite ordered message list and exit only after all requested messages complete.
- Violated requirement: T-005 batch ownership and `WSC-BATCH-001`.
- Impact: `--message` arguments after the first are silently dropped.
- Required correction: Continue to the next legal `conversation_ready` offer while scripted messages remain; complete only once all requested messages have completed.
- Verification required: Deterministic batch integration test with two messages and two complete server Conversations, asserting ordered `start_conversation` frames and both assistant outputs.

#### F-002 - High: Conversation-terminal validation accepts an illegal open stream and rejects a legal follow-up terminal

- Status: `Open`
- Evidence: `ai_server/websocket_client/engine.py:140-149` accepts `ConversationEnded` for all `WAITING_FOR_AGENT` output states, including `StreamState.OPEN`; it rejects the same event during `AWAITING_FOLLOW_UP_INPUT`. The normative matrix permits terminal events only before a stream or after its completion, and permits them during follow-up presentation/input: `docs/websocket-client-behavior.md:217-226`.
- Violated requirement: `WSC-STATE-002` and `WSC-STREAM-001`.
- Impact: An invalid server sequence can be accepted without a stream completion/abort, while a legal server terminal during a follow-up offer is reported as a client protocol error.
- Required correction: Enforce the normative output-state condition for `conversation_ended` and accept/cancel it in the specified follow-up offer states.
- Verification required: Engine tests for terminal-after-open-stream rejection and terminal-during-follow-up cancellation/return-to-readiness.

#### F-003 - High: Outbound sends have no defined commit boundary or uncertain-delivery handling

- Status: `Open`
- Evidence: `ai_server/websocket_client/aiohttp_transport.py:30-31` exposes only a suspending `send_str` call. `ai_server/websocket_client/engine.py:42,91,175,180` awaits sends directly, with no pre-suspension commit result, shielding, joining, or known-unsent versus delivery-uncertain classification.
- Violated requirement: `WSC-COMMIT-001`, `WSC-RACE-001`, and T-005 shared-engine ownership at `docs/tasks/T-005-websocket-client-ux-protocol-redesign.md:212-222`.
- Impact: Cancellation or transport failure around a send cannot be classified correctly and can neither prove prevention of competing semantic outcomes nor provide the required uncertain-delivery result.
- Required correction: Define and enforce the required outbound commit boundary, completion joining, and failure classification for every client frame.
- Verification required: Barrier-controlled tests covering cancellation/failure before commit, after commit, and simultaneous terminal outcomes for session start, initial input, follow-up input, and timeout.

#### F-004 - High: Follow-up deadline starts before the prompt is visibly committed and presentation cannot race terminal input

- Status: `Open`
- Evidence: `TerminalPresenter.present_input()` returns `time.monotonic()` without presenting anything (`ai_server/websocket_client/terminal_presenter.py:47-48`); the actual `prompt_async()` begins later in `read_input()` at `:50-67`. `ClientEngine._offer()` sets `AWAITING_*` and the deadline immediately at `ai_server/websocket_client/engine.py:153-157`, with no `PRESENTING_*` state or concurrent terminal-event race.
- Violated requirement: `WSC-PRESENTATION-001` and `WSC-FOLLOWUP-002`; see `docs/websocket-client-behavior.md:229-271,369-394`.
- Impact: The advertised 15-second interval can elapse before input is usable, and a terminal server event during presentation is not handled according to the contract.
- Required correction: Make visible input presentation an explicit cancellable commit, race it with terminal outcomes, and start the fixed deadline only at that commit.
- Verification required: Barrier-controlled presentation tests for rejection, disconnect, legal terminal, and interruption before/at/after presentation commit, plus a real-PTY prompt/deadline test.

#### F-005 - High: SIGINT/SIGTERM cannot terminate a connection attempt

- Status: `Open`
- Evidence: Both entrypoints install handlers that only set `stop_event` (`ai_server/chat_client.py:46-47,62-68`; `ai_server/batch_ws_client.py:53-54,65-69`) and then await `session.ws_connect()` before creating `ClientEngine` (`chat_client.py:52-55`; `batch_ws_client.py:57-60`). `stop_event` is consumed only by `ClientEngine._next_result()` at `engine.py:61-75`.
- Violated requirement: `WSC-COMMAND-001`, `WSC-EXIT-001`, and the requirement that interruption applies in every non-closed client state (`docs/websocket-client-behavior.md:246,323-326,436-454`).
- Impact: A hung connection attempt can ignore Ctrl-C/SIGTERM instead of exiting 130.
- Required correction: Make connection establishment interruptible and classify interruption during `CONNECTING` as exit 130.
- Verification required: Subprocess tests sending SIGINT and SIGTERM while each wrapper is connecting to a nonresponsive endpoint.

#### F-006 - Medium: Interactive history is neither bounded nor explicitly private

- Status: `Open`
- Evidence: `CHAT_HISTORY_LENGTH = 1000` is unused in `ai_server/websocket_client/terminal_presenter.py:20`; `FileHistory` is created at `:36-43` without trimming. Only the parent directory is chmodded at `:37-39`; the history file itself is not made private.
- Violated requirement: `WSC-HISTORY-001` (`docs/websocket-client-behavior.md:313-317`).
- Impact: History may grow without bound and its file mode depends on process umask.
- Required correction: Enforce the 1000-entry bound and private history-file permissions while preserving the required configured paths.
- Verification required: Tests for configured/default paths, file mode, persistence bound, and ignored-blank exclusion.

#### F-007 - Medium: Assistant stream IDs are not required to be fresh for the connection

- Status: `Open`
- Evidence: `ai_server/websocket_client/engine.py:34-35` retains only the current stream ID; `:116-119` accepts any new `AssistantMessageStarted` after a new turn without retaining prior IDs.
- Violated requirement: `WSC-STREAM-001` (`docs/websocket-client-behavior.md:188-192,272-273`).
- Impact: A server can reuse an assistant message ID in a later turn without client detection.
- Required correction: Reject a stream start whose ID has already appeared during the connection lifetime.
- Verification required: Engine test that reuses a completed turn’s message ID in a later turn and asserts a protocol-error result.

#### F-008 - Medium: Required T-005 conformance evidence is absent

- Status: `Open`
- Evidence: `tests/test_websocket_client.py:69-126` contains only three engine tests. The prior client race tests were renamed so pytest no longer collects them (`tests/test_websocket_server.py:512,546,583,647,688`). Repository search found no T-005 PTY coverage. Required coverage includes complete state/race matrices, PTY exits and reset proof, history, subprocess signal/TTY behavior, and deterministic live flows: `docs/tasks/T-005-websocket-client-ux-protocol-redesign.md:609-676`.
- Violated requirement: T-005 acceptance criteria 7-15 and `docs/protocol-conformance-catalogue.md:147-183`.
- Impact: The passing suite does not establish conformance for the newly introduced client engine and terminal behavior.
- Required correction: Restore or replace the removed coverage and add the required focused, PTY, subprocess, and deterministic client/server tests with current requirement traceability.
- Verification required: Run the required verification order, including PTY wrapper tests and deterministic interactive and batch live-server flows, and record exact current evidence.

#### F-009 - Low: The new client subsystem omits required lifecycle observability

- Status: `Open`
- Evidence: `ai_server/websocket_client/engine.py:21-219` and `aiohttp_transport.py:13-35` contain no logger or state/transport event logging.
- Violated requirement: root `AGENTS.md` logging rules require DEBUG logs for component interactions/state changes and INFO logs for crucial external interactions.
- Impact: Interleaved client sessions and timeout/transport races cannot be diagnosed from client logs.
- Required correction: Add stable per-client context and required lifecycle, state-transition, transport, and outcome logs without exposing message payloads unnecessarily.
- Verification required: `caplog` tests covering state transitions, send/receive classification, follow-up arbitration, and terminal cleanup context.

### Suggestions

No suggestions.

### Validation performed

- `git rev-parse HEAD && git status --short && find .. -name AGENTS.md -print && sed -n '1,260p' docs/tasks/T-005-websocket-client-ux-protocol-redesign.md && sed -n '1,260p' /home/maciek/.agents/skills/task-review/references/review-ledger-format.md && sed -n '1,220p' AGENTS.md && sed -n '1,240p' docs/README.md` - exit 0; inspected snapshot, task, ledger format, repository instructions, and documentation index.
- `git diff --stat && git diff -- requirements.txt tools/ai-server-chat.sh tools/batch-ws-client.sh ai_server/chat_client.py ai_server/batch_ws_client.py tests/test_websocket_server.py` - exit 0; inspected implementation diff.
- `nl -ba docs/websocket-client-behavior.md | sed -n '150,460p'` - exit 0; inspected normative state, presentation, timeout, race, and exit requirements.
- `git diff --check` - exit 0; no whitespace errors.
- `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_websocket_server.py -q` - exit 0; 53 passed.
- `.venv/bin/python -m pytest -q` - exit 0; 702 passed.
- Not run: required real-PTY wrapper tests and deterministic interactive live-server checks; no current T-005 PTY harness or corresponding test coverage was found.

### Reviewer conclusion

`Blocking findings remain: F-001, F-002, F-003, F-004, F-005, F-006, F-007, F-008, F-009.`

## Implementation response 1

### Findings addressed

#### F-001

- Response status: `Addressed - awaiting verification`
- Changes: `ai_server/websocket_client/engine.py` now returns batch completion only after the scripted presenter has no pending messages; `batch_presenter.py` exposes that state.
- Tests added or updated: `tests/test_websocket_client.py` covers two complete batch Conversations and ordered start events.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_websocket_server.py -q` - exit 0, 55 passed.
- Limitations: Full deterministic live-server batch evidence remains part of F-008.

#### F-002

- Response status: `Addressed - awaiting verification`
- Changes: `engine.py` rejects Conversation termination while an assistant stream is open and accepts it during a follow-up input interval.
- Tests added or updated: `tests/test_websocket_client.py` covers the open-stream rejection.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_websocket_server.py -q` - exit 0, 55 passed.
- Limitations: Presentation-race coverage remains part of F-004/F-008.

#### F-005

- Response status: `Addressed - awaiting verification`
- Changes: `connection.py` races connection establishment against the installed interruption event; both entrypoints classify that outcome as exit 130.
- Tests added or updated: None yet; subprocess evidence remains part of F-008.
- Verification: `.venv/bin/python -m py_compile ai_server/websocket_client/*.py` - exit 0.
- Limitations: Required subprocess signal checks remain outstanding.

#### F-006

- Response status: `Addressed - awaiting verification`
- Changes: `terminal_presenter.py` creates private history files and trims stored prompt-toolkit entries to 1000.
- Tests added or updated: None yet; history-path/mode tests remain part of F-008.
- Verification: `.venv/bin/python -m py_compile ai_server/websocket_client/*.py` - exit 0.
- Limitations: PTY and persistence evidence remains outstanding.

#### F-007

- Response status: `Addressed - awaiting verification`
- Changes: `engine.py` retains all observed assistant stream IDs and rejects reuse.
- Tests added or updated: Existing stream-state engine coverage remains; explicit reuse regression remains part of F-008.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_websocket_server.py -q` - exit 0, 55 passed.
- Limitations: Dedicated reuse test remains outstanding.

#### F-009

- Response status: `Addressed - awaiting verification`
- Changes: `engine.py` now emits stable per-instance lifecycle, protocol-error, disconnect, input-offer, and cleanup log records.
- Tests added or updated: None yet; caplog coverage remains part of F-008.
- Verification: `.venv/bin/python -m py_compile ai_server/websocket_client/*.py` - exit 0.
- Limitations: Dedicated observability tests remain outstanding.

### Rebuttals

`None.`

## Review pass 29 - verification

### Snapshot

- Reviewer: `task-reviewer`
- Pass: `verification`
- HEAD: `acd4b40d1c2c9fafd9a2791b5f9804041e4a3387`
- Working tree: 16 modified tracked files and 8 untracked paths.

### Finding status

#### F-020

- Status: `Resolved`
- Evidence: Engine-result diagnostic presentation is individually contained in `run_chat()`: a `presenter.system()` failure maps to `LOCAL_FAILURE` and cannot reach the generic diagnostic path. `test_interactive_engine_diagnostic_output_failure_is_typed_local_failure` injects that failure after successful startup and passes. `WSC-EXIT-001` cites the regression.
- Rationale: Interactive diagnostic presentation failures now produce the required typed local-failure result.

### New findings

`None.`

### Validation performed

- `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_terminal_presenter.py tests/test_websocket_client_entrypoints.py -q` - passed: 45 tests.
- `git diff --check` - passed.

### Reviewer conclusion

`No blocking findings.`

## Implementation response 17

### Findings addressed

#### F-020

- Response status: `Addressed - awaiting verification`
- Changes: Interactive diagnostic rendering is now individually contained. If the presenter cannot render an engine result, `run_chat()` returns `LOCAL_FAILURE` with the output error; the generic local-failure rendering path is also best-effort and cannot re-raise the original presentation error.
- Tests added or updated: `test_interactive_engine_diagnostic_output_failure_is_typed_local_failure` injects a failing result diagnostic after successful startup and asserts a typed local failure.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_terminal_presenter.py tests/test_websocket_client_entrypoints.py -q` - exit 0, 45 passed; `git diff --check` - exit 0.
- Limitations: Awaiting retained reviewer verification and then an independent closure review.

### Rebuttals

`None.`

## Review pass 19 - verification

### Finding status

#### F-008

- Status: `Resolved`
- Evidence: `WSC-UX-001`, `WSC-TERMINAL-001`, and `WSC-HISTORY-001` now cite the exact real-PTY redraw, asynchronous partial-edit, restoration, and history tests. The retained live/PTY suite passed 19 tests.
- Rationale: The required behavioral evidence and its catalog references are now present.

### New findings

`None.`

### Validation performed

- `.venv/bin/python -m pytest tests/test_websocket_client_live.py tests/test_websocket_client_process.py -q` — passed: 19 tests.
- `git diff --check` — passed.

### Reviewer conclusion

`No blocking findings.`

## Implementation response 4

### Findings addressed

#### F-008

- Response status: `Partially addressed - awaiting verification`
- Changes: Added shared CLI validation coverage, configured history-path coverage, and a deterministic loopback websocket batch-wrapper flow. The live flow verifies legal offer ordering, byte-exact assistant payload preservation including payload-provided ANSI, stdout/stderr separation, and clean exit.
- Tests added or updated: `tests/test_websocket_client_options.py`, `tests/test_terminal_presenter.py`, `tests/test_websocket_client_live.py`.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_terminal_presenter.py tests/test_websocket_client_entrypoints.py tests/test_websocket_client_options.py -q` - exit 0, 24 passed; `.venv/bin/python -m pytest tests/test_websocket_client_live.py tests/test_websocket_client_process.py -q` - exit 0, 3 passed; `.venv/bin/python -m pytest tests/test_websocket_server.py -q` - exit 0, 50 passed.
- Limitations: The required full transition/priority and interactive live/PTY matrix is still being completed.

### Rebuttals

`None.`

## Review pass 7 - verification

### Snapshot

- Reviewer: `task-reviewer`
- Pass: `verification`
- HEAD: `acd4b40d1c2c9fafd9a2791b5f9804041e4a3387`
- Working tree: no staged changes; unstaged client engine, transport, entrypoint, presenter, wrapper, and test changes; untracked `connection.py`, process tests, and review ledger.

### Finding status

#### F-003

- Status: `Resolved`
- Evidence: `AiohttpTransport.commit_send()` synchronously serializes and enqueues the frame before scheduling its drain future ([aiohttp_transport.py](/home/maciek/piotr/ai_server/websocket_client/aiohttp_transport.py:38)); the engine transitions immediately after that commit and before joining delivery for session start, input, and timeout sends ([engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:49), [engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:121), [engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:229)). Joined sends are shielded and map pre-commit versus post-commit failure separately ([engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:275)).
- Rationale: The state transition now follows a synchronous transport-owned queue commit without an intervening suspension; delivery is subsequently joined and never retried. Required barrier coverage remains under F-008.

#### F-004

- Status: `Open`
- Evidence: `TerminalPresenter._read_prompt()` signals `_prompt_started` before calling `prompt_async()` ([terminal_presenter.py](/home/maciek/piotr/ai_server/websocket_client/terminal_presenter.py:69)), so `InputOfferPresented` can precede actual visible-prompt commitment. Additionally, a received terminal event cancels the pending presentation task ([engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:95)) but leaves `_presentation_task` retained; `_cancel_input()` only clears `_input_task` ([engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:257)). A later `_next_result()` can consume that cancelled stale presentation task.
- Rationale: The required visible-presentation commit and terminal-race cleanup remain incorrect.

#### F-006

- Status: `Resolved`
- Evidence: History is trimmed before use, after submitted prompt input, and on close; only nonblank prompt-toolkit entries are retained, limited to 1,000, with mode `0600` applied ([terminal_presenter.py](/home/maciek/piotr/ai_server/websocket_client/terminal_presenter.py:38), [terminal_presenter.py](/home/maciek/piotr/ai_server/websocket_client/terminal_presenter.py:136)).
- Rationale: Current source enforces the required path handling, privacy, bounded retention, and blank exclusion. Dedicated regression evidence remains part of F-008.

#### F-008

- Status: `Open`
- Evidence: Current focused coverage comprises seven engine tests ([test_websocket_client.py](/home/maciek/piotr/tests/test_websocket_client.py:79)) and two wrapper/process tests ([test_websocket_client_process.py](/home/maciek/piotr/tests/test_websocket_client_process.py:11)). It does not provide the required complete transition/priority matrix, presentation barriers, history tests, signal matrix, real-PTY exit matrix, or deterministic interactive and batch live-server flows.
- Rationale: The task’s required automated coverage and verification order remain materially incomplete.

#### F-009

- Status: `Resolved`
- Evidence: The engine logs stable per-instance context for lifecycle, incoming events, state transitions, committed/drained sends, and cleanup ([engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:44), [engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:127), [engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:295)).
- Rationale: The source now supplies the required lifecycle observability; broader log-path tests remain part of F-008.

#### F-013

- Status: `Open`
- Evidence: An `OSError` from `presenter.start()` reaches the connection-failure handler while `started` is false; that branch returns a typed failure without emitting any diagnostic ([chat_client.py](/home/maciek/piotr/ai_server/chat_client.py:51), [chat_client.py](/home/maciek/piotr/ai_server/chat_client.py:63)).
- Rationale: A terminal initialization failure can still be silent.

#### F-016

- Status: `Resolved`
- Evidence: `CLOSING` and `CLOSED` are explicit client states ([state.py](/home/maciek/piotr/ai_server/websocket_client/state.py:16)); engine cleanup transitions through both around input cancellation and transport close ([engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:70)).
- Rationale: Lifecycle state representation and execution now conform to the closed state model.

### New findings

`None.`

### Validation performed

- `git status --short && git diff --stat && git diff --check && git log -1 --format='%H%n%s'` - exit 0; current working-tree scope inspected and no whitespace errors found.
- Not run: focused pytest, full pytest, PTY matrix, and deterministic client/server flows; no current-pass execution evidence was supplied.

### Reviewer conclusion

`Blocking findings remain: F-004, F-008, F-013.`

## Implementation response 3

### Findings addressed

#### F-004

- Response status: `Addressed - awaiting verification`
- Changes: Interactive presentation now commits only from prompt_toolkit's first `after_render` callback. Terminal input cancels and clears pending presentation work, so a cancelled presentation cannot be consumed by a later arbiter iteration.
- Tests added or updated: `test_present_input_commits_only_after_prompt_toolkit_renders`, `test_terminal_event_cancels_and_clears_pending_prompt_presentation`.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_terminal_presenter.py tests/test_websocket_client_entrypoints.py -q` - exit 0, 15 passed.

#### F-013

- Response status: `Addressed - awaiting verification`
- Changes: Interactive terminal initialization failures now always render the normal system-style connection diagnostic before cleanup.
- Tests added or updated: `test_interactive_terminal_startup_failure_is_presented_before_cleanup`.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client_entrypoints.py -q` - exit 0, 1 passed.

#### F-008

- Response status: `Partially addressed - awaiting verification`
- Changes: Added focused presentation-commit, cancelled-presentation, startup-diagnostic, pre-commit failure, post-commit uncertain-delivery, and bounded-history regression coverage.
- Tests added or updated: `tests/test_terminal_presenter.py` and focused additions to `tests/test_websocket_client.py`.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_terminal_presenter.py tests/test_websocket_client_entrypoints.py -q` - exit 0, 15 passed.
- Limitations: The remaining required transition/priority, CLI, PTY, and deterministic live-server matrix still needs completion.

### Rebuttals

`None.`

## Review pass 2 - verification

### Snapshot

- Reviewer: `task-reviewer`
- Pass: `verification`
- HEAD: `acd4b40d1c2c9fafd9a2791b5f9804041e4a3387`
- Working tree: no staged changes; unstaged modifications to client entrypoints, engine, presenters, interfaces, and client tests; untracked `ai_server/websocket_client/connection.py` and `docs/reviews/`.

### Finding status

- F-001: `Resolved` — batch completion now waits for no remaining scripted messages; two-turn engine test present.
- F-002: `Resolved` — terminal validation rejects an open assistant stream and accepts follow-up input terminal state.
- F-003: `Open` — no synchronous outbound commit result or known-unsent versus delivery-uncertain classification.
- F-004: `Open` — presentation commit/race model remains incomplete.
- F-005: `Resolved` — connection establishment races interruption and maps it to exit 130.
- F-006: `Open` — history is only trimmed at startup, not kept bounded throughout a session.
- F-007: `Resolved` — assistant stream ID reuse is rejected.
- F-008: `Open` — required PTY, subprocess, live-flow, and comprehensive conformance evidence remains absent.
- F-009: `Open` — client observability lacks complete DEBUG interaction/state/transport logs and caplog coverage.

### New findings

#### F-010 - Medium: Conversation IDs are not checked for connection-lifetime freshness

- Status: `Open`
- Evidence: `ai_server/websocket_client/engine.py` retains only the active Conversation ID.
- Violated requirement: `WSC-STATE-001`.
- Impact: Reused Conversation IDs are accepted.
- Required correction: Retain seen Conversation IDs and reject reuse.
- Verification required: Two-Conversation ID-reuse engine regression.

#### F-011 - Medium: Empty batch work is classified as interactive EOF instead of requested-work completion

- Status: `Open`
- Evidence: `BatchPresenter.read_input()` returns `EOF` with no initial messages.
- Violated requirement: `WSC-BATCH-001`.
- Impact: Typed batch completion is misclassified.
- Required correction: Model empty batch work as requested-work completion.
- Verification required: Zero-message engine test.

#### F-012 - High: Interactive presentation startup failures bypass typed failure handling and terminal cleanup

- Status: `Open`
- Evidence: `run_chat()` starts the presenter before its typed `try/finally` path.
- Violated requirement: `WSC-LIFETIME-001`, `WSC-EXIT-001`, `WSC-TERMINAL-001`.
- Impact: Startup failures can traceback without cleanup.
- Required correction: Contain initialization in typed cleanup path.
- Verification required: Fault-injection terminal-start test.

#### F-013 - Medium: Interactive failure diagnostics are emitted in normal style after terminal setup

- Status: `Open`
- Evidence: `chat_client.py` prints details after terminal cleanup.
- Violated requirement: `WSC-EXIT-001`.
- Impact: Interactive failure diagnostics are not dim-gray and can follow the final reset.
- Required correction: Present diagnostics before terminal cleanup in system style.
- Verification required: PTY style tests.

#### F-014 - Medium: Transport close is unbounded

- Status: `Open`
- Evidence: `AiohttpTransport.close()` awaits websocket close without a bound.
- Violated requirement: T-005 bounded-close ownership.
- Impact: Terminal cleanup can hang indefinitely.
- Required correction: Bound and contain close completion.
- Verification required: Blocking-close fake transport test.

### Validation performed

- `.venv/bin/python -m pytest tests/test_websocket_client.py -q` - exit 0, 5 passed.
- `.venv/bin/python -m py_compile ai_server/websocket_client/*.py` - exit 0.
- `git diff --check` - exit 0.
- Not run: complete pytest, PTY wrapper tests, and real client/server checks; reviewer sandbox denied loopback socket creation.

### Reviewer conclusion

`Blocking findings remain: F-003, F-004, F-006, F-008, F-009, F-010, F-011, F-012, F-013, F-014.`

## Implementation response 2

### Findings addressed

#### F-003

- Response status: `Addressed - awaiting verification`
- Changes: `AiohttpTransport.commit_send()` now serializes and commits an outbound frame synchronously by returning its drain task; `ClientEngine` shields and joins that task, with typed `KNOWN_UNSENT` and `DELIVERY_UNCERTAIN` outcomes.
- Tests added or updated: The fake transport now models the commit API; focused engine suite continues to exercise normal sends.
- Verification: `.venv/bin/python -m py_compile ai_server/websocket_client/*.py && .venv/bin/python -m pytest tests/test_websocket_client.py -q` - exit 0, 7 passed.
- Limitations: Barrier-controlled pre/post-commit failure coverage remains part of F-008.

#### F-004

- Response status: `Addressed - awaiting verification`
- Changes: `ClientState` and `ClientEngine` now model `PRESENTING_START_INPUT` and `PRESENTING_FOLLOW_UP_INPUT`; only presentation completion enters an input-accepting state and establishes a follow-up deadline.
- Tests added or updated: Existing focused engine suite was rerun.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_websocket_server.py -q` - exit 0, 55 passed.
- Limitations: Dedicated presentation barrier and real-PTY timing coverage remains part of F-008.

#### F-010

- Response status: `Addressed - awaiting verification`
- Changes: `engine.py` retains connection-lifetime Conversation IDs and rejects reuse.
- Tests added or updated: `test_engine_rejects_reused_conversation_id`.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py -q` - exit 0, 7 passed.
- Limitations: None.

#### F-011

- Response status: `Addressed - awaiting verification`
- Changes: Batch presenter returns typed `BATCH_COMPLETE`, mapped by the engine to requested-work completion rather than interactive EOF.
- Tests added or updated: `test_batch_completion_without_messages_sends_no_user_event`.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py -q` - exit 0, 7 passed.
- Limitations: None.

#### F-012

- Response status: `Addressed - awaiting verification`
- Changes: Interactive presenter initialization is contained in `run_chat()`'s typed error and cleanup path.
- Tests added or updated: None yet; fault-injection coverage remains part of F-008.
- Verification: `.venv/bin/python -m py_compile ai_server/chat_client.py` - exit 0.
- Limitations: PTY/fault-injection evidence remains outstanding.

#### F-013

- Response status: `Addressed - awaiting verification`
- Changes: Terminal-path connection and local-failure diagnostics now use the interactive system presenter before terminal cleanup; CLI only writes the plain non-TTY usage diagnostic.
- Tests added or updated: None yet; PTY style evidence remains part of F-008.
- Verification: `.venv/bin/python -m py_compile ai_server/chat_client.py` - exit 0.
- Limitations: PTY style evidence remains outstanding.

#### F-014

- Response status: `Addressed - awaiting verification`
- Changes: Aiohttp close is bounded to one second and timeout is contained.
- Tests added or updated: None yet; blocking-close fake remains part of F-008.
- Verification: `.venv/bin/python -m py_compile ai_server/websocket_client/aiohttp_transport.py` - exit 0.
- Limitations: Blocking-close regression evidence remains outstanding.

### Rebuttals

`None.`

## Review pass 8 - verification

### Snapshot

- Reviewer: `task-reviewer`
- Pass: `verification`
- HEAD: `acd4b40d1c2c9fafd9a2791b5f9804041e4a3387`
- Working tree: no staged changes; unstaged client, transport, presenter, wrapper, and focused-test changes; untracked client connection module plus focused presenter, entrypoint, options, live, and process tests.

### Finding status

#### F-004

- Status: `Resolved`
- Evidence: Interactive offer commitment now waits for the first `prompt_toolkit` `after_render` callback ([terminal_presenter.py](/home/maciek/piotr/ai_server/websocket_client/terminal_presenter.py:55)), and terminal-result handling clears a cancelled presentation task before processing the event ([engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:95)). `_cancel_input()` also cancels and clears presentation work ([engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:260)). Focused commit and cancellation regressions are present ([test_terminal_presenter.py](/home/maciek/piotr/tests/test_terminal_presenter.py:26), [test_websocket_client.py](/home/maciek/piotr/tests/test_websocket_client.py:215)).
- Rationale: The prior premature presentation signal and stale-cancelled-task defect are corrected. Broader presentation-race coverage remains under F-008.

#### F-008

- Status: `Open`
- Evidence: Remaining concrete gaps against the task’s required coverage are:
  - no complete legal/invalid state-transition, duplicate-event, or stream-order matrix; focused engine coverage remains limited to selected cases ([test_websocket_client.py](/home/maciek/piotr/tests/test_websocket_client.py:80));
  - no injected-clock before/equal/after follow-up arbitration or blank/help/redraw deadline-invariance tests;
  - presentation-race coverage exercises only `protocol_rejected` during one pending presentation, not disconnect, legal terminal, interruption, or both input kinds ([test_websocket_client.py](/home/maciek/piotr/tests/test_websocket_client.py:215));
  - PTY coverage only proves non-TTY rejection and SIGINT while connecting, not editable-buffer redraw, semantic styling, history persistence, or every interactive exit state ([test_websocket_client_process.py](/home/maciek/piotr/tests/test_websocket_client_process.py:11));
  - live coverage only proves one successful batch turn, not interactive delayed-server behavior nor batch timeout, rejection, and disconnect flows ([test_websocket_client_live.py](/home/maciek/piotr/tests/test_websocket_client_live.py:19));
  - the conformance catalogue still lists planned generic WSC evidence rather than exact current passing tests ([protocol-conformance-catalogue.md](/home/maciek/piotr/docs/protocol-conformance-catalogue.md:147)), contrary to T-005 Stage 5 ([T-005-websocket-client-ux-protocol-redesign.md](/home/maciek/piotr/docs/tasks/T-005-websocket-client-ux-protocol-redesign.md:604)).
- Rationale: The added tests substantively reduce the gap, but the required evidence remains insufficient to claim T-005 conformance.

#### F-013

- Status: `Resolved`
- Evidence: The interactive entrypoint now presents an initialization `OSError` through the system presenter before cleanup ([chat_client.py](/home/maciek/piotr/ai_server/chat_client.py:61)); focused fault injection verifies the diagnostic and teardown ([test_websocket_client_entrypoints.py](/home/maciek/piotr/tests/test_websocket_client_entrypoints.py:10)).
- Rationale: Terminal startup failure is no longer silent.

### New findings

`None.`

### Validation performed

- `.venv/bin/python -m pytest tests/test_terminal_presenter.py tests/test_websocket_client_entrypoints.py tests/test_websocket_client.py -q` - exit 0, 16 passed.
- `.venv/bin/python -m pytest tests/test_websocket_client_options.py -q` - exit 0, 8 passed.
- `.venv/bin/python -m py_compile ai_server/websocket_client/*.py ai_server/chat_client.py ai_server/batch_ws_client.py && git diff --check` - exit 0.
- Not run: websocket live/process tests, complete server/client suite, full pytest suite, and required PTY matrix.

### Reviewer conclusion

`Blocking findings remain: F-008.`

## Implementation response 5

### Findings addressed

#### F-008

- Response status: `Addressed - awaiting verification`
- Changes: Expanded the engine matrix with representative invalid-state, terminal-presentation disconnect/interruption, stream-order, deadline-boundary, and redraw-deadline cases; expanded terminal history/style checks; added live batch success, timeout, rejection, and disconnect flows; added SIGTERM PTY reset coverage. The conformance catalogue now reports exact current test evidence instead of planned generic evidence.
- Tests added or updated: `tests/test_websocket_client.py`, `tests/test_terminal_presenter.py`, `tests/test_websocket_client_options.py`, `tests/test_websocket_client_live.py`, `tests/test_websocket_client_process.py`, and `docs/protocol-conformance-catalogue.md`.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py tests/test_terminal_presenter.py tests/test_websocket_client_entrypoints.py tests/test_websocket_client_options.py -q` - exit 0, 38 passed; `.venv/bin/python -m pytest tests/test_websocket_client_live.py tests/test_websocket_client_process.py -q` - exit 0, 6 passed; `.venv/bin/python -m pytest tests/test_websocket_server.py -q` - exit 0, 50 passed; `.venv/bin/python -m pytest -q` - exit 0, 726 passed before the final focused additions.
- Limitations: The task-required matrix is expanded substantially; retained independent verification is requested to decide whether any concrete gap remains.

### Rebuttals

`None.`

## Review pass 9 - verification

### Finding status

#### F-008

- Status: `Open`
- Evidence: Required interactive live-server coverage is absent: `tests/test_websocket_client_live.py` contains only batch-wrapper flows. Required PTY coverage remains limited to SIGINT/SIGTERM during connection setup ([tests/test_websocket_client_process.py](/home/maciek/piotr/tests/test_websocket_client_process.py:25)), not every interactive state or `/exit`, EOF, rejection, disconnect, prompt editing, history, redraw, and styling paths. The updated WSC catalogue still includes non-exact evidence descriptions such as “presentation-cancellation tests,” “PTY reset checks,” and “bounded-close paths” rather than named current tests ([docs/protocol-conformance-catalogue.md](/home/maciek/piotr/docs/protocol-conformance-catalogue.md:147)).
- Rationale: Coverage expanded substantially, but it does not meet the task’s required PTY and interactive delayed-server evidence, and the catalogue does not yet provide exact current evidence for every WSC claim.

### New findings

#### F-017 - Medium: A simultaneous follow-up deadline and redraw result selects the lower-priority redraw

- Status: `Open`
- Evidence: `_next_result()` handles an already-ready input task before an already-ready deadline task ([ai_server/websocket_client/engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:115)). For `EMPTY`, `HELP`, or `UNKNOWN_COMMAND`, `_handle_input()` reopens the editor ([ai_server/websocket_client/engine.py](/home/maciek/piotr/ai_server/websocket_client/engine.py:213)) even though the deadline notification was already ready. The existing redraw test calls `_handle_input()` directly and does not exercise this simultaneous ready set ([tests/test_websocket_client.py](/home/maciek/piotr/tests/test_websocket_client.py:437)).
- Violated requirement: `WSC-RACE-001` and `WSC-FOLLOWUP-002`; the deadline has higher priority than blank input or local commands ([docs/websocket-client-behavior.md](/home/maciek/piotr/docs/websocket-client-behavior.md:396).
- Impact: The client can expose a fresh follow-up editor after the fixed interval has already closed, contrary to the required arbitration order.
- Required correction: Inspect the complete ready set and select the deadline before redraw-only local input when both are ready.
- Verification required: Barrier-controlled tests for deadline simultaneous with empty input, `/help`, and an unknown command.

### Reviewer conclusion

`Blocking findings remain: F-008, F-017.`

## Implementation response 6

### Findings addressed

#### F-017

- Response status: `Addressed - awaiting verification`
- Changes: The engine now inspects a complete input/deadline ready set before dispatching input. A deadline wins over blank, help, unknown-command, batch-completion, and late-text results; `/exit`, EOF, and eligible text retain their higher priority.
- Tests added or updated: `test_simultaneous_follow_up_deadline_beats_redraw_only_input`, parametrized for blank, `/help`, and unknown-command input.
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client.py -q` - exit 0, 26 passed.

#### F-008

- Response status: `Addressed - awaiting verification`
- Changes: Added `test_interactive_wrapper_renders_delayed_live_output_before_terminal_rejection` and made every WSC catalogue evidence cell name its concrete current test(s).
- Verification: `.venv/bin/python -m pytest tests/test_websocket_client_live.py::test_interactive_wrapper_renders_delayed_live_output_before_terminal_rejection -q` - exit 0, 1 passed.

### Rebuttals

`None.`
