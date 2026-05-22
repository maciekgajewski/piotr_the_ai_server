# Agent Instructions

- Do not run `sudo` commands from the agent terminal.
- When a task requires `sudo`, ask the user to run the exact command manually and wait for confirmation.
- Prefer one step at a time for ESP32-S3-BOX-3 setup work.

Before writing code, interview me to remove ambiguity.

Rules:
- Ask one question at a time
- Do not assume architecture, libraries, APIs, or coding style
- If multiple valid approaches exist, present options with tradeoffs
- Explicitly list assumptions
- Do not implement until I say "proceed"

# File naming

- always add .sh suffix to shell scripts

# AI server

- For AI server logging, include a stable per-instance context in log messages when concurrent sessions or connections can interleave. Prefer readable prefixes such as `Session[<id>]` or `WebsocketCommunicationEndpoint[<peer>]`.
- For AI server options that can be supplied by both config and command line, the command-line value always takes precedence over the config value.
