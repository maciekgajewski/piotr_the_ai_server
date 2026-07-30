# T-008: Add a Central Agent Loop Response Timeout

## Status

- **Authority:** Completed implementation record; not a runtime contract
- **Status:** Complete; implemented 2026-07-30
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

## Implemented behavior

- `AgentLoopConfig.response_timeout_seconds` defaults to 30 seconds.
- The deadline applies independently to each model request and covers the
  complete cloud/local selection and fallback operation.
- Tool execution and processing-update callbacks are outside the deadline.
- Deadline expiry cancels and drains the in-flight model operation.
- A timeout returns the existing user-facing failure reply,
  `Model się zesrał`, with `end_conversation=True`, so the current conversation
  leaves processing and terminates.
- Timeout-specific warning logging identifies the turn, cloud model, local
  model, and configured deadline.

The lower-level `request_timeout_seconds` remains separate. It bounds each HTTP
request but cannot enforce the complete model-response deadline across fallback
and racing requests.

## Automated evidence

- `tests/test_agent_loop.py` verifies the 30-second default and positive-finite
  validation.
- The timeout test uses a short injected deadline, verifies both cloud and local
  in-flight requests are cancelled, checks the timeout-specific log, and checks
  the terminal failure reply.
- A separate test verifies a lower-level HTTP timeout is not misreported as
  expiry of the central response deadline.
- The processing-update/tool test uses a deadline shorter than its tool
  execution to verify tools are not included in the model-response deadline.
- The complete repository suite passes: `810 passed`.

## Completion

The task is complete: a missing model reply is interrupted after the default
30-second deadline, the selected existing failure reply is returned, processing
terminates cleanly, and automated tests cover the behavior.
