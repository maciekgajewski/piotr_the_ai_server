# AI Server Conversation Protocol

This document describes the state machine shared by websocket and microphone input methods. The Session owns lifecycle state. Agents only handle an active Conversation through a limited ConversationEndpoint.

## Entities

- Session: top-level connection or microphone session. It has optional string attributes such as `user` and `area`.
- Conversation: at most one active per Session. It has a generated `conversation_id`, effective attributes, and per-conversation mutable state.
- ConversationEndpoint: limited agent API for active conversation message streams. It can receive user message events, send assistant message events, and explicitly request follow-up input.

Attributes are string key/value pairs. Empty keys and empty values are invalid. Conversation attributes override Session attributes only when provided.

## Session States

### Handshake

Websocket sessions start in Handshake. The first client event must be `SessionAttributes`, even when attributes are empty.

Allowed transitions:

- `SessionAttributes` -> WaitingForNewConversation
- endpoint closed -> Closed

Microphone sessions skip Handshake because their local Session is created with attributes directly.

### WaitingForNewConversation

Session sends `WaitForNewConversation`.

Allowed client events:

- `NewConversation` -> ReceivingMessage
- endpoint closed -> Closed

For microphone input, `WaitForNewConversation` maps to a silent `StartWakeWordListening` microphone
output event. Wake-word detection then causes `NewConversation` and the first message stream to be sent
automatically. Microphone drivers must not return to wake-word mode implicitly after a message or timeout;
the transition is owned by this state.

### ReceivingMessage

Session expects one complete user message stream.

Allowed client events:

- `MessageBegin` -> ReceivingMessageFragments
- `ConversationEnded` -> WaitingForNewConversation
- endpoint closed -> Closed

### ReceivingMessageFragments

Session forwards user message fragments to the agent's ConversationEndpoint.

Allowed client events:

- `MessageFragment` -> ReceivingMessageFragments
- `MessageEnd` -> AgentRunning
- endpoint closed -> Closed

Invalid streams, such as a fragment before begin or end before begin, are protocol violations. Internal components use asserts. Websocket clients are closed with a protocol error.

### AgentRunning

The agent coroutine processes the active Conversation. It may send zero or more assistant messages:

- `MessageBegin`
- zero or more `MessageFragment`
- `MessageEnd`

By default, the conversation ends when the agent tries to read after the completed user message. Most services are single-request-reply.
The agent gives the floor back to the user only by sending `RequestFollowUp` and then awaiting the next input event or iterating to the next message through `ConversationEndpoint.messages()`.

Allowed transitions:

- agent sends `RequestFollowUp` -> WaitingForNewMessage
- agent coroutine returns -> WaitingForNewConversation
- endpoint sends `ConversationEnded` -> WaitingForNewConversation
- endpoint closed -> Closed

Session asserts that no assistant message is left open when the agent coroutine returns.

### WaitingForNewMessage

Session sends `RequestFollowUp`.

Allowed client events:

- `MessageBegin` -> ReceivingMessageFragments
- `ConversationEnded` -> WaitingForNewConversation
- endpoint closed -> Closed

For microphone input, timeout while waiting for a follow-up sends `ConversationEnded`. The default lives under
`microphones.follow_up_timeout_seconds`, with per-device `follow_up_timeout_seconds` overrides. The legacy
`conversation.follow_up_timeout_seconds` value is still accepted as a fallback for old configs.

For websocket input, timeout while waiting for a follow-up sends `ConversationEnded`. The default lives under
`websocket.follow_up_timeout_seconds` and defaults to 60 seconds.

## Microphone Event Mapping

Microphone drivers expose raw audio input events to the server:

- `AudioStart`
- `AudioChunk`
- `AudioEnd`

The server sends microphone output/control events:

- `StartWakeWordListening`: silent transition into wake-word mode. Sent only when Session has emitted `WaitForNewConversation`.
- `StartFollowUpListening`: audible cue followed by transition into follow-up listening mode. Sent only when Session has emitted `RequestFollowUp`.
- `MessageEndCue`: audible cue after a complete user audio stream has been captured.
- `ConversationTimeoutCue`: audible cue when follow-up input times out.
- `AudioStart`, `AudioChunk`, `AudioEnd`: assistant audio playback.

Mapping:

| Session or conversation event | Microphone event/action |
|---|---|
| `WaitForNewConversation` | `StartWakeWordListening` |
| first microphone `AudioStart` | `NewConversation`, then `MessageBegin` |
| microphone `AudioChunk` stream | STT input; transcript fragments become `MessageFragment` |
| microphone `AudioEnd` and transcript end | `MessageEnd`, then `MessageEndCue` |
| assistant `MessageBegin` / `MessageFragment` / `MessageEnd` | playback `AudioStart` / `AudioChunk` / `AudioEnd` |
| `RequestFollowUp` | `StartFollowUpListening`, including the follow-up cue |
| follow-up microphone `AudioStart` / `AudioChunk` / `AudioEnd` | `MessageBegin` / `MessageFragment` / `MessageEnd`, then `MessageEndCue` |
| follow-up timeout | `ConversationTimeoutCue`, then `ConversationEnded` |

### Closed

The endpoint is gone and the Session is removed from the SessionManager.

## Agent API

Agents implement a conversation coroutine:

```python
async def run_conversation(
    self,
    conversation: Conversation,
    endpoint: ConversationEndpoint,
) -> None:
    ...
```

Simple agents can use assembled messages:

```python
async for message in endpoint.messages():
    await endpoint.send_message(TextMessage(text=f"reply: {message.text}"))
```

Agents that need another user message must explicitly request it:

```python
async for message in endpoint.messages():
    await endpoint.send_message(TextMessage(text=f"reply: {message.text}"))
    await endpoint.send(RequestFollowUp())
```

Streaming agents can use low-level message events:

```python
event = await endpoint.receive()
await endpoint.send(MessageBegin())
await endpoint.send(MessageFragment(text="partial"))
await endpoint.send(MessageEnd())
```

`ConversationEndpoint.receive()` raises `ConversationEnded` when the input side ends the conversation, or when the agent tries to read another message without first sending `RequestFollowUp`. `ConversationEndpoint.messages()` stops normally in the same case.

## Domain Agent Result Presentation

Domain agents return task result dictionaries to the orchestrator. A successful result can include
`final_reply_mode: "verbatim"` when its `text` is already the exact user-facing reply for a single-task
conversation. The orchestrator returns that text directly only when it is the sole successful task result.
For multi-task conversations, the final response writer still composes a combined reply and is instructed to
preserve verbatim result text.

## Websocket JSON Appendix

Each websocket text frame contains exactly one JSON event object. Event type names use snake_case.

Client to server:

```json
{"type":"session_attributes","attributes":{"user":"Maciek","area":"office"}}
{"type":"new_conversation","attributes":{"area":"kitchen"}}
{"type":"message_begin"}
{"type":"message_fragment","text":"cześć"}
{"type":"message_end"}
{"type":"conversation_ended"}
```

Server to client:

```json
{"type":"wait_for_new_conversation"}
{"type":"request_follow_up","timeout_seconds":60.0}
{"type":"message_begin"}
{"type":"message_fragment","text":"odpowiedź"}
{"type":"message_end"}
```

The websocket chat client sends `SessionAttributes` immediately after connecting. When it receives `WaitForNewConversation`, the next user text is sent as `NewConversation` plus a message stream. When it receives `RequestFollowUp`, the next user text is sent as only a message stream. If no follow-up text arrives before `websocket.follow_up_timeout_seconds`, the server ends the conversation and sends `WaitForNewConversation`.

Interactive prompts are:

- `connecting> `
- `waiting for new conversation> `
- `waiting for next message> `
- `waiting for server> `

Interactive chat suppresses the initial `WaitForNewConversation` status line because that event means
the server is ready, not that a conversation has ended. Later client and system status lines are printed
with client styling.

Batch mode uses `tools/batch-ws-client.sh` with repeated `--message` arguments. After all batch messages are sent, the client exits when the next wait-state event arrives, either `RequestFollowUp`, legacy `WaitForNewMessage`, or `WaitForNewConversation`.
