# Agent Instructions

- Do not run `sudo` commands from the agent terminal.
- When a task requires `sudo`, ask the user to run the exact command manually and wait for confirmation.
- Prefer one step at a time for ESP32-S3-BOX-3 setup work.
- Read the "Architecture decisions" section in README.md before architecture-related work.

Before writing code, interview me to remove ambiguity.

Rules:
- Ask one question at a time
- Do not assume architecture, libraries, APIs, or coding style
- If multiple valid approaches exist, present options with tradeoffs, as numbered list
- Explicitly list assumptions
- Do not implement until I say "proceed" or "make it so"
- Don't shy away from churn. Prioritize clean, systematic solutions even if the require more churn and deeper changes.

# File naming

- always add .sh suffix to shell scripts
- Python code used by tools belongs under `tools/lib/`; keep top-level `tools/` entries as shell wrappers or non-Python assets.

# AI server

- For AI server options that can be supplied by both config and command line, the command-line value always takes precedence over the config value.
- DSA stands for Domain Specific Agent
- A config file is required ot start the server. The config file always has to be provided by user in the command-line parameters, regardless if launched directly or trough a wrapper script. Do not hardcode config values in the code. Test tools are the only exception.
- Abstract components must stay sealed behind their abstract interfaces. DSAs, input modules, microphones, and similar pluggable components must expose behavior only through their interface methods; other parts of the system must not hardcode knowledge of concrete component types, prompts, commands, or shortcuts.


# Python coding guidelines

- Keep interfaces in interfaces.py, messages in messages.py, per module
- Keep imports on the top of the file. I don't like mid-function imports
- Do not cram code into __init__.py, it should be minimal, best empty
- If class uses logger, give it a instance member variable, _logger, with a prefix that identifies class type and the instance.
- Put reusable code in the utils/ module. When planning, check the module manifest for useful tools before reinventing them.
- For internal JSON-like dictionaries, omit keys with `None` values. Treat missing keys and null dictionary fields as the same unless an external protocol explicitly requires null.

# Error handling

When in doubt, kill the process. Use asserts to verify invariants and assumptions.
Catch only exceptions we know how to handle.

# DSA and Orchestrator behavior development

- There are behavior tests in orchestrator_and_dsa_tests/
- Run the entire suite using the currently used model after every change
- When fixing DSA or Orchestrator behavior, add tests to cover any corner cases


# Logging

- For AI server logging, include a stable per-instance context in log messages when concurrent sessions or connections can interleave. Prefer readable prefixes such as `Session[<id>]` or `WebsocketCommunicationEndpoint[<peer>]`.
- Each interaction between components, with external components and every state change should be logged on DEBUG, with enough details to allow for debugging.
- INFO level logs should be treated as user interface. They should be brief, and contain the most important information. Big object should be logged in abbreviated form.
- Each crucial event in every subsystem - each LLM request and reply, agent turn, call to external service, should be logged on INFO.
- When logging LLM interaction, make sure the model name, token count and duration are logged
