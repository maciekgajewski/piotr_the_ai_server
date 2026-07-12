from __future__ import annotations

import asyncio
import logging

from ai_server.microphones.manager import MicrophoneManager
from ai_server.microphones.messages import CueFinished, CueType, ListeningMode, ListeningStarted, ListeningStopped
from ai_server.microphones.messages import PlaybackBegin, PlaybackChunk, PlaybackEnd, PlaybackFinished, PlayCue
from ai_server.microphones.messages import AudioChunk, ResetWakeCandidate, SetVisualState, SpeechEnded, SpeechStarted
from ai_server.microphones.messages import StartListening, StopListening, SynthesizedAudioChunk
from ai_server.microphones.messages import SynthesizedAudioEnd, SynthesizedAudioStart
from ai_server.microphones.messages import VisualState
from ai_server.microphones.protocol import DriverState
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget
from ai_server.speech_to_text.messages import TextEnd, TextFragment, TextPartial


class FakeMicrophone:
    context = MicrophoneContext(type="test", name="fake", area="office")
    playback_target = PlaybackTarget(type="test", name="fake", address="fake", api_key="secret")

    def __init__(self) -> None:
        self.events = asyncio.Queue()
        self.commands = []
        self.trace = []
        self.close_count = 0

    async def send_output_event(self, command) -> None:
        self.commands.append(command)
        self.trace.append(("microphone", command))
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


class ScriptedStreamingSttSession(FakeStreamingSttSession):
    def __init__(self, final_text: str) -> None:
        super().__init__()
        self.final_text = final_text
        self.audio = []
        self.closed = False

    async def send_audio(self, chunk) -> None:
        self.audio.append(chunk)

    async def end_audio(self) -> None:
        return None

    async def transcribe_final(self) -> str:
        return self.final_text

    async def close(self) -> None:
        self.closed = True


class ScriptedStreamingStt(FakeLifecycle):
    def __init__(self, sessions: list[ScriptedStreamingSttSession]) -> None:
        self.sessions = sessions

    async def create_streaming_session(self, _microphone_name: str) -> ScriptedStreamingSttSession:
        return self.sessions.pop(0)


class ScriptedSttSession:
    def __init__(self, fragments: tuple[str, ...]) -> None:
        self.events = asyncio.Queue()
        for fragment in fragments:
            self.events.put_nowait(TextFragment(fragment))
        self.events.put_nowait(TextEnd())

    async def send_audio(self, _chunk) -> None:
        return None

    async def end_audio(self) -> None:
        return None

    async def receive_text(self):
        return await self.events.get()

    async def close(self) -> None:
        return None


class ScriptedStt(FakeLifecycle):
    def __init__(self, fragments: tuple[str, ...]) -> None:
        self.fragments = fragments

    async def create_session(self, _microphone_name: str) -> ScriptedSttSession:
        return ScriptedSttSession(self.fragments)


class RecordingEndpoint:
    def __init__(self, trace: list) -> None:
        self.events = []
        self.trace = trace

    async def send_to_session(self, event) -> None:
        self.events.append(event)
        self.trace.append(("session", event))


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


def test_open_mic_acceptance_has_exact_visual_stop_cue_and_message_order() -> None:
    """MP-OPENMIC-003/MAP-OPENMIC-001: final acceptance is forwarded exactly once."""

    async def run() -> None:
        microphone = FakeMicrophone()
        stt_session = ScriptedStreamingSttSession("Ryszardzie jaka pogoda")
        manager = _manager(microphone, open_mic=True)
        manager._stt = ScriptedStreamingStt([stt_session])
        endpoint = RecordingEndpoint(microphone.trace)
        logger = logging.getLogger("test")
        listen = StartListening("listen-1", ListeningMode.OPEN_MIC)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)
        microphone.trace.clear()

        microphone.events.put_nowait(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1))
        microphone.events.put_nowait(AudioChunk("listen-1", "utterance-1", b"audio"))
        microphone.events.put_nowait(SpeechEnded("listen-1", "utterance-1", "completed"))
        stt_session.events.put_nowait(TextPartial("Ryszardzie jaka", 0.0, 0.5, 0.5))
        stt_session.events.put_nowait(TextEnd())

        captured = await manager._capture_open_mic_utterance(
            microphone, endpoint, logger, listen.listen_id
        )

        assert captured.text_fragments == ("jaka pogoda",)
        assert [type(item).__name__ for _, item in microphone.trace] == [
            "SetVisualState",
            "SetVisualState",
            "StopListening",
            "PlayCue",
            "NewConversation",
            "MessageBegin",
            "MessageFragment",
            "MessageEnd",
        ]
        assert microphone.trace[0] == ("microphone", SetVisualState(VisualState.LISTENING))
        assert microphone.trace[1] == ("microphone", SetVisualState(VisualState.PROCESSING))
        assert sum(type(event).__name__ == "NewConversation" for event in endpoint.events) == 1

    asyncio.run(run())


def test_wake_word_acceptance_sets_processing_and_finishes_cue_before_message() -> None:
    """MAP-INPUT-001: accepted new input follows the complete normative order."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        manager._stt = ScriptedStt(("jaka pogoda",))
        endpoint = RecordingEndpoint(microphone.trace)
        logger = logging.getLogger("test")
        listen = StartListening("listen-1", ListeningMode.WAKE_WORD)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)
        microphone.trace.clear()
        microphone.events.put_nowait(
            SpeechStarted("listen-1", "utterance-1", 16000, 2, 1, "Ryszardzie")
        )
        microphone.events.put_nowait(AudioChunk("listen-1", "utterance-1", b"audio"))
        microphone.events.put_nowait(SpeechEnded("listen-1", "utterance-1", "completed"))

        captured = await manager._capture_utterance(
            microphone,
            endpoint,
            logger,
            starts_new_conversation=True,
            timeout_seconds=None,
            listen_id=listen.listen_id,
        )

        assert captured.text_fragments == ("jaka pogoda",)
        assert [type(item).__name__ for _, item in microphone.trace] == [
            "SetVisualState",
            "SetVisualState",
            "PlayCue",
            "NewConversation",
            "MessageBegin",
            "MessageFragment",
            "MessageEnd",
        ]
        assert microphone.trace[0] == ("microphone", SetVisualState(VisualState.LISTENING))
        assert microphone.trace[1] == ("microphone", SetVisualState(VisualState.PROCESSING))

    asyncio.run(run())


def test_follow_up_acceptance_sets_processing_without_new_conversation_cue() -> None:
    """MAP-FOLLOWUP-001: follow-up text stays in the existing Conversation."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        manager._stt = ScriptedStt(("jutro",))
        endpoint = RecordingEndpoint(microphone.trace)
        logger = logging.getLogger("test")
        listen = StartListening("listen-1", ListeningMode.FOLLOW_UP)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)
        microphone.trace.clear()
        microphone.events.put_nowait(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1))
        microphone.events.put_nowait(SpeechEnded("listen-1", "utterance-1", "completed"))

        await manager._capture_utterance(
            microphone,
            endpoint,
            logger,
            starts_new_conversation=False,
            timeout_seconds=10,
            listen_id=listen.listen_id,
        )

        assert [type(item).__name__ for _, item in microphone.trace] == [
            "SetVisualState",
            "SetVisualState",
            "MessageBegin",
            "MessageFragment",
            "MessageEnd",
        ]
        assert not any(isinstance(event, PlayCue) for event in microphone.commands)
        assert not any(type(event).__name__ == "NewConversation" for event in endpoint.events)

    asyncio.run(run())


def test_open_mic_rejection_resets_candidate_and_idle_without_rearming() -> None:
    """MP-OPENMIC-002/MAP-INVARIANT-002: rejected text stays private."""

    async def run() -> None:
        microphone = FakeMicrophone()
        first_session = ScriptedStreamingSttSession("ordinary speech")
        second_session = ScriptedStreamingSttSession("")
        manager = _manager(microphone, open_mic=True)
        manager._stt = ScriptedStreamingStt([first_session, second_session])
        endpoint = RecordingEndpoint(microphone.trace)
        logger = logging.getLogger("test")
        listen = StartListening("listen-1", ListeningMode.OPEN_MIC)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)
        microphone.trace.clear()

        microphone.events.put_nowait(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1))
        microphone.events.put_nowait(SpeechEnded("listen-1", "utterance-1", "completed"))
        first_session.events.put_nowait(TextPartial("Ryszardzie", 0.0, 0.5, 0.5))
        first_session.events.put_nowait(TextEnd())
        task = asyncio.create_task(
            manager._capture_open_mic_utterance(microphone, endpoint, logger, listen.listen_id)
        )

        async def rejection_visible() -> bool:
            return any(isinstance(command, ResetWakeCandidate) for command in microphone.commands)

        for _ in range(100):
            if await rejection_visible():
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("candidate rejection commands were not emitted")

        rejection_commands = [
            command
            for command in microphone.commands
            if isinstance(command, (ResetWakeCandidate, SetVisualState, StartListening))
        ]
        assert rejection_commands[-2:] == [
            ResetWakeCandidate("listen-1", "utterance-1"),
            SetVisualState(VisualState.IDLE),
        ]
        assert sum(isinstance(command, StartListening) for command in microphone.commands) == 1
        assert endpoint.events == []
        assert manager._protocols["fake"].snapshot.listen_id == "listen-1"

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

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
