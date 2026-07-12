# AI Server Conversation Protocol

## Status and scope

- **Authority:** Normative
- **Audience:** Agents changing sessions, conversation endpoints, websocket communication, or microphone-to-session behavior
- **Read when:** Working on `ai_server/messages.py`, `ai_server/interfaces.py`, `ai_server/sessions.py`, websocket clients, or the websocket server

This document defines the lifecycle and message protocol shared by all text and voice input adapters. It supersedes the previous informal Conversation Protocol.

The protocol begins where a communication endpoint exchanges typed events with a `Session`. Transport framing, microphone capture, STT, TTS, and device presentation are outside its boundary.

Requirement identifiers use the `CP-` prefix.

## Ownership boundaries

- `Session` MUST exclusively own session and Conversation lifecycle.
- A Session MUST have zero or one active Conversation.
- A Session MUST NOT depend on a concrete endpoint, microphone, driver, agent, or transport type.
- A communication adapter MUST translate its transport into protocol events without reproducing Session lifecycle rules.
- An agent MUST receive only an active `Conversation` and a limited `ConversationEndpoint`.
- An agent MUST NOT create, replace, or re-arm Sessions or Conversations.
- `ConversationEndpoint` MUST enforce active-conversation message ordering and conversational-floor ownership.
- Session MUST own the effective follow-up timeout. Agents express follow-up intent through `ConversationEndpoint.request_follow_up()` and MUST NOT select endpoint timeout policy.

## Terminology

- **Endpoint:** Adapter exchanging Conversation Protocol events with a Session.
- **Session:** Long-lived protocol owner associated with one endpoint.
- **Conversation:** One bounded interaction within a Session.
- **Message:** One ordered stream of text fragments from one sender.
- **Floor:** Permission to begin the next message or complete the current turn.
- **Protocol violation:** An event that is invalid in the current state or violates a field invariant.
- **Endpoint closure:** Transport or adapter termination that prevents further events.

Names in event tables are protocol events. Uppercase names in state tables are protocol states.

## Attributes

### Session attributes

`SessionAttributes.attributes` is a string-to-string mapping.

- `medium` MUST be present and MUST be `text` or `voice`. (`CP-ATTR-001`)
- `medium` MUST remain unchanged for the lifetime of the Session. (`CP-ATTR-002`)
- `user` and `area` MAY be present.
- All keys and values MUST be non-empty strings.
- Unknown attributes MAY be preserved but MUST NOT acquire undocumented protocol semantics.

External endpoints MUST send `SessionAttributes` during `HANDSHAKE`. A trusted local adapter MAY supply validated attributes while constructing the Session and begin directly in `IDLE`.

### Conversation attributes

`NewConversation.attributes` contains overrides for the new Conversation.

- Conversation attributes MAY override `user`, `area`, and unknown non-protocol attributes.
- Conversation attributes MUST NOT override `medium`. (`CP-ATTR-003`)
- Effective attributes MUST be copied into the Conversation and MUST NOT mutate Session attributes.
- Conversation-scoped mutable state MUST NOT survive into another Conversation. (`CP-SESSION-004`)

## Typed event inventory

### Endpoint to Session

| Event | Fields | Constraints | Meaning |
|---|---|---|---|
| `SessionAttributes` | `attributes` | Valid Session attribute mapping | Establish Session identity and medium |
| `NewConversation` | `attributes` | Valid Conversation overrides | Create one Conversation |
| `MessageBegin` | `message_id` | Non-empty and unique within the Conversation | Begin one user message |
| `MessageFragment` | `message_id`, `text` | ID matches the open user message; text is a string | Append text, including an empty fragment if intentionally emitted |
| `MessageEnd` | `message_id` | ID matches the open user message | Complete the user message |
| `ConversationEnded` | `reason` | Non-empty stable reason | Decline or stop further input for the active Conversation |
| endpoint closure | none | Transport-defined | Permanently end the Session |

### Session to endpoint

| Event | Fields | Constraints | Meaning |
|---|---|---|---|
| `ReadyForConversation` | none | Emitted once on entry to `IDLE` | Endpoint may start a Conversation |
| `FollowUpRequested` | `timeout_seconds` | Positive finite number supplied by Session | Agent returned the floor for one follow-up message |
| `ProcessingUpdate` | none | No assistant message open | Agent is still working; carries no response content |
| `MessageBegin` | `message_id` | Non-empty and unique within the Conversation | Begin one assistant message |
| `MessageFragment` | `message_id`, `text` | ID matches the open assistant message | Append assistant text |
| `MessageEnd` | `message_id` | ID matches the open assistant message | Complete the assistant message |
| `ConversationEnded` | `reason` | Non-empty stable reason | Conversation cleanup is complete |
| `SessionRejected` | `reason` | Non-empty diagnostic safe to expose to the endpoint | Protocol violation prevents continuation |

`WaitForNewConversation`, `WaitForNewMessage`, and `RequestFollowUp` are not part of the normative protocol. Stage 3 MUST remove them rather than maintain aliases unless a separate compatibility decision is approved.

## Message stream rules

- Every message MUST begin with `MessageBegin`, contain zero or more matching `MessageFragment` events, and end with matching `MessageEnd`. (`CP-MESSAGE-001`)
- A message ID MUST be unique within its Conversation and MUST NOT be reused by either sender. (`CP-MESSAGE-002`)
- At most one user message and at most one assistant message MAY be open. (`CP-MESSAGE-003`)
- User and assistant messages MUST NOT be open simultaneously. (`CP-FLOOR-001`)
- A fragment or end for an unknown, stale, or mismatched message ID is a protocol violation. (`CP-MESSAGE-004`)
- A nested `MessageBegin` from the same side is a protocol violation.
- Message fragments are ordered exactly as received. Session MUST NOT reorder or merge them.

## Protocol states

### `HANDSHAKE`

The Session has not accepted endpoint attributes.

On entry: no event is emitted.

### `IDLE`

The Session has no active Conversation.

On entry: Session emits `ReadyForConversation` exactly once. (`CP-SESSION-001`)

### `AWAITING_USER_MESSAGE`

A Conversation exists and the endpoint owns the floor, but no user message is open.

This state is entered immediately after `NewConversation` and after `FollowUpRequested`.

### `RECEIVING_USER_MESSAGE`

One user message is open. The endpoint retains the floor until matching `MessageEnd`.

### `AGENT_ACTIVE`

A complete user message is available and the agent owns the floor. The agent may produce complete assistant messages, processing updates, request one follow-up, or return.

### `AWAITING_FOLLOW_UP`

The agent requested another user message. The endpoint owns the floor until it begins that message, explicitly ends the Conversation, closes, or reaches the supplied deadline.

### `ENDING_CONVERSATION`

The Session is cancelling or joining Conversation-owned work, checking invariants, and destroying Conversation state.

On successful cleanup, Session emits `ConversationEnded` if the endpoint is available, then enters `IDLE`.

### `CLOSED`

Terminal state. No further events are accepted or emitted.

## Transition table

`Violation` means Session MUST reject an external endpoint when possible and close it. `Internal failure` means an impossible agent or trusted-adapter action MUST fail an invariant and terminate the affected work.

| Current state | Input or internal result | Action | Next state |
|---|---|---|---|
| `HANDSHAKE` | valid `SessionAttributes` | Store attributes | `IDLE` |
| `HANDSHAKE` | endpoint closure | Release Session | `CLOSED` |
| `HANDSHAKE` | any other endpoint event | Violation | `CLOSED` |
| `IDLE` | valid `NewConversation` | Create Conversation | `AWAITING_USER_MESSAGE` |
| `IDLE` | endpoint closure | Release Session | `CLOSED` |
| `IDLE` | any other endpoint event | Violation | `CLOSED` |
| `AWAITING_USER_MESSAGE` | `MessageBegin` | Open user message | `RECEIVING_USER_MESSAGE` |
| `AWAITING_USER_MESSAGE` | `ConversationEnded` | Record reason | `ENDING_CONVERSATION` |
| `AWAITING_USER_MESSAGE` | endpoint closure | Cancel Conversation and Session | `CLOSED` |
| `AWAITING_USER_MESSAGE` | any other endpoint event | Violation | `CLOSED` |
| `RECEIVING_USER_MESSAGE` | matching `MessageFragment` | Forward fragment | `RECEIVING_USER_MESSAGE` |
| `RECEIVING_USER_MESSAGE` | matching `MessageEnd` | Close input and run/resume agent | `AGENT_ACTIVE` |
| `RECEIVING_USER_MESSAGE` | endpoint closure | Cancel Conversation and Session | `CLOSED` |
| `RECEIVING_USER_MESSAGE` | any other endpoint event | Violation | `CLOSED` |
| `AGENT_ACTIVE` | agent `ProcessingUpdate` | Send update | `AGENT_ACTIVE` |
| `AGENT_ACTIVE` | agent `MessageBegin` | Open assistant message | `AGENT_ACTIVE` |
| `AGENT_ACTIVE` | matching agent `MessageFragment` | Send fragment | `AGENT_ACTIVE` |
| `AGENT_ACTIVE` | matching agent `MessageEnd` | Close assistant message | `AGENT_ACTIVE` |
| `AGENT_ACTIVE` | agent calls `request_follow_up()` | Emit `FollowUpRequested` with Session timeout and arm deadline | `AWAITING_FOLLOW_UP` |
| `AGENT_ACTIVE` | agent returns | Begin cleanup | `ENDING_CONVERSATION` |
| `AGENT_ACTIVE` | endpoint closure | Cancel agent and Session | `CLOSED` |
| `AWAITING_FOLLOW_UP` | `MessageBegin` | Cancel deadline and open user message | `RECEIVING_USER_MESSAGE` |
| `AWAITING_FOLLOW_UP` | `ConversationEnded` | Record reason | `ENDING_CONVERSATION` |
| `AWAITING_FOLLOW_UP` | deadline expires | Record `follow_up_timeout` | `ENDING_CONVERSATION` |
| `AWAITING_FOLLOW_UP` | endpoint closure | Cancel Conversation and Session | `CLOSED` |
| `AWAITING_FOLLOW_UP` | any other endpoint event | Violation | `CLOSED` |
| `ENDING_CONVERSATION` | cleanup succeeds | Emit end when possible; destroy state | `IDLE` |
| `ENDING_CONVERSATION` | endpoint closure | Destroy state | `CLOSED` |
| `CLOSED` | any event | No action; event is inapplicable | `CLOSED` |

While an assistant message is open in `AGENT_ACTIVE`, only its matching `MessageFragment`, matching `MessageEnd`, or endpoint closure is valid. `ProcessingUpdate`, `request_follow_up()`, another `MessageBegin`, and agent return are internal failures. (`CP-MESSAGE-005`)

## Conversational floor

- The endpoint owns the floor in `AWAITING_USER_MESSAGE` and `RECEIVING_USER_MESSAGE`.
- The agent owns the floor in `AGENT_ACTIVE`.
- The endpoint regains the floor only after Session emits `FollowUpRequested`. (`CP-FLOOR-002`)
- Endpoint message input while the agent owns the floor is a protocol violation.
- The agent MUST NOT call `request_follow_up()` before a complete user message or while an assistant message is open. (`CP-FOLLOWUP-001`)
- At most one follow-up request MAY be outstanding. (`CP-FOLLOWUP-002`)
- Session supplies `FollowUpRequested.timeout_seconds`; it is the sole follow-up deadline. An agent or adapter MUST NOT substitute a separate duration. (`CP-FOLLOWUP-003`)

## Conversation termination

Stable reasons include:

- `completed`: agent returned normally;
- `endpoint_ended`: endpoint explicitly ended the Conversation;
- `follow_up_timeout`: endpoint did not begin a follow-up before the deadline;
- `agent_failed`: agent failed and the failure was converted into orderly cleanup.

Endpoint closure closes the entire Session and does not require a `ConversationEnded` event.

On orderly termination, Session MUST:

1. prevent new Conversation events;
2. cancel or join Conversation-owned work;
3. assert that no user or assistant message remains open;
4. destroy Conversation-scoped state;
5. emit `ConversationEnded(reason)` when the endpoint remains usable;
6. enter `IDLE` and emit `ReadyForConversation`.

Cleanup MUST have one implementation path regardless of termination reason. (`CP-SESSION-003`)

## Timeouts and cancellation

- The Session owns the follow-up deadline after emitting `FollowUpRequested`.
- The endpoint adapter MAY implement the timer on Session's behalf, but it MUST use the supplied value and report expiration as `ConversationEnded(reason="follow_up_timeout")`.
- Message streams have no protocol timeout. A transport or adapter MAY have a liveness policy, but such a failure closes the endpoint rather than inventing a completed message.
- Endpoint closure MUST cancel active agent work and Conversation-owned tasks.

## Failure and recovery

### External endpoint violation

When transport permits, Session or its adapter MUST:

1. send `SessionRejected(reason)`;
2. close with a protocol-error indication;
3. cancel active Conversation work;
4. remove the Session.

It MUST NOT remain connected and guess the endpoint's intent. (`CP-ERROR-001`)

### Internal violation

Trusted adapters and agents MUST fail an assertion or equivalent invariant when they emit an impossible event. The Session MUST be torn down if state can no longer be trusted. (`CP-ERROR-002`)

## Normal sequences

### Single-turn text Conversation

```text
Endpoint                                 Session                         Agent
   |-- SessionAttributes(medium=text) ---->|
   |<-- ReadyForConversation --------------|
   |-- NewConversation ------------------->|
   |-- MessageBegin(user-1) -------------->|
   |-- MessageFragment(user-1, text) ----->|
   |-- MessageEnd(user-1) ---------------->|
   |                                       |-- run/resume -------------->|
   |<-- MessageBegin(assistant-1) ----------|<-- output ------------------|
   |<-- MessageFragment(assistant-1, text) -|
   |<-- MessageEnd(assistant-1) ------------|
   |                                       |<-- return ------------------|
   |<-- ConversationEnded(completed) -------|
   |<-- ReadyForConversation ---------------|
```

### Follow-up Conversation

```text
complete user message
complete assistant message
agent calls request_follow_up()
Session emits FollowUpRequested(timeout_seconds=60)
complete follow-up user message
complete assistant message
ConversationEnded(completed)
ReadyForConversation
```

### Processing update

```text
MessageEnd(user-1)
ProcessingUpdate
ProcessingUpdate
MessageBegin(assistant-1)
MessageFragment(assistant-1, "...")
MessageEnd(assistant-1)
```

## Invalid sequences

The following are protocol violations or internal failures:

- `SessionAttributes` without `medium`;
- changing `medium` in `NewConversation`;
- `MessageBegin` in `IDLE`;
- `MessageFragment` before `MessageBegin`;
- `MessageEnd` with a different message ID;
- endpoint message input in `AGENT_ACTIVE`;
- `ProcessingUpdate` inside an assistant message;
- agent return with an assistant message open;
- duplicate `request_follow_up()`;
- follow-up input after its deadline;
- reusing a message ID in the same Conversation.

## Websocket JSON serialization

Each websocket text frame contains exactly one JSON object. Event names use snake case. Unknown fields MUST be rejected. Fields whose value would be `None` MUST be omitted unless this external contract explicitly requires `null`; no event currently requires `null`.

### Endpoint to Session examples

```json
{"type":"session_attributes","attributes":{"medium":"text","user":"Maciek","area":"office"}}
{"type":"new_conversation","attributes":{"area":"kitchen"}}
{"type":"message_begin","message_id":"user-1"}
{"type":"message_fragment","message_id":"user-1","text":"cześć"}
{"type":"message_end","message_id":"user-1"}
{"type":"conversation_ended","reason":"endpoint_ended"}
```

### Session to endpoint examples

```json
{"type":"ready_for_conversation"}
{"type":"processing_update"}
{"type":"follow_up_requested","timeout_seconds":60.0}
{"type":"message_begin","message_id":"assistant-1"}
{"type":"message_fragment","message_id":"assistant-1","text":"odpowiedź"}
{"type":"message_end","message_id":"assistant-1"}
{"type":"conversation_ended","reason":"completed"}
{"type":"session_rejected","reason":"message_fragment received before message_begin"}
```

## Agent API

The abstract shape remains:

```python
async def run_conversation(
    self,
    conversation: Conversation,
    endpoint: ConversationEndpoint,
) -> None:
    ...
```

High-level helpers MAY assemble complete text messages, but they MUST preserve protocol ordering and unique IDs. An agent that needs another user message MUST call `ConversationEndpoint.request_follow_up()` before awaiting it. Session then emits `FollowUpRequested` with the configured Session timeout.

## Observability requirements

- Every Session transition MUST be logged on DEBUG with `Session[session_id]` context, old state, triggering event, and new state. (`CP-OBS-001`)
- Every Conversation start and end MUST be logged on INFO with Session ID, Conversation ID, medium, user, area, and termination reason.
- Message begin and end MUST be logged on DEBUG with Conversation and message IDs.
- Message text MUST follow the project's privacy and logging policy; transition logs MUST NOT require content.
- Protocol rejection MUST be logged on WARNING with peer or adapter identity, state, event type, and reason.

## Implementation and conformance references

Stage 3 implementation targets:

- `ai_server/messages.py`
- `ai_server/interfaces.py`
- `ai_server/sessions.py`
- `ai_server/websocket_server.py`
- `ai_server/ws_client_common.py`

Conformance targets:

- `tests/test_messages.py`
- `tests/test_interfaces.py`
- focused Session state-machine tests
- `tests/test_websocket_server.py`
- [Protocol Conformance Catalogue](protocol-conformance-catalogue.md)

Voice adaptation is defined by [Microphone-Conversation Mapping](microphone-conversation-mapping.md).

## Compatibility policy

This protocol intentionally replaces the legacy waiting events and message streams without IDs. Stage 3 MUST migrate all in-repository producers and consumers together. No compatibility aliases or permissive parsing are required.

## Unresolved decisions

None. Changes to the event vocabulary, state machine, floor ownership, or termination semantics require a normative document change before implementation.
