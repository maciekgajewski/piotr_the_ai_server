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

4. When verifying lost-connection behavior, do a real lifecycle check:

- Start `ai_server.server` with an explicit temporary config that uses a non-Ollama agent such as `echo`, has `users: {Maciek: {}}`, sets `microphones.devices: []`, and binds a test port that is not in use.
- Start `tools/ai-server-chat.sh --user Maciek --area office ws://127.0.0.1:<port>/chat` in a PTY.
- Confirm the server logs accepted the websocket and session attributes.
- Kill only the temporary server process, for example with `kill -9 <pid>` when reproducing a hard loss.
- Poll the client and require a visible error plus nonzero exit.

```text
Connection lost: websocket closed.
CLIENT_EXIT:1
```

Clean up the temporary config and verify no temporary server/client process remains.

## Protocol Expectations

- The client sends `session_attributes` immediately after connecting.
- After `wait_for_new_conversation`, the client sends `new_conversation` and then the first message stream.
- After `wait_for_new_message`, the client sends only the next message stream.
- The interactive client exits with status `1` on established websocket loss; initial connection failures may still retry.
- Batch/scripted clients exit when their requested messages are complete or when a terminal protocol/connection condition is reached.

For state-machine details, read `docs/ai-server-conversation-protocol.md`.
