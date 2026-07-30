# T-008: Add a Central Agent Loop Response Timeout

## Status

- **Authority:** Draft implementation task; not a runtime contract
- **Status:** Draft
- **Audience:** Maintainers of the central Agent Loop and conversation error handling
- **Read when:** Planning or implementing model-response timeout behavior

## Objective

Prevent the central Agent Loop from waiting indefinitely for a model reply.

## Work needed

- Apply a 30-second deadline while waiting for a model reply.
- Interrupt the in-flight operation when no reply arrives within that deadline.
- Emit a user-facing apology for the interrupted request.
- Ensure the conversation exits the processing state cleanly.
- Add automated coverage and update applicable documentation.

This draft does not select an interruption mechanism or apology wording.

## Completion

The task is complete when a missing model reply is interrupted after 30 seconds,
the user receives an apology, processing terminates cleanly, and the behavior is
covered by tests.
