# Orchestrator and DSA Architecture

The orchestrator is a compact router and task splitter. It should decide which configured Domain Specific Agent owns each part of a user utterance, preserve cross-task ordering, and keep only generic conversation context needed for routing.

DSAs own domain knowledge. Exact utterances, planning prompts, command shapes, tool semantics, aliases, and execution shortcuts must come through the `DomainAgent` interface. Do not add central orchestrator tables or branches that know the concrete commands, prompts, or shortcuts of Home Assistant, media, weather, Wikipedia, time, or system-status agents.

Keep orchestrator prompts short. The planning request should not include user settings, voice profile paths, media defaults, credentials, or other DSA-private state. Pass only routing context such as user, area, server location/timezone, active context, and compact DSA-provided capabilities. The selected DSA receives the full `Conversation` when it runs and can read the settings it owns.

Preferred direction for future prompt-size work:

- First planner call: use a small, DSA-agnostic prompt for domain routing, task splitting, dependencies, and minimal shared slots such as explicit area.
- DSA execution: let the selected DSA construct and validate its own command shape from the task and conversation context.
- DSA capability summaries: expose compact routing/query summaries through `DomainAgent` methods instead of injecting full DSA command schemas into every orchestrator call.
- Validation: after orchestrator or DSA behavior changes, run `orchestrator_and_dsa_tests/run.sh --no-transcript` using the currently configured model, then the focused pytest suite for touched modules.
