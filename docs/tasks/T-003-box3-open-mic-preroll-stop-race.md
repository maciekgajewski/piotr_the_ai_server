# T-003: Bound Open-Mic Pre-Roll and Eliminate the Accepted-Turn Stop Race

## Status

- **Authority:** Completed defect-remediation record
- **Audience:** Agents and maintainers working on the shared ESPHome microphone driver, open-mic capture, and Microphone Protocol conformance
- **Read when:** Fixing unbounded open-mic pre-roll, `SpeechStarted invalid in stopping`, persistent satellite `ERROR` after an accepted utterance, or resuming T-001 Box3 hardware validation
- **Unblocked:** T-001 end-to-end microphone and hardware acceptance testing on 2026-07-13
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
- current `end_silence_seconds`: `3.0`;
- current Box3 `speech_peak_threshold`: `2950`;
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

## Pre-fix implementation behavior

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

The user approved the following decisions one at a time on 2026-07-13 and then authorized implementation with `make it so`:

1. Pre-roll is 1.0 second, or 32,000 bytes at 16 kHz mono PCM16.
2. The bound is a private shared `box3_esphome` driver constant, not a per-device configuration option.
3. `StopListening` closes the concrete capture-event gate synchronously at command entry, before its first network operation or `await`.
4. After closing the gate, normal stop removes queued capture events for the matching `listen_id` while preserving unrelated protocol events. Stale-stream recovery remains a separate transport-recovery path.

The user has already selected `end_silence_seconds=3.0` for the natural wake-word pause. That decision is separate from the pre-roll bound.

## Implemented design

Implemented in the working tree on 2026-07-13:

- idle audio is retained in an exactly byte-bounded rolling tail using `OPEN_MIC_PRE_ROLL_SECONDS=1.0` and `OPEN_MIC_PRE_ROLL_BYTES=32000`;
- old bytes are evicted continuously, including partial eviction of the oldest chunk when necessary;
- `SpeechStarted` is queued before the retained `AudioChunk` tail;
- segment chunk/byte counters now count emitted segment audio rather than all idle transport audio;
- the capture-event gate closes before stop awaits ESPHome, and late audio or stream-start callbacks cannot create capture events;
- queued `SpeechStarted`, `AudioChunk`, `AudioProgress`, and `SpeechEnded` events for the stopped `listen_id` are drained at the concrete driver boundary;
- a later explicit `StartListening` reopens the gate for its fresh `listen_id`;
- `/home/maciek/ai_server_config.yaml` now selects `end_silence_seconds: 3.0`.
- STT transcript content logging is available only when the global
  `stt.log_transcripts` option is explicitly `true`; it defaults to `false`;
- enabled transcript diagnostics log every raw and preprocessed normal, partial,
  and final STT result at `DEBUG`, while `INFO` remains content-free;
- Faster Whisper startup performs one discarded, content-safe warm-up inference
  using `partial_window_seconds` of zero PCM and `partial_beam_size`; microphone
  sessions are armed only after model inference is ready;
- `/home/maciek/ai_server_config.yaml` enables `stt.log_transcripts: true` for
  the next controlled hardware run.
- normal ESPHome stop now waits up to 2.0 seconds for its acknowledgement before
  stale-stream recovery disconnects; the wait returns immediately when the
  acknowledgement arrives, and a genuinely stale stream still has bounded
  recovery.
- the shared satellite firmware API exposes `stop_listening`, backed by a
  device-side `voice_assistant.stop` action and a bounded wait for
  `voice_assistant.is_running` to become false;
- the driver invokes the explicit firmware stop while the device pipeline is
  still active, waits for the resulting device stop notification, and only then
  sends `VOICE_ASSISTANT_RUN_END` and completes `StopListening`; cue playback
  therefore cannot overlap normal disconnect recovery;
- the service is optional during staged rollout so unflashed Voice PE units use
  the existing bounded disconnect recovery until they receive the shared
  firmware update.

The 2.0-second timeout is an experimental working-tree change that failed hardware
acceptance. `aioesphomeapi.handle_stop` reports that the device stopped sending
audio; it is not an acknowledgement of `VOICE_ASSISTANT_RUN_END`. The satellite
did not emit that callback, so the timeout merely delayed the forced disconnect
until the `Momencik...` cue. Do not treat the longer timeout as the final fix.

The failed timeout experiment established the normative `MP-STOP-001` rule: a
run-end event is not a stop acknowledgement. The concrete driver and shared Box3
and Voice PE firmware now implement an explicit device stop. The observability
contract separately documents transcript diagnostics as `MP-OBS-003`.

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
8. Repeat focused and full Python verification plus live hardware verification.

The firmware-side cause is now confirmed. Follow `.codex/skills/build-flash-esp-box/SKILL.md`: validate and compile both shared firmware definitions, inspect generated `main.cpp`, flash only the selected test Box, and record the deployment separately. Voice PE units remain unflashed until Box acceptance succeeds.

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
```

Working-tree automated evidence from 2026-07-13:

- `tests/test_box3_esphome_microphone.py`: **35 passed**;
- focused microphone protocol, manager, and Box3 driver tests: **85 passed**;
- single-microphone config generator: **6 passed**;
- STT configuration and transcription diagnostics: **87 passed**;
- full Python suite: **530 passed** with 28 existing aiohttp warnings.

The orchestrator/DSA behavioral suite is not part of microphone-driver verification
and was intentionally not rerun for the firmware stop change.

Dedicated automated coverage maps to the required defects as follows:

| Required behavior or defect | Automated coverage |
|---|---|
| Exact 1-second bound across long idle audio and partial oldest-chunk eviction | `test_open_mic_idle_pre_roll_retains_only_the_configured_audio_tail` |
| One oversized transport chunk retains only its exact tail | `test_open_mic_pre_roll_trims_one_oversized_transport_chunk_to_exact_tail` |
| `SpeechStarted` precedes only the bounded retained audio | `test_open_mic_speech_flushes_only_bounded_pre_roll_after_speech_started` |
| Sequential segments retain one `listen_id` and use fresh `utterance_id` values | `test_open_mic_continuous_audio_starts_fresh_correlated_segment` |
| Active capture retains T-002 `AudioProgress` correlation | `test_open_mic_progress_is_correlated_to_active_segment` |
| Stop closes the gate before awaiting, drains queued capture events, preserves unrelated events, and rejects late audio/start/stop callbacks | `test_stop_closes_capture_gate_and_drains_only_stopped_generation_events` |
| Stale disconnect/reconnect recovery cannot expose old capture events | `test_stale_stream_recovery_cannot_expose_stopped_generation_capture_events` |
| A later explicit generation uses a fresh ID and captures normally | `test_new_generation_captures_normally_after_stopped_generation_is_drained` |
| One-shot wake-word and follow-up completion closes the gate before late callbacks | `test_one_segment_mode_closes_capture_gate_before_late_callbacks` |
| Wake-word, open-mic, and follow-up single-segment contracts remain valid | `test_box3_satisfies_reusable_listening_and_capture_contract` |
| A controlled pause below 3 seconds does not end capture, while silence beyond it does | `test_three_second_end_silence_allows_natural_wake_phrase_pause` |
| Transcript diagnostics default to content-safe metadata logging | `test_faster_whisper_stt_logs_metadata_without_transcript_text_by_default`, `test_faster_whisper_streaming_stt_emits_partial_and_final_text_without_logging_content_by_default` |
| Explicit diagnostics log raw and preprocessed normal, partial, and final STT results | `test_faster_whisper_stt_logs_raw_and_processed_transcript_when_enabled`, `test_faster_whisper_streaming_stt_logs_partial_and_final_transcripts_when_enabled` |
| Transcript diagnostics reject non-boolean configuration | `test_load_config_rejects_invalid_values` |
| Explicit firmware stop and its callback precede RUN_END, avoiding disconnect of the authoritative client | `test_voice_assistant_stop_awaits_explicit_device_stop_before_run_end_without_disconnect` |
| A missing stop acknowledgement still triggers bounded stale-stream recovery | `test_voice_assistant_stop_timeout_recovers_stale_stream` |
| Explicit device stop completes before the accepted-turn cue | `test_stop_listening_service_completes_before_message_end_cue` |
| Unflashed satellites without the new service remain rollout-safe | `test_missing_stop_listening_service_is_rollout_safe` |
| Unflashed satellites preserve legacy RUN_END then callback ordering | `test_missing_stop_listening_service_uses_legacy_run_end_then_stop_callback` |
| Isolated test configs retain all settings, select exactly one named microphone, reject invalid selection, and use private atomic output | `tests/test_single_microphone_config_tool.py` |
| Faster Whisper is warmed once with the configured partial path before microphones arm; output remains private and failures abort startup | `test_faster_whisper_start_warms_configured_partial_path_without_logging_output`, `test_faster_whisper_start_propagates_warmup_failure_and_remains_unstarted` |

### Live hardware result at 2026-07-13 15:23 UTC

The first post-fix accepted-turn run confirmed the original stale-event invariant failure is fixed, but it did not pass UX acceptance:

- Box3 flushed exactly 32 chunks / 32,000 bytes of pre-roll;
- wake candidate detection and the `LISTENING` visual occurred about 0.96 seconds after speech detection;
- the captured segment lasted 10.44 seconds, including 3.03 seconds of final silence;
- final STT took 0.45 seconds and the utterance was accepted;
- `StopListening` closed the capture gate before sending `VOICE_ASSISTANT_RUN_END`, and no later capture event reached `STOPPING`;
- ESPHome did not acknowledge stop within the existing 0.20-second grace period, so stale-stream recovery disconnected and reconnected the transport;
- the user observed a brief firmware-owned `ERROR` during that reconnect;
- `ListeningStopped` arrived 1.53 seconds after stop began, the assistant reply played successfully, and the device returned to `IDLE`;
- no `SpeechStarted invalid in stopping`, assertion, traceback, unhandled callback exception, or persistent `ERROR` occurred.

The run therefore proves the bounded pre-roll and atomic stop fix, while exposing two remaining UX issues: the configured 3-second end silence plus a low Box3 peak threshold can produce long captures, and the 0.20-second stop grace forces a visible reconnect on a normal accepted turn. T-003 remains open pending approved remediation and another live run.

### Transcript-enabled hardware result at 2026-07-13 18:58 UTC

The controlled phrase was `Ryszardzie, która godzina jest teraz w Jacksonville?`,
spoken with a natural pause after the wake phrase. The final raw and processed STT
transcripts both matched it exactly. The assistant returned the requested response.

The brief `ERROR` bitmap was independently reproduced and correlated to the same
normal-stop recovery path:

```text
18:58:49.034 speech detected
18:58:53.734 partial STT raw='Ryszardzie'
18:58:53.767 set_visual_listening
18:58:57.645 partial STT raw='Która godzina jest teraz w Jacksonville?'
18:59:00.338 end of speech detected silence_seconds=3.01
18:59:00.892 final STT raw='Ryszardzie, która godzina jest teraz w Jacksonville?'
18:59:00.922 set_visual_processing; StopListening entered STOPPING
18:59:00.923 VOICE_ASSISTANT_RUN_END sent with capture gate closed
18:59:01.124 0.20-second stop wait expired; stale-stream recovery disconnected
18:59:02.141 ListeningStopped arrived after reconnect
18:59:12.092 assistant playback finished
18:59:12.177 set_visual_idle
```

There was no protocol assertion, traceback, unhandled callback exception, or STT
error. The firmware-owned `ERROR` bitmap is therefore a deterministic consequence
of disconnecting the authoritative voice-assistant client after only 0.20 seconds,
while the satellite's normal stop acknowledgement takes about 1.22 seconds in this
run. T-003 remains open because the visible reconnect violates UX acceptance even
though the accepted turn and reply complete successfully.

### Two-second stop-wait hardware result at 2026-07-13 19:12 UTC

The final raw and processed transcript again matched
`Ryszardzie, która godzina jest teraz w Jacksonville?` exactly. The longer wait
did not prevent the disconnect:

```text
19:12:15.964 StopListening; VOICE_ASSISTANT_RUN_END sent; wait started (2.00s)
19:12:17.965 no device stop callback; stale-stream recovery disconnected
19:12:18.864 ListeningStopped; Momencik... cue started
19:12:19.128 Momencik... cue finished
19:12:27.129 assistant reply playback finished
19:12:27.217 set_visual_idle
```

The user observed `ERROR` while the cue played. This disproves the assumption that
the device normally acknowledges `VOICE_ASSISTANT_RUN_END` within a longer grace
period. `aioesphomeapi.subscribe_voice_assistant` documents `handle_stop` as a
notification that the device stopped sending audio, generated by a device stop
request or an audio-end message. `send_voice_assistant_event(RUN_END)` has no
correlated acknowledgement. A correct fix must not model that callback as a
`RUN_END` acknowledgement.

### Explicit-stop firmware deployment at 2026-07-13 19:35 UTC

- Box3 and all three Voice PE entrypoints passed ESPHome configuration validation.
- Box3 and representative Voice PE 02 firmware compiled successfully.
- Generated Box3 `main.cpp` contains the `stop_listening` API service, state-gate
  resets, `voice_assistant::StopAction`, and the bounded wait for
  `voice_assistant.is_running == false`; the dual wake-word models, server visual
  services, and GT911 red button remain present.
- Box3 build hash `0xa2dac250` was uploaded OTA to `192.168.0.180`; the device
  rebooted and served ESPHome API version `2026.4.5` with 5 entities and 9 user
  services.
- Voice PE firmware was validated and compiled but deliberately not flashed. It
  will be deployed only after the Box3 live acceptance run succeeds.

### First explicit-stop hardware result at 2026-07-13 19:36 UTC

The requested sentence was recognized exactly as
`Ryszardzie, która godzina jest teraz w Jacksonville?`, and the response played,
but the user again observed `ERROR` immediately before the accepted-turn cue.

```text
19:36:25.989 set_visual_processing; StopListening; RUN_END sent
19:36:26.252 stop_listening service completed
19:36:28.253 no device stop callback; stale recovery disconnected
19:36:29.328 reconnect completed; ListeningStopped; cue began
```

`RUN_END` had already made `voice_assistant.is_running` false before the firmware
service executed, so `voice_assistant.stop` was a no-op and could not emit the
required callback. The approved correction reverses this order: explicit device
stop, device-stop callback, then RUN_END. Subsequent hardware tests use an ad-hoc
config containing only the tested microphone.

### Corrected stop-order hardware result at 2026-07-13 19:48 UTC

The isolated runtime config contained only `box3-office`. The accepted turn
completed without any `ERROR` bitmap or transport disconnect:

```text
19:48:11.336 StopListening entered; explicit stop requested
19:48:11.696 stop_listening service completed
19:48:11.704 device-stop callback observed
19:48:11.704 RUN_END sent; ListeningStopped; cue began
```

The user did not see `LISTENING`. The first streaming partial took 8.19 seconds
and misrecognized the wake phrase as `Wreszcie.`; the next rolling partial window
contained only `w Jacksonville.`. Final STT recognized
`W Ryszardzie. Która godzina jest teraz w Jacksonville?` and therefore moved
directly from `IDLE` to `PROCESSING`. The approved remediation warms Faster
Whisper before microphones arm so the first real partial does not pay one-time
inference initialization latency.

### Warmed-STT acceptance result at 2026-07-13 19:55 UTC

The final controlled run again used `/tmp/ai-server-box3-office.yaml`, containing
only `box3-office`. Startup warmed Faster Whisper with the configured four-second
partial window before the microphone manager armed the Box. The warm-up started
at `19:53:48` and completed before the first listening generation began.

The requested Jacksonville turn began at `19:55:45`. Hardware and runtime
evidence showed:

- speech detection flushed exactly 32 chunks / 32,000 bytes, proving the
  one-second pre-roll bound remained effective;
- the user observed prompt `LISTENING`, followed by the normal accepted-turn
  states, and described the result as perfect;
- the assistant response played completely, playback finished at `19:56:17`,
  and the device returned to `IDLE`;
- no transport disconnect, brief or persistent `ERROR`, protocol assertion,
  unhandled callback exception, or post-stop capture defect was observed;
- the isolated foreground server stopped cleanly with `Ctrl-C` after the run.

This closes the cold-first-partial UX defect and satisfies T-003 hardware
acceptance. Voice PE units intentionally remain on their prior firmware until
their separate staged rollout.

## Acceptance criteria

- Open-mic pre-roll has an explicit, tested upper bound.
- Long idle periods cannot increase captured utterance duration, memory use, or event burst size beyond that bound.
- Once stop begins, no capture event from that generation can be created or delivered.
- Stale events are discarded at the concrete driver boundary; abstract protocol invariants remain strict.
- Accepted Box3 turns complete through reply playback without entering persistent `ERROR`.
- Sequential rejected open-mic segments remain supported while the generation is active.
- T-002 audio-progress correlation behavior remains covered and passing.
- `end_silence_seconds=3.0` supports the natural wake-word pause and is verified on hardware.
- Focused microphone tests and the full Python suite pass. Orchestrator/DSA behavior tests are outside microphone-driver verification.
- Live evidence is recorded in this task, T-001, and `notes/setting-up-esp-box.md` before T-003 is marked complete.

## Assumptions and non-goals

- The ESPHome transport may stream audio continuously during open-mic listening.
- Pre-roll is immediate context before detected speech, not an archive of all audio since the previous segment or stream start.
- The current invariant failure is correct evidence of driver drift; it must not be suppressed.
- The 3-second end-silence value is a confirmed user decision, not the proposed pre-roll duration.
- The explicit-stop fix changes the shared satellite firmware contract; only the selected Box3 is flashed for initial hardware acceptance.
- This task does not change wake-phrase matching, Conversation ownership, visual ownership, or half-duplex playback semantics.
