# OpenClaw satellite voice subproject

T-011 keeps Piotr's microphone drivers, Whisper, Piper, and speaker recognition
behind their existing interfaces. Satellites connect only to this voice service;
one server-owned node represents the service to OpenClaw. Captain selected
Python and native Gateway WebSocket, one history per configured room, one
request/reply without follow-up, spoken `system.notify` presentation, and no
Talk API integration. The earlier operator `chat.send` selection is being
reconciled with Captain's server-node clarification; the implemented adapter
remains isolated until the spoken-input binding is settled. Internal satellite
selection for server-node notifications also remains open.

For `system.notify`, Captain selected success only after the matching
`PlaybackFinished` and manager cleanup restore trusted readiness. The node
cannot acknowledge success at reservation or synthesis start. A caller timeout
after dispatch remains uncertain and must not cause automatic replay.
Requests that cannot safely finish before the native invoke deadline must be
rejected before playback. The manager-owned preflight and presentation path is
not implemented yet.
Captain selected manager-owned suspension of idle wake-word or open-mic
listening for a notification, followed by re-arm of the same mode after cleanup.
Open-mic STT work must also be joined. Speech that starts before the
notification reservation wins and the notification returns busy.
The spoken content is title followed by body with a short pause, or whichever
single field is present.
Omitted delivery and `delivery=system` are accepted; requested sound, priority,
overlay, and auto delivery are rejected before playback.
`gateway.notify.decode_notify_request` checks the native node ID, command,
idempotency key, remaining timeout, JSON size, and bounded text. It preserves
title, body, and the supported system-delivery hint for a future manager-owned
presenter. These
early checks do not estimate synthesized audio duration or reserve media.
`completed_notify_result` is valid only after matching playback and cleanup;
`rejected_notify_result` is valid only after a proven before-playback rejection.
`uncertain_notify_result` reports possibly started playback separately.
`gateway.node_notify.handle_notify_invocation` now sends one bounded native
`node.invoke.result` after an abstract presenter settles. It reserves native
receipt and presenter-cleanup time, requires the installed Gateway's exact
`{ok: true}` result acknowledgement, and treats an ignored result, lost result
request, or disconnect as receipt uncertainty without replay.
A node event loop and
the actual manager presenter are still pending.

`media.prepare.prepare_notification_audio` uses the abstract TTS interface to
synthesize title and body separately before playback. It bounds PCM bytes,
measures both segments, and includes the configured pause and cleanup/receipt
margin in the native deadline check. It returns prepared audio or a typed
before-playback rejection. The manager-owned reservation, playback, re-arm,
and result send are still unimplemented.

The current implementation is the shared Gateway foundation, isolated
`chat.send` turn adapter, and bounded `system.notify` frame/result codec in `gateway/`,
plus room-context helpers in `utils/`.
There is no voice-service entry point yet. Capture/STT integration, media
notification admission, configuration loading, reconnect supervision, and
deployment remain pending as described in
[T-011](../docs/tasks/T-011-openclaw-satellite-voice-interface.md).

## Connection contract

- `GatewaySettings` requires an explicit endpoint, verified Gateway version,
  positive finite operation/liveness deadlines, and frame/request/event bounds.
  Protocol 4 is required. Version, role, and requested scopes are checked before
  readiness. Plaintext connections are restricted to loopback; remote endpoints
  require `wss` with normal certificate verification. Redirects are rejected.
- `DeviceIdentity.load_or_create(path)` atomically creates one private Ed25519
  key file or loads the existing one. It never replaces an invalid key. The
  voice service node uses one durable identity path; satellites have no Gateway
  identity. If a separate server-owned operator connection is selected, it uses
  a distinct identity path. Preserve service identity files across restarts.
- `GatewayClientSettings` carries the requested role, scopes, declarations,
  and optional explicit shared token or paired device token. The
  current implementation supports shared `auth.token` and `auth.deviceToken`;
  it does not implement the Gateway's separate `auth.bootstrapToken` mechanism.
  The V3 signature uses the Gateway challenge timestamp and nonce. Credentials,
  transcripts, and remote diagnostic text are excluded from connection logs.
- `connect()` returns `GatewayHello`, including a secret `device_token` receipt
  when the server supplies one. Durable token storage and pairing approval
  belong to the later configuration/setup owner; this foundation neither saves
  that receipt nor changes Gateway authentication or pairing.
- One client instance represents one connection generation. It never retries,
  replays, or reconnects automatically. A later supervisor must create a fresh
  client and quarantine unresolved run/session bindings before restoring
  readiness. The connection reader routes RPC replies separately from the
  bounded event queue; an event overflow or sequence discontinuity fails the
  connection and releases pending work.
- `GatewayRejected` reports a typed server rejection. A timeout or disconnect
  after dispatch produces `GatewayRequestUncertain`; cancellation after dispatch
  produces `GatewayRequestCancelled`, an `asyncio.CancelledError` carrying the
  request ID and `may_have_been_accepted=True`. Neither outcome permits replay.
  Cancelling a Python await is never evidence that the remote action stopped.

## Spoken chat turn contract

- `room_session_key(agent_id, room)` derives a stable, agent-qualified key from
  the configured room. All speakers in that room share its OpenClaw history;
  different rooms get distinct keys. Recognition never selects a history.
- `spoken_request_message(room, speaker, transcript)` sends the room and an
  optional recognized name with an explicit tentative warning. Unknown speaker
  remains unknown. The spoken transcript is JSON-quoted within the message;
  this is text context, not a separate authenticated Gateway metadata field.
- `GatewayChat` requires a connected operator client that observes `chat`
  events and advertises `chat.send` and `chat.abort`. It submits one bounded
  message with a unique idempotency key and `deliver: false`, then waits for a
  matching run and room terminal event. Only a positive, non-yielded text final
  is returned for speech. Delta, stale, and unrelated events cannot become a
  reply. Malformed matching chat states fail the turn and quarantine its route.
  A run may finish before its start acknowledgement arrives.
- A timeout, mismatched event, lost connection, adapter close, or task
  cancellation triggers a bounded exact-run `chat.abort` attempt and
  quarantines the room route. Closing revokes queued reply authority and joins
  active exchanges; concurrent close callers await the same cleanup even if
  one caller is cancelled. Quarantined routes retain their operation-capacity slots.
  Proven local pre-dispatch limits leave the room available.
  Neither abort acknowledgement nor a late reply clears that quarantine.
  `quarantined_sessions` must be carried into a new connection generation by
  the future supervisor; durable settlement and recovery are still open.
  There is no automatic retry or replay.

## Verification

Install the subproject dependency set from `requirements-openclaw-voice.txt`.
The Ed25519 dependency is explicitly pinned to the tested `cryptography==46.0.7`;
the client uses its public key serialization/signing API without implementing
cryptographic primitives. The current environment has `aiohttp==3.13.5`.

```bash
.venv/bin/python -m pytest -q tests/test_openclaw_gateway.py
```

Tests use ephemeral loopback fake Gateways. They cover separate node/operator
declarations, credential-bound signatures, response correlation, event ordering,
queue and frame limits, uncertain submission, cancellation, incompatible hello,
invalid challenge, liveness failure, redirect rejection, room keys, tentative
speaker context, native chat turn correlation, terminal-before-ack ordering,
failure, concurrency, timeout, quarantine, and exact-run abort. Socket tests
need permission to bind loopback sockets.

The committed public test vector was generated by the copied OpenClaw
`2026.9.8` V3 payload builder and Node's Ed25519 implementation; its fixture
records the source SHA-256. It contains a public test seed, not a deployment
identity. Optional probes feed Python node/operator connect envelopes into the
copied installed Gateway's actual request/connect validators and signature
verifier, and check synthetic chat send/abort/final and notification
invoke/completed/rejected shapes against the installed schemas and request
builder, without starting a Gateway or reading its configuration:

```bash
OPENCLAW_TEST_DIST=/path/to/copied/openclaw/dist \
  .venv/bin/python -m pytest -q tests/test_openclaw_gateway.py tests/test_openclaw_notification_audio.py
```

This proves schema and signature interoperability for the tested artifacts.
It does not prove live pairing/authentication, native chat dispatch, notification
delivery or playback, model or token observability, reconnect recovery, or
hardware acceptance.
