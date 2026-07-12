# Microphone-Conversation Mapping

## Status and scope

- **Authority:** Normative
- **Audience:** Agents changing the adapter between microphone behavior and Session/Conversation behavior
- **Read when:** Working on `MicrophoneManager`, microphone session endpoints, voice follow-up, TTS playback sequencing, or microphone Conversation tests

This document defines how the normative [Microphone Protocol](microphone-protocol.md) is translated into the normative [AI Server Conversation Protocol](ai-server-conversation-protocol.md).

It is not a third lifecycle owner. It assigns translation and sequencing responsibilities to a voice adapter implemented by `MicrophoneManager` and its Session endpoint.

Requirement identifiers use the `MAP-` prefix.

## Ownership boundaries

- Session owns Session and Conversation state.
- MicrophoneManager owns microphone protocol state, capture processing, playback, cues, visual state while connected, and the voice adapter.
- A microphone driver knows only the Microphone Protocol.
- Session knows only the Conversation Protocol.
- An agent knows only `Conversation` and `ConversationEndpoint`.
- The voice adapter MUST NOT copy Session's lifecycle state machine or expose raw microphone events to Session. (`MAP-OWNER-001`)

## Terminology

- **Voice adapter:** The MicrophoneManager role that translates accepted speech and assistant output across protocols.
- **Accepted utterance:** Non-empty final text approved by the active capture mode's acceptance rules.
- **Pending playback:** A complete assistant message received from Session but not yet fully drained by the device.
- **Re-arm:** Starting a new microphone listening generation after Session is ready and conflicting output is complete.

## Event and state dependencies

This mapping defines no new typed events and no independent protocol state machine. It consumes and produces only events defined by the two base protocols:

- Conversation events and states come from the [AI Server Conversation Protocol](ai-server-conversation-protocol.md).
- Listening, capture, cue, playback, visual, and driver events and states come from the [Microphone Protocol](microphone-protocol.md).

The adapter MAY keep implementation bookkeeping such as pending playback or a queued Session event, but such bookkeeping is not a third lifecycle authority. Its valid actions are determined by the current state of both base protocols.

## Transition principle

For every cross-protocol action, all prerequisites from both base protocols MUST be satisfied. If one side is not ready, the adapter retains the already-received event in order and waits; it MUST NOT fabricate a transition on the other side.

The detailed sections below are the normative transition table for each supported mapping trigger. Any cross-protocol trigger not listed is invalid until this document is amended.

## Session establishment

Each microphone has one persistent local Session.

- The adapter MUST create it with validated `medium=voice` attributes. (`MAP-SESSION-001`)
- The adapter MAY include configured `area`.
- `user` MUST be a Conversation attribute derived from accepted speaker recognition, not a permanent microphone Session attribute.
- The trusted local Session skips external `HANDSHAKE` and begins in Conversation Protocol `IDLE`.
- The adapter MUST close the Session when the driver is permanently closed.

## Ready-for-conversation mapping

When Session emits `ReadyForConversation`, the adapter MUST wait until:

- no assistant playback is pending or draining;
- no cue is active;
- no previous listening generation is stopping;
- the driver is connected and disarmed.

It then MUST:

1. send `SetVisualState(IDLE)`;
2. generate a new `listen_id`;
3. send `StartListening` in the microphone's configured new-conversation mode: `WAKE_WORD` or `OPEN_MIC`;
4. await matching `ListeningStarted`.

`ReadyForConversation` does not itself create a Conversation. (`MAP-SESSION-002`)

## Accepted new-conversation utterance

An accepted wake-word or open-mic utterance maps to exactly one Conversation and exactly one user message. (`MAP-INPUT-001`)

Before sending Conversation events, the adapter MUST:

1. possess non-empty accepted final text;
2. ensure the capture segment is complete;
3. stop and join any still-active listening generation;
4. set the visual state to `PROCESSING`;
5. complete the `UTTERANCE_ACCEPTED` cue;
6. await speaker recognition only if it was enabled for the accepted utterance.

It then sends, without interleaving another microphone utterance:

```text
NewConversation(attributes)
MessageBegin(message_id)
MessageFragment(message_id, final_text)
MessageEnd(message_id)
```

Rules:

- `message_id` MUST be new.
- `attributes.user` is included only when speaker recognition produced an accepted user.
- `medium` MUST NOT be overridden in `NewConversation`.
- Empty or whitespace-only fragments MUST NOT be used to manufacture a valid utterance.
- Text MUST be final accepted text, never a partial transcript.

## Wake-word mode mapping

```text
ReadyForConversation
-> SetVisualState(IDLE)
-> StartListening(WAKE_WORD)
-> ListeningStarted
-> SpeechStarted(wake_word=...)
-> SetVisualState(LISTENING)
-> captured audio
-> SpeechEnded
-> final STT
-> accepted non-empty text
-> SetVisualState(PROCESSING)
-> accepted cue
-> NewConversation and one complete user message
```

- `SpeechStarted` with a wake word moves the visual state to `LISTENING` immediately.
- Wake detection without usable final text MUST NOT create a Conversation. (`MAP-WAKE-001`)
- If wake-word capture produces no usable text, the adapter MUST wait for Session to remain `IDLE`, restore `IDLE`, and explicitly start a new listening generation.

## Open-mic mode mapping

Open-mic candidate detection and acceptance follow the Microphone Protocol.

### Ordinary or rejected segment

- Ordinary speech without an accepted wake phrase MUST remain private.
- It MUST NOT send `NewConversation`, `MessageBegin`, `MessageFragment`, `MessageEnd`, or `ConversationEnded`.
- If a partial candidate changed the visual to `LISTENING`, final rejection MUST reset candidate UI and restore `IDLE`.
- The current open-mic listening generation remains active.

### Accepted segment

- Acceptance occurs only after final transcript validation.
- The adapter MUST set `PROCESSING`, stop the active open-mic generation, complete the accepted cue, and then send one new-Conversation message sequence.
- The accepted segment MUST be forwarded exactly once even if multiple partials detected the wake phrase. (`MAP-OPENMIC-001`)

## Follow-up mapping

When Session emits `FollowUpRequested(timeout_seconds)`, the adapter MUST use that supplied deadline. It MUST NOT substitute a microphone configuration timeout. (`MAP-FOLLOWUP-001`)

If assistant playback is pending, the adapter MUST:

1. keep `PROCESSING` visible;
2. complete playback and await `PlaybackFinished`;
3. set `LISTENING`;
4. play and complete `FOLLOW_UP_READY`;
5. start a new `FOLLOW_UP` listening generation using the remaining Session deadline.

If there is no pending playback, it begins at step 3.

An accepted follow-up maps to one message in the existing Conversation:

```text
MessageBegin(message_id)
MessageFragment(message_id, final_text)
MessageEnd(message_id)
```

It MUST NOT send `NewConversation`.

After accepted follow-up text:

- set `PROCESSING`;
- send the complete user message;
- do not re-arm until another `FollowUpRequested` or `ReadyForConversation` is received.

### Follow-up timeout

If `SpeechStarted` does not occur before the supplied deadline:

1. stop and join the follow-up listening generation;
2. play and complete `FOLLOW_UP_TIMEOUT`;
3. send `ConversationEnded(reason="follow_up_timeout")` to Session;
4. set `IDLE` only after the timeout cue is complete;
5. wait for `ReadyForConversation` before starting a new generation.

A segment that completes without usable text does not reset the deadline. The adapter MAY re-arm follow-up for the remaining time, or end it when no time remains, but MUST NOT create an empty message. (`MAP-FOLLOWUP-002`)

## Assistant message mapping

Session-to-endpoint assistant message events are assembled by `message_id`.

- The adapter MUST validate begin, fragment, and end ordering.
- It MUST NOT begin TTS for a message before matching `MessageEnd` unless a future normative streaming-TTS extension is approved.
- A complete non-empty assistant message is synthesized into exactly one playback stream.
- An empty assistant message produces no playback but remains a valid Conversation Protocol message.
- Multiple assistant messages are played in protocol order.

For one non-empty message:

```text
MessageBegin(assistant-message-id)
MessageFragment(...)*
MessageEnd(assistant-message-id)
SetVisualState(PROCESSING)
PlaybackBegin(playback-id, format)
PlaybackChunk(playback-id, data)*
PlaybackEnd(playback-id)
PlaybackFinished(playback-id)
```

`PROCESSING` MUST remain visible throughout synthesis and playback. (`MAP-OUTPUT-001`)

## Processing-update mapping

`ProcessingUpdate` carries no response content.

- The adapter MUST keep the main visual state `PROCESSING`.
- It MAY synthesize one configured spoken processing cue.
- A processing cue MUST use normal cue/playback exclusion rules and MUST NOT overlap another cue or assistant playback.
- A processing update MUST NOT re-arm listening, request follow-up, or end the Conversation.
- Repeated updates MAY be throttled by the agent/Session processing-update policy.

## Conversation termination and output draining

Session may emit `ConversationEnded` before already queued assistant audio has fully drained.

- The adapter MUST preserve event order and complete already accepted assistant playback unless the endpoint or driver closes or playback fails. (`MAP-END-001`)
- It MUST keep `PROCESSING` visible until playback completes.
- It MUST NOT start a new listening generation merely because `ConversationEnded` was received.
- It MUST wait for the subsequent `ReadyForConversation`, and for playback/cues to finish, before setting `IDLE` and re-arming.

Endpoint or driver closure cancels pending output and closes the Session rather than re-arming.

## Failure mapping

### Capture failure before Conversation creation

- Do not send Conversation events.
- Stop or discard the failed generation.
- Select an explicit connected visual state.
- Retry with a new `listen_id` only while Session remains ready for a new Conversation.

### Capture failure during follow-up

- Do not send a partial or empty user message.
- Retry only within the remaining Session deadline.
- If recovery cannot complete before the deadline, use the normative follow-up-timeout sequence.

### Cue or playback failure

- Do not infer that audio completed successfully.
- Abort the correlated operation.
- End or preserve the Conversation according to Session state, but never start listening while the driver may still be playing.
- A protocol-state mismatch requires closing and recreating the microphone boundary rather than guessing.

### Driver unavailable

Temporary unavailability does not create or end a Conversation by itself. The adapter logs and retries only when the current Session state still permits the intended operation.

## Timeouts and cancellation

- Follow-up timing uses only `FollowUpRequested.timeout_seconds` and the rules in the Follow-up mapping.
- Microphone operation timeouts remain owned by MicrophoneManager under the Microphone Protocol.
- Endpoint closure cancels active capture processing, STT, speaker recognition, cues, playback, and agent work as applicable, then closes both boundaries.
- Session Conversation termination does not cancel already accepted assistant playback; the output-draining rules apply.
- Cancellation MUST preserve correlation in logs and MUST prevent late results from crossing into a newer generation or Conversation.

## Cross-protocol invariants

- One accepted microphone utterance produces exactly one complete user message. (`MAP-INVARIANT-001`)
- A new Conversation is created only for accepted wake-word or open-mic text.
- Follow-up text belongs to the existing Conversation.
- Rejected open-mic speech, wake detection without usable text, and empty follow-up capture never reach an agent. (`MAP-INVARIANT-002`)
- No driver creates or consumes Conversation Protocol events.
- No Session creates or consumes Microphone Protocol events.
- No concrete driver knowledge escapes the abstract microphone interface.
- Listening is re-armed only after an explicit Session event and after conflicting output finishes. (`MAP-INVARIANT-003`)
- Stale microphone events cannot affect a new listening generation or Conversation.
- The Session-supplied follow-up deadline has one meaning across both protocols.
- `PROCESSING` covers agent work, synthesis, and assistant playback.

## Invalid sequences

- Sending `NewConversation` on wake detection before final usable text exists;
- forwarding a partial open-mic transcript;
- sending an empty user message after no-transcript capture;
- sending `NewConversation` for follow-up input;
- starting follow-up listening before pending assistant playback drains;
- replacing the Session follow-up deadline with microphone configuration;
- re-arming on `ConversationEnded` without waiting for `ReadyForConversation`;
- setting `IDLE` when assistant playback begins;
- sending driver events directly to Session;
- invoking concrete firmware services outside a driver.

## Observability requirements

- Mapping logs MUST include microphone instance, Session ID, Conversation ID when available, `listen_id`, `utterance_id`, message ID, cue ID, and playback ID as applicable. (`MAP-OBS-001`)
- Every cross-protocol translation MUST be logged on DEBUG without requiring transcript content.
- Accepted utterance, follow-up timeout, playback start/finish, and re-arm are crucial events and MUST be logged briefly on INFO.
- Logs MUST make it possible to distinguish wake candidate, final rejection, final acceptance, Conversation creation, agent processing, playback, and re-arm.

## Implementation and conformance references

Stage 3 implementation targets:

- `ai_server/microphones/manager.py`
- `ai_server/microphones/agent_endpoint.py`
- `ai_server/sessions.py`
- microphone TTS/STT adapters

Conformance targets:

- `tests/test_microphones.py`
- `tests/test_mic_protocol_test.py`
- focused Conversation state tests
- [Protocol Conformance Catalogue](protocol-conformance-catalogue.md)

## Compatibility policy

Stage 3 migrates the manager and Session adapter together. Legacy mappings from `WaitForNewConversation`, `WaitForNewMessage`, `RequestFollowUp`, and overloaded audio events are not retained.

## Unresolved decisions

None. Streaming TTS, full-duplex operation, or alternative follow-up ownership requires a normative protocol extension before implementation.
