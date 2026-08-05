# T-010: Recover Stopped ESPHome Transports

## Status

- **Authority:** Active implementation and verification plan; not a runtime contract
- **Status:** Complete; implemented, independently reviewed, and live-fault-tested on `voice-pe-03` on 2026-08-05
- **Audience:** Maintainers of the microphone manager, ESPHome microphone driver, and protocol conformance tests
- **Read when:** Investigating a Voice PE or Box3 that shows disconnected `ERROR` while the AI server retains a stale client object

## Objective

Make an unexpected ESPHome transport stop immediately invalidate the concrete
driver boundary and enter the existing manager-owned recovery path. Recovery
must reconnect, initialize the manager-owned visual state, and create a fresh
listening generation without an implicit driver reconnect.

## Binding documents

1. root `AGENTS.md` and the architecture decisions in `README.md`;
2. `docs/README.md`;
3. `docs/microphone-protocol.md`;
4. `docs/ai-server-conversation-protocol.md`;
5. `docs/microphone-conversation-mapping.md`;
6. `docs/protocol-conformance-catalogue.md`;
7. `docs/project-standard-satellite-behavior.md`;
8. this task.

The normative protocols already require manager-owned recovery, fresh
correlations, firmware-owned `ERROR` while disconnected, and explicit
post-reconnect visual initialization. This task repairs implementation drift;
it does not change those contracts.

## Observed defect

On 2026-08-05, the manually started server retained `voice-pe-02` and
`voice-pe-03` client objects after their ESPHome TCP sessions had disappeared.
Both devices still resolved, answered ICMP, and accepted new TCP connections on
port 6053, but the server had no TCP session to either address. The driver
reported `connected=True` solely because its client object remained non-null
and waited indefinitely for a microphone event. The devices correctly showed
firmware-owned `ERROR`, while the server never entered its availability retry
path.

The installed `aioesphomeapi` client already detects socket and keepalive loss
and exposes it through `APIClient.connect(on_stop=...)`. The driver did not
register that callback.

The separately configured Box3 and bedroom Voice Preview failed `.local`
resolution throughout the same run. That reachability problem is not part of
this defect.

## Work needed

- Register a concrete-driver transport-stop callback for each API client.
- Treat a stop of the current client as temporary unavailability regardless of
  the dependency's `expected_disconnect` classification; device-initiated
  disconnects are still availability loss from the manager's perspective.
- Wake a blocked `wait_for_event()` and reject later operations on the stopped
  transport.
- Ignore callbacks from clients that the driver has already closed or replaced.
- Coalesce duplicate callbacks and discard their private wake-up state during
  boundary closure.
- Keep outage logging episode-based: one manager WARNING for the first failure,
  DEBUG retry detail, and one INFO as soon as recovered readiness commits.
- Keep reconnect and retry ownership in `MicrophoneManager`; the driver must not
  use dependency-level automatic reconnect.
- Report concrete connection state from transport validity rather than client
  object presence.
- Preserve T-009 recovery ordering: firmware `ERROR`, new connection,
  `SetVisualState(IDLE)`, then fresh `StartListening` and `ListeningStarted`.

## Required regression coverage

- Unexpected transport stop releases an already-blocked event waiter.
- A stopped current client cannot be reused or silently reconnected.
- Playback chunks and completion commands issued after transport invalidation
  fail before producing side effects or waiting for drain.
- Connection diagnostics remain disconnected until the API handshake completes.
- A stop callback that wins during handshake prevents connection readiness and
  voice-assistant subscription from committing.
- Normal close does not create a false availability failure.
- Late old-client and duplicate callbacks cannot affect a current boundary.
- Manager recovery after callback-driven failure uses fresh correlations and
  initializes `IDLE` before listening becomes operational.
- Existing timeout, stale-stream, correlation, visual, playback, cue, and
  teardown containment coverage remains passing.

## Verification

1. Run the focused Box3/Voice PE driver tests.
2. Run the combined microphone protocol, manager, firmware-source, and mapping tests.
3. Run the complete Python test suite.
4. Run `orchestrator_and_dsa_tests/run.sh --no-transcript` with the configured model.
5. Run `git diff --check`.
6. Review the complete change and address every finding until clean.
7. Rebuild and restart the manually started runtime.
8. Fault-test a configured Voice PE selected by the Captain, recording
   callback-driven availability loss, boundary recreation, explicit `IDLE`, a
   fresh listening correlation, and restored audio capture. Smoke-test the
   other affected Voice PE during normal startup.

No ESPHome firmware build or flash is required unless implementation or live
verification shows a firmware defect.

## Acceptance criteria

- A stopped ESPHome transport cannot leave the server blocked behind a stale
  client object.
- The manager, not the concrete driver, owns every retry and reconnect.
- Every recovered connection satisfies `MP-VISUAL-005` before listening resumes.
- Repeated and stale callbacks cannot invalidate a newer connection.
- Focused, complete, behavioral, review, representative live-fault, and
  second-device startup checks pass.
- The documentation index, this task, and the conformance catalogue agree.

## Implementation and verification record

Implemented on 2026-08-05 in the ESPHome concrete driver and microphone
manager. The driver now registers the dependency's transport-stop callback,
tracks completed transport validity independently of client-object presence,
wakes a blocked event waiter, rejects commands after transport loss, and
guards duplicate, stale-client, and handshake-race callbacks. The manager
retains retry ownership and records recovery as soon as fresh listening
readiness commits.

Automated verification passed:

- 120 focused driver and manager tests;
- 311 microphone protocol, manager, firmware-source, and mapping tests;
- 1008 complete Python tests;
- all 45 orchestrator and DSA behavior scenarios with the configured
  `qwen3:14b` model;
- `git diff --check`;
- three independent review/fix passes, ending with no blocking findings.

The updated manually started server connected both affected devices normally:
`voice-pe-02` received listening generation `...-4`, and `voice-pe-03`
received `...-5`. At the Captain's direction, the live transport fault was
then injected on the nearby office `voice-pe-03`:

- `11:23:00.455`: the client callback reported the stopped current transport
  as `connected=False transport_unavailable=True`;
- `11:23:00.456`: the manager emitted the outage WARNING and recreated the
  protocol boundary that held listening generation `...-5`;
- `11:23:01.408`: the replacement connection and subscription completed;
- `11:23:01.440`: `set_visual_idle` completed before listening was started;
- `11:23:01.467`: `start_open_mic_listening` completed, fresh generation
  `...-117` reached `ListeningStarted`, and the manager emitted the recovery
  INFO;
- the replacement stream became active and resumed audio capture.

This live sequence closes the stale-client defect without a firmware change.
The unrelated Box3 and bedroom `.local` resolution failures remain outside
this task.
