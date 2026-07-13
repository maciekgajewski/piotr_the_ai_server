from __future__ import annotations

import asyncio

import pytest

import ai_server.microphones.drivers.box3_esphome as box3_driver
from ai_server.microphones.drivers.box3_esphome import OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL
from ai_server.microphones.drivers.box3_esphome import OPEN_MIC_PRE_ROLL_BYTES, OPEN_MIC_PRE_ROLL_SECONDS
from ai_server.microphones.drivers.box3_esphome import STOP_LISTENING_SERVICE
from ai_server.microphones.drivers.box3_esphome import VOICE_ASSISTANT_STOP_ACK_TIMEOUT_SECONDS
from ai_server.microphones.drivers.box3_esphome import Box3EsphomeMicrophone
from ai_server.microphones.messages import CueFinished, CueType, ListeningMode, ListeningStarted, ListeningStopped
from ai_server.microphones.messages import AudioChunk, AudioProgress, PlaybackBegin, PlaybackChunk, PlaybackEnd
from ai_server.microphones.messages import PlaybackFinished
from ai_server.microphones.messages import PlayCue, ResetWakeCandidate, SetVisualState, SpeechEnded, SpeechStarted
from ai_server.microphones.messages import StartListening, StopListening
from ai_server.microphones.messages import VisualState
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget
from tests.microphone_driver_conformance import assert_cue_contract, assert_listening_and_capture_contract
from tests.microphone_driver_conformance import assert_playback_contract


def _microphone() -> Box3EsphomeMicrophone:
    return Box3EsphomeMicrophone(
        context=MicrophoneContext(type="box3_esphome", name="box", area="office"),
        playback_target=PlaybackTarget(
            type="box3_esphome",
            name="box",
            address="box.local",
            api_key="secret",
        ),
        initial_silence_seconds=3,
        end_silence_seconds=3.0,
        speech_peak_threshold=500,
        post_speech_ignore_seconds=1,
    )


def test_visual_state_maps_to_private_firmware_service() -> None:
    async def run() -> None:
        microphone = _microphone()
        services = []

        async def execute(service: str) -> None:
            services.append(service)

        microphone._execute_api_service = execute
        await microphone.send_output_event(SetVisualState(VisualState.PROCESSING))
        assert services == ["set_visual_processing"]

    asyncio.run(run())


def test_duplicate_visual_state_repeats_only_the_idempotent_private_service() -> None:
    async def run() -> None:
        microphone = _microphone()
        services = []

        async def execute(service: str) -> None:
            services.append(service)

        microphone._execute_api_service = execute
        command = SetVisualState(VisualState.LISTENING)
        await microphone.send_output_event(command)
        await microphone.send_output_event(command)

        assert services == ["set_visual_listening", "set_visual_listening"]
        assert microphone._listen_id is None
        assert microphone._playback_id is None

    asyncio.run(run())


def test_nested_listening_generation_is_rejected_by_box3_internal_state() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"

        with pytest.raises(AssertionError):
            await microphone.send_output_event(StartListening("listen-2", ListeningMode.OPEN_MIC))

    asyncio.run(run())


def test_stale_stop_is_rejected_by_box3_internal_state() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"

        with pytest.raises(AssertionError):
            await microphone.send_output_event(StopListening("stale-listen", "cancelled"))

        assert microphone._listen_id == "listen-1"

    asyncio.run(run())


@pytest.mark.parametrize(
    "command",
    [PlaybackChunk("stale-playback", b"audio"), PlaybackEnd("stale-playback")],
)
def test_stale_playback_command_is_rejected_by_box3_internal_state(command) -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._playback_id = "playback-1"

        with pytest.raises(AssertionError):
            await microphone.send_output_event(command)

        assert microphone._playback_id == "playback-1"

    asyncio.run(run())


def test_playback_is_rejected_while_box3_listening_generation_is_active() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"

        with pytest.raises(AssertionError, match="disarmed microphone"):
            await microphone.send_output_event(PlaybackBegin("playback-1", 22050, 2, 1))

        assert microphone._playback_id is None
        assert microphone._listen_id == "listen-1"

    asyncio.run(run())


def test_start_open_mic_echoes_listening_correlation() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._finish_voice_assistant_run = _async_noop
        microphone._execute_api_service = _async_noop
        command = StartListening("listen-1", ListeningMode.OPEN_MIC)

        await microphone.send_output_event(command)

        assert await microphone._events.get() == ListeningStarted("listen-1", ListeningMode.OPEN_MIC)

    asyncio.run(run())


def test_stop_listening_echoes_correlation_and_clears_generation() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._finish_voice_assistant_run = _async_noop
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True

        await microphone.send_output_event(StopListening("listen-1", "accepted"))

        assert await microphone._events.get() == ListeningStopped("listen-1", "accepted")
        assert microphone._listen_id is None

    asyncio.run(run())


def test_cue_completion_echoes_cue_id() -> None:
    async def run() -> None:
        microphone = _microphone()
        services = []

        async def execute(service: str) -> None:
            services.append(service)

        microphone._execute_api_service = execute
        await microphone.send_output_event(PlayCue("cue-1", CueType.FOLLOW_UP_TIMEOUT))

        assert services == ["play_conversation_timeout_cue"]
        assert await microphone._events.get() == CueFinished("cue-1")

    asyncio.run(run())


def test_reset_candidate_requires_active_correlations() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._execute_optional_api_service = _async_noop

        await microphone.send_output_event(ResetWakeCandidate("listen-1", "utterance-1"))

    asyncio.run(run())


def test_capture_callbacks_echo_active_listen_and_new_utterance_ids() -> None:
    microphone = _microphone()
    microphone._listen_id = "listen-1"
    microphone._listening_mode = ListeningMode.OPEN_MIC

    microphone._queue_speech_started()
    started = microphone._events.get_nowait()
    assert isinstance(started, SpeechStarted)
    assert started.listen_id == "listen-1"

    microphone._queue_audio_chunk(b"audio")
    assert microphone._events.get_nowait() == AudioChunk("listen-1", started.utterance_id, b"audio")
    microphone._queue_speech_ended("completed")
    assert microphone._events.get_nowait() == SpeechEnded("listen-1", started.utterance_id, "completed")
    assert microphone._listen_id == "listen-1"


def test_open_mic_inter_segment_audio_emits_no_capture_events() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True

        for _ in range(OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL * 2):
            await microphone._handle_audio(b"\x00\x00")

        assert microphone._events.empty()
        assert microphone._utterance_id is None
        assert not microphone._speech_started

    asyncio.run(run())


def test_open_mic_idle_pre_roll_retains_only_the_configured_audio_tail() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True
        chunks = [sample.to_bytes(2, "little", signed=True) * 512 for sample in range(64)]

        for chunk in chunks:
            await microphone._handle_audio(chunk)

        retained = b"".join(microphone._pending_audio_chunks)
        assert OPEN_MIC_PRE_ROLL_SECONDS == 1.0
        assert microphone._pending_audio_byte_count == OPEN_MIC_PRE_ROLL_BYTES
        assert len(retained) == OPEN_MIC_PRE_ROLL_BYTES
        assert retained == b"".join(chunks)[-OPEN_MIC_PRE_ROLL_BYTES:]
        assert microphone._events.empty()
        assert microphone._audio_chunk_count == 0
        assert microphone._byte_count == 0

    asyncio.run(run())


def test_open_mic_pre_roll_trims_one_oversized_transport_chunk_to_exact_tail() -> None:
    async def run() -> None:
        microphone = _microphone()
        oversized_chunk = bytes(range(256)) * ((OPEN_MIC_PRE_ROLL_BYTES // 256) + 2)

        microphone._append_pending_audio_chunk(oversized_chunk)

        assert microphone._pending_audio_byte_count == OPEN_MIC_PRE_ROLL_BYTES
        assert b"".join(microphone._pending_audio_chunks) == oversized_chunk[-OPEN_MIC_PRE_ROLL_BYTES:]

    asyncio.run(run())


def test_open_mic_speech_flushes_only_bounded_pre_roll_after_speech_started() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True
        idle_chunks = [sample.to_bytes(2, "little", signed=True) * 512 for sample in range(64)]
        loud_chunk = b"\xff\x7f" * 512

        for chunk in idle_chunks:
            await microphone._handle_audio(chunk)
        await microphone._handle_audio(loud_chunk)
        assert microphone._speech_candidate_started_at is not None
        microphone._speech_candidate_started_at -= 1
        await microphone._handle_audio(loud_chunk)

        events = _drain_events(microphone)
        assert isinstance(events[0], SpeechStarted)
        audio_events = [event for event in events[1:] if isinstance(event, AudioChunk)]
        assert events[1:] == audio_events
        emitted_audio = b"".join(event.data for event in audio_events)
        expected_audio = b"".join([*idle_chunks, loud_chunk, loud_chunk])[-OPEN_MIC_PRE_ROLL_BYTES:]
        assert emitted_audio == expected_audio
        assert len(emitted_audio) == OPEN_MIC_PRE_ROLL_BYTES
        assert microphone._audio_chunk_count == len(audio_events)
        assert microphone._byte_count == OPEN_MIC_PRE_ROLL_BYTES
        assert microphone._pending_audio_byte_count == 0

    asyncio.run(run())


def test_stop_closes_capture_gate_and_drains_only_stopped_generation_events() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True
        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()

        async def finish() -> None:
            assert not microphone._capture_events_enabled
            stop_entered.set()
            await release_stop.wait()

        microphone._finish_voice_assistant_run = finish
        microphone._events.put_nowait(
            SpeechStarted("listen-1", "utterance-1", 16000, 2, 1)
        )
        microphone._events.put_nowait(AudioChunk("listen-1", "utterance-1", b"old"))
        microphone._events.put_nowait(AudioProgress("listen-1", "utterance-1", 1, 3))
        microphone._events.put_nowait(SpeechEnded("listen-1", "utterance-1", "completed"))
        microphone._events.put_nowait(CueFinished("unrelated-cue"))

        stop_task = asyncio.create_task(
            microphone.send_output_event(StopListening("listen-1", "accepted"))
        )
        await stop_entered.wait()
        await microphone._handle_audio(b"\xff\x7f" * 512)
        await microphone._handle_start("stale-conversation", 0, None, None)
        await microphone._handle_stop(False)
        release_stop.set()
        await stop_task

        assert _drain_events(microphone) == [
            CueFinished("unrelated-cue"),
            ListeningStopped("listen-1", "accepted"),
        ]
        assert not microphone._capture_events_enabled
        assert microphone._listen_id is None
        assert microphone._utterance_id is None
        assert microphone._pending_audio_byte_count == 0

    asyncio.run(run())


def test_stale_stream_recovery_cannot_expose_stopped_generation_capture_events() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True
        microphone._events.put_nowait(
            SpeechStarted("listen-1", "utterance-1", 16000, 2, 1)
        )
        microphone._events.put_nowait(AudioChunk("listen-1", "utterance-1", b"old"))

        async def mark_disconnected() -> None:
            return None

        async def finish_with_stale_stream_recovery() -> None:
            microphone._run_end_sent = True
            microphone._stream_done.clear()
            microphone._mark_disconnected = mark_disconnected
            await microphone._recover_stale_voice_assistant_stream_before_rearm()
            await microphone._handle_start("stale-conversation", 0, None, None)
            await microphone._handle_audio(b"\xff\x7f" * 512)

        microphone._finish_voice_assistant_run = finish_with_stale_stream_recovery
        await microphone.send_output_event(StopListening("listen-1", "accepted"))

        assert _drain_events(microphone) == [ListeningStopped("listen-1", "accepted")]
        assert microphone._stream_done.is_set()
        assert microphone._audio_ended
        assert not microphone._capture_events_enabled
        assert microphone._pending_audio_byte_count == 0

    asyncio.run(run())


def test_voice_assistant_stop_awaits_explicit_device_stop_before_run_end_without_disconnect() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._voice_assistant_run_active = True
        disconnected = False
        operations = []

        async def ensure_connected() -> None:
            return None

        def send_run_end_once() -> None:
            operations.append("run_end")
            microphone._run_end_sent = True

        async def execute_optional_service(service: str) -> bool:
            operations.append(service)
            await microphone._handle_stop(False)
            return True

        async def mark_disconnected() -> None:
            nonlocal disconnected
            disconnected = True

        microphone._ensure_connected = ensure_connected
        microphone._send_run_end_once = send_run_end_once
        microphone._execute_optional_api_service = execute_optional_service
        microphone._mark_disconnected = mark_disconnected

        await microphone._finish_voice_assistant_run()

        assert VOICE_ASSISTANT_STOP_ACK_TIMEOUT_SECONDS == 2.0
        assert operations == [STOP_LISTENING_SERVICE, "run_end"]
        assert not disconnected
        assert microphone._stream_done.is_set()

    asyncio.run(run())


def test_voice_assistant_stop_timeout_recovers_stale_stream(monkeypatch) -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._voice_assistant_run_active = True
        disconnect_count = 0
        operations = []

        async def ensure_connected() -> None:
            return None

        def send_run_end_once() -> None:
            operations.append("run_end")
            microphone._run_end_sent = True

        async def execute_optional_service(service: str) -> bool:
            operations.append(service)
            return True

        async def mark_disconnected() -> None:
            nonlocal disconnect_count
            disconnect_count += 1

        microphone._ensure_connected = ensure_connected
        microphone._send_run_end_once = send_run_end_once
        microphone._execute_optional_api_service = execute_optional_service
        microphone._mark_disconnected = mark_disconnected
        monkeypatch.setattr(box3_driver, "VOICE_ASSISTANT_STOP_ACK_TIMEOUT_SECONDS", 0.01)

        await microphone._finish_voice_assistant_run()

        assert operations == [STOP_LISTENING_SERVICE, "run_end"]
        assert disconnect_count == 1
        assert microphone._stream_done.is_set()
        assert microphone._audio_ended
        assert not microphone._run_end_sent

    asyncio.run(run())


def test_stop_listening_service_completes_before_message_end_cue() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True
        microphone._voice_assistant_run_active = True
        operations = []

        async def ensure_connected() -> None:
            return None

        def send_run_end_once() -> None:
            operations.append("run_end")
            microphone._run_end_sent = True

        async def execute_optional_service(service: str) -> bool:
            operations.append(service)
            await microphone._handle_stop(False)
            return True

        async def execute_service(service: str) -> None:
            operations.append(service)

        async def fail_if_disconnected() -> None:
            raise AssertionError("normal explicit stop must not disconnect")

        microphone._ensure_connected = ensure_connected
        microphone._send_run_end_once = send_run_end_once
        microphone._execute_optional_api_service = execute_optional_service
        microphone._execute_api_service = execute_service
        microphone._mark_disconnected = fail_if_disconnected

        await microphone.send_output_event(StopListening("listen-1", "accepted"))
        assert await microphone._events.get() == ListeningStopped("listen-1", "accepted")
        await microphone.send_output_event(PlayCue("cue-1", CueType.UTTERANCE_ACCEPTED))

        assert operations == [STOP_LISTENING_SERVICE, "run_end", "play_message_end_cue"]
        assert await microphone._events.get() == CueFinished("cue-1")

    asyncio.run(run())


def test_missing_stop_listening_service_is_rollout_safe(caplog) -> None:
    async def run() -> None:
        microphone = _microphone()

        async def execute_service(service: str) -> None:
            raise RuntimeError(f"ESPHome satellite API service not found: {service}")

        microphone._execute_api_service = execute_service
        assert not await microphone._execute_optional_api_service(STOP_LISTENING_SERVICE)

    with caplog.at_level("WARNING"):
        asyncio.run(run())

    assert "optional API service unavailable service=stop_listening" in caplog.text


def test_missing_stop_listening_service_uses_legacy_run_end_then_stop_callback() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._voice_assistant_run_active = True
        operations = []

        async def ensure_connected() -> None:
            return None

        async def execute_optional_service(service: str) -> bool:
            operations.append(f"missing:{service}")
            return False

        def send_run_end_once() -> None:
            operations.append("run_end")
            microphone._run_end_sent = True
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.create_task(microphone._handle_stop(False))
            )

        async def fail_if_disconnected() -> None:
            raise AssertionError("legacy stop callback must avoid disconnect recovery")

        microphone._ensure_connected = ensure_connected
        microphone._execute_optional_api_service = execute_optional_service
        microphone._send_run_end_once = send_run_end_once
        microphone._mark_disconnected = fail_if_disconnected

        await microphone._finish_voice_assistant_run()

        assert operations == [f"missing:{STOP_LISTENING_SERVICE}", "run_end"]
        assert microphone._stream_done.is_set()

    asyncio.run(run())


def test_new_generation_captures_normally_after_stopped_generation_is_drained() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._finish_voice_assistant_run = _async_noop
        microphone._execute_api_service = _async_noop
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True

        await microphone.send_output_event(StopListening("listen-1", "accepted"))
        assert await microphone._events.get() == ListeningStopped("listen-1", "accepted")
        await microphone.send_output_event(StartListening("listen-2", ListeningMode.OPEN_MIC))
        assert await microphone._events.get() == ListeningStarted("listen-2", ListeningMode.OPEN_MIC)
        await _start_detected_speech(microphone)

        events = _drain_events(microphone)
        assert isinstance(events[0], SpeechStarted)
        assert events[0].listen_id == "listen-2"
        assert all(
            event.listen_id == "listen-2"
            for event in events
            if isinstance(event, (SpeechStarted, AudioChunk, AudioProgress, SpeechEnded))
        )

    asyncio.run(run())


@pytest.mark.parametrize("mode", [ListeningMode.WAKE_WORD, ListeningMode.FOLLOW_UP])
def test_one_segment_mode_closes_capture_gate_before_late_callbacks(mode: ListeningMode) -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = mode
        microphone._capture_events_enabled = True
        microphone._queue_speech_started()
        started = microphone._events.get_nowait()
        assert isinstance(started, SpeechStarted)
        microphone._queue_audio_chunk(b"audio")
        microphone._queue_speech_ended("completed")
        _drain_events(microphone)

        assert not microphone._capture_events_enabled
        assert microphone._listen_id is None
        await microphone._handle_start("stale-conversation", 0, None, None)
        await microphone._handle_audio(b"\xff\x7f" * 512)
        await microphone._handle_stop(False)
        assert microphone._events.empty()

    asyncio.run(run())


def test_open_mic_progress_is_correlated_to_active_segment() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True

        await _start_detected_speech(microphone)
        started = microphone._events.get_nowait()
        assert isinstance(started, SpeechStarted)

        while microphone._audio_chunk_count < OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL:
            await microphone._handle_audio(b"\xff\x7f")

        events = _drain_events(microphone)
        progress = [event for event in events if isinstance(event, AudioProgress)]
        assert progress == [
            AudioProgress(
                "listen-1",
                started.utterance_id,
                OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL,
                OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL * 2,
            )
        ]
        assert all(
            event.listen_id == "listen-1" and event.utterance_id == started.utterance_id
            for event in events
            if isinstance(event, AudioChunk)
        )

    asyncio.run(run())


def test_open_mic_continuous_audio_starts_fresh_correlated_segment() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True

        await _start_detected_speech(microphone)
        first_started = microphone._events.get_nowait()
        assert isinstance(first_started, SpeechStarted)
        _drain_events(microphone)

        assert microphone._last_speech_at is not None
        microphone._last_speech_at -= microphone._end_silence_seconds
        await microphone._handle_audio(b"\x00\x00")
        ended_events = _drain_events(microphone)
        assert ended_events[-1] == SpeechEnded("listen-1", first_started.utterance_id, "completed")
        assert microphone._listen_id == "listen-1"
        assert microphone._utterance_id is None

        for _ in range(OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL):
            await microphone._handle_audio(b"\x00\x00")
        assert microphone._events.empty()

        await _start_detected_speech(microphone)
        second_segment_events = _drain_events(microphone)
        second_started = second_segment_events[0]
        assert isinstance(second_started, SpeechStarted)
        assert second_started.listen_id == "listen-1"
        assert second_started.utterance_id != first_started.utterance_id
        assert any(isinstance(event, AudioChunk) for event in second_segment_events[1:])
        assert all(
            event.listen_id == "listen-1" and event.utterance_id == second_started.utterance_id
            for event in second_segment_events[1:]
            if isinstance(event, (AudioChunk, AudioProgress))
        )

    asyncio.run(run())


def test_three_second_end_silence_allows_natural_wake_phrase_pause() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC
        microphone._capture_events_enabled = True
        await _start_detected_speech(microphone)
        _drain_events(microphone)

        assert microphone._last_speech_at is not None
        microphone._last_speech_at -= 2.9
        await microphone._handle_audio(b"\x00\x00")
        assert not any(isinstance(event, SpeechEnded) for event in _drain_events(microphone))
        assert microphone._speech_started

        microphone._last_speech_at -= 0.2
        await microphone._handle_audio(b"\x00\x00")
        assert any(isinstance(event, SpeechEnded) for event in _drain_events(microphone))

    asyncio.run(run())


def test_playback_completion_is_emitted_only_after_end() -> None:
    async def run() -> None:
        microphone = _microphone()
        writes = []

        class Stream:
            def write(self, data: bytes) -> None:
                writes.append(data)

        async def start(event: PlaybackBegin) -> None:
            microphone._playback_id = event.playback_id
            microphone._playback_stream = Stream()

        async def finish() -> None:
            microphone._playback_stream = None

        microphone._start_playback = start
        microphone._finish_playback = finish
        await microphone.send_output_event(PlaybackBegin("playback-1", 22050, 2, 1))
        await microphone.send_output_event(PlaybackChunk("playback-1", b"audio"))
        assert microphone._events.empty()
        await microphone.send_output_event(PlaybackEnd("playback-1"))

        assert writes == [b"audio"]
        assert await microphone._events.get() == PlaybackFinished("playback-1")

    asyncio.run(run())


async def _async_noop(*_args, **_kwargs) -> None:
    return None


async def _start_detected_speech(microphone: Box3EsphomeMicrophone) -> None:
    await microphone._handle_audio(b"\xff\x7f")
    assert microphone._speech_candidate_started_at is not None
    microphone._speech_candidate_started_at -= 1
    await microphone._handle_audio(b"\xff\x7f")
    assert microphone._speech_started


def _drain_events(microphone: Box3EsphomeMicrophone) -> list:
    events = []
    while not microphone._events.empty():
        events.append(microphone._events.get_nowait())
    return events


@pytest.mark.parametrize("mode", list(ListeningMode))
def test_box3_satisfies_reusable_listening_and_capture_contract(mode: ListeningMode) -> None:
    async def run() -> None:
        stimulus = _Box3Stimulus(_microphone())
        await stimulus.prepare()
        await assert_listening_and_capture_contract(stimulus, mode)

    asyncio.run(run())


def test_box3_satisfies_reusable_cue_contract() -> None:
    async def run() -> None:
        stimulus = _Box3Stimulus(_microphone())
        await stimulus.prepare()
        await assert_cue_contract(stimulus.microphone)

    asyncio.run(run())


def test_box3_satisfies_reusable_playback_contract() -> None:
    async def run() -> None:
        stimulus = _Box3Stimulus(_microphone())
        await stimulus.prepare()
        await assert_playback_contract(stimulus.microphone)
        assert stimulus.playback_bytes == [b"audio"]

    asyncio.run(run())


class _Box3Stimulus:
    def __init__(self, microphone: Box3EsphomeMicrophone) -> None:
        self.microphone = microphone
        self.playback_bytes: list[bytes] = []

    async def prepare(self) -> None:
        self.microphone._ensure_connected = _async_noop
        self.microphone._finish_voice_assistant_run = _async_noop
        self.microphone._start_wake_word_listening = _async_noop
        self.microphone._execute_api_service = _async_noop

        class Stream:
            def __init__(self, chunks: list[bytes]) -> None:
                self._chunks = chunks

            def write(self, data: bytes) -> None:
                self._chunks.append(data)

        async def start(event: PlaybackBegin) -> None:
            self.microphone._playback_id = event.playback_id
            self.microphone._playback_stream = Stream(self.playback_bytes)

        async def finish() -> None:
            self.microphone._playback_stream = None

        self.microphone._start_playback = start
        self.microphone._finish_playback = finish

    async def emit_speech(self, data: bytes, reason: str = "completed") -> None:
        self.microphone._queue_speech_started()
        self.microphone._queue_audio_chunk(data)
        self.microphone._queue_speech_ended(reason)
