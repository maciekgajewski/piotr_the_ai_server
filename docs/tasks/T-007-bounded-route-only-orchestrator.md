# T-007: Make the Orchestrator a Bounded Route-Only Planner

## Status

- **Authority:** Implementation task; not a runtime contract
- **Status:** Ready for implementation
- **Audience:** Maintainers of orchestrator and DSA prompt architecture
- **Read when:** Changing orchestrator routing, task construction, DSA interfaces,
  or orchestrator prompt bounds
- **Binding architecture:** [Orchestrator and DSA Architecture](../orchestrator-dsa-architecture.md)

## Problem

The orchestrator planning prompt is too large and has no enforceable bound.
It currently includes the complete planning prompt and read-only query command
schema of every loaded DSA. The planner then constructs concrete DSA commands,
and central orchestrator code validates and repairs some Home Assistant and
media-player command shapes.

This is both a size problem and an ownership defect. The normative architecture
defines the orchestrator as a compact, DSA-agnostic router and task splitter.
Concrete commands, validation rules, tool semantics, aliases, and execution
shortcuts belong to the selected DSA.

Do not solve this by truncating the assembled prompt. Truncation would make
routing behavior depend on DSA registration order and could silently remove an
available domain or a required instruction.

## Investigation evidence

Measurements were taken on 2026-07-30 from the six DSAs configured in
`/home/maciek/ai_server_config.yaml`: `time`, `wikipedia`, `weather`,
`home_assistant`, `media_player`, and `system_status`.

| Prompt contribution | Characters | Words |
|---|---:|---:|
| Router template without DSA material | 1,748 | 204 |
| DSA planning prompts | 5,972 | 775 |
| DSA read-only query capability text | 2,856 | 293 |
| Complete fixed system prompt | 10,600 | 1,272 |

The largest fixed contributions are:

| DSA | Planning prompt | Query text | Total characters |
|---|---:|---:|---:|
| Media player | 2,467 | 824 | 3,291 |
| Home Assistant | 1,807 | 851 | 2,658 |
| Time | 639 | 323 | 962 |
| Weather | 589 | 247 | 836 |
| System status | 291 | 282 | 573 |
| Wikipedia | 179 | 329 | 508 |

The current planner uses `num_ctx=4096` and permits up to 768 generated tokens,
leaving at most 3,328 tokens for all input. A real local Ollama request using
the configured `qwen3:14b` tokenizer, the current fixed prompt, a representative
two-domain utterance (`Włącz światło w biurze, a potem puść muzykę w kuchni.`),
and representative `office` and `kitchen` Home Assistant area records consumed
2,570 input tokens. Only 758 input tokens remained before the response
reservation. The dynamic payload has no configured maximum because it can
include every Home Assistant area and alias plus active conversation context.

The current logging records token counts only after a successful planning
response. It does not reject an oversized request before inference and does not
identify which prompt component caused growth.

No AI-server process was running during the investigation. The historical
manual log contained no current `planning output` token records, and the
Compose AI-server container had been stopped for four weeks, so the explicit
measurement above is the authoritative current evidence.

## Selected fix

Implement the preferred two-stage flow from the normative architecture:

1. The orchestrator performs only domain routing, task splitting, dependency
   ordering, and generic conversation-context updates.
2. Each routed task carries the user's task text, not a DSA command object.
3. The selected DSA constructs and validates its own command or directly
   executes the task using the full scoped conversation context it already
   receives.
4. Each DSA exposes one compact routing summary through the `DomainAgent`
   interface. The summary says what the domain owns, including its read-only
   scope, but contains no command schema, tool schema, examples, aliases, or
   execution instructions.
5. Home Assistant area inventory and aliases are not sent to the route-only
   planner. Area resolution and validation move to the Home Assistant and media
   DSAs that own those concepts.
6. A clarification raised by a DSA is resumed through that same DSA. The
   orchestrator stores only the generic routed task, the DSA's clarification
   question, and an opaque DSA continuation value; it does not reconstruct a
   concrete command from the user's answer.

This is a clean interface migration. Do not retain the old prompt assembly or
central concrete-command validators as a compatibility path.

## Alternatives considered

1. **Increase `num_ctx`: rejected.** It increases prompt evaluation cost and
   hides unbounded growth without correcting DSA ownership.
2. **Truncate DSA material or inventory: rejected.** It makes available
   capabilities depend on ordering and silently changes behavior at the bound.
3. **Compress the existing concrete command schemas further: rejected as the
   final design.** It may provide temporary byte savings but leaves concrete DSA
   command construction and validation in the orchestrator.
4. **Route-only planner followed by DSA-owned interpretation: selected.** It
   removes the dominant prompt material, establishes a deterministic bound, and
   matches the normative architecture.

## Required bounded contract

The implementation must satisfy all of these bounds:

- The fixed route-only system prompt for the configured six DSAs is at most
  4,096 UTF-8 bytes.
- A representative full planning request, measured by the configured
  `qwen3:14b` model, is at most 1,536 input tokens.
- The planning request contains no unbounded inventory, user settings, voice
  profile paths, media defaults, credentials, DSA command schemas, or tool
  schemas.
- Prompt construction fails explicitly if a DSA routing summary or the combined
  fixed prompt exceeds its deterministic byte budget. It must never truncate.
- INFO logging reports model, prompt tokens, completion tokens, and duration
  for every planner request. DEBUG logging additionally reports the
  fixed-prompt byte count, dynamic-payload byte count, and per-DSA routing-summary
  byte counts.

The 1,536-token acceptance ceiling leaves 1,792 tokens of input headroom below
the current 3,328-token input allowance while preserving the configured
768-token maximum response.

## Generic route plan

Replace the command-producing planner schema with a DSA-agnostic task shape.
The exact Python representation belongs in `ai_server/orchestrator/messages.py`,
but its JSON binding must carry only:

```json
{
  "kind": "single_task|multi_task|followup|clarification_answer|chat",
  "confidence": 0.0,
  "tasks": [
    {
      "id": "t1",
      "domain": "available domain",
      "request": "the user text for this task",
      "depends_on": []
    }
  ],
  "context_updates": {
    "salient_entities": [],
    "active_domain": null
  },
  "needs_clarification": false,
  "clarification_question": null
}
```

The route planner may request clarification only when it cannot confidently
select or split domains. Missing device, media, place, query, or operation
details are DSA-owned clarification decisions made after routing.

`request` must preserve the user's wording for a single task. For a composite
utterance it must contain a faithful, self-contained split of the relevant
clause without inventing command parameters. Multiple clauses owned by the same
domain stay in one routed task unless there is a real dependency that requires
separate execution; this replaces the current concrete media-command merge.

Task IDs must be unique. Every dependency must name an earlier task in the same
plan; unknown, self, forward, and cyclic dependencies are invalid.

A route-planner clarification answer produces this same generic task shape. A
DSA clarification answer bypasses the route planner and is passed back through
the selected DSA's abstract continuation method with the original request,
question, latest answer, scoped conversation, active context, and an optional
opaque continuation value returned by that DSA.

## Implementation scope

### Domain interface and messages

- Define the generic routed-task and route-plan message types in
  `ai_server/orchestrator/messages.py`; keep interfaces in their module
  `interfaces.py`.
- Replace `DomainAgent.planning_prompt()`, `query_capabilities()`, and
  `query_capabilities_prompt()` with one bounded routing-summary method.
- Add an abstract DSA clarification-continuation method; its opaque continuation
  value must be JSON-serializable and must not be inspected by the orchestrator.
- Remove `QueryCapability` if it has no non-prompt consumer after the migration.
- Keep the orchestrator unaware of concrete DSA command types.
- Change DSA-provided known utterances to identify routing ownership without
  giving the orchestrator a concrete command to interpret.

### Orchestrator

- Replace the current modular command prompt with the bounded route-only prompt.
- Remove Home Assistant area inventory from the planner payload.
- Parse and validate only the generic route plan.
- Delete central Home Assistant and media-player command validation, repair,
  context-reference rewriting, merge, and area-canonicalization logic.
  Equivalent behavior that is still required moves behind the owning DSA
  interface.
- Preserve dependency ordering, partial execution of independent tasks,
  unsupported-domain handling, low-confidence fallback, final synthesis, and
  the single-verbatim-result fast path.
- Split pending clarification handling by owner: route ambiguity returns to the
  route-only planner, while DSA clarification returns directly to the same DSA.
  Delete the current central command-producing clarification prompt.
- Preserve deterministic known-utterance bypasses without exposing their
  concrete execution shape to the orchestrator.

### DSAs

- Adapt all six current DSAs to accept generic routed task text.
- Move concrete command construction and validation into the owning DSA.
- Keep or extend deterministic fast paths for common time, weather,
  system-status, media, and Home Assistant utterances so the architecture change
  does not require an unnecessary second LLM call.
- Move room/area alias resolution and unknown-area clarification into Home
  Assistant and media-player ownership.
- Keep each DSA's existing read-only query behavior discoverable through its
  compact routing summary and executable behind its interface.
- Ensure each DSA omits `None` fields in internal JSON-like dictionaries.

### Documentation

- Update [Orchestrator and DSA Architecture](../orchestrator-dsa-architecture.md)
  if implementation exposes any contract detail not already covered there.
- Update the documentation index and this task's status/evidence at completion.
- Record the before/after character and Qwen token measurements in this task.

## Behavior that must be preserved

- Correct routing for all currently supported single-domain requests.
- Correct splitting and dependency order for multi-domain requests.
- One routed task for multiple same-domain clauses that can be executed
  together, including media requests naming multiple rooms.
- Follow-up routing using active domain and salient entities.
- Router clarification for genuinely ambiguous domain selection.
- DSA clarification for missing or invalid domain-specific details.
- Current read-only state/configuration queries in every supporting DSA.
- Home Assistant and media room aliases, canonical area IDs, and unknown-area
  rejection.
- Known-utterance fast paths and the absence of an LLM planner call for those
  paths.
- Low-confidence and invalid-plan retry through the configured clarification
  model.
- Polish voice/text reply behavior and existing failure replies.

## Tests and acceptance

Add automated regression coverage for the ownership and size defect, not just
the resulting prompt text:

- A prompt-budget unit test with all six DSAs asserts the 4,096-byte fixed
  system-prompt ceiling and reports each contribution on failure.
- Oversized individual routing summaries and oversized combined prompts fail
  explicitly; registration order never changes which domains are represented.
- Planner payload tests prove that command schemas, tool schemas, Home Assistant
  inventory, aliases, user settings, voice profiles, credentials, and media
  defaults are absent.
- Parser tests accept the generic task schema and reject concrete `command`
  fields, unknown domains, duplicate task IDs, unknown/self/forward/cyclic
  dependencies, and invented task fields.
- Orchestrator tests prove it never branches on concrete DSA types or command
  shapes.
- Focused DSA tests cover task-text-to-command behavior, deterministic fast
  paths, clarification, query behavior, context-reference resolution,
  multi-room behavior, and area canonicalization in the owning module.
- Existing single-task, multi-task, follow-up, clarification, partial-failure,
  final-synthesis, and known-utterance tests remain green.
- Tests prove that DSA clarification answers return to the same DSA without a
  route-planner call and that opaque continuation values are passed through
  unchanged.
- The full `orchestrator_and_dsa_tests/run.sh --no-transcript` suite passes using
  the currently configured model.
- A checked-in measurement fixture uses the configured six DSAs, the
  representative utterance and two area names recorded in the investigation,
  and a real `qwen3:14b` request; it consumes at most 1,536 input tokens and the
  result is recorded here.
- Focused pytest, the full repository test suite, formatting/lint checks, and
  `git diff --check` pass.
- Independent implementation review is repeated until it reports no findings.

## Assumptions

- The configured planner context remains `num_ctx=4096` with
  `num_predict=768`; increasing the context window is not the fix.
- All currently configured DSAs migrate atomically. There is no mixed old/new
  interface mode.
- The normative architecture's preferred two-stage direction is already the
  selected architecture and does not require a new ADR.
- Prompt size is controlled by explicit construction and rejection, never by
  hidden truncation.

## Out of scope

- Changing the configured orchestrator or clarification models.
- Increasing the planner context window to hide prompt growth.
- Changing external websocket or microphone protocols.
- Adding new DSA domains or new end-user capabilities.

## Completion

T-007 is complete when the route-only architecture is implemented atomically,
all concrete command ownership is behind DSA interfaces, the prompt bounds are
enforced without truncation, automated and live-model acceptance evidence is
recorded, the full behavior suite passes, and independent review closes with no
findings.
