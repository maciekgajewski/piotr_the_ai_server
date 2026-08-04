# T-009: Close Microphone Protocol Conformance Gaps

## Status

- **Authority:** Active implementation and verification plan; not a runtime contract
- **Status:** Complete on 2026-08-03; automated remediation, exact evidence audit, clean independent review, representative live fault injection, and normal multi-device deployment complete
- **Audience:** Maintainers of the Microphone Protocol, microphone manager, microphone drivers, satellite firmware, conformance tests, and hardware verification
- **Read when:** Fixing the Voice PE reconnect visual defect or reviewing whether every normative `MP-*` requirement has exact current evidence

## Objective

Restore full conformance with the normative Microphone Protocol and make future
implementation drift detectable before deployment.

The task covers both the observed reconnect defect and a systematic audit of
every `MP-*` requirement. Passing adjacent unit tests, compiled firmware, or a
single clean hardware sequence must not be treated as evidence for a
cross-layer requirement that those checks do not exercise.

## Binding documents

Read before implementation:

1. root `AGENTS.md` and the architecture decisions in `README.md`;
2. `docs/README.md`;
3. `docs/microphone-protocol.md`;
4. `docs/ai-server-conversation-protocol.md`;
5. `docs/microphone-conversation-mapping.md`;
6. `docs/protocol-conformance-catalogue.md`;
7. `docs/project-standard-satellite-behavior.md`;
8. this task.

The Microphone Protocol is already normative. Implementation drift is a
defect. If the audit finds a genuine ambiguity or a required semantic change,
stop and obtain Captain approval for the protocol change before changing
runtime behavior.

## Observed defect

On 2026-08-03, both `voice-pe-02` in the living room and `voice-pe-03` in the
office displayed firmware-owned `ERROR` while remaining connected and
delivering open-mic audio to the server.

The live log establishes this recovery sequence:

1. an open-mic stream timed out and the manager recreated its microphone
   boundary;
2. the new attempt connected and successfully sent `SetVisualState(IDLE)`;
3. `StartListening` encountered stale concrete-driver stream state;
4. stale-stream recovery performed another transport disconnect and reconnect;
5. the second reconnect did not receive a new manager-owned visual command;
6. firmware correctly retained `ERROR` while audio capture resumed.

This violates the connected-visual and reconnection requirements, including
`MP-VISUAL-005`. It also demonstrates that manager-only, driver-only, and
firmware-only checks can all pass while their composition violates the
protocol.

## Implementation record

The authorized remediation makes stale-stream cleanup fail the current driver
operation with `MicrophoneUnavailable` after disconnecting. This returns
control to the manager instead of allowing a concrete driver to reconnect
silently. The manager invalidates its connected visual cache, closes the old
boundary, recreates protocol state, and the next readiness attempt sends
`SetVisualState(IDLE)` before `StartListening` can become operational.

Concrete driver closure now continues transport teardown after playback-cleanup
or unsubscribe failures, escalates a failed graceful disconnect to the API
client's forced transport disconnect, clears all concrete state, and then
re-raises the original programming or invariant failure. If even forced
disconnect fails, the manager invokes application-wide fatal containment; the
production hard exit closes the process socket instead of leaving one dead
microphone supervisor beside a potentially connected device. Focused
concrete-driver tests cover every failure stage, preservation of the original
failure, and fatal escalation from both manager teardown and the internal
stale-recovery path. Representative live hardware evidence is recorded below.
Other
focused tests cover the observed timeout/stale-stream path, clean reconnect,
repeated recovery with fresh identifiers, and the absence of
`ListeningStarted` before post-reconnect visual initialization.

During independent review, the audit exposed an omission in the normative
transition table: `DriverClosed` was defined in the event inventory but had no
legal transition. Captain selected option 1 on 2026-08-03. `DriverClosed` is
therefore valid from every non-closed state, discards the active operation, and
transitions to `CLOSED`; repeated delivery in `CLOSED` is an inapplicable,
idempotent no-op.

The 23 normative `MP-*` identifiers have been reconciled with the conformance
catalogue. Exact automated test names now replace suite-level evidence claims.
Exhaustive state/command/event matrices and source-level checks for both shared
satellite firmware packages were added. Live-device results and the explicitly
waived duplicate sequence are recorded below and are not inferred from these
automated checks.

Current automated checkpoint on 2026-08-03:

- focused microphone, concrete-driver, protocol, and firmware-source suite:
  304 passing;
- complete Python suite: 1001 passing in 19.01 seconds;
- `orchestrator_and_dsa_tests/run.sh --no-transcript`: 45/45 passing with
  orchestrator and DSA model `qwen3:14b` in 35.36 seconds;
- `git diff --check`: clean.

Independent review required four passes. Passes 1 through 3 identified and
closed teardown containment, `DriverClosed` transition, rejected-event
observability, and stale status-surface findings. Pass 4 reported exactly
`No findings.`

### Voice PE 03 hardware checkpoint

On 2026-08-03 from 19:44 through 19:55 UTC, `voice-pe-03` in the office was
verified alone through the foreground Docker runtime started by
`tools/ai-server.sh`, using the private `0600` config
`/tmp/ai-server-voice-pe-03-t009.yaml` generated from the operator config. The
device reported name `piotr-voice-pe-03`, ESPHome `2026.4.5`, firmware
compilation time `2026-07-13 20:05:26 +0000`, and model
`esp32-s3-devkitc-1`. No firmware was changed or flashed for T-009.

Visible device results and correlated server evidence passed:

- stopping the controlling server produced firmware-owned `ERROR`; a fresh
  start issued `SetVisualState(IDLE)` before `StartListening`, visibly returned
  to `IDLE`, and replaced session prefix `8f13329e...` with `cf1d6ae4...`;
- clean open-mic arm remained visibly `IDLE`; ordinary non-wake speech was
  rejected without Conversation creation;
- `Ryszardzie, która godzina?` accepted, entered `PROCESSING`, completed
  playback `cf1d6ae4...-7`, returned to `IDLE`, and re-armed with fresh
  `listen_id=cf1d6ae4...-8`;
- a disposable 5 ms `audio_event_timeout_seconds` forced the real active-stream
  recovery path. The device briefly showed `ERROR`, then visibly returned to
  `IDLE` only after reconnect initialization. One recorded recovery replaced
  active `listen_id=3df80e15...-19` / `utterance_id=3df80e15...-20` with new
  session `...-21`, sent `SetVisualState(IDLE)`, and only then acknowledged
  fresh `listen_id=3df80e15...-22`. Ambient sound caused repeated recoveries;
  each used fresh identifiers and none left a connected device in `ERROR`;
- the clarification turn `Ustaw klimatyzację w salonie` completed playback,
  committed follow-up with `listen_id=483f7a8e...-8`, timed out at the configured
  15-second deadline, played `play_conversation_timeout_cue`, terminated with
  `follow_up_timeout`, returned visibly to `IDLE`, and re-armed.

The final clarification capture exposed a separate latency observation: a wake
candidate was detected at `19:53:53.930`, but continuous captured sound delayed
end-of-speech until `19:54:40.379`, producing a 75.98-second segment before the
ack cue. Once speech ended, the accepted cue, clarification, follow-up, timeout,
and cleanup ordering completed normally. This is not treated as a T-009
protocol failure or concealed as a clean latency result.

At `20:00:24` UTC the normal operator configuration started the same current
server code for all configured satellites. `voice-pe-02` connected at
`20:00:25.338`, received `SetVisualState(IDLE)` at `20:00:25.361`, and only
then acknowledged fresh open-mic `listen_id=b17912e5...-5` at `20:00:25.387`.
`voice-pe-03` followed the same deployed initialization ordering with fresh
`listen_id=b17912e5...-4`.

Captain explicitly declined a duplicate isolated `voice-pe-02` fault-injection
run on 2026-08-03 and directed deployment of the same shared change instead.
Because T-009 changed only the shared server driver/manager, not device
firmware or per-device behavior, the accepted closure uses the complete visible
`voice-pe-03` sequence plus automated coverage of both shared firmware packages
and the logged normal `voice-pe-02` initialization. This is an authorized
deviation from the original two-device live-fault-injection plan, not a claim
that the skipped visible sequence was performed.

No firmware file changed in this remediation. The new firmware-source
conformance tests cover both shared hardware packages; the ESPHome
validation/compile/generated-source gate remains required if review or hardware
verification demonstrates that a firmware edit is necessary.

## Work needed

### Correct the observed violation

- Ensure every effective controlling-server reconnection is followed by the
  required explicit manager-owned visual initialization before normal
  listening continues.
- Ensure recoverable microphone-boundary recreation does not carry stale
  concrete-driver state into the next listening generation.
- Preserve firmware-owned `ERROR` while disconnected and until the first valid
  post-reconnect visual command.
- Preserve the sealed `Microphone` interface and the protocol ownership split:
  the manager owns desired connected visual state and recovery policy; drivers
  own concrete transport and firmware interaction.
- Add fail-fast enforcement where an internal reconnect or stale recovery could
  otherwise bypass a normative obligation.

### Audit every Microphone Protocol requirement

- Enumerate every current `MP-*` requirement from the normative protocol.
- Reconcile that inventory with `docs/protocol-conformance-catalogue.md`; add
  missing requirements and remove or correct stale entries only when supported
  by the normative document.
- For every requirement, record the implementation owner and exact current
  evidence: test module and test name, generated-firmware inspection, or a
  dated manual hardware result.
- Mark missing, partial, indirect, or obsolete evidence explicitly. A test
  category or suite name alone is not sufficient evidence.
- Verify that every recovery, reconnect, timeout, cancellation, and stale-state
  path is covered at the boundary where the obligation can actually be
  violated.
- Add cross-layer coverage wherever isolated manager, driver, or firmware tests
  cannot prove the composed behavior.
- Keep protocol assertions intact. Fix invalid event creation or recovery
  ordering rather than suppressing invariant failures.

### Correct status and governance surfaces

- Update the conformance catalogue so it distinguishes required evidence from
  evidence that has actually passed.
- Correct task, index, and protocol implementation-status claims that are
  broader than the available evidence.
- Make incomplete hardware or cross-layer evidence visible in the primary
  status surfaces rather than only in historical notes.
- Ensure future implementation changes touching a named requirement update its
  exact evidence in the same change.

## Required regression coverage

Coverage must include at least:

- the exact timeout -> boundary recreation -> stale-stream cleanup -> internal
  reconnect sequence observed on both Voice PE devices;
- visual initialization after each effective reconnect, including reconnects
  initiated below the manager boundary;
- no listening acknowledgement or captured audio becoming operational while
  the connected visual remains unintentionally uninitialized;
- a clean reconnect without stale state;
- repeated recovery, proving idempotence and preventing visual-state cache
  drift;
- failed recovery, proving firmware remains in `ERROR` and no false connected
  state is reported;
- all existing normal, timeout, cancellation, correlation, half-duplex,
  playback, cue, and open-mic protocol sequences required by the audited
  `MP-*` inventory.

Tests must exercise the real concrete-driver path where the defect lives.
Mocking out the reconnect or visual interaction is not sufficient for the
regression that claims to cover this incident.

## Hardware verification

After automated verification, exercise real hardware one device at a time.
The original plan required each affected firmware/driver class and both
reported Voice PE devices. Captain accepted one complete representative Voice
PE fault-injection sequence plus normal multi-device deployment and waived the
duplicate `voice-pe-02` sequence on 2026-08-03.

Verify visibly and from correlated server/device logs:

- disconnected -> `ERROR`;
- first connected initialization -> `IDLE`;
- clean open-mic arm remains `IDLE`;
- forced recoverable stream failure returns to `IDLE` after reconnection;
- stale-stream recovery cannot leave a connected device in `ERROR`;
- wake candidate, rejection, acceptance, playback, follow-up, timeout, and
  cleanup retain their normative visual sequences;
- repeated recovery does not reuse protocol identifiers or stale driver state.

Record the exact runtime path, configuration path, device, firmware build,
timestamps, protocol identifiers, commands, events, and visible result. Do not
infer a hardware result solely from server logs.

## Out of scope

- Changing the approved meaning of Microphone Protocol states or events without
  a separately approved normative change.
- Websocket, orchestrator, DSA, or model behavior unrelated to microphone
  conformance.
- Treating STT backlog warnings or unrelated `.local` resolution failures as
  causes of the reported visual defect.
- Silencing assertions, weakening fail-fast behavior, or adding hidden
  compatibility fallbacks.

## Acceptance criteria

- The observed two-Voice-PE reconnect defect has a focused automated
  regression that fails on the current behavior and passes after the fix.
- Every effective reconnect satisfies `MP-VISUAL-005`; connected devices do not
  remain in firmware-owned `ERROR` after successful initialization.
- Recoverable boundary recreation starts from trusted concrete-driver and
  protocol state.
- Every normative `MP-*` requirement has an exact evidence entry with a current
  pass result, or is explicitly recorded as an open blocker.
- Every discovered evidence gap is either closed or prevents the task from
  being marked complete.
- The focused microphone suites, complete Python suite, and complete
  orchestrator/DSA behavior suite pass using the currently configured model.
- Affected ESPHome configurations validate and compile; generated behavior is
  inspected when firmware changes.
- Required live hardware sequences pass and are recorded one device at a time.
- `docs/README.md`, the protocol, the conformance catalogue, related task
  statuses, implementation references, and test references agree.
- The implementation receives an independent review. All findings are
  addressed and review is repeated until it reports `No findings.`

## Completion rule

T-009 completed with the exact regression, full `MP-*` evidence audit,
corrected status surfaces, current automated results, clean independent review,
complete representative `voice-pe-03` hardware evidence, and normal
`voice-pe-02` deployment initialization. The duplicate `voice-pe-02` live
fault-injection sequence was explicitly waived by Captain and must not be
represented as performed.
