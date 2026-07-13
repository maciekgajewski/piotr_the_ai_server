# T-002: Fix Open-Mic Audio Progress Correlation

## Status

- **Authority:** Completed defect-remediation record
- **Audience:** Agents and maintainers working on the ESPHome microphone driver and Microphone Protocol conformance
- **Read when:** Fixing `audio event without active utterance_id`, changing Box3/Voice Preview open-mic audio callbacks, or resuming T-001 hardware validation
- **Unblocked:** T-001 end-to-end microphone and hardware acceptance testing on 2026-07-13

## Summary

Fix `Box3EsphomeMicrophone` so continuous open-mic audio received between speech segments does not attempt to emit a correlated `AudioProgress` event without an active `utterance_id`.

The defect was discovered during live Box3 hardware validation on 2026-07-13. It is in the shared Python ESPHome microphone driver, not in the flashed Box3 firmware. The same driver type is used for the configured Box3 and Voice Preview devices, so every device using `type: box3_esphome` in open-mic mode is potentially affected.

## Resolution

Completed on 2026-07-13.

- `_handle_audio()` now emits open-mic `AudioProgress` only while `_speech_started` represents an active capture segment.
- `_required_utterance_id()` remains unchanged and continues to fail the correlation invariant if capture state and correlation ever diverge.
- Dedicated Box3 tests drive `_handle_audio()` through idle inter-segment audio, correlated active progress, segment completion, continued transport audio, and a later segment with a fresh `utterance_id` under the same `listen_id`.
- No firmware change or flash was required.

Verification results:

```text
.venv/bin/python -m pytest tests/test_box3_esphome_microphone.py -q
21 passed in 0.18s

.venv/bin/python -m pytest tests/test_microphone_protocol.py tests/test_microphones.py tests/test_box3_esphome_microphone.py -q
71 passed in 0.30s

.venv/bin/python -m pytest -q
505 passed, 28 warnings in 1.52s

orchestrator_and_dsa_tests/run.sh --no-transcript
PASS 45/45 using qwen3:14b in 223.49s
```

The controlled foreground live run used the required explicit configuration paths. `box3-office` connected at `192.168.0.180`, executed `set_visual_idle`, and armed open-mic listening. Sequential rejected segments retained `listen_id=4b2a7ede-0aae-49ec-86f1-fe0d51d1a25d` while using fresh utterance IDs including `014b8da9-503d-4990-bc85-cfab625a1ac4`, `be28a3e0-4bde-41ca-8a02-108d443337d4`, and `a5bf9480-9405-4b15-9c43-1a48ab2e3aac`. Inter-segment audio crossed repeated 50-chunk progress boundaries without `audio event without active utterance_id` or `Task exception was never retrieved`. The server then stopped cleanly with `Ctrl-C`, and no AI-server process or container remained.

## Normative requirements

Read before implementation:

1. root `AGENTS.md` and the architecture decisions in `README.md`;
2. `docs/microphone-protocol.md`;
3. `docs/microphone-conversation-mapping.md`;
4. `docs/protocol-conformance-catalogue.md`;
5. this task.

Applicable requirements:

- `MP-ID-002`: every speech segment has a new associated `utterance_id`;
- `MP-ID-003`: stale events cannot mutate state;
- `MP-STATE-001`: `AudioChunk` and `AudioProgress` are valid only while the driver is `CAPTURING` an active segment;
- open-mic listening may contain zero or more sequential speech segments, and ordinary silence in `LISTENING` is normal;
- completing an open-mic segment returns to `LISTENING` under the same `listen_id`;
- protocol violations must fail an invariant rather than be silently repaired.

The assertion that rejects an audio event without an active `utterance_id` is correct. The defect is that `_handle_audio()` tries to construct the invalid event.

## Environment and runtime path used to reproduce

The live reproduction used:

- repository: `/home/maciek/piotr`;
- configuration: `/home/maciek/ai_server_config.yaml`;
- launcher: `tools/ai-server.sh`;
- services configuration: `config/services.env`;
- Box3 configuration entry: `box3-office`, `type: box3_esphome`, open-mic enabled;
- Box3 address from configuration: `piotr-box3-01-cbfaA8.local`;
- resolved Box3 address: `192.168.0.180`;
- flashed firmware hash: `0x96b56234`;
- server execution: Docker Compose `run --rm` with `./ai_server:/app/ai_server:ro`.

The Compose bind mount proves that the live process used the current working-tree Python source. The failure was not caused by a stale container image.

## Reproduction procedure

Before reading logs, confirm how the AI server is running. For this controlled reproduction, ensure no manual or Compose AI-server instance is already active:

```bash
pgrep -af 'ai_server.server|tools/ai-server|docker compose'
docker ps --format '{{.Names}}\t{{.Status}}'
```

Start the real server in the foreground with the required explicit configuration paths:

```bash
tools/ai-server.sh \
  --services-config config/services.env \
  --config /home/maciek/ai_server_config.yaml
```

Then:

1. Wait for the server to connect to `box3-office`, execute `set_visual_idle`, and start `start_open_mic_listening`.
2. Speak briefly near the Box3 so the local speech gate opens a segment. The utterance does not need to contain the configured wake phrase; ordinary rejected speech reproduces the boundary.
3. Stop speaking and wait for the configured `end_silence_seconds=0.9` to end the segment.
4. Leave the server running for several more seconds. The ESPHome voice-assistant stream remains active and continues sending audio while the driver has returned to `LISTENING` between segments.
5. Observe repeated unhandled task failures.
6. Stop the foreground server with `Ctrl-C`; do not leave it producing repeated callback failures.

Exact exception:

```text
ERROR asyncio: Task exception was never retrieved
future: <Task finished ... exception=AssertionError('audio event without active utterance_id')>
Traceback (most recent call last):
  File "/app/ai_server/microphones/drivers/box3_esphome.py", line 461, in _handle_audio
    utterance_id=self._required_utterance_id(),
  File "/app/ai_server/microphones/drivers/box3_esphome.py", line 773, in _required_utterance_id
    assert self._utterance_id is not None, "audio event without active utterance_id"
AssertionError: audio event without active utterance_id
```

Observed Box3 event order from the second reproduction on 2026-07-13:

```text
10:30:43.416 SpeechEnded emitted for the active utterance
10:30:43.416 manager transition CAPTURING -> LISTENING; utterance_id cleared
10:30:43.434 first inter-segment audio chunk received
10:30:44.716 AudioProgress construction asserts because no utterance_id is active
```

The failure repeats as continuous idle audio crosses subsequent progress intervals. Similar errors appeared for other active open-mic satellites because they share the same driver.

## Expected behavior

While an open-mic listening generation is armed:

- audio used for local speech detection and pre-roll may arrive continuously;
- inter-segment audio while the driver is `LISTENING` must not emit `AudioChunk` or `AudioProgress` because no `utterance_id` exists;
- speech detection must create a fresh `utterance_id` and emit `SpeechStarted` before correlated capture events;
- `AudioProgress` may be emitted only while that segment is active;
- after `SpeechEnded`, the driver must remain armed under the same `listen_id` and wait for the next segment without errors;
- the next segment must receive a different `utterance_id`.

## Actual behavior and root cause

`Box3EsphomeMicrophone._handle_audio()` increments `_audio_chunk_count` for continuous stream audio, including audio received while no speech segment is active. In open-mic mode it attempts to emit `AudioProgress` whenever the count advances by `OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL`, currently 50 chunks.

The progress condition checks the listening mode and chunk count, but it does not check that a speech segment is active. After `_queue_speech_ended()` clears `_utterance_id` and `_reset_open_mic_segment_state()` resets the counters, ordinary inter-segment audio starts incrementing the count again. At the next 50-chunk boundary, `_handle_audio()` calls `_required_utterance_id()` and correctly hits the invariant.

The repeated `Task exception was never retrieved` reports are a consequence of the invalid progress emission occurring inside asynchronous ESPHome callback tasks. Do not fix this by swallowing the assertion or inventing a correlation ID. Preserve fail-fast protocol enforcement and prevent the invalid event from being constructed.

## Test gap

The dedicated driver suite passed during diagnosis:

```text
.venv/bin/python -m pytest tests/test_box3_esphome_microphone.py -q
18 passed in 0.16s
```

This is a false negative. Existing tests primarily exercise `_queue_speech_started()`, `_queue_audio_chunk()`, and `_queue_speech_ended()` directly. The reusable driver stimulus also queues protocol events directly. It does not drive `_handle_audio()` across the real open-mic boundary:

```text
active speech -> SpeechEnded -> inter-segment continuous audio -> next speech
```

The defect therefore sits in a concrete-driver internal callback path that the abstract reusable harness does not cover. Per project direction, driver-internal behavior belongs in dedicated per-implementation tests.

## Implementation scope

Primary files:

- `ai_server/microphones/drivers/box3_esphome.py`;
- `tests/test_box3_esphome_microphone.py`.

Required work:

1. Restrict `AudioProgress` emission to an active, correlated speech segment.
2. Preserve continuous open-mic speech detection and pre-roll buffering while the driver is `LISTENING`.
3. Preserve the invariant in `_required_utterance_id()`; do not catch or suppress it.
4. Add dedicated Box3-driver tests that exercise `_handle_audio()` rather than only private event-queue helpers.
5. Confirm one completed/rejected segment leaves the same `listen_id` armed and a later segment receives a fresh `utterance_id`.
6. Re-run the real server reproduction and confirm there are no unhandled callback exceptions between segments.
7. Resume the T-001 Box3 visual and end-to-end hardware sequences only after the live defect is cleared.

No firmware change or flash is expected for this Python-driver defect. If investigation shows firmware changes are required, follow `.codex/skills/build-flash-esp-box/SKILL.md`, validate and compile before flashing, inspect generated `main.cpp`, and record the flash separately.

## Required dedicated regression tests

At minimum, add tests proving:

- inter-segment audio in open-mic `LISTENING` emits no `AudioChunk` or `AudioProgress` and does not assert;
- progress emitted during active capture includes the active `listen_id` and `utterance_id`;
- after `SpeechEnded`, further continuous audio does not reuse the completed `utterance_id`;
- a later detected segment under the same open-mic `listen_id` receives a fresh `utterance_id`;
- pre-roll audio for that later segment is still forwarded only after `SpeechStarted`;
- wake-word and follow-up one-segment behavior is unchanged.

Tests should use controlled audio-level or state stimuli and avoid timing-dependent sleeps where direct state control is sufficient.

## Verification

Run in this order:

```bash
.venv/bin/python -m pytest tests/test_box3_esphome_microphone.py -q
.venv/bin/python -m pytest tests/test_microphone_protocol.py tests/test_microphones.py tests/test_box3_esphome_microphone.py -q
.venv/bin/python -m pytest -q
orchestrator_and_dsa_tests/run.sh --no-transcript
```

Then repeat the live reproduction with the foreground Compose server. Evidence must show:

- `set_visual_idle` and open-mic listening start succeed;
- ordinary inter-segment audio produces no exception;
- at least two sequential speech segments use the same `listen_id` and different `utterance_id` values;
- rejected ordinary speech returns to idle open-mic listening;
- the server stops cleanly when the controlled run is complete.

## Acceptance criteria

- No `AudioChunk` or `AudioProgress` can be constructed without an active `utterance_id`.
- Continuous open-mic audio between segments is handled without an exception or synthetic correlation.
- `AudioProgress` still provides liveness during an active capture segment.
- Sequential open-mic segments retain the listening generation and use unique utterance IDs.
- Dedicated Box3 internal regression tests cover the real `_handle_audio()` boundary.
- Focused microphone tests, the entire pytest suite, and the full orchestrator/DSA behavior suite pass.
- Live Box3 reproduction no longer emits `audio event without active utterance_id` or `Task exception was never retrieved`.
- T-001 hardware validation can proceed and the result is recorded in its task and `notes/setting-up-esp-box.md`.

## Assumptions and non-goals

- The ESPHome transport is expected to stream audio continuously during one open-mic listening generation.
- Idle inter-segment audio is input to device-local speech gating and pre-roll; it is not a Microphone Protocol capture event.
- `AudioProgress` remains part of active-segment liveness and should not be removed globally.
- The task does not weaken correlation assertions or add compatibility aliases.
- The task does not change Conversation semantics, wake-phrase acceptance, visual ownership, or firmware rendering.
