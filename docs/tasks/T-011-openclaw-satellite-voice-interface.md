# T-011: OpenClaw Satellite Voice Interface

## Status

- **Authority:** Plan; not a runtime contract
- **Status:** Planned; implementation not started or authorized
- **Created:** 2026-10-09
- **Audience:** Maintainers of the OpenClaw voice subproject and reused satellite components
- **Read when:** Planning or implementing OpenClaw voice input, replies, announcements, or initiated conversations

## Objective

Create a separate subproject in this repository that gives OpenClaw voice input
and output through our existing satellites. Reuse the satellite drivers,
conversation protocol, Whisper, Piper, speaker recognition, and room context.
OpenClaw owns all reasoning and actions for this subproject.

This is a sidequest, not a replacement or direction change for Piotr. Preserve
the existing Piotr application, orchestrator, and DSAs. Piotr and the new voice
tool are assumed never to run simultaneously on the same machine; shared
satellite ownership and switching between assistants are outside scope.

Captain requested the path of least resistance and explicitly chose to retain
the current conversation protocol. This document records the plan only.
Implementation requires Captain's separate "proceed" or "make it so".

## Binding documents

Read before implementation:

1. root `AGENTS.md` and the architecture decisions in `README.md`;
2. `docs/README.md`, including the remaining T-004 hardware verification scope;
3. `docs/ai-server-conversation-protocol.md`;
4. `docs/microphone-protocol.md`;
5. `docs/microphone-conversation-mapping.md`;
6. `docs/protocol-conformance-catalogue.md`;
7. `docs/project-standard-satellite-behavior.md`;
8. this task.

The new OpenClaw transport is a separate integration boundary. Do not assume
the existing websocket client protocol is its transport or change that binding
incidentally. Read its normative documents if implementation touches it.

## Agreed behavior and ownership

| Concern | Owner and behavior |
|---|---|
| Reasoning, tools, permissions, and assistant policy | OpenClaw |
| Destination selection for proactive speech | OpenClaw supplies an explicit satellite destination; the voice tool exposes satellite names, rooms, and availability |
| Presence inference and person-location history | OpenClaw, if desired; no presence tracker in the voice tool |
| Satellite connections, capture, playback, cues, and recovery | Voice tool through the existing manager and abstract driver interfaces |
| Speech recognition and synthesis | Existing Whisper and Piper components |
| Speaker recognition | Existing conversation-start recognition; the identity stays fixed for that Conversation |
| Unknown speaker | Normal accepted input; forward to OpenClaw without inventing a known identity or rejecting speech merely because recognition failed |
| Room context | Configured area of the originating satellite |
| Follow-up presentation and timing | Voice input adapter under the existing protocol |
| Announcements and initiated conversations | OpenClaw requests either speak-and-finish or speak-and-listen |
| Busy or unavailable destination | Voice tool reports the result; OpenClaw owns retry and alternative-destination policy |

Recognition is known to be unreliable, so unknown speakers must be a routine
tested case. Do not introduce per-utterance identity fields or change the
immutable Conversation context. A follow-up retains the conversation-start
identity even if somebody else answers; this is an accepted limitation of
reusing the current protocol. The initially recognized user is context, not a
new authorization policy enforced by this tool.

## Working architecture

Use a separate Python voice-service entry point and a thin OpenClaw channel
plugin, subject to checking the installed OpenClaw version and interfaces:

```text
satellites <-> Python voice service <-> OpenClaw channel plugin <-> OpenClaw
```

- Reuse Python components directly from this repository. Avoid copying their
  implementation, a broad shared-library extraction, or rewriting them in the
  plugin's language.
- Implement the OpenClaw agent adapter behind the existing `Agent` and
  `AgentConversation` interfaces. Keep concrete OpenClaw behavior out of the
  Conversation bridge and satellite drivers.
- The plugin adapts channel ingress, outbound delivery, and OpenClaw session
  correlation. The Python service owns voice and hardware lifetimes.
- Provide an explicit user-supplied config path. Command-line values take
  precedence where a setting is also configurable in the file. The new entry
  point must not require Piotr's orchestrator, DSAs, or reasoning-model startup.
- Keep one active interaction per satellite. Report competing outbound work
  as busy instead of adding a durable queue or interrupting an active exchange.
- Keep buffers and operation deadlines bounded, preserve cancellation and
  shutdown guarantees, and distinguish delivery acceptance from audio playback
  completion. Do not automatically replay speech after an uncertain delivery.
- Use existing firmware and device behavior unless verification reveals a
  separately scoped defect; no firmware change is planned.

Process supervision, transport, authentication, and exact plugin API names are
implementation choices to settle against the installed version. Prefer the
smallest supported local integration, not a general-purpose voice platform.

## Proactive output without changing the Conversation protocol

The current bridge starts only after an input adapter accepts a complete,
non-empty user message. It has no assistant-initiated Conversation event.

Use a separate delivery operation in the new tool's adapter/coordinator:

1. OpenClaw supplies the target, text, delivery correlation, and finish/listen
   intent.
2. The coordinator reserves an idle satellite and uses manager-owned
   listening/playback exclusion, cues, visuals, and correlated playback.
3. For an announcement, report playback completion and restore readiness.
4. For speak-and-listen, present a bounded reply opportunity after playback
   drains. If accepted speech arrives, use it as the initial user message of a
   normal Conversation, with conversation-start speaker recognition and room
   context. Correlate that Conversation with the initiating OpenClaw interaction
   outside the immutable bridge context.
5. If nobody replies or delivery fails, report that outcome and restore or
   recover the satellite. Do not create an empty or fabricated Conversation.

This path requires new coordination code; it is not already supplied by the
existing voice adapter. Verify that it can be composed without violating the
normative microphone and mapping contracts. Preserve existing protocol events,
interfaces, and state transitions. If the proposed composition cannot meet
those constraints, document the conflict and return to Captain before changing
the contract or dropping initiated conversations from scope.

## Implementation stages

### 1. Verify the installed OpenClaw integration boundary

OpenClaw is reported installed under account `ryszard`. During planning,
`/home/ryszard` was inaccessible from the agent account; the installed version,
configuration, and startup method have not been verified.

- Establish the version, startup method, supported plugin API, and local
  connection/authentication mechanism without exposing credentials.
- Verify channel ingress and outbound delivery, including initiated output.
- Choose OpenClaw session correlation and history scope explicitly; do not
  accidentally merge unrelated satellite conversations or assume the initial
  speaker identifies every future user of that satellite.
- Establish how OpenClaw expresses end versus follow-up intent, progress,
  failure, and cancellation through the existing agent interface. Do not infer
  control flow from punctuation or diagnostic strings.
- Define the minimal local transport contract, including correlation,
  boundedness, completion results, and disconnect behavior.
- Record how proactive reply capture satisfies the unchanged voice contracts.

Do not run `sudo` from the agent terminal. If privileged inspection is needed,
follow root `AGENTS.md` and ask Captain to run the exact command manually.

### 2. Complete one user-initiated voice exchange

- Add the separate entry point and explicit configuration.
- Connect reused drivers, Whisper, Piper, and speaker recognition to the
  OpenClaw adapter and thin channel plugin.
- Deliver accepted text with conversation-start identity and room context.
- Render replies and support ordinary multi-turn follow-up under the existing
  bridge, including unknown-speaker input.

### 3. Add proactive speech and initiated conversations

- Expose satellite destinations and current availability.
- Implement speak-and-finish and speak-and-listen through the separate delivery
  coordinator described above.
- Report busy, unavailable, playback completion, no reply, and failures with
  sufficient correlation for OpenClaw to act on the result.
- Preserve one owner of satellite state across unsolicited delivery, normal
  wake-word acceptance, reply capture, and teardown.

### 4. Verify, review, and accept on hardware

- Add focused tests for the new adapter/plugin contract and lifecycle behavior.
- Run relevant existing Conversation, microphone, mapping, STT, and TTS tests,
  plus the complete Python suite if shared runtime components change.
- Run plugin tests appropriate to the installed SDK. Run the configured-model
  orchestrator/DSA behavior suite if those behaviors are changed, as required
  by `AGENTS.md`; no such changes are planned.
- Perform independent implementation review using the project's review-agent
  workflow, address findings, and repeat until a fresh review reports
  `No findings.`
- With Piotr stopped, verify the new runtime on a configured satellite and
  record Captain's hardware acceptance separately from automated results.
- Document startup, shutdown, configuration, and returning to the existing
  Piotr runtime. Starting the sidequest must not rewrite Piotr's configuration.

## Acceptance criteria

- A wake-word request reaches OpenClaw with the expected room and initial
  recognized or unknown speaker; the response plays through Piper.
- Ordinary follow-up uses the existing Conversation protocol unchanged.
- OpenClaw can address an idle satellite without preceding user speech, both
  for an announcement and for a question whose spoken answer reaches the
  initiating OpenClaw session.
- Proactive reply timeout restores readiness and reports no reply without a
  fabricated user message.
- Busy and unavailable destinations produce explicit results; concurrent
  requests cannot interleave audio or corrupt listening state.
- Cancellation, disconnects, shutdown, and late results cannot leak audio or
  context into a later interaction. Delivery uncertainty does not cause blind
  replay of speech or resubmission of an action-triggering user message.
- Piotr's existing behavior and normative protocols remain intact; unrelated
  outstanding hardware verification is not declared complete by this task.
- Automated checks, independent review, deployment evidence, and Captain's
  satellite acceptance are recorded as separate gates before marking complete.

## Reference material and evidence

Current code inspected during planning includes
`ai_server/conversations/interfaces.py`, `contexts.py`, `messages.py`,
`ai_server/microphones/conversation_adapter.py`, `manager.py`, and
`ai_server/speaker_recognition/client.py`. `UserMessage` contains text only;
speaker identity and area are immutable conversation context fields.

Upstream references consulted on 2026-10-09:

- [Building channel plugins](https://docs.openclaw.ai/plugins/sdk-channel-plugins)
- [Gateway protocol](https://docs.openclaw.ai/gateway/protocol)

These establish candidate integration surfaces, not compatibility with the
uninspected installation. No OpenClaw connection, plugin installation,
deployment, or satellite acceptance has been performed for this task.
