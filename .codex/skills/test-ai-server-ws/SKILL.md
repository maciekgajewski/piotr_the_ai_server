---
name: test-ai-server-ws
description: Test the Piotr AI server websocket conversation protocol using pytest, the real ai_server server, and the repo chat clients. Use when validating websocket sessions, typed conversation protocol events, ai_server.chat_client or tools/ai-server-chat.sh behavior, client disconnect/liveness handling, or scripted WS flows.
---

# Test AI Server WS

Use this skill to verify the websocket conversation path with both automated tests and, when behavior depends on terminal or connection lifecycle, a real server/client run.

## Core Workflow

1. Prefer the automated pytest coverage first:

```bash
.venv/bin/python -m pytest tests/test_websocket_server.py -q
```

2. For a normal scripted flow, run the batch websocket client against a server or test fixture. The current client option is `--area`, not `--location`.

```bash
.venv/bin/python -m ai_server.batch_ws_client \
  --user Maciek \
  --area office \
  --message "cześć" \
  --message "koniec" \
  ws://127.0.0.1:2137/chat
```

3. For terminal client behavior, run the real wrapper in a PTY:

```bash
tools/ai-server-chat.sh \
  --user Maciek \
  --area office \
  ws://127.0.0.1:2137/chat
```

4. When verifying long-running server work, do a real lifecycle check:

- Start a real `create_app(...)`/`WebsocketCommunicationEndpoint` server path with a slow non-Ollama agent, or start `ai_server.server` with an explicit temporary config if the needed agent is available there.
- Start `tools/ai-server-chat.sh --user Maciek --area office ws://127.0.0.1:<port>/chat` in a PTY.
- Confirm the server logs accepted the websocket and session attributes.
- Send a message, have the server delay longer than the client liveness interval, then send the reply.
- Require the client to stay connected, print the delayed reply, and return to the next wait prompt. Also require the server side to show no send/reset error.

```text
odpowiedź po opóźnieniu
Conversation ended; waiting for a new conversation.
```

Hard-kill checks are useful diagnostics, but do not treat them as the primary validation for long-query websocket health unless the current implementation explicitly supports that scenario and the live client actually exits nonzero.

Clean up the temporary config and verify no temporary server/client process remains.

## Protocol Expectations

- The client sends `session_attributes` immediately after connecting.
- After `wait_for_new_conversation`, the client sends `new_conversation` and then the first message stream.
- After `wait_for_new_message`, the client sends only the next message stream.
- The interactive client must stay connected while the server is legitimately busy. Connection failures and broken established connections exit without retrying.
- Batch/scripted clients exit when their requested messages are complete or when a terminal protocol/connection condition is reached.

For state-machine details, read `docs/ai-server-conversation-protocol.md`.
