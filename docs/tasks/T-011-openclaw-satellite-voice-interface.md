# T-011: OpenClaw Satellite Voice Interface

## Status

- **Authority:** Plan; not a runtime contract
- **Status:** Stage 1 in progress; Python Gateway foundation, isolated `chat.send` turn adapter, and notification codec, audio preflight, and result sender implemented; voice-service integration pending; Talk excluded
- **Created:** 2026-10-09
- **Updated:** 2026-10-11
- **Audience:** Maintainers of the OpenClaw voice subproject and reused satellite components
- **Read when:** Planning or implementing OpenClaw satellite input, replies, or notifications

## Objective and current scope

Create a separate subproject that presents the voice server to OpenClaw through
its native node/chat interfaces. Satellites connect only to the voice server and
are abstracted from OpenClaw. Reuse satellite drivers, Whisper, Piper, speaker
recognition, and room context. OpenClaw owns reasoning and actions. The Python
service translates between speech and text and owns hardware/media lifetimes.

This is a sidequest, not a replacement for Piotr. Piotr and the voice service
will not run simultaneously on the same machine. Shared satellite ownership
and switching between assistants remain outside scope.

Captain simplified the direction on 2026-10-09: use ordinary text exchanges and
be a native node notification target. Captain accepts either no follow-up or a
fixed default follow-up policy for now. The working choice is **no follow-up**:
one accepted spoken request, one assistant response, playback/cleanup, then
return to normal wake-word readiness. A new utterance requires a new wake.
Neither response punctuation nor model intent determines microphone reopening.
Spoken notifications also finish without opening a reply interval.

This supersedes the custom channel-plugin/dedicated-voice-tool architecture for
the current milestone. Model-selected `finish`/`listen`, proactive questions
with correlated spoken answers, and the full server-start/listen extension are
deferred. Their earlier investigation is retained as historical design evidence.
No custom OpenClaw plugin is assumed necessary for this milestone. Captain
excluded the Talk API family on 2026-10-10; keep the existing Whisper/Piper
media path and the native node/chat interfaces.

Captain selected operator `chat.send` for spoken requests on 2026-10-10. On
2026-10-11 Captain corrected the node boundary: one voice-server node abstracts
all satellites. Whether spoken text uses that node's `voice.transcript` event or
a separate server-owned operator `chat.send` connection is being reconciled;
the isolated `chat.send` adapter remains a tested candidate, not a runtime
binding. Captain
selected one OpenClaw history per configured room, shared by speakers in that
room; unreliable speaker recognition does not select history. A recognized name
is included only as a tentative hint, and unknown remains unknown. The selected
method still needs live native-path verification.

Captain selected completed local playback as the `system.notify` success
boundary on 2026-10-10. The node returns success only after the matching
`PlaybackFinished` and manager cleanup restore trusted readiness. A reservation
or synthesis start is not success. An invocation timeout or disconnect after
dispatch leaves the caller uncertain and never triggers automatic replay.
Captain selected rejection before playback when a notification cannot safely
fit the native invoke deadline. The isolated bounded preflight is implemented
and tested; manager-owned admission and playback remain unimplemented.
Captain selected manager-owned interruption of otherwise idle wake-word or
open-mic listening on 2026-10-11: stop and join that listening generation and
any open-mic STT task before notification playback, then re-arm the same mode
after cleanup. Speech that starts before notification reservation wins the race
and causes a busy rejection.
Captain selected speaking notification title followed by body, with a short
pause when both are present. A single present field is spoken by itself.
Captain selected rejection of unsupported native notification hints: an
omitted delivery hint or `delivery=system` is accepted; requested sound,
priority, overlay, or auto delivery is rejected before playback.

Captain said "proceed" on 2026-10-10 after excluding Talk, authorizing continued
implementation of this subproject. Resolve outstanding binding decisions and
verify the native contract before wiring the runtime. This authorization does
not itself settle those choices or approve deployment to running services.

## Binding documents

Read root `AGENTS.md`, the architecture decisions in `README.md`, and
`docs/README.md`, including T-004's remaining hardware verification. Before
changing runtime behavior, read:

1. [Conversation protocol](../ai-server-conversation-protocol.md);
2. [Microphone protocol](../microphone-protocol.md);
3. [Microphone-Conversation mapping](../microphone-conversation-mapping.md);
4. [Conformance catalogue](../protocol-conformance-catalogue.md);
5. [Satellite behavior](../project-standard-satellite-behavior.md).

OpenClaw's Gateway protocol is a separate external binding from Piotr's
websocket conversation protocol. Do not change the latter incidentally.

## Behavior and ownership

| Concern | Current requirement |
|---|---|
| Spoken input | Existing wake/capture/STT pipeline supplies complete accepted text |
| Reply | Ordinary OpenClaw assistant text is rendered through Piper; end after output and cleanup |
| Follow-up | No automatic follow-up in the current milestone, including after a question |
| Notification | OpenClaw addresses a node using native `system.notify`; speak title then body with a short pause, or the one present field; success requires completed playback and cleanup |
| Destination | One OpenClaw node represents the voice server; satellites have no Gateway identity or credentials. Internal satellite selection for a notification remains to be specified |
| Speaker identity | Keep existing initial recognition; unknown is normal accepted input |
| Room | Configured originating satellite area, included explicitly in each text request; live native context carriage still needs verification |
| Session history | One stable scope per configured room; speakers in a room share that history; recognition does not determine the key |
| Notification identity | No recognized speaker and no inference from an intended recipient or originating session |
| Busy/unavailable | Reject explicitly; no durable notification queue and no interruption of an active exchange |
| Idle listening | A notification may reserve an idle satellite, stop and join its wake-word or open-mic listening generation and any STT task, speak, then re-arm the same mode; accepted speech wins the race |
| Media ownership | Existing manager and abstract drivers own events, listening/playback exclusion, cues, visuals, and recovery |
| Reasoning, tools, permissions, retry decisions | OpenClaw |

## Working architecture and unresolved bindings

```text
satellites <-> Python voice service <-> native OpenClaw Gateway node/chat APIs
              Whisper / Piper
```

- Reuse repository Python components without copying implementations or a broad
  shared-library extraction. Use abstract Agent interfaces for accepted text
  exchanges; keep OpenClaw details out of the bridge and drivers.
- Reconcile node `voice.transcript` with the earlier operator `chat.send`
  selection for accepted spoken text. The copied installed version separates
  these role/auth surfaces. The node event does not return its generated run
  ID; `chat.send` offers explicit run correlation but needs a separate
  server-owned operator connection. Verify the selected path through an
  isolated native fixture before wiring the voice runtime.
- Native `nodes` tool action `notify` invokes `system.notify` with title/body.
  Our node implementation would render that text through Piper. This is a
  proposed custom presentation of a native command, not an existing headless
  node feature that already controls these satellites.
- Captain corrected the earlier per-satellite node plan on 2026-10-11. The
  Python voice service owns one OpenClaw node identity and connection; ESPHome
  hardware connects only to the voice service. Any operator connection also
  belongs to the service. Node pairing and credentials therefore belong to the
  service, never to individual satellites. Internal notification destination,
  command declaration/allowlists, session keys, and credential ownership remain
  deployment/design work.
- Captain selected a Python client in the voice service for native Gateway
  WebSocket on 2026-10-10. Use the existing `aiohttp` WebSocket dependency;
  declare a direct Ed25519 dependency before implementation. Verify the
  installed Gateway's handshake, pairing, scopes, event framing, and reconnect
  contract with a version-pinned fixture. Do not introduce a JavaScript sidecar
  or a new local Python-to-JavaScript link for this milestone. Do not merge
  different configured rooms into one history; speakers within one room share
  its history by Captain's choice.
- Require an explicit user-supplied configuration path; CLI overrides matching
  config values. Do not start Piotr's orchestrator, DSAs, or reasoning model.
- Bound text, buffers, concurrent work, operation deadlines, and local cleanup.
  Correlate runs and connection generations; never replay old replies,
  notifications, or uncertain user submissions automatically after reconnect.

Captain selected the existing Gateway's native WebSocket transport on 2026-10-09
and retired the earlier requirement for both loopback TCP and Unix sockets for
T-011. The [TCP/Unix local transport draft](T-011-local-transport-contract.md)
describes the superseded custom Python-to-plugin link. No proxy or Gateway
bind/auth configuration change is part of this selection.

## Local protocol impact

For ordinary request/reply, the adapter can propose mapping successful native
run completion to `END_CONVERSATION`, without requiring a model voice tool or
parsing punctuation. Exact final-output/run-completion semantics still need
verification; a transport acknowledgement is not completed reasoning or audio.

Notifications still require explicit admission and output ownership. The current
Conversation protocol requires non-empty initial user input, and the current
microphone mapping renders output only inside an active Conversation. Do not
fabricate a user message or bypass the manager to play a notification. The
[stage-one investigation](T-011-stage-one-investigation.md#minimal-speak-only-notification-binding-candidate)
proposes a manager-owned speak-only operation outside the Conversation bridge,
with a coordinated mapping extension. Stage 1 must prove its contention with
capture/STT, stop/join before playback, cancellation, completion, and cleanup
before re-arm. The full speak-and-listen design is deferred; unchanged core
interfaces or firmware are not yet proven.

Retain Captain's selected remote-cancellation policy: stop voice and join local
work, keep the shared Gateway running, and report remote settlement as uncertain
when unproven. This remains relevant to native chat requests. Unresolved remote
sessions cannot be reused for voice, and late output cannot enter a later
exchange. Native cancellation/recovery evidence must replace assumptions about
in-process SDK access. Generic local Agents retain strict lifecycle guarantees;
necessary remote-contract changes must be explicit and tested.

## Stages and verification

### 1. Verify and finish the native integration design

- Verify installed versus loaded version and native handshake/pairing/auth
  without exposing credentials or changing running services.
- Prove native text submission, correlated reply delivery, typed completion,
  failure/cancellation, and bounded output with isolated fixtures, then a
  bounded live check once the required credentials/pairing are available.
- Prove native notification invocation, node-side rejection propagation,
  the installed built-in tool's 30-second default invoke deadline,
  timeout/disconnect behavior, and the selected completed-playback receipt.
  Bound admitted speech so the native deadline can be met, and reject an
  impossible request before playback. Do not equate native notification
  acceptance with completed playback.
- Prove one voice-server node identity, safe internal notification routing
  without cross-satellite delivery, room-scoped session/history isolation, and
  room/speaker context carriage as finally selected.
- Specify the minimal speak-only notification binding and the remote-lifetime
  contract; reconcile affected normative documents and conformance plans before
  their eventual approved runtime cutover.

The [investigation](T-011-stage-one-investigation.md) separates installed-source
findings, earlier plugin fixtures, and outstanding native-path proof. Prior SDK
fixtures do not establish native node/chat correctness. Stage 1 remains open.

The shared [Python Gateway foundation](../../openclaw_voice/README.md) was
implemented and tested before the spoken-request method was selected. It
provides durable Ed25519 identities, signed protocol-4 connections, bounded RPC
and event handling, and explicit uncertain submission/cancellation outcomes.
Isolated tests include the installed `2026.9.8` connect validators and signature
verifier; authenticated live connection and voice-service integration remain
outstanding.

The isolated operator `chat.send` adapter now constructs room-scoped session
keys and explicit room/tentative-speaker messages, correlates terminal replies
by run and room, and quarantines uncertain routes. Synthetic request and event
shapes pass the installed `2026.9.8` schema probe. These checks do not prove
that the running Gateway executes a request or returns a real-model reply.

### 2. Implement one spoken request and reply

After the remaining Stage 1 binding decisions and verification, add the
separate entry point/configuration and native adapter.
Reuse capture/STT, recognition, immutable context, TTS, and playback. End after
one successful reply and cleanup; exercise unknown-speaker input and failures.

### 3. Implement spoken native notifications

After the speak-only binding and internal destination policy are settled, expose
the voice-server node as a notification target and handle `system.notify` using
the approved speak-only binding. Preserve exclusion
with user input and report busy, unavailable, failed, and uncertain outcomes.
Do not open follow-up listening or introduce a durable playback queue.

### 4. Review and accept

Run focused native-adapter and lifecycle tests, relevant Conversation/microphone/
mapping/STT/TTS tests, and the complete Python suite if shared runtime changes.
Run configured-model orchestrator/DSA tests if those behaviors change; none are
planned. Use the project's independent review-agent workflow, fixing findings
until a fresh review reports `No findings.`.

With Piotr stopped, verify both an ordinary exchange and an addressed spoken
notification on a configured satellite. Record Captain's hardware acceptance
separately. Document startup, shutdown, configuration, and return to Piotr
without rewriting Piotr's config. Do not run sudo from the agent terminal.

## Acceptance criteria

- A wake-word request reaches OpenClaw as text with the intended isolated
  context; ordinary reply text plays through Piper, then readiness returns.
- Unknown speakers work normally. Neither replies nor notifications
  automatically open a follow-up interval.
- OpenClaw's existing notification operation addresses the intended idle
  satellite and produces speech without preceding user input.
- Busy/unavailable destinations reject without overlapping audio or capturing
  speech into the wrong exchange. Success and uncertainty reflect the verified
  native receipt contract and local playback facts.
- Cancellation, disconnect, restart, and late results cannot leak speech or
  context into a later interaction; uncertain operations are not auto-replayed.
- Local cleanup and explicit remote uncertainty follow the selected policy;
  unresolved remote routes stay excluded, without killing the shared Gateway.
- Piotr behavior is preserved. Any affected normative protocols and tests
  change together in an approved cutover. T-004 hardware work stays separate.
- Automated validation, independent review, deployment evidence, and Captain's
  hardware acceptance are separate gates before completion.

## References

- [Native nodes](https://docs.openclaw.ai/nodes)
- [Node notification CLI](https://docs.openclaw.ai/cli/nodes)
- [Gateway protocol](https://docs.openclaw.ai/gateway/protocol)
- [WebChat interface](https://docs.openclaw.ai/web/webchat)

Documentation was checked on 2026-10-09 and supplemented by the copied installed
`2026.9.8` distribution. No live OpenClaw connection, plugin installation,
deployment, or satellite acceptance has been performed for T-011.
