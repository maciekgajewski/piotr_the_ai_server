# T-011 local transport contract draft

- **Authority:** Historical protocol proposal; not a normative runtime contract
- **Status:** Superseded custom-link design; Captain selected native Gateway WebSocket for T-011 on 2026-10-09
- **Date:** 2026-10-09
- **Parent:** [T-011](T-011-openclaw-satellite-voice-interface.md)

Captain's later 2026-10-09 simplification selects ordinary native text exchanges
and node notifications. The Python-to-plugin link described below is no longer
the selected architecture and must not be implemented from this draft. Retain
it as evidence of earlier bounds, lifecycle concerns, and transport choices.
Captain has since retired the earlier dual-backend requirement for T-011 in
favor of native Gateway WebSocket. This historical draft does not prove native
Unix-socket support. See the
[current investigation](T-011-stage-one-investigation.md#current-direction-native-text-exchanges-and-spoken-notifications).

## Earlier requirements and proposed topology

Captain earlier required support for both loopback TCP and Unix-domain sockets. The
backend changes connection establishment and authentication only. Session
correlation, server initiation, output, acknowledgement, cancellation, errors,
and disconnect behavior use one shared contract.

The proposed topology has the Python voice service listen and the OpenClaw
channel plugin connect. A connection carries requests in both directions.
The plugin owns reconnect attempts as part of its account lifecycle; a new
connection never resumes old work automatically. The server accepts at most
one authenticated owning plugin connection per configured service instance.
Another connection receives a rejection rather than replacing the owner.

Service account selection remains a deployment decision. Both backends must
work with the Python service under either `maciek` or `ryszard`, subject to
resource and endpoint access. Unix sockets do not require identical accounts:
an explicitly configured group/directory/socket permission arrangement may
grant access across accounts. No permission change is authorized by this draft.

## Endpoint configuration

The Python entry point requires an explicit config path. CLI options override
matching config values. No deployment address, port, socket path, or credential
is embedded in runtime code.

| Backend | Required configuration | Connection/authentication behavior |
|---|---|---|
| `tcp` | Numeric loopback bind/connect address, port, dedicated token-file path on each side | Reject non-loopback addresses. Authenticate before exposing destinations or admitting operations. Use a dedicated integration token, not the existing Gateway token; never log its value or place it in process arguments |
| `unix` | Absolute socket path; explicit access policy for its parent directory and socket | Restrict filesystem access to the service and intended plugin principal(s). Fail on an unsafe/inaccessible path or unexpected existing endpoint; never unlink an arbitrary pre-existing file/socket |

Credential provisioning, socket-directory creation, ownership, and restart
policy belong to deployment. Startup validates its configured backend and fails
clearly on missing access; it must not fall back to another backend or silently
broaden permissions. Token replacement takes effect through an explicit reload
or restart policy to be settled before deployment; no rotation behavior is
implied by this draft.

The existing OpenClaw Gateway's `lan` bind, port 18789, and `token` auth remain
separate. This channel plugin runs inside OpenClaw; the new voice link is not a
second client of the Gateway WebSocket protocol and does not reuse Piotr's
external websocket conversation binding.

## Common framing and limits proposal

Use UTF-8 JSON objects separated by a newline on either reliable byte stream.
JSON string newlines are escaped. Reject invalid UTF-8, duplicate object keys,
non-object frames, unknown protocol versions/types, and oversized frames before
dispatch. Read buffers must enforce the byte limit before a newline arrives.
Partial reads and multiple frames in one read have identical behavior on both
backends. Omit optional dictionary fields whose value would be null.

Proposed version-1 hard ceilings, with positive deployment limits no larger
than these ceilings:

| Bound | Ceiling / rule |
|---|---|
| Encoded frame | 65,536 bytes including delimiter |
| Opaque correlation ID | 128 UTF-8 bytes, non-empty |
| Text in one data frame | 8,192 UTF-8 bytes; chunk only at character boundaries |
| Outstanding output per active Conversation | One unacknowledged data frame per direction |
| Pending control frames per connection | 16; overflow fails the connection rather than dropping cancellation |
| Active interactions | One per satellite; total cap required in configuration |
| Admission backlog | No waiting user/start queue; contention returns `busy` |

Handshake, write/ack, execution, liveness, and cancellation/cleanup deadlines
must be positive finite configuration values. Version 1 must match exactly.
Both peers offer their frame/text/concurrency limits and use the smaller offered
value for each. Require negotiated frame capacity of at least six times the
negotiated text-byte capacity plus 4,096 bytes for envelope/metadata; reject an
incompatible pair rather than exceeding the frame cap on escaped JSON text.
Non-text payloads must also fit the negotiated total frame limit.

Use the smaller offered write/ack, execution, and liveness timeout. Select the
smaller offered heartbeat interval and require it to be less than half the
agreed liveness timeout. Each endpoint separately enforces its local handshake
deadline before negotiation can complete. Each advertises its cancellation
budget; reject a configuration whose remote-cancellation budget, local cleanup
budget, and control-ack allowance do not fit within its bridge cancellation
deadline. Expiry of the remote budget selects an uncertain remote result;
it does not relax the local cleanup deadline.
Exact operational timeout values remain explicit deployment configuration.
A slow data
ack must not prevent processing bounded cancellation, terminal, or heartbeat
control frames. The wire credit bound does not by itself bound the SDK's output
queue: upstream output admission also requires an explicit tested bound.

TCP's first bounded handshake authenticates with the dedicated token; Unix
uses the configured filesystem access boundary. Successful handshake establishes
a fresh connection generation. All subsequent frames carry that generation,
message type, and operation correlation. Pre-auth failures reveal no satellite
inventory or conversation content. Authentication is not a new speaker policy.

## Operation families and commit points

These are proposed wire families, not new core Conversation events or names of
existing OpenClaw SDK methods. Exact closed JSON schemas remain a stage-one
completion item.

Captain selected dedicated voice tools inside the OpenClaw channel plugin.
The proposed `voice_reply` maps to assistant output/disposition, `voice_start`
to server-start, and `voice_destinations` to destination query/result. Tool
names and exact schemas remain proposals. Trusted plugin context supplies
session/turn correlation; model arguments cannot select it. A tool receipt
does not collapse wire admission, model completion, and audio completion into
one success. See the [earlier tool design](T-011-stage-one-investigation.md#earlier-plugin-and-dedicated-voice-tools-superseded).

| Family | Direction | Required meaning |
|---|---|---|
| Handshake / heartbeat | Both | Version/auth/limit agreement, generation assignment, bounded liveness |
| Destination query/result | Plugin -> Python -> plugin | Bounded inventory of names, areas, and availability; inventory is advisory and admission rechecks availability |
| User-start / user-turn | Python -> plugin | Complete accepted speech, Conversation and turn IDs, immutable context at creation; new user-start gets isolated history, follow-up retains it |
| Server-start | Plugin -> Python | Destination, request ID, opening text, explicit finish/listen intent, initiating OpenClaw correlation; create a real server-started Conversation with lifetime-unknown user |
| Admission result | Receiver -> requester | Exactly one accepted/rejected result when known; accepted includes resolved correlation; busy/unavailable/closed/unsupported/invalid are typed rejections, not successful empty replies |
| Progress / assistant output / disposition | Plugin -> Python | Content-free progress before output, at most one ordered assistant stream per turn, explicit end/follow-up disposition |
| Output acknowledgement | Python -> plugin | Credit/text acceptance distinguished from completed audio playback; completion comes only after the sink commits completion |
| Cancel / cancellation result | Both | Correlated idempotent cancellation; first reason wins. Report local closure separately from remote `settled` / `uncertain` status; a signal receipt is not settlement |
| Conversation/delivery result | Python -> plugin | Typed finish, timeout/no reply, cancellation, failure, or uncertain delivery; preserve whether acceptance/playback already committed and any remote cancellation uncertainty independently |

Server-start admission and the first spoken answer use one Conversation ID. The
opening text is not user input. A finish-only opening may end without any user
speech. For listen intent, the reply interval begins after playback drains and
listening presentation commits. Unknown identity stays fixed even if recognition
would identify a later speaker.

An accepted start is not a promise that the opening was heard. User-message
admission is not a promise that OpenClaw finished reasoning or actions. Request
and Conversation correlation must survive through final cleanup, and terminal
results must identify the operation to which those facts apply.

## Cancellation, disconnect, and replay

No retry of a mutating/voice operation is implicit in request IDs. Each direction
uses strictly consecutive positive frame sequence numbers, starting at 1 after
handshake, with a maximum of `2^53 - 1`. A duplicate, gap, or backwards sequence
fails the connection before dispatch. Start a fresh generation before sequence
exhaustion; never wrap a counter.

A new request's operation ID is derived from its origin role and initiating
frame sequence, scoped by the connection generation. A response or event echoes
the active operation ID. Validate that every new request uses its own current
sequence; reusing an old operation ID with a new sequence fails validation.
This needs only the last received sequence plus the bounded active-operation
registry, not a growing history or an expiring deduplication cache. Completed
operation IDs cannot become new requests after their registry entry is retired.
An event for an unknown/retired operation cannot create state or cause playback.

Loss of connection closes admission, revokes voice authority, requests remote
cancellation, and joins locally owned work on both peers. It invalidates the
generation. Each side independently enforces liveness so cleanup does not need
another packet. Reject late results for the old generation. A replacement
connection starts with fresh inventory and no active voice correlation; it does
not resend speech, replay user input, or resume an old Conversation. Outstanding
remote-operation exclusion survives separately from those wire correlations.

If acknowledgement was lost, retain the outcome as uncertain instead of
reclassifying it as rejected. Captain selected explicit uncertain remote
cancellation: after local cleanup, report remote status as `uncertain` if its
budget expired, the connection was lost, or settlement otherwise cannot be
proven. Do not terminate the shared Gateway for that outcome. Only proven
settlement permits `settled`; absence from the run registry or `forceCleared`
alone is insufficient. Failure to contain local work still invokes local fatal
termination under the Conversation contract.

Installed-SDK probes reproduced a run that accepted abort but did not drain
before its deadline. The [investigation](T-011-stage-one-investigation.md)
defines the selected policy and proposed typed Agent results. A wire result
cannot assert the receiver's local cleanup: each side must prove its own local
closure before releasing its resources. If no result arrives, Python constructs
the uncertain result locally after its own cleanup; it does not require a final
packet over a dead connection.

Quarantine an unresolved OpenClaw session against further voice turns or
proactive requests that would reuse it. Release the satellite only after media
cleanup; it may serve a new isolated route if capacity permits. Track unresolved
operations in a bounded registry outside the ended Conversation; they consume
capacity and cannot be evicted by timeout or reconnect. On restart, recover the
exclusion records or keep voice admission closed until their status can be
reconciled. The recovery format and authoritative settlement source remain
verification items, not assumed SDK guarantees. No record may contain work to
replay automatically. Trusted ownership checks must also reject fresh wire IDs
created by a cancelled remote run.

These are proposed changes to the remote-Agent lifecycle contract. Existing
normative rules remain in force until the coordinated implementation cutover;
ordinary local Agents retain their strict cancellation guarantee.

## Required verification and remaining decisions

Run the same conformance scenarios against both backends: fragmented/coalesced
frames, invalid/oversized inputs, version mismatch, rejected authentication or
filesystem access, duplicate requests, bounded slow-consumer handling, competing
starts, busy/unavailable destinations, finish/listen/no-reply outcomes, immutable
unknown user, cancellation during admission/output, lost acknowledgements,
disconnect/reconnect, stale generations, uncertain cancellation, same-route
exclusion, independent-route admission, bounded unresolved-operation capacity,
restart recovery, local cleanup failure, and shutdown. Test idle-socket cleanup
without deleting someone else's endpoint. No such fixture has run yet.

Before stage-one closure, specify exact schemas and SDK mappings, and verify
the proposed negotiation and sequence rules with both backends. Record required
deadline configuration and any deployment values used in fixtures. Verify a producer of
structured end/follow-up intent, source-message admission, history isolation,
upstream output bounds, and authoritative settlement/recovery against the installed
OpenClaw version. These are distinct from later deployment and hardware tests.
