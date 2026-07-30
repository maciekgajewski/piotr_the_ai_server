# T-006: Verify Main Workhorse Model GPU Residency

## Status

- **Authority:** Completed implementation record; not a runtime contract
- **Status:** Complete; implemented 2026-07-30
- **Audience:** Maintainers of AI-server startup, model supervision, and Ollama integration
- **Read when:** Planning or implementing main-model readiness monitoring

## Objective

Ensure that the AI server verifies that its configured main workhorse model is
fully loaded into GPU memory.

## Implemented contract

- Top-level `agent.local_model` is the one protected workhorse. DSA-specific
  overrides are not protected.
- `server.run_server()` creates one process-wide `OllamaConnection` and injects
  it through the Agent factory. The Conversation bridge remains unaware of
  Ollama.
- Requests for the protected model force `keep_alive: -1`.
- Startup checks residency before microphone and websocket admission.
- Periodic checks run at a fixed 60-second interval.
- Each Ollama residency API operation has a fixed 60-second timeout.
- Full GPU residency means the matching Ollama `/api/ps` entry has valid
  positive `size`, valid `size_vram`, and `size_vram == size`.
- Ordinary Ollama requests may run concurrently. Repair takes an exclusive
  lease which waits for active requests and blocks new requests.
- Repair unloads competing Ollama models, unloads the protected model when it
  is already present, reloads it with `keep_alive: -1`, and rechecks residency.
- Failed startup repair aborts startup. Failed periodic repair stops the AI
  server with an error so its service manager can restart it.

## Implementation

- Shared transport and exclusive request gate:
  `ai_server/ollama_connection.py`
- Fixed-interval supervision: `ai_server/ollama_residency.py`
- Application ownership, startup gating, periodic failure propagation, and
  shutdown: `ai_server/server.py`
- Shared injection: `ai_server/agent/__init__.py`,
  `ai_server/domain_agents/__init__.py`, and `ai_server/ai_tools/__init__.py`
- Operational procedure:
  `docs/managing-docker-compose-services.md`

## Verification

- `tests/test_ollama_connection.py` covers pinning, healthy checks, ordered
  eviction/reload, failed repair, exclusive request coordination, terminal
  connection closure, and the fixed periodic interval.
- `tests/test_agent_factory.py` verifies that the same central connection
  reaches the orchestrator, DSAs, and legacy tools.
- `tests/test_server_lifecycle.py` verifies that failed startup checking blocks
  admission, that periodic failure interrupts suspended startup operations,
  and that failure after admission closes the runtime.
- Full automated checkpoint on 2026-07-30:
  `.venv/bin/python -m pytest -q` -> `800 passed`.

The live orchestrator/DSA suite was attempted with its configured
`qwen3:14b` model. The first model-backed case timed out after 240.57 seconds
because the running `piotr-ollama-1` reported the model at `100% CPU`; inside
that container `nvidia-smi` failed with `Failed to initialize NVML: Unknown
Error`. The host GPU remained healthy and visible. The run was stopped rather
than repeating the same four-minute environmental timeout across the remaining
cases. This is deployment evidence of the condition T-006 now rejects, not an
automated test failure in the residency implementation.
