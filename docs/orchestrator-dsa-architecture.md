# Orchestrator and DSA Architecture

## Document status

- **Authority:** Normative architecture
- **Audience:** Agents changing orchestrator or DSA planning, routing, ownership, prompts, or execution boundaries
- **Read when:** Planning or implementing orchestrator or DSA architecture changes

The orchestrator is a compact router and task splitter. It should decide which configured Domain Specific Agent owns each part of a user utterance, preserve cross-task ordering, and keep only generic conversation context needed for routing.

DSAs own domain knowledge. Exact utterances, planning prompts, command shapes, tool semantics, aliases, and execution shortcuts must come through the `DomainAgent` interface. Do not add central orchestrator tables or branches that know the concrete commands, prompts, or shortcuts of Home Assistant, media, weather, Wikipedia, time, or system-status agents.

Keep orchestrator prompts short. The planning request should not include user settings, voice profile paths, media defaults, credentials, or other DSA-private state. Pass only routing context such as user, area, server location/timezone, active context, and compact DSA-provided capabilities. The selected DSA receives the full `Conversation` when it runs and can read the settings it owns.

The route-only planner contract is:

- The planner emits only generic tasks with `id`, `domain`, `request`, and
  `depends_on`. It owns routing, task splitting, dependencies, and generic
  routing context; it never emits DSA commands.
- Each selected DSA constructs and validates its own command shape from the
  routed request and scoped conversation context.
- Each DSA exposes one bounded routing summary through `DomainAgent`. Command
  schemas, tool schemas, aliases, examples, and inventory do not belong in
  that summary or the planning request.
- A router clarification returns to the route-only planner. A clarification
  raised by a DSA returns directly to that same DSA with the original routed
  task, question, answer, scoped conversation context, and optional opaque
  JSON continuation.
- Prompt construction rejects an individual routing summary over 512 UTF-8
  bytes or a combined fixed prompt over 4,096 UTF-8 bytes. It never truncates
  or drops a domain to fit.
- After orchestrator or DSA behavior changes, run
  `orchestrator_and_dsa_tests/run.sh --no-transcript` using the currently
  configured model, then the focused pytest suite for touched modules.
