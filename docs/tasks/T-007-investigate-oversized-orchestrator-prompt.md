# T-007: Investigate the Oversized Orchestrator Prompt

## Status

- **Authority:** Draft investigation task; not a runtime contract
- **Status:** Draft
- **Audience:** Maintainers of orchestrator and DSA prompt architecture
- **Read when:** Investigating or planning work on orchestrator prompt size

## Objective

Investigate the oversized orchestrator prompt and define the work required to
reduce or truncate it without breaking routing behavior.

## Work needed

- Measure the current prompt size and identify what contributes to it.
- Determine the required prompt-size boundary.
- Identify the behavior and information that must be preserved.
- Record the affected architecture, implementation, tests, and documentation.
- Produce an implementation-ready follow-up plan.

This task selects no truncation or prompt-construction solution.

## Completion

The task is complete when the cause, required boundary, preserved behavior, and
implementation scope are documented well enough for a separate implementation
decision.
