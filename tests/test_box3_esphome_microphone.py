from __future__ import annotations

import asyncio

import pytest

from ai_server.microphones.drivers.box3_esphome import Box3EsphomeMicrophone
from ai_server.microphones.messages import CueFinished, CueType, ListeningMode, ListeningStarted, ListeningStopped
from ai_server.microphones.messages import AudioChunk, PlaybackBegin, PlaybackChunk, PlaybackEnd, PlaybackFinished
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
