from __future__ import annotations

import asyncio
import logging

import pytest

from ai_server.conversations.bridge import BridgeSettings
from ai_server.conversations.contexts import ConversationMedium, InputConversationContext
from ai_server.conversations.messages import AssistantAbortReason, AssistantSinkTerminalResult, ConversationEnded
from ai_server.conversations.messages import ContextRejectionCode, ConversationEndReason, FollowUpRequestCommitted
from ai_server.conversations.messages import FollowUpTimedOut
from ai_server.conversations.messages import InputSessionClosed, UserMessage
from ai_server.microphones.conversation_adapter import VoiceInputConversation, VoiceInputSession
from ai_server.microphones.conversation_adapter import VoiceAssistantSink, VoiceSessionState
from ai_server.microphones.interfaces import MicrophoneUnavailable
from ai_server.microphones.manager import CapturedUtterance, MicrophoneManager
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


class DelayedPlaybackMicrophone(FakeMicrophone):
    async def send_output_event(self, command) -> None:
        self.commands.append(command)
        self.trace.append(("microphone", command))
        if isinstance(command, StartListening):
            self.events.put_nowait(ListeningStarted(command.listen_id, command.mode))
        elif isinstance(command, StopListening):
            self.events.put_nowait(ListeningStopped(command.listen_id, command.reason))
        elif isinstance(command, PlayCue):
            self.events.put_nowait(CueFinished(command.cue_id))


class PlaybackCommitBarrierMicrophone(FakeMicrophone):
    def __init__(self) -> None:
        super().__init__()
        self.playback_committed = asyncio.Event()
        self.release_playback = asyncio.Event()

    async def send_output_event(self, command) -> None:
        self.commands.append(command)
        self.trace.append(("microphone", command))
        if isinstance(command, PlaybackBegin):
            self.playback_committed.set()
            await self.release_playback.wait()
        elif isinstance(command, PlaybackEnd):
            self.events.put_nowait(PlaybackFinished(command.playback_id))


class CueCommitBarrierMicrophone(FakeMicrophone):
    def __init__(self) -> None:
        super().__init__()
        self.cue_committed = asyncio.Event()
        self.release_cue = asyncio.Event()

    async def send_output_event(self, command) -> None:
        if not isinstance(command, PlayCue):
            await super().send_output_event(command)
            return
        self.commands.append(command)
        self.trace.append(("microphone", command))
        self.cue_committed.set()
        await self.release_cue.wait()
        self.events.put_nowait(CueFinished(command.cue_id))


class ListeningCommitBarrierMicrophone(FakeMicrophone):
    def __init__(self) -> None:
        super().__init__()
        self.listening_committed = asyncio.Event()
        self.release_listening = asyncio.Event()

    async def send_output_event(self, command) -> None:
        if not isinstance(command, StartListening):
            await super().send_output_event(command)
            return
        self.commands.append(command)
        self.trace.append(("microphone", command))
        self.listening_committed.set()
        await self.release_listening.wait()
        self.events.put_nowait(ListeningStarted(command.listen_id, command.mode))


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
        bridge_settings=BridgeSettings(1.0, 0.1),
        microphone_assistant_text_buffers={"fake": 100},
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
    """MAP-INVARIANT-002: timeout does not manufacture an empty message."""

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


def test_follow_up_timeout_uses_event_deadline_and_orders_cues_before_end() -> None:
    """MAP-FOLLOWUP-001/002: the voice adapter owns the complete timeout sequence."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        manager._microphone_follow_up_timeouts["fake"] = 0.001
        session = VoiceInputSession(
            manager=manager,
            microphone=microphone,
            assistant_text_buffer_characters=100,
        )
        conversation = VoiceInputConversation(
            session=session,
            manager=manager,
            microphone=microphone,
            context=InputConversationContext(
                conversation_id="conversation-1",
                input_session_id="session-1",
                medium=ConversationMedium.VOICE,
                area="office",
            ),
            initial_message=UserMessage("hello"),
            assistant_text_buffer_characters=100,
        )
        token = await conversation.request_follow_up()
        conversation.acknowledge_follow_up_ready(token)

        assert isinstance(await conversation.receive_control(), FollowUpTimedOut)
        trace_types = [type(item).__name__ for _, item in microphone.trace]
        assert trace_types == [
            "SetVisualState",
            "PlayCue",
            "StartListening",
            "StopListening",
            "PlayCue",
            "SetVisualState",
        ]
        cues = [item for _, item in microphone.trace if isinstance(item, PlayCue)]
        assert [cue.cue_type for cue in cues] == [CueType.FOLLOW_UP_READY, CueType.FOLLOW_UP_TIMEOUT]
        timeout_cue_index = next(
            index
            for index, (_, item) in enumerate(microphone.trace)
            if isinstance(item, PlayCue) and item.cue_type is CueType.FOLLOW_UP_TIMEOUT
        )
        idle_index = microphone.trace.index(("microphone", SetVisualState(VisualState.IDLE)))
        assert timeout_cue_index < idle_index
        assert not any(isinstance(command, StartListening) for command in microphone.commands[3:])
        await conversation.cleanup()

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
    """MP-OPENMIC-003/MAP-INVARIANT-001: final acceptance is forwarded exactly once."""

    async def run() -> None:
        microphone = FakeMicrophone()
        stt_session = ScriptedStreamingSttSession("Ryszardzie jaka pogoda")
        manager = _manager(microphone, open_mic=True)
        manager._stt = ScriptedStreamingStt([stt_session])
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
            microphone, logger, listen.listen_id
        )

        assert captured.text_fragments == ("jaka pogoda",)
        assert [type(item).__name__ for _, item in microphone.trace] == [
            "SetVisualState",
            "SetVisualState",
            "StopListening",
            "PlayCue",
        ]
        assert microphone.trace[0] == ("microphone", SetVisualState(VisualState.LISTENING))
        assert microphone.trace[1] == ("microphone", SetVisualState(VisualState.PROCESSING))

    asyncio.run(run())


def test_wake_word_acceptance_sets_processing_and_finishes_cue_before_message() -> None:
    """MAP-INVARIANT-001: accepted new input follows the complete normative order."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        manager._stt = ScriptedStt(("jaka pogoda",))
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
        ]
        assert microphone.trace[0] == ("microphone", SetVisualState(VisualState.LISTENING))
        assert microphone.trace[1] == ("microphone", SetVisualState(VisualState.PROCESSING))

    asyncio.run(run())


def test_follow_up_acceptance_sets_processing_without_new_conversation_cue() -> None:
    """MAP-INPUT-002: follow-up capture stays inside the existing Conversation."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        manager._stt = ScriptedStt(("jutro",))
        logger = logging.getLogger("test")
        listen = StartListening("listen-1", ListeningMode.FOLLOW_UP)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)
        microphone.trace.clear()
        microphone.events.put_nowait(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1))
        microphone.events.put_nowait(SpeechEnded("listen-1", "utterance-1", "completed"))

        await manager._capture_utterance(
            microphone,
            logger,
            starts_new_conversation=False,
            timeout_seconds=10,
            listen_id=listen.listen_id,
        )

        assert [type(item).__name__ for _, item in microphone.trace] == [
            "SetVisualState",
            "SetVisualState",
        ]
        assert not any(isinstance(event, PlayCue) for event in microphone.commands)

    asyncio.run(run())


def test_open_mic_rejection_resets_candidate_and_idle_without_rearming() -> None:
    """MP-OPENMIC-002/MAP-INVARIANT-002: rejected text stays private."""

    async def run() -> None:
        microphone = FakeMicrophone()
        first_session = ScriptedStreamingSttSession("ordinary speech")
        second_session = ScriptedStreamingSttSession("")
        manager = _manager(microphone, open_mic=True)
        manager._stt = ScriptedStreamingStt([first_session, second_session])
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
            manager._capture_open_mic_utterance(microphone, logger, listen.listen_id)
        )

        async def rejection_visible() -> bool:
            return any(isinstance(command, ResetWakeCandidate) for command in microphone.commands) and any(
                isinstance(command, SetVisualState) and command.state is VisualState.IDLE
                for command in microphone.commands
            )

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


def test_visual_transition_logs_old_new_cause_and_active_correlations(caplog) -> None:
    """MP-OBS-002/MAP-OBS-001: visual logs explain the transition in context."""

    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        logger = logging.getLogger("test.microphone.visual_observability")
        listen = StartListening("listen-1", ListeningMode.OPEN_MIC)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)

        with caplog.at_level(logging.DEBUG, logger=logger.name):
            await manager._set_visual_state(
                microphone, VisualState.IDLE, "ready_for_conversation", logger
            )
            await manager._set_visual_state(
                microphone, VisualState.LISTENING, "open_mic_wake_candidate", logger
            )

        assert "visual transition old=unknown new=idle cause=ready_for_conversation" in caplog.text
        assert "visual transition old=idle new=listening cause=open_mic_wake_candidate" in caplog.text
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


def test_voice_session_close_replaces_queued_follow_up_with_terminal_event() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        session = VoiceInputSession(
            manager=manager,
            microphone=microphone,
            assistant_text_buffer_characters=100,
        )
        conversation = VoiceInputConversation(
            session=session,
            manager=manager,
            microphone=microphone,
            context=InputConversationContext(
                "conversation-1",
                session.context.input_session_id,
                ConversationMedium.VOICE,
            ),
            initial_message=UserMessage("initial"),
            assistant_text_buffer_characters=100,
        )
        session._active = conversation
        session._state = VoiceSessionState.ACTIVE
        conversation._control.put_nowait(UserMessage("queued follow-up"))

        await session.close()

        assert isinstance(await conversation.receive_control(), InputSessionClosed)

    asyncio.run(run())


def test_speech_start_cancellation_joins_receive_and_timer_tasks() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        before = set(asyncio.all_tasks())
        operation = asyncio.create_task(
            manager._wait_for_speech_start(
                microphone,
                logging.getLogger("test"),
                "listen-1",
                60.0,
            )
        )
        await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await asyncio.sleep(0)
        leaked = [
            task
            for task in asyncio.all_tasks()
            if task not in before and task is not asyncio.current_task() and not task.done()
        ]
        assert leaked == []

    asyncio.run(run())


def test_microphone_runtime_recovers_unavailable_acceptance_boundary() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        original_recover = manager._recover_microphone_boundary
        recovered = asyncio.Event()

        async def unavailable(*_args, **_kwargs):
            raise MicrophoneUnavailable("offline")

        async def recover(mic, logger, error):
            await original_recover(mic, logger, error)
            recovered.set()
            manager._closing = True

        manager._begin_new_conversation_listening = unavailable
        manager._recover_microphone_boundary = recover

        await manager._run_microphone(microphone)

        assert recovered.is_set()
        assert microphone.close_count == 1

    asyncio.run(run())


def test_tts_playback_is_correlated_and_waits_for_drain_completion() -> None:
    """MAP-OUTPUT-002: one flushed batch becomes one ordered playback."""

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


def test_ready_rearm_waits_for_playback_finished_and_then_orders_idle_before_listening() -> None:
    """MAP-INVARIANT-003: queued readiness cannot cross pending output."""

    async def run() -> None:
        microphone = DelayedPlaybackMicrophone()
        manager = _manager(microphone, open_mic=True, tts=FakeTts())
        logger = logging.getLogger("test")
        await manager._set_visual_state(
            microphone, VisualState.PROCESSING, "assistant_message", logger
        )
        playback_task = asyncio.create_task(
            manager._speak_tts_text(microphone, "reply", logger, volume=None)
        )

        for _ in range(100):
            if any(isinstance(command, PlaybackEnd) for command in microphone.commands):
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("playback did not reach PlaybackEnd")

        assert not playback_task.done()
        assert manager._visual_states["fake"] is VisualState.PROCESSING
        assert not any(isinstance(command, StartListening) for command in microphone.commands)
        playback_id = next(
            command.playback_id for command in microphone.commands if isinstance(command, PlaybackBegin)
        )
        microphone.events.put_nowait(PlaybackFinished(playback_id))
        await playback_task

        await manager._begin_new_conversation_listening(microphone, logger)
        relevant = [
            command
            for command in microphone.commands
            if isinstance(command, (SetVisualState, PlaybackBegin, PlaybackChunk, PlaybackEnd, StartListening))
        ]
        assert isinstance(relevant[-2], SetVisualState)
        assert relevant[-2].state is VisualState.IDLE
        assert isinstance(relevant[-1], StartListening)
        assert relevant[-1].mode is ListeningMode.OPEN_MIC

    asyncio.run(run())


class _SinkManager:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.speak_started = asyncio.Event()
        self.release_speak = asyncio.Event()
        self.block_speak = False

    async def _set_visual_state(self, microphone, state, cause, logger) -> None:
        del microphone, state, cause, logger

    async def _speak_reply_text(self, microphone, text, logger, on_playback_commit=None) -> None:
        del microphone, logger
        self.speak_started.set()
        if self.block_speak:
            await self.release_speak.wait()
        if on_playback_commit is not None:
            on_playback_commit()
        self.spoken.append(text)


def _voice_sink(manager: _SinkManager, bound: int = 5) -> VoiceAssistantSink:
    async def failure_callback(error: Exception) -> None:
        raise error

    return VoiceAssistantSink(
        manager=manager,
        microphone=object(),
        logger=logging.getLogger("test.voice-sink"),
        buffer_characters=bound,
        unavailable_callback=lambda error: (_ for _ in ()).throw(error),
        failure_callback=failure_callback,
    )


def test_voice_renderer_bound_blocks_producer_and_preserves_all_text() -> None:
    async def run() -> None:
        manager = _SinkManager()
        manager.block_speak = True
        sink = _voice_sink(manager, bound=5)
        await sink.start()
        producer = asyncio.create_task(sink.send_text("abcdef"))
        await manager.speak_started.wait()
        assert not producer.done()
        assert len(sink._buffer) <= 5
        manager.release_speak.set()
        await producer
        assert await sink.complete() is AssistantSinkTerminalResult.COMPLETED
        assert manager.spoken == ["abcde", "f"]
        assert "".join(manager.spoken) == "abcdef"

    asyncio.run(run())


def test_voice_sink_cancellation_while_blocked_can_abort_without_leaking_text() -> None:
    async def run() -> None:
        manager = _SinkManager()
        manager.block_speak = True
        sink = _voice_sink(manager, bound=4)
        await sink.start()
        producer = asyncio.create_task(sink.send_text("blocked"))
        await manager.speak_started.wait()
        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer
        assert await sink.abort(AssistantAbortReason.INPUT_CANCELLED) is AssistantSinkTerminalResult.ABORTED
        assert sink._buffer == ""
        assert manager.spoken == []

    asyncio.run(run())


def test_voice_sink_cancellation_before_playback_commit_cancels_renderer() -> None:
    class PreCommitTts(FakeLifecycle):
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def synthesize(self, _text: str):
            self.entered.set()
            await asyncio.Event().wait()
            yield SynthesizedAudioStart(22050, 2, 1, 0.7)

    async def run() -> None:
        microphone = FakeMicrophone()
        tts = PreCommitTts()
        manager = _manager(microphone, tts=tts)
        sink = VoiceAssistantSink(
            manager=manager,
            microphone=microphone,
            logger=logging.getLogger("test.voice-pre-commit"),
            buffer_characters=4,
            unavailable_callback=lambda error: (_ for _ in ()).throw(error),
            failure_callback=lambda error: asyncio.sleep(0),
        )
        await sink.start()
        producer = asyncio.create_task(sink.send_text("play"))
        await tts.entered.wait()
        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer
        assert not any(isinstance(command, PlaybackBegin) for command in microphone.commands)
        assert await sink.abort(AssistantAbortReason.INPUT_CANCELLED) is AssistantSinkTerminalResult.ABORTED

    asyncio.run(run())


def test_voice_sink_cancellation_at_playback_commit_drains_committed_renderer() -> None:
    async def run() -> None:
        microphone = PlaybackCommitBarrierMicrophone()
        manager = _manager(microphone, tts=FakeTts())
        sink = VoiceAssistantSink(
            manager=manager,
            microphone=microphone,
            logger=logging.getLogger("test.voice-at-commit"),
            buffer_characters=4,
            unavailable_callback=lambda error: (_ for _ in ()).throw(error),
            failure_callback=lambda error: asyncio.sleep(0),
        )
        await sink.start()
        producer = asyncio.create_task(sink.send_text("play"))
        await microphone.playback_committed.wait()
        producer.cancel()
        await asyncio.sleep(0)
        assert not producer.done()
        microphone.release_playback.set()
        with pytest.raises(asyncio.CancelledError):
            await producer
        assert any(isinstance(command, PlaybackEnd) for command in microphone.commands)
        assert await sink.abort(AssistantAbortReason.INPUT_CANCELLED) is AssistantSinkTerminalResult.ABORTED

    asyncio.run(run())


def test_processing_update_cancellation_drains_committed_playback() -> None:
    async def run() -> None:
        microphone = PlaybackCommitBarrierMicrophone()
        manager = _manager(microphone, tts=FakeTts())
        conversation = _voice_conversation(manager, microphone)
        operation = asyncio.create_task(conversation.processing_update())
        await microphone.playback_committed.wait()
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        microphone.release_playback.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert any(isinstance(command, PlaybackEnd) for command in microphone.commands)
        await conversation.cleanup()

    asyncio.run(run())


def test_follow_up_cancellation_drains_committed_cue_without_starting_listening() -> None:
    async def run() -> None:
        microphone = CueCommitBarrierMicrophone()
        manager = _manager(microphone)
        conversation = _voice_conversation(manager, microphone)
        request = asyncio.create_task(conversation.request_follow_up())
        await microphone.cue_committed.wait()
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        microphone.release_cue.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not any(isinstance(command, StartListening) for command in microphone.commands)
        await conversation.cleanup()

    asyncio.run(run())


def test_follow_up_cancellation_stops_committed_listening_generation() -> None:
    async def run() -> None:
        microphone = ListeningCommitBarrierMicrophone()
        manager = _manager(microphone)
        conversation = _voice_conversation(manager, microphone)
        request = asyncio.create_task(conversation.request_follow_up())
        await microphone.listening_committed.wait()
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        microphone.release_listening.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        stop = next(command for command in microphone.commands if isinstance(command, StopListening))
        assert stop.reason == "follow_up_presentation_cancelled"
        await conversation.cleanup()

    asyncio.run(run())


def test_follow_up_deadline_is_fixed_at_listening_started_before_collector_scheduling() -> None:
    async def run() -> None:
        microphone = ListeningCommitBarrierMicrophone()
        manager = _manager(microphone)
        clock = {"now": 100.0}
        manager._monotonic_time = lambda: clock["now"]
        recorded: dict[str, float] = {}
        collector_started = asyncio.Event()

        async def delayed_capture(*_args, **kwargs):
            await asyncio.sleep(0)
            recorded["deadline"] = kwargs["speech_start_deadline"]
            collector_started.set()
            await asyncio.Event().wait()

        manager._capture_utterance = delayed_capture
        conversation = _voice_conversation(manager, microphone)
        request = asyncio.create_task(conversation.request_follow_up())
        await microphone.listening_committed.wait()
        clock["now"] = 500.0
        microphone.release_listening.set()
        await request
        await collector_started.wait()
        assert recorded["deadline"] == 560.0
        await conversation.cleanup()

    asyncio.run(run())


def test_voice_sink_completion_and_abort_have_one_terminal_commit() -> None:
    async def run() -> None:
        manager = _SinkManager()
        sink = _voice_sink(manager)
        await sink.start()
        await sink.send_text("done")
        assert await sink.complete() is AssistantSinkTerminalResult.COMPLETED
        assert await sink.complete() is AssistantSinkTerminalResult.COMPLETED
        assert await sink.abort(AssistantAbortReason.INTERNAL_FAILURE) is AssistantSinkTerminalResult.COMPLETED

        aborted = _voice_sink(_SinkManager())
        assert await aborted.abort(AssistantAbortReason.INPUT_CANCELLED) is AssistantSinkTerminalResult.ABORTED
        assert await aborted.abort(AssistantAbortReason.AGENT_FAILED) is AssistantSinkTerminalResult.ABORTED

    asyncio.run(run())


def test_voice_session_close_wins_uncommitted_acceptance_and_is_idempotent() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        acceptance_started = asyncio.Event()

        async def blocked_accept(*_args, **_kwargs):
            acceptance_started.set()
            await asyncio.Event().wait()

        manager._begin_new_conversation_listening = blocked_accept
        session = VoiceInputSession(
            manager=manager,
            microphone=microphone,
            assistant_text_buffer_characters=100,
        )
        scope = session.accept_conversation()
        accepting = asyncio.create_task(scope.__aenter__())
        await acceptance_started.wait()
        first_close = asyncio.create_task(session.close())
        second_close = asyncio.create_task(session.close())
        with pytest.raises(InputSessionClosed):
            await accepting
        await asyncio.gather(first_close, second_close)
        assert session.closed
        assert session._active is None

    asyncio.run(run())


def test_voice_session_rejects_overlapping_acceptance_and_close_is_valid_from_idle() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        acceptance_started = asyncio.Event()

        async def blocked_accept(*_args, **_kwargs):
            acceptance_started.set()
            await asyncio.Event().wait()

        manager._begin_new_conversation_listening = blocked_accept
        session = VoiceInputSession(
            manager=manager,
            microphone=microphone,
            assistant_text_buffer_characters=100,
        )
        first_scope = session.accept_conversation()
        first_accept = asyncio.create_task(first_scope.__aenter__())
        await acceptance_started.wait()
        with pytest.raises(AssertionError, match="only in IDLE"):
            await session.accept_conversation().__aenter__()
        await session.close()
        with pytest.raises(InputSessionClosed):
            await first_accept

        idle_session = VoiceInputSession(
            manager=manager,
            microphone=microphone,
            assistant_text_buffer_characters=100,
        )
        await asyncio.gather(idle_session.close(), idle_session.close())
        assert idle_session.closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "state",
    [
        VoiceSessionState.ACCEPTING,
        VoiceSessionState.ACTIVE,
        VoiceSessionState.CLOSING,
        VoiceSessionState.CLOSED,
    ],
)
def test_voice_input_session_accept_operation_matrix_rejects_every_non_idle_state(
    state: VoiceSessionState,
) -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        session = VoiceInputSession(
            manager=_manager(microphone),
            microphone=microphone,
            assistant_text_buffer_characters=100,
        )
        session._state = state
        with pytest.raises(AssertionError, match="only in IDLE"):
            await session.accept_conversation().__aenter__()
        if state is not VoiceSessionState.CLOSED:
            session._state = VoiceSessionState.IDLE
        await session.close()

    asyncio.run(run())


def test_voice_session_close_after_accept_commit_releases_active_control_and_never_rearms() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)

        async def begin(*_args, **_kwargs):
            return StartListening("listen-1", ListeningMode.WAKE_WORD)

        async def capture(*_args, **_kwargs):
            return CapturedUtterance(True, ("hello",), None)

        manager._begin_new_conversation_listening = begin
        manager._capture_utterance = capture
        session = VoiceInputSession(
            manager=manager,
            microphone=microphone,
            assistant_text_buffer_characters=100,
        )
        scope = session.accept_conversation()
        conversation = await scope.__aenter__()
        with pytest.raises(AssertionError, match="only in IDLE"):
            await session.accept_conversation().__aenter__()
        receive = asyncio.create_task(conversation.receive_control())
        closing = asyncio.create_task(session.close())
        assert isinstance(await receive, InputSessionClosed)
        assert session._state is VoiceSessionState.CLOSING
        assert not closing.done()
        await scope.__aexit__(None, None, None)
        await closing
        assert session.closed
        assert session._state is VoiceSessionState.CLOSED

    asyncio.run(run())


@pytest.mark.parametrize(
    ("captured", "expected_type"),
    [
        (CapturedUtterance(True, ("follow-up",), None), UserMessage),
        (CapturedUtterance(False, (), None), FollowUpTimedOut),
    ],
)
def test_voice_follow_up_outcome_is_retained_until_matching_ack(captured, expected_type) -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)

        async def capture(*_args, **_kwargs):
            if not captured.captured:
                await manager._stop_listening(
                    microphone,
                    _kwargs["listen_id"],
                    "speech_start_timeout",
                    _kwargs["logger"],
                )
            return captured

        manager._capture_utterance = capture
        conversation = _voice_conversation(manager, microphone)
        token = await conversation.request_follow_up()
        receive = asyncio.create_task(conversation.receive_control())
        await asyncio.sleep(0)
        assert not receive.done()
        with pytest.raises(AssertionError, match="token mismatch"):
            conversation.acknowledge_follow_up_ready(FollowUpRequestCommitted("wrong"))
        conversation.acknowledge_follow_up_ready(token)
        assert isinstance(await receive, expected_type)
        await conversation.cleanup()

    asyncio.run(run())


def test_voice_terminal_input_bypasses_retained_follow_up_before_ack() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)

        async def capture(*_args, **_kwargs):
            return CapturedUtterance(True, ("too early",), None)

        manager._capture_utterance = capture
        conversation = _voice_conversation(manager, microphone)
        token = await conversation.request_follow_up()
        await asyncio.sleep(0)
        conversation.publish_session_closed("offline")
        conversation.acknowledge_follow_up_ready(token)
        assert isinstance(await conversation.receive_control(), InputSessionClosed)
        await conversation.cleanup()
        assert conversation._follow_up_task is None or conversation._follow_up_task.done()

    asyncio.run(run())


def test_voice_cleanup_immediately_after_follow_up_commit_stops_listening_generation() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        conversation = _voice_conversation(manager, microphone)
        await conversation.request_follow_up()
        await conversation.cleanup()
        stop = next(command for command in microphone.commands if isinstance(command, StopListening))
        assert stop.reason == "conversation_scope_exit"
        assert manager._protocols["fake"].snapshot.state is DriverState.DISARMED

    asyncio.run(run())


def test_voice_cleanup_during_follow_up_capture_stops_capturing_generation() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        capture_started = asyncio.Event()

        async def capture_until_cancelled(*_args, **kwargs):
            speech = await manager._wait_for_speech_start(
                microphone,
                logging.getLogger("test.follow-up-capture"),
                kwargs["listen_id"],
                kwargs["timeout_seconds"],
                deadline=kwargs["speech_start_deadline"],
            )
            assert speech is not None
            capture_started.set()
            await asyncio.Event().wait()

        manager._capture_utterance = capture_until_cancelled
        conversation = _voice_conversation(manager, microphone)
        await conversation.request_follow_up()
        listen = next(command for command in microphone.commands if isinstance(command, StartListening))
        microphone.events.put_nowait(
            SpeechStarted(listen.listen_id, "utterance-1", 16000, 2, 1)
        )
        await capture_started.wait()
        assert manager._protocols["fake"].snapshot.state is DriverState.CAPTURING
        await conversation.cleanup()
        stop = next(command for command in microphone.commands if isinstance(command, StopListening))
        assert stop.reason == "conversation_scope_exit"
        assert manager._protocols["fake"].snapshot.state is DriverState.DISARMED

    asyncio.run(run())


@pytest.mark.parametrize(
    ("timeout_seconds", "clock_values", "speech_wins"),
    [
        (10.0, [40.0, 41.0], True),
        (0.0, [42.0, 42.0], True),
        (0.0, [42.0, 42.000001], False),
    ],
)
def test_voice_follow_up_monotonic_before_equal_after_arbiter(
    timeout_seconds: float,
    clock_values: list[float],
    speech_wins: bool,
) -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        logger = logging.getLogger("test.voice-boundary")
        listen = StartListening("listen-1", ListeningMode.FOLLOW_UP)
        await manager._send_command(microphone, listen, logger)
        await manager._await_listening_started(microphone, listen, logger)
        expected = SpeechStarted("listen-1", "utterance-1", 16000, 2, 1)
        microphone.events.put_nowait(expected)
        values = iter(clock_values)
        manager._monotonic_time = lambda: next(values)
        result = await manager._wait_for_speech_start(
            microphone,
            logger,
            "listen-1",
            timeout_seconds,
        )
        assert (result == expected) is speech_wins

    asyncio.run(run())


def test_voice_context_rejection_codes_remain_typed_and_detail_is_not_control_flow() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        manager = _manager(microphone)
        for code in ContextRejectionCode:
            conversation = _voice_conversation(manager, microphone)
            terminal = ConversationEnded(
                ConversationEndReason.CONTEXT_REJECTED,
                code,
                "arbitrary diagnostic text",
            )
            await conversation.end_conversation(terminal)
            assert conversation._terminal_event == terminal

    asyncio.run(run())


def _voice_conversation(
    manager: MicrophoneManager,
    microphone: FakeMicrophone,
) -> VoiceInputConversation:
    session = VoiceInputSession(
        manager=manager,
        microphone=microphone,
        assistant_text_buffer_characters=100,
    )
    session._state = VoiceSessionState.ACTIVE
    session._active_scope_exited.clear()
    conversation = VoiceInputConversation(
        session=session,
        manager=manager,
        microphone=microphone,
        context=InputConversationContext(
            "conversation-1",
            session.context.input_session_id,
            ConversationMedium.VOICE,
        ),
        initial_message=UserMessage("initial"),
        assistant_text_buffer_characters=100,
    )
    session._active = conversation
    return conversation


class FakeTts(FakeLifecycle):
    async def synthesize(self, _text: str):
        yield SynthesizedAudioStart(22050, 2, 1, 0.7)
        yield SynthesizedAudioChunk(b"audio")
        yield SynthesizedAudioEnd()
