# T-003: Bound Open-Mic Pre-Roll and Eliminate the Accepted-Turn Stop Race

## Status

- **Authority:** Active defect-remediation task
- **Audience:** Agents and maintainers working on the shared ESPHome microphone driver, open-mic capture, and Microphone Protocol conformance
- **Read when:** Fixing unbounded open-mic pre-roll, `SpeechStarted invalid in stopping`, persistent satellite `ERROR` after an accepted utterance, or resuming T-001 Box3 hardware validation
- **Blocks:** T-001 end-to-end microphone and hardware acceptance testing
- **Discovered:** 2026-07-13 during live Box3 accepted-turn validation after T-002 was completed

## Summary

Fix `Box3EsphomeMicrophone` so idle open-mic transport audio retains only a deliberate, bounded speech pre-roll and can never create or deliver a new speech segment after the listening generation has begun stopping.

The live failure occurred after the server correctly recognized and accepted `Ryszardzie, która godzina?`. The manager commanded `PROCESSING` and `StopListening`, but the shared Python ESPHome driver detected another speech segment from audio accumulated during final transcription. Stale-stream recovery then reconnected with a queued `SpeechStarted`. The Microphone Protocol correctly rejected that event in `STOPPING`:

```text
AssertionError: SpeechStarted invalid in stopping; expected listening
```

The manager treated the invariant failure as microphone unavailability, and the flashed Box3 firmware correctly remained in its server-disconnected `ERROR` state. No reply was played.

This is a shared Python-driver defect, not evidence of a Box3 display-firmware defect. Every configured microphone using `type: box3_esphome` and open-mic mode is potentially affected, including Voice Preview units.

## Relationship to T-002

[T-002](T-002-box3-open-mic-audio-progress-correlation.md) fixed an earlier correlation defect: inter-segment audio attempted to emit `AudioProgress` without an active `utterance_id`. That fix is valid and remains complete.

T-003 is a distinct defect exposed by the next accepted-turn test:

- `_pending_audio_chunks` remains an unbounded `list[bytes]` while open-mic transport audio is idle;
- speech detection flushes the entire accumulated list into a new segment;
- an accepted utterance can leave enough transport audio and queued events for another `SpeechStarted` to be created while the manager is stopping the generation;
- stale-stream recovery reconnects the transport but does not make that event valid in the existing protocol generation.

Do not reopen T-002 or weaken its correlation assertions to address T-003.

## Normative requirements

Read before implementation:

1. root `AGENTS.md` and the architecture decisions in `README.md`;
2. `docs/microphone-protocol.md`;
3. `docs/ai-server-conversation-protocol.md`;
4. `docs/microphone-conversation-mapping.md`;
5. `docs/protocol-conformance-catalogue.md`;
6. this task.

Applicable requirements include:

- `MP-ID-002`: every speech segment has a new associated `utterance_id`;
- `MP-ID-003`: stale events cannot mutate state;
- `MP-STATE-001`: `SpeechStarted` is valid only in `LISTENING`, and audio events are valid only while `CAPTURING`;
- completing an open-mic segment returns to `LISTENING` only while the listening generation remains active;
- the manager must explicitly stop the open-mic generation before cue or assistant playback;
- timeout recovery must stop or discard the failed generation before creating another identifier;
- `MP-ERROR-001`: internal protocol violations fail an invariant and recreate the driver/session boundary rather than guessing recovery.

The observed `SpeechStarted invalid in stopping` assertion is correct. The defect is that the concrete driver created and retained the invalid event.

## Environment and runtime path

The controlled reproduction used:

- repository: `/home/maciek/piotr`;
- server configuration: `/home/maciek/ai_server_config.yaml`;
- services configuration: `config/services.env`;
- launcher: `tools/ai-server.sh`;
- microphone config entry: `box3-office`, `type: box3_esphome`, open-mic enabled;
- configured hostname: `piotr-box3-01-cbfaA8.local`;
- resolved address: `192.168.0.180`;
- flashed Box3 firmware hash: `0x96b56234`;
- current `end_silence_seconds`: `0.9`;
- current Box3 `speech_peak_threshold`: `250`;
- server execution: Docker Compose `run --rm` with the working-tree `./ai_server` bind-mounted read-only at `/app/ai_server`.

The bind mount confirms that the live process used the current working-tree Python source rather than stale code from a container image.

## Reproduction procedure

Before reading logs, determine how the AI server was started. For this controlled reproduction, ensure that neither a manual nor Compose AI-server instance is already active:

```bash
pgrep -af '[a]i_server.server|[t]ools/ai-server.sh'
docker ps --format '{{.Names}}\t{{.Status}}'
```

Start the real server in the foreground with explicit configuration paths:

```bash
tools/ai-server.sh \
  --services-config config/services.env \
  --config /home/maciek/ai_server_config.yaml
```

Then:

1. Wait for `box3-office` to connect, execute `set_visual_idle`, and start open-mic listening.
2. Confirm that the physical Box3 display is `IDLE`.
3. Say `Ryszardzie, która godzina?` continuously, without an intentional pause, for the control reproduction.
4. Observe that the visual response is delayed, then quickly changes `IDLE -> LISTENING -> PROCESSING/BUSY -> ERROR`.
5. Observe that no spoken reply is produced and `ERROR` remains displayed.
6. Capture the driver and manager logs through the invariant failure.
7. Stop the foreground server with `Ctrl-C`; do not leave a failed test instance running.

After the defect is fixed, also repeat the natural interaction with a pause after the wake phrase. The selected end-of-speech silence cutoff for that usability check is 3 seconds.

## Exact live evidence

The accepted-turn failure used listening generation:

```text
listen_id=ca855a30-eb9c-4355-94e2-27e2367d94f6
utterance_id=41cf3391-50bb-4d41-9fd8-f1b06d8f1bc5
```

Observed order on 2026-07-13:

```text
13:33:39.658 speech detected peak=1335
13:33:39.658 flushed speech pre-roll chunks=257 bytes=263168
13:33:39.659 SpeechStarted; LISTENING -> CAPTURING
13:33:41.860 end of speech detected silence_seconds=0.96 peak=46
13:33:41.860 SpeechEnded; CAPTURING -> LISTENING
13:33:46.487 partial STT detected the open-mic wake candidate
13:33:46.488 set_visual_listening executed
13:33:46.523 final STT started for 10.43 seconds / 333824 bytes of audio
13:33:46.972 speech detected again while final STT/acceptance was completing
13:33:46.972 flushed speech pre-roll chunks=162 bytes=165888
13:33:46.977 final utterance accepted; set_visual_processing commanded
13:33:47.005 StopListening entered STOPPING and sent VOICE_ASSISTANT_RUN_END
13:33:47.208 ESPHome stop had not arrived within the 0.20-second re-arm delay
13:33:47.208 stale ESPHome stream recovery disconnected the transport
13:33:48.467 transport reconnected with queued events still present
13:33:48.467 protocol received SpeechStarted while STOPPING
13:33:48.467 AssertionError: SpeechStarted invalid in stopping; expected listening
```

The first flush contained 263,168 bytes, approximately 8.2 seconds of 16 kHz mono PCM16 audio. Earlier live evidence showed the same unbounded behavior at larger scales:

- Box3 flushed 848 chunks / 868,352 bytes, approximately 27.1 seconds;
- Voice PE 02 flushed 366 chunks / 374,784 bytes;
- Voice PE 02 later accumulated and flushed 1,944 chunks / 1,990,656 bytes, approximately 62.2 seconds;
- shutdown evidence from an earlier run showed 1,006 chunks / 1,030,144 bytes pending on Voice PE 02.

These are not intentional speech pre-roll durations. They create large same-timestamp `AudioChunk` bursts, delay partial/final STT and visual commands, contaminate utterances with idle audio, consume unbounded memory, and enlarge the race window during accepted-turn shutdown.

## Current implementation behavior

Primary code is in `ai_server/microphones/drivers/box3_esphome.py`:

- `_pending_audio_chunks` is initialized as an unbounded `list[bytes]`;
- every non-speech chunk is appended in `_handle_audio()`;
- `_flush_pending_audio_chunks()` iterates and forwards the entire list after speech detection;
- `_observe_audio_level()` may call `_queue_speech_started()` whenever the local peak gate detects speech and `_speech_started` is false;
- `_finish_voice_assistant_run()` marks the run inactive and waits only `VOICE_ASSISTANT_REARM_DELAY_SECONDS` for the ESPHome stop event;
- stale-stream recovery disconnects when that event is late, but events already queued at the driver boundary can survive until the manager consumes them;
- `StopListening` has already moved the protocol to `STOPPING`, where a later `SpeechStarted` is invalid by design.

## Expected behavior

While an open-mic listening generation is actively `LISTENING`:

- continuous transport audio may feed local speech detection;
- only a small, deliberate, bounded window of audio immediately preceding speech detection may be retained as pre-roll;
- old idle audio must be evicted continuously rather than flushed into a future utterance;
- speech detection creates exactly one fresh `utterance_id` and queues `SpeechStarted` before correlated audio;
- after the manager begins `StopListening`, the driver must not detect, create, queue, or deliver another segment for that generation;
- stop/recovery must discard queued capture events belonging to the stopped generation before reconnecting or re-arming;
- the next listening generation must use a fresh `listen_id` and may create fresh utterance IDs only after its matching `ListeningStarted`.

The fix must preserve fail-fast protocol enforcement. Do not catch the assertion, silently ignore an invalid event at the abstract protocol layer, reuse an identifier, or reinterpret `STOPPING` as `LISTENING`.

## Required design decisions before implementation

Interview the user before coding and settle these points one at a time:

1. Select the intentional pre-roll duration or byte/chunk bound. It must be short enough to represent immediate speech context rather than accumulated idle history.
2. Decide whether the bound is a private driver constant or explicit microphone configuration. Avoid a hidden behavioral default if operators are expected to tune it.
3. Define the concrete-driver stop gate: identify the earliest point at which audio callbacks can no longer create segment events for the current generation.
4. Define where stale queued capture events are drained during a normal accepted-turn stop, distinct from failure recovery and later re-arm.

The user has already selected `end_silence_seconds=3.0` for the natural wake-word pause. That decision is separate from the pre-roll bound.

## Implementation scope

Expected primary files:

- `ai_server/microphones/drivers/box3_esphome.py`;
- `tests/test_box3_esphome_microphone.py`;
- microphone configuration schema/loading and `/home/maciek/ai_server_config.yaml` only if pre-roll becomes configurable;
- `/home/maciek/ai_server_config.yaml` for the selected `end_silence_seconds=3.0` runtime value;
- protocol documentation and conformance tests only if investigation demonstrates that the normative contract itself must change.

Required work:

1. Replace unbounded idle pre-roll retention with the approved bounded design.
2. Make normal stop atomic at the concrete driver boundary: no new capture event may be generated after stop begins.
3. Drain or invalidate already queued capture events for the stopped generation without weakening protocol assertions.
4. Preserve sequential open-mic segments while the generation remains actively listening.
5. Preserve active-segment `AudioProgress` correlation from T-002.
6. Change the selected runtime end-of-speech silence cutoff from 0.9 to 3 seconds after the control defect is fixed.
7. Add dedicated per-implementation tests for pre-roll eviction and stop/event ordering.
8. Repeat focused, full, behavioral, and live hardware verification.

No firmware change is currently expected. If investigation shows that ESPHome firmware behavior must change, follow `.codex/skills/build-flash-esp-box/SKILL.md`, validate and compile before flashing, inspect generated `main.cpp`, and record the deployment separately.

## Required dedicated regression tests

At minimum, add deterministic Box3-driver tests proving:

- idle open-mic audio retained for pre-roll never exceeds the approved bound;
- when speech starts, only the retained tail is flushed and ordering remains `SpeechStarted -> AudioChunk...`;
- a long idle period cannot enlarge the resulting utterance or create an unbounded event burst;
- sequential rejected segments under one active open-mic `listen_id` still receive distinct utterance IDs;
- active capture still emits correctly correlated `AudioProgress`;
- `StopListening` prevents later audio callbacks from queuing `SpeechStarted`, `AudioChunk`, `AudioProgress`, or `SpeechEnded` for the stopped generation;
- capture events queued immediately before or concurrently with stop cannot cross into `STOPPING` or a later listening generation;
- stale-stream disconnect/reconnect recovery cannot expose old-generation capture events;
- the next explicit `StartListening` uses a fresh `listen_id` and operates normally;
- wake-word and follow-up one-segment behavior remains unchanged;
- the 3-second silence cutoff permits a natural pause after the wake phrase without splitting the command, using controlled time rather than wall-clock sleeps.

Tests of concrete callbacks, buffering, and transport races belong in `tests/test_box3_esphome_microphone.py`, not only in the reusable abstract microphone harness.

## Verification

Run in this order:

```bash
.venv/bin/python -m pytest tests/test_box3_esphome_microphone.py -q
.venv/bin/python -m pytest tests/test_microphone_protocol.py tests/test_microphones.py tests/test_box3_esphome_microphone.py -q
.venv/bin/python -m pytest -q
orchestrator_and_dsa_tests/run.sh --no-transcript
```

Then repeat the controlled foreground hardware test. Evidence must show:

- no idle backlog larger than the approved pre-roll bound;
- wake candidate visual `LISTENING` occurs while capture is active;
- accepted utterance commands `PROCESSING` and stops the listening generation cleanly;
- no capture event appears after `StopListening` begins;
- no `SpeechStarted invalid in stopping`, unhandled callback exception, or persistent `ERROR` occurs;
- the assistant reply plays and the device returns to the protocol-selected next visual state;
- a natural pause of less than 3 seconds after `Ryszardzie` does not split the request;
- the server stops cleanly with `Ctrl-C` after the controlled run.

## Acceptance criteria

- Open-mic pre-roll has an explicit, tested upper bound.
- Long idle periods cannot increase captured utterance duration, memory use, or event burst size beyond that bound.
- Once stop begins, no capture event from that generation can be created or delivered.
- Stale events are discarded at the concrete driver boundary; abstract protocol invariants remain strict.
- Accepted Box3 turns complete through reply playback without entering persistent `ERROR`.
- Sequential rejected open-mic segments remain supported while the generation is active.
- T-002 audio-progress correlation behavior remains covered and passing.
- `end_silence_seconds=3.0` supports the natural wake-word pause and is verified on hardware.
- Focused microphone tests, the full Python suite, and the full orchestrator/DSA behavior suite pass.
- Live evidence is recorded in this task, T-001, and `notes/setting-up-esp-box.md` before T-003 is marked complete.

## Assumptions and non-goals

- The ESPHome transport may stream audio continuously during open-mic listening.
- Pre-roll is immediate context before detected speech, not an archive of all audio since the previous segment or stream start.
- The current invariant failure is correct evidence of driver drift; it must not be suppressed.
- The 3-second end-silence value is a confirmed user decision, not the proposed pre-roll duration.
- No firmware flash is required unless implementation investigation finds a firmware-side cause or contract change.
- This task does not change wake-phrase matching, Conversation ownership, visual ownership, or half-duplex playback semantics.
