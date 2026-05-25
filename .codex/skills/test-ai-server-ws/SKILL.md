---
name: test-ai-server-ws
description: Test the Piotr AI server websocket conversation protocol using the repo's scripted chat client. Use when validating ai_server websocket sessions, typed conversation protocol events, the interrogator agent, or scripted WS client behavior.
---

# Test AI Server WS

Use this skill to verify the websocket conversation path end to end with `ai_server.chat_client` scripted mode.

## Core Workflow

1. Prefer the automated pytest coverage first:

```bash
.venv/bin/python -m pytest tests/test_websocket_server.py::test_scripted_websocket_client_drives_interrogator_flow
```

2. For a manual local run, configure the server with the `interrogator` agent and start it with the repo helper or Python module.

3. Drive the websocket client in scripted mode:

```bash
.venv/bin/python -m ai_server.chat_client \
  --user Maciek \
  --location office \
  --message "cześć" \
  --message "koniec" \
  ws://127.0.0.1:2137/chat
```

Expected output contains:

```text
Twoja wiadomość numer 1 to: cześć
Koniec konwersacji, wysłałeś 2 wiadomości.
```

## Protocol Expectations

- The client sends `session_attributes` immediately after connecting.
- After `wait_for_new_conversation`, the client sends `new_conversation` and then the first message stream.
- After `wait_for_new_message`, the client sends only the next message stream.
- In scripted mode, after all `--message` values are sent, the client exits when the next wait-state event arrives.

For state-machine details, read `docs/ai-server-conversation-protocol.md`.
