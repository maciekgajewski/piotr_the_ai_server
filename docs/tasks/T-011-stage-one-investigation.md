# T-011 stage-one integration investigation

- **Authority:** Investigation and candidate design; not a runtime contract
- **Status:** In progress; one server-owned node abstracts satellites; spoken-input binding under reconciliation; single request/reply and spoken notifications; native binding verification remains open
- **Date:** 2026-10-09
- **Updated:** 2026-10-11
- **Scope:** Stage 1 of [T-011](T-011-openclaw-satellite-voice-interface.md), authorized by Captain

## Current direction: native text exchanges and spoken notifications

Captain simplified T-011 on 2026-10-09 after reconsidering OpenClaw's native
peripheral interfaces. The current [task plan](T-011-openclaw-satellite-voice-interface.md)
selects native node/chat integration. On 2026-10-11 Captain clarified that one
server-owned OpenClaw node abstracts all satellites; satellites connect only to
the Python voice server. The earlier per-satellite node plan is superseded.
Captain accepts no follow-up or a fixed
default; the working choice is no follow-up: one request, one response, then
normal wake-word readiness. Notifications are speak-and-finish.

This section and the current checklist supersede earlier architecture proposals
below. Dedicated `voice_reply`/`voice_start` tools, a custom channel plugin and
two-backend Python-to-plugin link, and proactive questions with correlated spoken
answers are not current implementation requirements. Captain's later
2026-10-10 "proceed" authorizes continued T-011 implementation. Runtime work
that changes a normative Piotr protocol still needs its coordinated contract
cutover; deployment and hardware acceptance remain separate.
Retain remote cancellation uncertainty, local cleanup, unknown-speaker support,
room context, boundedness, and exclusion of unresolved remote routes. The native
binding must establish how to preserve them without in-process SDK access.

### Installed native interface evidence

Read-only inspection of the copied installed `2026.9.8` bundles found:

| Evidence | Observed behavior and limit |
|---|---|
| `server-node-events-BhuJchmT.mjs`, `voice.transcript` | Accepts transcript text and optional session key, creates a run, and dispatches an agent command with voice provenance. The default session is the configured main session, so the adapter must explicitly choose scope; no history-isolation guarantee follows from merely being a node |
| Same bundle, `chat.subscribe` / `chat.unsubscribe` | Registers node subscription to a canonical session; delivery, authorization, run correlation, and reconnect behavior still need an executable fixture |
| `gateway-chat-COLZ1NLZ.mjs` | Native chat client sends `chat.send` with session/message/idempotency key and supports `chat.abort`; this is an alternative to node-originated transcript events |
| `role-policy-C4wSnCoS.mjs` | Separates `node` and `operator` methods. Do not call operator chat RPCs on a node connection by assumption |
| `openclaw-tools-BwWTN0hs.mjs`, nodes tool `notify` | Resolves the requested node and invokes native `system.notify` with title/body and optional sound/priority/delivery, using a generated idempotency key; requires non-empty title or body |
| `nodes-cli-TwYkDHOS.mjs`, `notify` | The operator CLI uses the same native command and an invocation timeout |
| `node-command-policy-B8V85z2C.mjs` | Includes `system.notify` in platform/default command lists, including unknown platform. This does not bypass pairing, declared commands, or configured policy |

The native notification command is real; spoken presentation through Piper on
our satellites would be new adapter behavior. No notification was sent and no
device was paired, configured, or activated during this inspection. These are
source checks, not native-path runtime verification. Current upstream
[node notification documentation](https://docs.openclaw.ai/cli/nodes) agrees
that `notify` targets nodes declaring `system.notify`.

Captain asked on 2026-10-10 whether using the node for spoken ingress would
carry user, device, and location metadata. The installed `ConnectParamsSchema`
allows a device ID and client display name, platform, device family, model
identifier, and time zone, plus declared capabilities/commands/permissions.
It has no current-speaker, room, or configured-area field. Optional
`location.get` is a separate node command for geographic coordinates, not an
automatic room label. The installed `voice.transcript` handler reads only text,
optional session key, and deduplication fields from its event; it passes fixed
voice provenance into the agent request, but no node ID, human identity, or
room. The `agent.request` node event defaults to a `node-<id>` session and also
does not add speaker or room to the agent request. Pairing authenticates the
node, not whichever person is speaking near the satellite. Node-originated
ingress may still be selected, but it needs explicit context carriage and
native reply/acceptance correlation proof. Captain's node preference reopens
the choice between node ingress and operator `chat.send`; no new choice is
recorded yet. See the current upstream [Gateway handshake](https://docs.openclaw.ai/gateway/protocol/handshake)
and [location command](https://docs.openclaw.ai/nodes/location-command) for
the node-level metadata and optional geographic location surface; installed
source evidence is `sessions-Bw59kgfQ.mjs` and
`server-node-events-BhuJchmT.mjs`.

The installed nodes tool returns a simple `{ok: true}` after its invocation
awaits; it does not itself interpret an audio-completion receipt. Before using
that path, verify how node-side errors propagate and define the node's success
boundary. Notification title/body are candidate spoken text; sound, priority,
and delivery hints need an explicit supported/rejected mapping. A timeout or
disconnect after possible playback is uncertain and must not cause replay.

Further read-only tracing of the copied installed bundles on 2026-10-09 narrows
the candidate native binding:

- `voice.transcript` silently returns for empty or over-20,000-character text,
  allocates its own run ID, and dispatches the agent command without returning
  that ID to the sending node. The enclosing `node.event` method responds
  after the handler schedules that dispatch, so its acknowledgement is not
  agent acceptance or a run receipt. Its default session is the main session. This
  makes it a poor primary ingress for a voice request that needs an immediate
  acceptance boundary and exact run correlation. This is a source-level
  assessment, not an executed rejection test.
- `chat.subscribe` canonicalizes and registers a node's session subscription,
  but its event handler also returns no subscription proof. The registry can
  decline registration when the connection or pairing generation is stale;
  `node.event` can still return its normal acknowledgement. A node-only ingress
  candidate therefore needs a fixture for subscription, missed terminal
  events, reconnect, and unique session correlation before it can be accepted.
- The operator-side `GatewayChatClient.sendChat` calls `chat.send` with a caller
  idempotency key and receives a run ID and status. The server's normal start
  acknowledgement has `status: "started"`; it precedes agent dispatch and is
  not the final reply. `chat` events carry the run ID and session key, and
  terminate with `final`, `error`, or `aborted`. A `final` event may have no
  speakable text or may be marked yielded. The working candidate is operator
  `chat.send` for accepted speech, with exact run/session matching and a
  positive, non-yielded text final before TTS. Verify this in an isolated
  native fixture before selecting it as the binding.
- The Gateway's `node.invoke` awaits a matching node result. A node result with
  `ok: false` becomes a failed Gateway response with node error details;
  `ok: true` becomes a successful response. The built-in `nodes notify` tool
  then reduces the successful invocation to `{ok: true}`. Captain selected
  node `ok: true` only after matching `PlaybackFinished` and manager cleanup
  restore trusted readiness. An earlier acknowledgement would establish only
  acceptance. Timeout or disconnection after dispatch may still be uncertain,
  even if the Gateway reports an error.
- The built-in `nodes notify` tool does not pass an invocation `timeoutMs`.
  The installed node registry defaults an omitted timeout to 30 seconds; the
  installed CLI explicitly defaults `--invoke-timeout` to 15 seconds.
  The dispatched `node.invoke.request` includes the remaining `timeoutMs`.
  The installed registry sends a `node.invoke.cancel` on timeout only for
  progress-handled invocations or worker nodes, so the ordinary notify path
  cannot rely on receiving a cancel event when its caller times out.
  Speech synthesis plus playback and cleanup could exceed that deadline, so
  success-after-playback and the native tool timeout must be reconciled with
  an explicit accepted-duration bound or another supported invocation path.
  A tool timeout after dispatch cannot establish that speech did not play;
  it is an uncertain result and must not trigger automatic replay. The installed
  Gateway acknowledges a late `node.invoke.result` as ignored after its pending
  invocation has gone away, so that late result does not repair the original
  caller's timeout.
- Captain selected rejection before playback on 2026-10-10 when the speech
  cannot safely finish within the native invoke window. Preflight must use the
  request's remaining `timeoutMs`, reserve time for cleanup and the result
  round trip, and reject before sending any `PlaybackBegin` when the bound
  cannot be met. Exact size/duration limits and media implementation are open.
- Captain selected speaking title followed by body, with a short pause between
  them, on 2026-10-11. A title-only or body-only notification speaks its one
  available field. Boundaries apply to the resulting combined audio.
- Captain selected explicit before-playback rejection for unsupported
  notification hints on 2026-10-11. Omitted delivery or `delivery=system`
  remains supported. Sound, priority, overlay, and auto delivery are not
  implemented by the spoken satellite and must return a clear error.
- The installed role policy reserves node methods for the node role and chat
  methods for the operator role. A voice service using this candidate needs
  separately authorized node and operator connections; reusing one connection
  for both is not supported by the observed role policy. Exact scopes, pairing,
  credential delivery, and least-privilege configuration remain unverified.
- `chat.abort` is an operator method under that policy. Choosing node-originated
  input would not give the node connection a native abort operation. The
  selected remote-cancellation behavior would require an operator connection
  as well, or a revised settlement contract approved before implementation.
- The operator client can send `chat.abort` for an exact run and session. The
  inspected Gateway abort response reports whether run IDs were aborted, but
  does not expose the earlier SDK's `drained` proof. An abort response alone
  cannot satisfy Captain's selected settled-remote contract or establish that
  external actions stopped. The Python adapter must preserve an uncertain
  result and exclude that session until a stronger settlement signal is proven.

Source locations in the copied installed distribution are
`server-node-events-BhuJchmT.mjs` (`voice.transcript`),
`gateway-chat-COLZ1NLZ.mjs` (`sendChat`/`abortChat`),
`chat-send-handler-EwrCzZoZ.mjs` (start acknowledgement),
`chat-abort-handler-SgAsKFdy.mjs` (abort response),
`server-chat-Du3bvN_g.mjs` (terminal chat events),
`nodes-Cg1cpmc9.mjs` and `nodes.helpers-D-xrp4Rq.mjs` (invoke result),
`node-registry-private-BuH6xC9e.mjs` (default timeout),
`openclaw-tools-BwWTN0hs.mjs` (`notify`), and
`role-policy-C4wSnCoS.mjs` plus `nodes-Cg1cpmc9.mjs` (role separation and
node event acknowledgements). These observations do not
establish the loaded live Gateway version or native end-to-end behavior.

Current upstream [Gateway client guidance](https://docs.openclaw.ai/gateway/clients)
documents published JavaScript `@openclaw/gateway-client` and
`@openclaw/gateway-protocol` packages, currently recommending exact `2026.8.1`
pins and warning that wire protocol v4 does not by itself prove compatibility
with every Gateway release. It also documents challenge-bound Ed25519 device
identity and operator pairing. The [scope guide](https://docs.openclaw.ai/gateway/operator-scopes)
places `chat.send` under `operator.write` and reserves node-originated methods
for the node role. These mutable docs support the source findings but are not
proof that those package versions work with this installed `2026.9.8` Gateway.
Captain selected a Python Gateway client in the voice service on 2026-10-10.
`aiohttp` is a direct requirement and is installed in the project `.venv` as
`3.13.5`; `cryptography` is installed there as `46.0.7` but is not a direct
project requirement. The implementation must declare a direct Ed25519
dependency and use the verified installed protocol contract. The Python client
avoids a JavaScript sidecar and another local process boundary, but the
challenge-bound handshake, pairing, scopes, framing, and reconnect logic need
native fixture and live proof. This is a design selection, not a verified
Gateway connection.

Captain selected operator `chat.send` for accepted spoken requests on
2026-10-10. The selection was based on its explicit run ID and terminal chat
events. Captain's 2026-10-11 server-node clarification reopens whether spoken
input should use the single node's `voice.transcript` event or a separate
server-owned operator connection; the isolated `chat.send` adapter remains a
tested candidate. Captain
then selected one history per configured room, shared by speakers in that room,
because recognition is unreliable. Recognized names are included only as
tentative hints; unknown remains unknown. The isolated native schema fixture
now passes, while live execution remains unproved.

### Consequences and remaining design work

- Captain excluded Talk and said "proceed" on 2026-10-10. Implementation is
  authorized; the earlier operator `chat.send` choice must be reconciled with
  Captain's clarification of the server-node boundary before runtime wiring.
  History is scoped by configured room, independent of speaker recognition.
  Each text request explicitly carries the room and tentative or unknown
  speaker context. No running-service change or device pairing is implied.
- Ordinary replies can propose a fixed `END_CONVERSATION` after successful
  native response/run completion, without an LLM finish/listen producer.
  Distinguish streamed text, final response, failure, and remote settlement.
- Captain corrected the earlier per-satellite node selection on 2026-10-11.
  One server-owned OpenClaw node abstracts all satellites. The installed
  `nodes notify` action selects that node and sends only title/body plus
  sound/priority/delivery hints; it has no satellite selector inside the
  command. The voice service must define a separate internal satellite
  destination policy before notification runtime wiring. The ESPHome hardware
  does not speak the OpenClaw protocol or own Gateway credentials. Verify the
  durable service node identity, pairing, and command declaration in the native
  fixture and live deployment. A notification must not play on an unintended
  satellite, including after reconnect or when the intended target is unavailable.
- Captain selected the existing Gateway's native WebSocket transport on
  2026-10-09 and retired the earlier TCP/Unix dual-backend requirement for
  T-011. This removes the custom local link from the current design. It does
  not change the Gateway's existing bind/auth configuration.
- Speak-only notifications still need a sanctioned local output path. Current
  `CP-CREATION-001` requires initial user input and the normative mapping
  confines rendering to an active Conversation. Define minimal admission and
  output ownership; do not fabricate input or bypass manager event ownership.
- Native pairing/auth, receipt/error semantics, finite bounds, speaker/room
  carriage, session scope, restart handling, remote cancellation/settlement,
  and notification arbitration remain open. A Gateway abort receipt alone is
  not proof all external actions stopped.

### Python Gateway foundation checkpoint (2026-10-10)

After Captain excluded Talk and said "proceed", the shared
[`openclaw_voice.gateway`](../../openclaw_voice/README.md) foundation was added.
This part preceded the choice between `chat.send` and `voice.transcript`;
neither spoken-input binding was wired to the media runtime at this checkpoint.
It implements durable private Ed25519 identities, protocol-4 challenge signing,
explicit version/role/scope checks, finite frame/request/event limits, liveness,
and one connection generation per client. Requests are never automatically
replayed. Task cancellation after dispatch preserves Python cancellation while
exposing the request ID and explicit possible remote acceptance.

The direct dependency is `cryptography==46.0.7` in a separate subproject
requirements file. The existing environment's `aiohttp==3.13.5` is reused.
Cryptography's public Ed25519 signing and serialization API was checked against
its [official documentation](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/).
No cryptographic primitives are implemented locally.

Validation: `OPENCLAW_TEST_DIST=/tmp/t011-installed-full/dist .venv/bin/python
-m pytest -q tests/test_openclaw_gateway.py` passed **32 tests**. The suite uses
ephemeral fake Gateway sockets, plus a local Node probe against the copied
installed distribution's actual connect/request schemas and signature verifier.
The committed deterministic public test vector records the V3 payload builder's
SHA-256. The initial sandbox run could not bind loopback sockets; the unchanged
suite passed when loopback access was permitted. No production endpoint was
contacted. `git diff --check` passed.

Independent review found that aiohttp's default WebSocket redirect behavior
could redirect a credential-bearing connect frame. The client now rejects
redirects before following them, with a regression test. A fresh independent
re-review of the foundation, tests, probes, and documentation returned
**No findings.**

These results prove the tested schema/signature interoperability and the
foundation's isolated lifecycle behavior. They do not prove live pairing/auth,
native chat execution, notification playback, token persistence, reconnect
supervision, or hardware acceptance. The main Piotr runtime and normative
media/Conversation protocols remain unchanged.

### Isolated operator chat checkpoint (2026-10-10)

After Captain selected `chat.send` and room-scoped history, the separate
[`GatewayChat`](../../openclaw_voice/gateway/chat.py) adapter and room helpers
were added. A configured agent ID and room produce a stable agent-qualified
session key. Every request carries the room and a tentative recognized speaker,
or an unknown speaker, in bounded text. It submits once with an idempotency key
and `deliver: false`, accepts only a matching non-yielded text final, and never
speaks deltas or stale events. Timeouts, lost results, mismatched room events,
adapter close, and cancellation trigger a bounded exact-run abort attempt and
quarantine the room route. Close revokes queued replies and joins active
exchanges, including across concurrent close callers; quarantined routes retain
capacity. Malformed matching event states fail safely. Proven local pre-dispatch
rejections do not quarantine a room. Abort acknowledgement does not clear
uncertainty. A future
supervisor must preserve quarantined routes across reconnect and restart.

`OPENCLAW_TEST_DIST=/tmp/t011-installed-full/dist .venv/bin/python -m pytest -q
tests/test_openclaw_gateway.py` passed **54 tests** in the permitted loopback
namespace. Fake-Gateway cases cover terminal-before-ack ordering, concurrent
same-room exclusion, failure, timeout before and after start acknowledgement,
late-event isolation, cancellation, close races and concurrent close callers,
retained quarantine capacity,
local pre-dispatch rejection, and malformed event state. A local Node probe checks synthetic
send/abort/final shapes against the copied installed `2026.9.8` validators and
event schema, with the source bundle hash pinned. `git diff --check` passed.
The same installed-schema probe validates synthetic exact-run `agent.wait`
parameters; authorization and settlement behavior remain a live gate.
An isolated Node probe checks synthetic `system.notify`
invocation, completed receipt, and before-playback rejection shapes against
the installed `2026.9.8` schemas and request builder. A bounded Python frame
codec validates node identity, command, remaining timeout, JSON/text limits,
and rejects unsupported hints without touching media. A separate audio
preflight now synthesizes title and body before playback, bounds buffered PCM,
measures duration including the pause, and checks cleanup and receipt time
against the remaining deadline. TTS cleanup has its own finite join bound;
an unjoinable TTS worker requires process-fatal containment. The combined
82-test focused suite passes. An isolated node invocation adapter sends the
completed, before-playback rejection, or uncertain result after an abstract
presenter settles, bounded by the native deadline and an exact non-ignored
Gateway receipt; manager reservation,
playback/re-arm, node event-loop supervision, and the live
notification path remain untested.

This is isolated contract evidence only. No authenticated Gateway chat run,
real-model reply, device notification, or satellite playback has occurred.
The adapter is not yet connected to Piotr's Conversation and microphone runtime;
remote cancellation semantics and durable quarantine still need the planned
contract work.

### Minimal speak-only notification binding candidate

The current Conversation bridge requires accepted non-empty user text
(`CP-CREATION-001`), while the voice mapping permits assistant rendering only
inside an active `InputConversation`. A notification therefore needs a separate
manager-owned, speak-only operation above the bridge. It must not create a fake
user message or let a node connection call a driver directly. This is a design
candidate for approval, not a change to the normative protocols.

1. The per-satellite media owner arbitrates notification admission with its
   ongoing acceptance, capture/STT, Conversation, and playback work. It accepts
   at most one bounded notification when there is no accepted or in-progress
   utterance, active Conversation, or other notification. Captain selected
   pausing an otherwise idle wake-word or open-mic listening generation on
   2026-10-11. The same owner stops and joins it, including any open-mic STT
   task, before output and re-arms the same mode after cleanup. Both modes
   need race proof. If speech starts before notification reservation, report
   busy.
   The Box3 driver currently drains queued capture events for that listening
   generation before it emits `ListeningStopped`. The manager must still join
   its competing event receive task before committing `StopListening`: if that
   task has already consumed `SpeechStarted`, speech owns the boundary and the
   notification returns busy.
2. After reservation, stop the current listening generation and await its exact
   `ListeningStopped` acknowledgement. Join or contain every owned capture/STT
   task before starting output. Keep `PROCESSING` visible during synthesis and
   playback. Use the existing manager/driver `Playback*` path and await the
   matching `PlaybackFinished`; stale events cannot commit a newer operation.
3. Restore trusted readiness only after output drain and cleanup. Do not open
   follow-up listening. If the device fails or shutdown begins, finish or
   contain committed output according to the existing playback contract, then
   recover the boundary. Return a typed before-playback rejection or an
   after-commit failure/uncertain result; never claim a spoken notification was
   unheard simply because an acknowledgement was lost.
4. The node's `system.notify` success result is sent only after matching
   `PlaybackFinished` and manager cleanup restore trusted readiness, as Captain
   selected on 2026-10-10. The built-in
   OpenClaw nodes tool hides successful result details, so diagnostics must
   retain a local operation ID and log the separate admission, playback, and
   re-arm outcomes. The caller must not auto-replay an uncertain notification.

This would add a speak-only admission/presentation state and conformance cases
to the Microphone-Conversation Mapping. Current `MicrophoneManager` and
`VoiceInputSession` have no external notification entry point; the normal
acceptance task already owns listening and event consumption. A second task
calling `_speak_tts_text` would violate that ownership. The abstract
Microphone Protocol already has the needed stop/playback commands and visual
states, subject to the exact race and lifecycle proof. The core Conversation
creation rule can remain intact if the new operation stays outside the bridge.
The remote Agent cancellation guarantee still needs a separate design for the
native `chat.send` adapter.

Independent review of the native-direction documentation update returned
`No findings.` on 2026-10-09. Whitespace and local Markdown file/heading links
passed. No native fixture, live connection, or runtime test ran for this update.

## Verified evidence and limitations

The repository was clean at investigation start. The agent account is `maciek`;
`getent passwd ryszard` identifies UID 1001 and home `/home/ryszard`.
Listing that home returns `Permission denied`. `command -v openclaw` finds no
executable in the agent environment. Captain subsequently supplied the
operator-run CLI result: `/home/ryszard/.npm-global/bin/openclaw`, version
`2026.9.8 (fc23bc8)`. Captain also identified a system-level systemd service;
the earlier user-service query failed because its user bus environment was
absent, not because the gateway was absent.

Read-only host `systemctl show` confirmed:

- unit `openclaw-gateway.service`, loaded, active, running as `ryszard`;
- unit file `/etc/systemd/system/openclaw-gateway.service`, no drop-ins;
- main PID 43004 at inspection, started 2026-10-05 at 20:42:39 Europe/Warsaw;
- filtered unit inspection found the same CLI executable, the `gateway`
  subcommand, and explicit `--port 18789`; restart policy is `always`.

The sandbox could not connect to the system bus; the read-only host query
succeeded without sudo. No service restart was needed. The service command
selects the reported installation, but the version loaded by the process at its
October 5 start has not been independently attested against the current CLI.

Only the selected non-secret configuration values supplied by Captain below have
been inspected. No credential values, complete private OpenClaw configuration,
or service logs have been read. No authenticated Gateway request, installation,
service restart, or satellite operation has been performed. The Piotr runtime
and normative documents are unchanged; the separate Gateway foundation added
on 2026-10-10 is described above.

The first operator check requested is:

```bash
sudo -iu ryszard bash -lc 'command -v openclaw; openclaw --version; systemctl --user list-units --all --no-pager "*openclaw*"; systemctl --user list-unit-files --no-pager "*openclaw*"'
```

This reports the executable, CLI version, and matching user-service names,
without requesting config contents or service environment values. A missing
executable or unavailable user bus is inconclusive; subsequent inspection must
follow the actual installation and startup method. The CLI version alone also
does not prove the running gateway uses that same installation. Root `AGENTS.md`
requires Captain to run any commands needing sudo manually.

Captain completed the second operator check, requesting only the resolved
executable and non-secret bind/authentication modes:

```bash
sudo -iu ryszard bash -lc 'readlink -f /home/ryszard/.npm-global/bin/openclaw; openclaw config get gateway.bind; openclaw config get gateway.auth.mode'
```

Its results are:

- resolved executable:
  `/home/ryszard/.npm-global/lib/node_modules/openclaw/openclaw.mjs`;
- configured `gateway.bind`: `lan`;
- configured `gateway.auth.mode`: `token`.

A subsequent read-only host `ss -ltn 'sport = :18789'` check observed
`0.0.0.0:18789` listening, consistent with the configured LAN bind. This is
listener evidence, not an authenticated handshake or independent attestation of
the running process's auth mode. The wildcard IPv4 bind provides a local
loopback address for a future Gateway connection; an authenticated connection
would still require securely provisioned credentials. No token, password,
complete environment, or complete config dump is needed in chat.

The existing Gateway endpoint and the proposed Python-to-channel-plugin link
are distinct boundaries. These settings do not automatically select the new
link's framing, authentication, service account, or lifecycle. No change to the
existing Gateway bind/auth configuration is proposed by this investigation.

## Version-matched SDK source findings

### Installed package evidence

Captain supplied `/tmp/t011-openclaw-sdk.tar` on 2026-10-09. Its SHA-256 is
`77d13e7b423901ace0ca934a1f2306845e9337b22a9abcd425a25e682f5365b1`.
Archive paths and entry types were checked before copying regular package files
to `/tmp/t011-installed-sdk/`; no archived code was executed. The archive has
512 entries, with 1,768,451 bytes of regular-file content, and contains the
installed manifest plus 510 SDK files.

The installed manifest reports `openclaw` version `2026.9.8` and 354 export
entries. The five proposed public entrypoints (`channel-core`,
`channel-inbound`, `channel-outbound`, `plugin-entry`, `reply-runtime`) are
present with both declaration and JavaScript targets. The installed inbound
facade exposes the ingress helpers and typed terminal-outcome reader identified
in the upstream investigation.

The first archive was too narrow: its facades import generated bundles in the
parent `dist/` directory. Captain subsequently supplied the complete
`/tmp/t011-openclaw-dist.tar`, SHA-256
`0d4c86574b3dd613e377b1e89153d5d03b87b5f5ce9a81f38340deff8bc0cb7e`.
The complete archive contains 11,521 entries and 355,527,473 regular-file bytes.
Path/type checks admitted only `package.json` and regular files/directories
under `dist/`, copied to `/tmp/t011-installed-full/`. Static traversal from the
five SDK roots found 2,312 bundled modules and no missing relative imports.
No configuration, credentials, or user/session data were requested.

The installed manifest's supported Node range is `>=24.16.0 <25 || >=26.1.0`.
The agent's default `node` is `v22.23.1`; fixtures used the existing compatible
`/usr/bin/node`, version `v24.21.0`. The archive excludes external dependencies.
For isolated checks, 25 directly needed public npm dependencies were pinned to
the installed manifest versions and installed under `/tmp/t011-sdk-dependencies`
with lifecycle scripts disabled. This added 199 packages; a symlink connects
only the extracted copy to those dependencies. Transitive resolution is a fresh
fixture dependency tree, not an attestation of the service's dependency tree.
The fixture lockfile SHA-256 is
`7645be67cb1633bfdd7fa7dc95fc30ac2f69edb3c47bb2856185daff52d9e378`.
The actual installed Gateway and its dependencies were not modified.

### Upstream implementation evidence

The public upstream commit resolves to
`fc23bc864e4553c2d215e479eeec47b67a0bf943`; its
[package manifest](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/package.json)
reports `2026.9.8`. Public files were downloaded into
`/tmp/t011-openclaw-source/` for read-only inspection. This is source evidence,
not a running-service compatibility test. Installed-bundle checks below now
supplement that source inspection. Mutable latest documentation is no longer
the basis for selecting exact SDK names.

| Boundary | Verified upstream surface at the reported commit | Consequence for the voice adapter |
|---|---|---|
| SDK exports | Manifest exports `openclaw/plugin-sdk/channel-core`, `channel-inbound`, `channel-outbound`, `plugin-entry`, and `reply-runtime` | Use public exports; do not import internal source paths into the future plugin |
| Ingress | `channel-inbound` exports `buildChannelInboundEventContext`, `runChannelInboundEvent`, and `dispatchChannelInboundReply`; injected runtime also exposes inbound helpers | Accepted speech can become a channel event; admission and exact acceptance commit still need a fixture |
| Account lifecycle | `ChannelGatewayAdapter.startAccount` / `stopAccount`; startup context includes `abortSignal` and optional `channelRuntime` | Tie the local connection to plugin account lifetime; handle absent injected runtime explicitly |
| Outbound | Message adapter text/payload send contexts contain destination, account, optional thread/reply identity, and cancellation signal | A send surface exists independently of a preceding voice utterance; proving initiated speech still requires an end-to-end fixture |
| Reply intent | `ReplyPayload` has text and extensible `channelData`; inspected fields contain no voice `END_CONVERSATION` / `REQUEST_FOLLOW_UP` enum | Add and validate an explicit intent producer/contract; a field capable of carrying metadata does not ensure the model/runtime produces it |
| Run outcome/progress | `GetReplyOptions` includes `onAgentRunStart`, `onAgentRunTerminalOutcome`, progress callbacks, and `abortSignal`; public `readAgentRunTerminalOutcome` reads `completed` / `failed` | Run completion classifies execution, not whether the user should be invited to reply; convert progress before opening the single assistant stream |
| Delivery settlement | Dispatcher receipts distinguish delivered, cancelled, failed-before-send, failed-after-send, and pending delivery | Do not turn queued counts or text acceptance into a playback-completed result |

Source references:

- [Inbound exports](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/src/plugin-sdk/channel-inbound.ts),
  [account lifecycle types](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/src/channels/plugins/types.adapters.ts),
  and [message adapter types](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/src/channels/message/types.ts).
- [Reply payload](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/src/shared/reply-payload.types.ts),
  [reply options](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/src/auto-reply/get-reply-options.types.ts),
  and [dispatcher receipt types](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/src/auto-reply/reply/reply-dispatcher.types.ts).

Two source-level constraints were reproduced with the installed bundles:

1. [Routing](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/src/routing/resolve-route.ts)
   falls back to DM scope `main`. A fresh peer ID alone must not be assumed to
   isolate history. Select a per-conversation scope explicitly through supported
   routing. The fixture below verifies matching dispatch and record targets;
   real transcript persistence remains a deployment/integration check.
2. [Dispatcher implementation](https://github.com/openclaw/openclaw/blob/fc23bc864e4553c2d215e479eeec47b67a0bf943/src/auto-reply/reply/reply-dispatcher.ts)
   queues delivery and `waitForReplyDispatcherIdle(dispatcher, abortSignal)` can
   return when abort wins, before idle. Neither asynchronous send callbacks nor
   this abort-aware wait prove bounded production or quiescent cancellation.
   The adapter must bound upstream admission/output and separately join owned
   local run/delivery work before reporting local closure, and prove remote
   settlement before reporting remote cancellation as settled. A deliberately delayed
   delivery reproduced the premature abort-aware return in the fixture below.

The SDK also has structured question metadata (`channelData.askUser`), but that
has its own question-resolution lifecycle. It has not been established as a
compatible producer of ordinary Conversation `TurnDisposition`, and must not be
silently equated with one.

### Executed installed-SDK fixtures

On 2026-10-09 the following five checks passed using the copied installed
bundles. Model execution and session recording were stubbed where stated;
these are boundary probes, not a plugin, transport, live-Gateway, model-behavior,
or satellite acceptance test.

| Check | Observed result |
|---|---|
| Session-key construction | Default direct-message scope merged different peer IDs. Explicit `per-account-channel-peer` isolated peers and accounts and reused the same key for follow-ups |
| Typed terminal status | The public reader recognized recorded `failed`; JSON serialization dropped its symbol metadata. Ordinary `{outcome: "failed"}` was not recognized. Convert the typed result into the transport's explicit failure before serializing |
| Delayed delivery and abort | Two final replies were synchronously admitted while delivery was blocked. The abort-aware idle helper returned with zero deliveries complete; the direct dispatcher join remained pending until release, then reported both deliveries. This demonstrates neither producer backpressure nor physical playback completion |
| Abort and drain | The installed run registry/helper returned `aborted=true, drained=true` for a cooperative stub, and `aborted=true, drained=false` for an uncooperative stub after a 30 ms fixture deadline. `forceClear=false` in both. Sending abort is not proof of settlement |
| Channel ingress and reply | Public context builder, `dispatchChannelInboundReply`, and buffered reply dispatcher ran with a fake reply resolver, recording callback, and delivery callback. Two inputs in one Conversation reused the record/dispatch key; a different Conversation used a different key. All three explicit metadata-bearing replies reached delivery. This proves metadata carriage, not a real model's production of disposition |

Scratch scripts are `/tmp/t011-installed-fixture.mjs` (SHA-256
`2d3c449837949acffb4569fe3cdc09d20679bc44e338458992c958e5e43a4c9f`)
and `/tmp/t011-ingress-fixture.mjs` (SHA-256
`b3e5a2d7b48337d0fdfdb2e5ef83d7e294074e56e72b08698cf7b0aed35d81fb`).
They are temporary investigation artifacts, not maintained conformance tests.
Both ran with `OPENCLAW_STATE_DIR=/tmp/t011-fixture-state` and
`OPENCLAW_CONFIG_PATH=/tmp/t011-fixture-state/config.json`, using
`/usr/bin/node`. The final ingress fixture disables bundled plugins in its
explicit test config. It performs no real model request. Internal generated
imports in the cancellation/metadata probes are version-pinned inspection
hooks; the eventual plugin must use public SDK exports.

Initial fixture setup failures were corrected: missing archive dependencies,
then an incorrect assumption that channel dispatch counts were top-level
(they are under `dispatchResult`). An initial ingress run also attempted bundled
plugin discovery and reported a missing optional speech dependency; explicit
plugin disabling removed that fixture side effect. An additional Node
permission-mode attempt stopped at the SDK's worker-thread permission check;
it is not counted as a passing restricted-runtime test. No existing service
was contacted or restarted.

### Earlier plugin API mapping and blockers (superseded direction)

- Channel registration uses public `defineChannelPluginEntry` and
  `createChatChannelPlugin` from `channel-core`, not a presumed generic plugin
  entry contract. Its `registerFull` callback can register dedicated tools in
  the same plugin and runs in both `full` and `tool-discovery` modes. Keep
  registration free of transport/media side effects; account start/stop owns
  the transport connection.
- Captain selected dedicated voice tools inside the channel plugin. The SDK's
  shared-message-tool schema extension remains an inspected alternative, not
  the selected intent producer. Verify the dedicated tool-to-wire path before
  closing stage 1; neither ordinary reply text nor permissive `channelData`
  establishes explicit finish/listen intent.
- Public `OpenClawPluginApi.registerTool` accepts contextual tool factories.
  Their trusted context includes optional `sessionKey`, `sessionId`, and
  plugin-owned `toolBindings`; model-supplied fields must not select the
  initiating session. The version-2 factory contract requires
  `assertInvocationCurrent`. Its installed implementation checks plugin
  lifecycle and admitted invocation/continuation authority; this does not
  replace the plugin's Conversation, turn, generation, or quarantine checks.
- Public `agent-harness` exports `abortAndDrainAgentHarnessRun`. Its result
  distinguishes `aborted`, `drained`, and `forceCleared`. Registry removal via
  force-clear is not a join. The adapter must also await its dispatch/delivery
  work; this helper alone does not prove all tools or delegated work stopped.
- **Cancellation policy selected:** Captain chose option 3: close voice locally
  and explicitly report uncertain remote cancellation when OpenClaw does not
  settle. Keep the shared Gateway running. The proposed contract below replaces
  the earlier requirement to contain remote work by terminating its host;
  local cleanup remains mandatory. This is a design decision for the coordinated
  protocol cutover, not permission to change a running service now.
- Proactive opening submission must not await the spoken answer inside the
  originating model turn: that turn may own the same OpenClaw session needed
  to process the answer. Admission, playback, and reply are separate results.
  Specify a bounded session-ownership handoff before the follow-up is presented;
  do not fix the deadlock by silently queueing another SDK turn.

## Earlier plugin and dedicated voice tools (superseded)

Captain selected option 1 on 2026-10-09: one OpenClaw plugin packages the
incoming voice channel and dedicated voice tools. The Python service continues
to own speech/media and satellites. This selects the interface direction;
names, detailed schemas, and lifecycle bindings below remain design proposals.
At that point runtime implementation required separate stages 2/3 approval;
Captain's later "proceed" authorized the current native subproject instead.

Assumptions retained: the shared Gateway stays running; Piotr and the voice
service do not run concurrently; OpenClaw owns reasoning/actions; proactive
Conversations retain unknown identity throughout.

| Proposed tool | Model-supplied fields | Trusted binding and result |
|---|---|---|
| `voice_reply` | Non-whitespace `text`, required `intent` of `finish` or `listen` | Bound to the active voice Conversation and turn by the plugin; no model-selected destination, session, Conversation, or turn. Admission is distinct from playback completion |
| `voice_start` | Explicit `destination`, non-whitespace opening `text`, required `intent` of `finish` or `listen` | Initiating session comes from trusted runtime context. Return bounded admission/rejection with an operation reference; do not wait for the spoken answer inside the originating turn |
| `voice_destinations` | No routing or identity override | Return bounded satellite names, rooms, and availability; inventory is advisory and start admission rechecks availability |

Both mutating tools require explicit intent with no default. Reject missing or
invalid fields and unknown fields. Ordinary final prose is not a second speech
path and cannot supply or override disposition. Missing required trusted context,
revoked authority, an unresolved route, or an unavailable voice connection must
produce an explicit failure/rejection before any new voice side effect.
An acknowledgement lost after submission remains uncertain, not rejected.

The proposed active-reply path reserves at most one bounded reply per voice
turn. Concurrent/duplicate calls cannot append, replace, or replay it. A tool
receipt must describe only what committed; it cannot claim that the model run
ended or the satellite finished playback. Before finalizing the contract,
verify whether reply delivery should be staged until successful run completion
and how ownership passes from the expiring tool invocation to the admitted
plugin operation. Recheck invocation authority at submission and plugin-owned
operation authority after waits before each voice side effect. Never retain an
expired tool callback as the authority for later playback.

Installed-source check on 2026-10-09: the copied distribution's
`agent-harness-runtime-y6jiaaqJ.d.ts` declares contextual `registerTool` and
version-2 factories; `tools-CL6qlaud.mjs` supplies and checks
`assertInvocationCurrent`, including around asynchronous callbacks;
`core-wag68zLm.mjs` exposes the channel entry's `registerFull` callback and its
registration-mode branches. These are
source-inspection findings, not executed tool-path or real-model evidence.
Use public exports in the eventual plugin; generated bundle names are evidence
pointers only.

Stage-one verification must cover actual plugin registration and policy-based
tool availability; trusted binding propagation; valid/missing/invalid intent;
duplicate or parallel replies; plain final text without a tool reply; tool
admission followed by run failure/cancellation; bounded text and output;
stale callbacks after cancellation/reload; and proactive session handoff before
reply presentation. A successful voice turn still maps to at most one assistant
stream and exactly one disposition. Real-model use, both transport backends,
playback receipts, and authoritative settlement/recovery remain open.

Independent review of this tool-selection update, including the final
registration-mode clarification, returned `No findings.` on 2026-10-09.
Whitespace and local Markdown file-link checks passed. No new executable
fixtures, real-model tests, or runtime tests ran for this documentation update.

## Retained remote cancellation policy and earlier binding proposal

The policy remains current. References to a plugin-owned registry or the custom
wire protocol below describe the earlier binding and must be adapted and
verified for native node/chat integration; they do not require a new plugin.

Captain selected option 3 on 2026-10-09. OpenClaw owns external reasoning and
actions; the voice service owns its adapter tasks, media, and satellite
interaction. An ended voice Conversation does not prove that remote actions
stopped. This applies to user-initiated and server-initiated Conversations over
both transport backends.

Assumptions: retain the shared Gateway; no host termination on a remote timeout;
do not resume or replay cancelled voice work. OpenClaw may still complete an
already-started action, and cancellation does not undo committed effects.

### Typed result and ownership proposal

Extend the abstract Agent contract with an explicitly declared remote-lifetime
capability, rather than checking a concrete Agent class in the bridge. Strict
local Agents retain their existing acknowledged-cancellation guarantee. Only
the explicit remote binding may return an uncertain remote result.

Proposed `AgentCancellationResult` alternatives are:

- `AgentCancellationAcknowledged`: local resources are closed and the scoped
  remote work, if any, is proven settled. It does not mean past effects were
  rolled back.
- `AgentCancellationUncertain`: all locally owned adapter work is joined and
  its input/output authority revoked, but remote settlement is unproven. Carry
  a closed reason enum (`deadline`, `connection_lost`, or `remote_status_unknown`)
  and an opaque operation reference. Do not pass an OpenClaw session key into
  generic bridge policy.

The bridge separately completes/aborts and joins the input sink, capture, and
presenter work before ending the Conversation or exposing readiness. Add the
remote status to the terminal result independently of the Conversation end
reason and the existing acceptance/playback facts. Neither an uncertain action
outcome nor uncertain playback can be represented by the other field.

| Situation | Proposed result and next state |
|---|---|
| No remote dispatch occurred, with local proof | Acknowledged after local cleanup; no remote work exists |
| Cancellation settles remote work within its budget | Acknowledged after local cleanup and observed settlement |
| Remote cancellation budget expires or connection disappears | Uncertain after local cleanup; end voice and quarantine the affected remote route |
| Remote dispatch may have committed but its acknowledgement was lost | Uncertain; preserve admission uncertainty and never resend the message |
| Local tasks, capture, or playback cannot be joined/contained within the local deadline | Existing local fatal containment; do not return an uncertain result to excuse escaped local work |
| Remote work settles after voice has ended | Update the adapter's quarantine record only; no speech, follow-up, or second Conversation terminal event |

Revocation happens at cancellation start, before awaiting remote abort. Reject
late progress, output, disposition, and tool-initiated speech belonging to the
cancelled run. Checks must use trusted run/operation ownership and happen again
after asynchronous waits immediately before a voice side effect. A new wire
operation ID alone must not restore an old run's authority.

### Reuse, reconnect, and bounded tracking

Local media cleanup permits the satellite to return to readiness, independently
of whether the remote route can be used again. A fresh user-start may use a new
isolated OpenClaw session if capacity permits. Any voice request or proactive
reply that would reuse the unresolved OpenClaw session receives `busy`; never
queue it behind the uncertain run. This is a plugin voice-admission restriction,
not a claim to suspend unrelated users of the shared Gateway.

The plugin must retain bounded, session-scoped exclusion and revoked-run records
outside the ended Conversation. Uncertain records continue to consume a
configured outstanding-operation slot; do not evict them on age, reconnect, or
capacity pressure. Refuse new work at capacity. Releasing a route requires
authoritative settlement of all work owned by that operation, not merely an
abort receipt, registry removal, or absence from one lookup.

Reconnect invalidates old output authority without clearing unresolved-route
exclusion. Plugin/voice-service restart must preserve enough operation ownership
to reconcile safely, or keep voice admission closed when that cannot be proven.
The bounded recovery-record format and authoritative SDK settlement query are
still stage-one verification items. Records retain identifiers/status, not
replayable speech or user requests. A late settlement cannot authorize playback
from a retired connection generation.

The copied installed `2026.9.8` method policy advertises `agent.wait` with
`operator.write`; `<=2026.7` is its introduction marker, not a restriction to
an old protocol profile. Its handler can return a run ID, status, terminal
fields, and a session snapshot from process-local agent-job records. Those
records have a 600-second cache TTL, and an absent snapshot returns a timeout;
absence therefore cannot prove settlement after a process restart or cache
expiry. `chat.abort` replies with abort/run IDs, while `chat.history` returns
stored messages, so neither alone proves that all side effects of an uncertain
run have settled. Authenticated native tests must establish `agent.wait`
authorization, chat-run correlation, terminal meaning, and restart behavior
before it can release a quarantined room route. Until then, restart recovery
must retain or fail closed on unresolved routes.

The durable route record should be committed before `chat.send` can leave the
voice process. It needs the stable room session key, exact run/idempotency ID,
and an unresolved state, but no transcript, reply, speaker name, or credential.
If the process stops between that commit and dispatch, the room remains
conservatively unavailable on restart. A verified terminal outcome may retire
the record only after its settlement meaning is established by the native
fixture; a local timeout, abort acknowledgement, missing cache entry, elapsed
age, or Gateway reconnection cannot retire it. A malformed or unreadable record
store closes chat admission until an operator resolves it. The storage format,
exclusive ownership, atomic commit, and recovery procedure still need design
and crash tests before the voice service is wired.

Shutdown closes local admission, revokes voice authority, attempts remote abort
within a finite budget, and joins local tasks/media within the existing global
shutdown deadline. Proven local cleanup with remote uncertainty is an explicit
reported outcome, not a reason to kill the shared Gateway. Failure of local
cleanup retains the existing non-zero fatal-exit behavior. The plugin must
retain or safely recover route exclusion after either process restarts.

### Coordinated contract and test changes

The eventual cutover must update `CP-LIFETIME-001`, `CP-CANCEL-002`,
`CP-HANDOFF-001`, scoped Agent entry/exit, and `CP-SHUTDOWN-001/002` to distinguish
local ownership from externally owned work. Remote input acceptance can also
be uncertain when a handoff races disconnect; do not preserve the current
pre-acceptance no-later-effect promise for that case without proof. Local Agents
retain strict behavior. Audit existing bindings for the new result union;
do not silently map uncertainty to the old acknowledgement or success result.

Required conformance cases: cooperative cancellation; ignored cancellation;
disconnect before/after dispatch commit; lost acceptance/cancellation results;
late speech/tool attempts; local cleanup failure; same-route rejection and
independent-route admission; bounded tracker exhaustion; reconnect/restart
without lost exclusion; late settlement; and shutdown with remote uncertainty.
The existing uncooperative SDK probe supports this policy but does not test the
future bridge, quarantine, or recovery implementation. Normative documents and
runtime behavior remain unchanged until their approved atomic cutover.

## Earlier full Conversation initiation extension (deferred)

The full speak-and-listen extension below is deferred by the current scope.
Only the minimal speak-only notification admission/output binding remains to
be designed for this milestone; this section is not a current implementation
checklist.

On 2026-10-09 Captain explicitly selected: extend the Conversation protocol and
add server-to-speaker messages for starting a conversation from the server.
This supersedes the mapping-only proposal and the earlier requirement that the
Conversation protocol remain unchanged. Captain subsequently selected unknown
user identity for the entire lifetime of a server-started Conversation.

The investigation exposed the original conflict: current bridge creation
requires an accepted non-empty initial user message (`CP-CREATION-001` and
`CP-SESSION-001`), while the mapping confines rendering and follow-up to an
active Conversation. Its invariant 4 prohibits follow-up capture from creating
a new InputConversation. The selected design resolves this by admitting an
explicit server-originated Conversation before the opening speech. It must not
fabricate a user message or delay Conversation creation until the first answer.

### Agreed identity and ownership

- Server-started Conversation context has `user=None` throughout. No first-reply
  recognition, intended recipient, or later speaker result changes that context
  or its anonymous user-settings snapshot. Unknown input is normal accepted
  input; the new entry point must supply an appropriate context provider.
- The selected satellite supplies the immutable area. An originating OpenClaw
  session is a routing/history correlation, never a recognized speaker identity.
- User-initiated Conversations retain current initial speaker recognition.
- The manager remains the sole owner of driver events, listening/playback
  exclusion, cues, visuals, and media recovery. The bridge owns Conversation
  sequencing and uses abstract interfaces; device-specific names stay in drivers.

### Proposed start-message and lifecycle design

This is a design outline for stage 1, not a normative protocol or implemented
interface. Names below describe proposed typed operations rather than installed
OpenClaw methods. Full operation matrices and wire schemas remain to be written.

1. The server submits a start request containing destination, request ID,
   non-empty opening text, and explicit finish/listen intent. Application
   routing resolves the destination to an abstract input session; the bridge
   does not inspect concrete satellites or OpenClaw session keys.
2. One session-owned admission operation arbitrates the request against normal
   input acceptance. Unavailable, closing, active Conversation, capture/STT,
   accepted wake candidate, recovery, or another start produces a typed
   rejection. Input activity already observed when both contenders are eligible
   wins and the start receives `busy`; close wins over either. No durable queue.
3. Successful server-start admission reserves the session, stops and joins its
   ordinary listening generation, and creates a fresh scoped InputConversation
   with a typed server-originated start descriptor. The input session becomes
   active without `initial_message`. Keep user-start and server-start as explicit
   alternatives, not nullable text with implicit behavior. Start acceptance is
   distinct from audio playback completion; cancellation/close during admission
   must have an explicit commit boundary and must release partial acquisitions.
4. Resolve immutable unknown-user context and enter the Agent scope with the
   server-start descriptor through an abstract interface. The OpenClaw adapter
   binds the existing initiating interaction and emits its supplied opening
   text; it must not submit that text as user input or run reasoning again merely
   to repeat it. The generic bridge receives the normal assistant stream and
   explicit disposition from that endpoint. Unsupported Agent/input capabilities
   must reject before admission; the exact capability interface remains open.
5. Extend startup states so server-originated starts skip the initial
   `send_user_message()` and can proceed to Agent output. The normal assistant
   sink controls bounded rendering and correlated playback completion. Finish
   intent ends the Conversation after output cleanup. Listen intent presents
   the normal bounded follow-up opportunity only after playback drains, using
   the existing commit-token gate and presenter-owned timer.
6. An accepted spoken reply becomes a `UserMessage` in that same Conversation.
   No re-identification occurs. It reaches the initiating OpenClaw interaction
   through the adapter's correlation. Later turns use normal output/disposition
   sequencing. Timeout ends this already-existing Conversation with a typed
   no-reply outcome and cleans media before readiness; it creates no user input.
7. Cancel/disconnect/shutdown suppress late input/output and join or contain
   locally owned Agent/media work under explicit deadlines. Apply the selected
   remote cancellation contract above: report uncertainty and quarantine the
   unresolved route without terminating the shared Gateway. Preserve committed
   playback facts and report uncertain delivery separately. Never retry an uncertain
   start, utterance, or audio delivery automatically after reconnect.

The first implementation cutover must update the Conversation context/start
inventory, interfaces, creation/startup rules and state/source matrices, voice
mapping, affected input/Agent bindings, and conformance catalogue together.
The websocket binding must be audited for shared-interface compatibility; this
is not authorization to add unsolicited websocket JSON events incidentally.
The server-to-speaker binding must specify whether existing driver commands
suffice or an explicit driver extension is needed before claiming unchanged
firmware. No concrete device services may enter the generic bridge.

Required conformance cases include both start variants, unsupported capability,
reservation versus input, cancel/close before and after acceptance, unknown-user
context rejection/unavailability, delayed playback, finish/listen distinction,
exact reply-timeout boundary, immutable unknown identity after every reply,
empty/rejected speech, disconnect, stale results, and cleanup before re-arm.
Normative documents remain in force until the new contract is approved and
implemented in a coordinated cutover.

## Earlier plugin integration constraints (superseded binding)

Captain earlier required both loopback TCP with dedicated token authentication
and Unix-domain sockets with filesystem access control. This requirement was
retired for T-011 on 2026-10-09 in favor of native Gateway WebSocket. The
[local transport draft](T-011-local-transport-contract.md) proposes a shared
protocol for both. This supersedes the earlier question that coupled `maciek`
with TCP and `ryszard` with Unix sockets: account selection is a deployment
choice, and configured socket permissions can support different accounts.

Assumptions retained from T-011: Piotr and the voice service do not run together;
Python owns hardware/media lifetimes; OpenClaw owns reasoning and actions.
The following are design candidates, not an agreed wire schema or verified API:

- Give each accepted voice Conversation its own OpenClaw session/history scope;
  retain that scope for ordinary follow-ups. Neither the satellite name nor
  recognized speaker alone is a conversation key. Explicit proactive correlation
  binds a server-started Conversation to the initiating OpenClaw interaction
  before opening output and persists through its replies; active correlation
  loses input/output authority on timeout, cancellation, disconnect, or
  completion; unresolved-operation exclusion survives separately. Verify the
  installed SDK can express this without merging unrelated histories.
- Require structured end/follow-up intent. Map successful output to at most one
  assistant stream and exactly one `TurnDisposition`; map typed failure to
  `AgentConversationFailed`. Progress is content-free and only precedes the
  assistant stream. Never derive intent from punctuation or diagnostic text.
  Dedicated voice tools are selected as the intent producer; their complete
  installed-runtime execution and delivery path remains unverified.
- Correlate connection generation, Conversation, turn, and proactive delivery
  separately. Reject stale results after cancellation or reconnection. Separate
  input acceptance, text acceptance, playback completion, and reply outcome.
- Define positive finite payload, buffering, concurrency, handoff, execution,
  liveness, and cleanup limits in explicit configuration. Preserve the Agent's
  zero-capacity event rendezvous and the voice sink's bounded renderer. Both
  selected backends must share framing, backpressure, and lifecycle behavior;
  their endpoint authentication/access controls differ as described in the draft.
- Report local closure only after local work is quiescent. Report remote
  cancellation as settled only with proof; otherwise use the selected explicit
  uncertain result and route exclusion. An uncertain handoff or playback result
  must not trigger automatic resubmission or speech replay after disconnect.

## Stage-one completion checklist

| Required result | Current evidence / next check |
|---|---|
| Installed version and startup | Operator reports CLI `2026.9.8 (fc23bc8)`; host confirms running system service using that executable. Loaded-process version remains unverified |
| Native connection/auth | Python Gateway foundation implemented with durable per-device identity and one connection generation per client. Isolated handshake/lifecycle tests and installed connect-schema/signature checks pass. One server-owned node abstracts satellites; the earlier operator `chat.send` choice is being reconciled with node ingress. Live pairing/auth, token persistence, reconnect supervision, and account/credential ownership remain open |
| Ingress and ordinary reply | Isolated `GatewayChat` and installed send/abort/final schema probe pass. Room keys, tentative speaker text, run/room correlation, bounded final text, shutdown revocation, local pre-dispatch rejection, and uncertain-route quarantine are covered by fake-Gateway tests. Authenticated native execution, real-model reply, model/token observability, and voice-service integration remain untested |
| Native notifications | Built-in nodes tool and CLI invoke `system.notify`; title/body handling confirmed in source. Built-in tool omits invoke timeout, producing a 30-second registry default; the CLI explicitly defaults to 15 seconds. Captain selected success after matching `PlaybackFinished` and manager cleanup, and rejection before playback if the remaining deadline cannot be met. Title then body is selected; unsupported sound/priority/delivery hints reject before playback. Synthetic installed-schema, bounded Python frame/result codec, isolated result sender, and buffered audio preflight tests pass. Manager-owned playback/re-arm, node event-loop delivery, and live delivery remain untested |
| Session/history scope | Captain selected one history per configured room, shared by its speakers. Stable agent-qualified room keys are implemented and tested locally. One server-node pairing, internal notification target selection, native room-key isolation, and restart behavior remain untested |
| Disposition and cancellation | No model disposition producer required for the current no-follow-up choice. Map successful native completion to END; prove cancellation/settlement and unresolved-route exclusion through the native interface |
| Transport | Captain selected native Gateway WebSocket and retired the earlier TCP/Unix dual-backend requirement for T-011. Authenticated native connection remains untested |
| Local notification contract | Full proactive conversation/listen design deferred. Captain selected stopping idle wake-word and open-mic listening for notifications and re-arming the same mode after cleanup, with speech winning a pre-reservation race. Minimal speak-only admission/rendering and remote-lifetime changes still need design and coordinated normative approval |
| Review and acceptance | Gateway foundation, isolated chat adapter, notification schema probe, bounded frame/result codec, and audio preflight have 82 passing focused tests including installed connect/chat/agent.wait/notify schema checks. The earlier foundation re-review, the fresh fifth chat adapter review, notification codec review, fourth audio preflight review, seventh notification result review, and source/probe re-review returned `No findings.` Four chat adapter, four audio preflight, three result sender, and one source-description findings were repaired. Native chat/notification execution, service integration, deployment, and hardware acceptance remain outstanding |

Stages 2 and 3 have not started. T-004's outstanding hardware acceptance remains
separate and unchanged.
