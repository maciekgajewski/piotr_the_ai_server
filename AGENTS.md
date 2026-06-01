# Agent Instructions

- Do not run `sudo` commands from the agent terminal.
- When a task requires `sudo`, ask the user to run the exact command manually and wait for confirmation.
- Prefer one step at a time for ESP32-S3-BOX-3 setup work.
- Read the "Architecture decisions" section in README.md before architecture-related work.

Before writing code, interview me to remove ambiguity.

Rules:
- Ask one question at a time
- Do not assume architecture, libraries, APIs, or coding style
- If multiple valid approaches exist, present options with tradeoffs
- Explicitly list assumptions
- Do not implement until I say "proceed"

# File naming

- always add .sh suffix to shell scripts
- Python code used by tools belongs under `tools/lib/`; keep top-level `tools/` entries as shell wrappers or non-Python assets.

# AI server

- For AI server logging, include a stable per-instance context in log messages when concurrent sessions or connections can interleave. Prefer readable prefixes such as `Session[<id>]` or `WebsocketCommunicationEndpoint[<peer>]`.
- For AI server options that can be supplied by both config and command line, the command-line value always takes precedence over the config value.
- DSA stands for Domain Specific Agent


# Python coding guidelines

- Keep interfaces in interfaces.py, messages in messages.py, per module
- Keep imports on the top of the file. I don't like mid-function imports
- Do not cram code into __init__.py, it should be minimal, best empty
- If class uses logger, give it a instance member variable, _logger, with a prefix that identifies class type and the instance.
- Put reusable code in the utils/ module. When planning, check the module manifest for useful tools before reinventing them.
