# T-006: Verify Main Workhorse Model GPU Residency

## Status

- **Authority:** Draft implementation task; not a runtime contract
- **Status:** Draft
- **Audience:** Maintainers of AI-server startup, model supervision, and Ollama integration
- **Read when:** Planning or implementing main-model readiness monitoring

## Objective

Ensure that the AI server verifies that its configured main workhorse model is
fully loaded into GPU memory.

## Work needed

- Perform the verification during AI-server startup.
- Repeat the verification periodically, with a target interval of one minute.
- Detect when the model is not fully loaded into GPU memory.
- Define and perform an appropriate corrective action when verification fails.
- Add operational logging and automated coverage for the resulting behavior.

No corrective mechanism is selected by this draft.

## Completion

The task is complete when startup and periodic verification are implemented,
failure causes corrective action, and the behavior is covered by tests and
documented operationally.
