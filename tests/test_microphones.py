from __future__ import annotations

import asyncio
import logging

from ai_server.microphones.manager import MicrophoneManager
from ai_server.microphones.messages import CueFinished, CueType, ListeningMode, ListeningStarted, ListeningStopped
from ai_server.microphones.messages import PlaybackBegin, PlaybackChunk, PlaybackEnd, PlaybackFinished, PlayCue
from ai_server.microphones.messages import SetVisualState, StartListening, StopListening, SynthesizedAudioChunk
from ai_server.microphones.messages import SynthesizedAudioEnd, SynthesizedAudioStart
from ai_server.microphones.messages import VisualState
from ai_server.microphones.protocol import DriverState
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget
from ai_server.speech_to_text.messages import TextEnd, TextPartial


class FakeMicrophone:
    context = MicrophoneContext(type="test", name="fake", area="office")
    playback_target = PlaybackTarget(type="test", name="fake", address="fake", api_key="secret")

    def __init__(self) -> None:
        self.events = asyncio.Queue()
        self.commands = []
        self.close_count = 0

    async def send_output_event(self, command) -> None:
        self.commands.append(command)
        if isinstance(command, StartListening):
            self.events.put_nowait(ListeningStarted(command.listen_id, command.mode))
        elif isinstance(command, StopListening):
            self.events.put_nowait(ListeningStopped(command.listen_id, command.reason))
        elif isinstance(command, PlayCue):
            self.events.put_nowait(CueFinished(command.cue_id))
        elif isinstance(command, PlaybackEnd):
            self.events.put_nowait(PlaybackFinished(command.playback_id))

    async def wait_for_event(self):
        return await self.events.get()

    async def close(self) -> None:
        self.close_count += 1


class FakeLifecycle:
    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _manager(
    microphone: FakeMicrophone,
    *,
    open_mic: bool = False,
    tts=None,
) -> MicrophoneManager:
    return MicrophoneManager(
        microphones=[microphone],
        stt=FakeLifecycle(),
        tts=tts or FakeLifecycle(),
        agent=object(),
        follow_up_timeout_seconds=60,
        open_microphones={"fake"} if open_mic else set(),
    )


def test_new_conversation_listening_generation_has_fresh_correlation() -> None:
    microphone = FakeMicrophone()
    manager = _manager(microphone, open_mic=True)

    first = manager._new_conversation_listening_event(microphone)
    second = manager._new_conversation_listening_event(microphone)

    assert first.mode is ListeningMode.OPEN_MIC
    assert first.listen_id != second.listen_id


def test_manager_validates_command_and_driver_event_together() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        logger = logging.getLogger("test")
        command = StartListening("listen-1", ListeningMode.WAKE_WORD)

        await manager._send_command(microphone, SetVisualState(VisualState.IDLE), logger)
        await manager._send_command(microphone, command, logger)
        await manager._await_listening_started(microphone, command, logger)

        assert manager._protocols["fake"].snapshot.state is DriverState.LISTENING
        assert microphone.commands[:2] == [SetVisualState(VisualState.IDLE), command]

    asyncio.run(run())


def test_manager_stop_joins_matching_generation() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone, open_mic=True)
        logger = logging.getLogger("test")
        command = StartListening("listen-1", ListeningMode.OPEN_MIC)
        await manager._send_command(microphone, command, logger)
        await manager._await_listening_started(microphone, command, logger)

        await manager._stop_listening(microphone, "listen-1", "test", logger)

        assert manager._protocols["fake"].snapshot.state is DriverState.DISARMED
        assert microphone.commands[-1] == StopListening("listen-1", "test")

    asyncio.run(run())


def test_manager_cue_waits_for_correlated_completion() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        logger = logging.getLogger("test")

        await manager._play_cue(microphone, CueType.UTTERANCE_ACCEPTED, logger)

        assert manager._protocols["fake"].snapshot.state is DriverState.DISARMED
        assert isinstance(microphone.commands[-1], PlayCue)

    asyncio.run(run())


def test_open_mic_partial_candidate_commands_listening_before_final_text() -> None:
    """MP-OPENMIC-001: candidate feedback does not wait for final transcription."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone, open_mic=True)
        logger = logging.getLogger("test")
        stt = FakeStreamingSttSession()
        candidate = asyncio.Event()
        task = asyncio.create_task(manager._collect_open_mic_partials(stt, candidate, microphone, logger))

        stt.events.put_nowait(TextPartial("Ryszardzie jaka pogoda", 0.0, 0.5, 0.5))
        await asyncio.wait_for(candidate.wait(), timeout=1)

        assert microphone.commands == [SetVisualState(VisualState.LISTENING)]
        assert not task.done()
        stt.events.put_nowait(TextEnd())
        assert await task is True

    asyncio.run(run())


def test_follow_up_speech_start_timeout_stops_generation() -> None:
    """MAP-FOLLOWUP-002: timeout does not manufacture an empty message."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        logger = logging.getLogger("test")
        listen = StartListening("listen-1", ListeningMode.FOLLOW_UP)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)

        result = await manager._wait_for_speech_start(microphone, logger, listen.listen_id, 0.001)

        assert result is None
        assert microphone.commands[-1] == StopListening("listen-1", "speech_start_timeout")
        assert manager._protocols["fake"].snapshot.state is DriverState.DISARMED

    asyncio.run(run())


class FakeStreamingSttSession:
    def __init__(self) -> None:
        self.events = asyncio.Queue()

    async def receive_text(self):
        return await self.events.get()


def test_open_mic_idle_has_no_segment_timeout() -> None:
    """MP-TIMEOUT-001: silence before SpeechStarted remains normally armed."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone, open_mic=True)
        logger = logging.getLogger("test")
        listen = StartListening("listen-1", ListeningMode.OPEN_MIC)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)
        task = asyncio.create_task(
            manager._wait_for_speech_start(microphone, logger, listen.listen_id, None)
        )
        await asyncio.sleep(0.02)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert manager._protocols["fake"].snapshot.state is DriverState.LISTENING

    asyncio.run(run())


def test_protocol_command_and_event_logs_include_state_and_correlations(caplog) -> None:
    """MP-OBS-001: logs expose operation state and correlation context."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        logger = logging.getLogger("test.microphone.observability")
        command = StartListening("listen-1", ListeningMode.WAKE_WORD)
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            await manager._send_command(microphone, command, logger)
            await manager._await_listening_started(microphone, command, logger)
        assert "command=StartListening old_state=disarmed new_state=arming" in caplog.text
        assert "event=ListeningStarted old_state=arming new_state=listening" in caplog.text
        assert "listen_id='listen-1'" in caplog.text

    asyncio.run(run())


def test_unavailable_boundary_is_closed_and_protocol_state_is_recreated() -> None:
    """MP-ERROR-001: recovery recreates state instead of guessing."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        logger = logging.getLogger("test")
        command = StartListening("listen-1", ListeningMode.WAKE_WORD)
        await manager._send_command(microphone, command, logger)
        old_protocol = manager._protocols["fake"]

        await manager._recover_microphone_boundary(microphone, logger, RuntimeError("offline"))

        assert microphone.close_count == 1
        assert manager._protocols["fake"] is not old_protocol
        assert manager._protocols["fake"].snapshot.state is DriverState.DISARMED

    asyncio.run(run())


def test_tts_playback_is_correlated_and_waits_for_drain_completion() -> None:
    """MAP-OUTPUT-001: one synthesized reply becomes one ordered playback."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone, tts=FakeTts())
        await manager._speak_tts_text(microphone, "reply", logging.getLogger("test"), volume=None)

        playback = [
            command
            for command in microphone.commands
            if isinstance(command, (PlaybackBegin, PlaybackChunk, PlaybackEnd))
        ]
        assert len(playback) == 3
        assert isinstance(playback[0], PlaybackBegin)
        assert playback[1] == PlaybackChunk(playback[0].playback_id, b"audio")
        assert playback[2] == PlaybackEnd(playback[0].playback_id)
        assert manager._protocols["fake"].snapshot.state is DriverState.DISARMED

    asyncio.run(run())


class FakeTts(FakeLifecycle):
    async def synthesize(self, _text: str):
        yield SynthesizedAudioStart(22050, 2, 1, 0.7)
        yield SynthesizedAudioChunk(b"audio")
        yield SynthesizedAudioEnd()
