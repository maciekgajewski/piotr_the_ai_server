from __future__ import annotations

import asyncio

import pytest

from ai_server.microphones.drivers.box3_esphome import OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL
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
        end_silence_seconds=0.9,
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

        for _ in range(OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL * 2):
            await microphone._handle_audio(b"\x00\x00")

        assert microphone._events.empty()
        assert microphone._utterance_id is None
        assert not microphone._speech_started

    asyncio.run(run())


def test_open_mic_progress_is_correlated_to_active_segment() -> None:
    async def run() -> None:
        microphone = _microphone()
        microphone._listen_id = "listen-1"
        microphone._listening_mode = ListeningMode.OPEN_MIC

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
