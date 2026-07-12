import asyncio
import logging

import pytest

from ai_server.config import ConversationConfig, MicrophoneConfig, SpeakerRecognitionConfig, SttConfig, TtsConfig
from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, ProcessingUpdate, FollowUpRequested, TextMessage
from ai_server.messages import ReadyForConversation
from ai_server.microphones.agent_endpoint import MicrophoneAgentEndpoint
from ai_server.microphones.interfaces import MicrophoneUnavailable
from ai_server.microphones.manager import MicrophoneManager, _MicrophoneAvailabilityLogger, init_mics
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioProgress, AudioStart, ConversationTimeoutCue, MessageEndCue
from ai_server.microphones.messages import OpenMicWakeCandidateRejected
from ai_server.microphones.messages import StartFollowUpListening
from ai_server.microphones.messages import StartOpenMicListening
from ai_server.microphones.messages import StartWakeWordListening
from ai_server.microphones.messages import SetVisualState
from ai_server.microphones.messages import TextEnd, TextFragment
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget
from ai_server.speech_to_text.messages import TextPartial
from ai_server.speech_to_text.types import PcmAudioChunk
from ai_server.speaker_recognition.client import SpeakerRecognitionResult


def _without_visual_events(events):
    return [event for event in events if not isinstance(event, SetVisualState)]


class FakeAgent:
    def __init__(self) -> None:
        self.messages = []
        self.conversations = []

    async def run_conversation(self, conversation, endpoint) -> None:
        self.conversations.append(conversation)
        async for message in endpoint.messages():
            self.messages.append(message.text)
            await endpoint.send_message(TextMessage(text=f"reply:{message.text}"))

    async def close(self) -> None:
        pass


class FakeFollowUpAgent(FakeAgent):
    async def run_conversation(self, conversation, endpoint) -> None:
        async for message in endpoint.messages():
            self.messages.append(message.text)
            await endpoint.send_message(TextMessage(text=f"reply:{message.text}"))
            await endpoint.request_follow_up()


class FakeProcessingAgent(FakeAgent):
    async def run_conversation(self, conversation, endpoint) -> None:
        async for message in endpoint.messages():
            self.messages.append(message.text)
            await endpoint.send(ProcessingUpdate())
            await endpoint.send_message(TextMessage(text=f"reply:{message.text}"))


class FakeMicrophone:
    def __init__(self, events=None) -> None:
        self.context = MicrophoneContext(type="fake", name="office", area="office")
        self.playback_target = PlaybackTarget(
            type="fake",
            name="office",
            address="box.local",
            api_key="key",
        )
        self.closed = False
        self.sent_audio_events = []
        if events is None:
            events = [
                AudioStart(wake_word="Ryszardzie"),
                AudioChunk(data=b"audio"),
                AudioEnd(),
            ]
        self._events = list(events)

    async def wait_for_event(self):
        if not self._events:
            await asyncio.sleep(3600)
        return self._events.pop(0)

    async def send_output_event(self, event) -> None:
        self.sent_audio_events.append(event)

    async def close(self) -> None:
        self.closed = True


class ProgressOnlyOpenMicMicrophone(FakeMicrophone):
    def __init__(self) -> None:
        super().__init__(events=[])
        self._started = False
        self.progress_count = 0

    async def wait_for_event(self):
        if not self._started:
            self._started = True
            return AudioStart(wake_word=None)
        await asyncio.sleep(0.001)
        self.progress_count += 1
        return AudioProgress(
            chunks=self.progress_count * 50,
            bytes=self.progress_count * 51200,
        )


class UnavailableMicrophone(FakeMicrophone):
    def __init__(self) -> None:
        super().__init__(events=[])
        self.unavailable_seen = asyncio.Event()

    async def send_output_event(self, event) -> None:
        self.unavailable_seen.set()
        raise MicrophoneUnavailable("address=box.local error=offline")


class FlakyWakeWordMicrophone(FakeMicrophone):
    def __init__(self) -> None:
        super().__init__()
        self.wake_word_attempts = 0

    async def send_output_event(self, event) -> None:
        self.sent_audio_events.append(event)
        if isinstance(event, StartWakeWordListening):
            self.wake_word_attempts += 1
            if self.wake_word_attempts <= 2:
                raise MicrophoneUnavailable(f"offline-{self.wake_word_attempts}")


class FlakyPlaybackMicrophone(FakeMicrophone):
    def __init__(self) -> None:
        super().__init__()
        self.playback_attempts = 0

    async def send_output_event(self, event) -> None:
        self.sent_audio_events.append(event)
        if isinstance(event, AudioStart) and event.rate is not None:
            self.playback_attempts += 1
            if self.playback_attempts <= 2:
                raise MicrophoneUnavailable(f"playback-offline-{self.playback_attempts}")


class FakeSttSession:
    def __init__(self, text_events=None) -> None:
        self.audio_chunks = []
        self.ended = False
        self.closed = False
        self._text_events = list(text_events or [
            TextFragment(text="cześć"),
            TextEnd(),
        ])

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.audio_chunks.append(chunk)

    async def end_audio(self) -> None:
        self.ended = True

    async def receive_text(self):
        return self._text_events.pop(0)

    async def close(self) -> None:
        self.closed = True


class FakeStreamingSttSession:
    def __init__(self, partial_events=None, final_text: str = "Ryszardzie, cześć") -> None:
        self.audio_chunks = []
        self.ended = False
        self.closed = False
        self.final_transcribed = False
        self._partial_events = list(
            partial_events
            or [
                TextPartial(
                    text="Ryszardzie, cześć",
                    audio_start_seconds=0.0,
                    audio_end_seconds=1.0,
                    duration_seconds=1.0,
                ),
                TextEnd(),
            ]
        )
        self._final_text = final_text

    async def send_audio(self, chunk: PcmAudioChunk) -> None:
        self.audio_chunks.append(chunk)

    async def end_audio(self) -> None:
        self.ended = True

    async def receive_text(self):
        if not self._partial_events:
            return TextEnd()
        return self._partial_events.pop(0)

    async def transcribe_final(self) -> str:
        self.final_transcribed = True
        return self._final_text

    async def close(self) -> None:
        self.closed = True


class FakeStt:
    def __init__(self, session_text_events=None, streaming_sessions=None) -> None:
        self.started = False
        self.closed = False
        self.sessions = []
        self.streaming_sessions = []
        self._session_text_events = list(session_text_events or [])
        self._streaming_sessions = list(streaming_sessions or [])

    async def start(self) -> None:
        self.started = True

    async def create_session(self, session_id: str):
        text_events = self._session_text_events.pop(0) if self._session_text_events else None
        session = FakeSttSession(text_events)
        self.sessions.append(session)
        return session

    async def create_streaming_session(self, session_id: str):
        session = self._streaming_sessions.pop(0) if self._streaming_sessions else FakeStreamingSttSession()
        self.streaming_sessions.append(session)
        return session

    async def close(self) -> None:
        self.closed = True


class FakeTts:
    def __init__(self) -> None:
        self.spoken = []
        self.synthesized = []
        self.started = False
        self.closed = False
        self.spoke = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def speak(self, target: PlaybackTarget, text: str) -> None:
        self.spoken.append((target, text))
        self.spoke.set()

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        yield AudioStart(rate=22050, width=2, channels=1, volume=1.0)
        yield AudioChunk(data=b"reply-audio")
        yield AudioEnd()
        self.spoke.set()

    async def close(self) -> None:
        self.closed = True


class FailingTts(FakeTts):
    async def speak(self, target: PlaybackTarget, text: str) -> None:
        self.spoken.append((target, text))
        self.spoke.set()
        raise RuntimeError("speaker unavailable")

    async def synthesize(self, text: str):
        self.spoke.set()
        raise RuntimeError("speaker unavailable")
        yield AudioEnd()


class StartFailingTts(FakeTts):
    async def start(self) -> None:
        await super().start()
        raise RuntimeError("tts startup failed")


class FakeSpeakerRecognitionStream:
    def __init__(self, result) -> None:
        self.audio_chunks = []
        self.ended = False
        self.cancelled = False
        self._result = result

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.audio_chunks.append(chunk)

    async def end_audio(self) -> None:
        self.ended = True

    async def result(self):
        return self._result

    def cancel(self) -> None:
        self.cancelled = True


class FakeSpeakerRecognitionClient:
    def __init__(self, result) -> None:
        self.timeout_seconds = 1.0
        self.streams = []
        self._result = result

    @property
    def enabled(self) -> bool:
        return True

    def start_stream(self, audio_format):
        stream = FakeSpeakerRecognitionStream(self._result)
        self.streams.append((audio_format, stream))
        return stream


def test_microphone_agent_endpoint_exchanges_one_message() -> None:
    async def run() -> None:
        endpoint = MicrophoneAgentEndpoint()

        await endpoint.send_to_session(MessageBegin(message_id="user-1"))
        await endpoint.send_to_session(MessageFragment(message_id="user-1", text="hello"))
        await endpoint.send_to_session(MessageEnd(message_id="user-1"))

        assert await endpoint.receive() == MessageBegin(message_id="user-1")
        assert await endpoint.receive() == MessageFragment(message_id="user-1", text="hello")
        assert await endpoint.receive() == MessageEnd(message_id="user-1")

        await endpoint.send(ReadyForConversation())
        assert await endpoint.receive_from_session() == ReadyForConversation()

    asyncio.run(run())


def test_microphone_manager_sends_transcript_to_agent_and_speaks_reply() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        stt = FakeStt()
        tts = FakeTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=tts,
            agent=FakeAgent(),
            follow_up_timeout_seconds=0.1,
        )

        await manager.start()
        await asyncio.wait_for(tts.spoke.wait(), timeout=1)
        await manager.close()

        assert stt.started is True
        assert tts.started is True
        assert stt.closed is True
        assert microphone.closed is True
        assert tts.closed is True
        assert len(stt.sessions) == 1
        assert stt.sessions[0].audio_chunks == [PcmAudioChunk(data=b"audio")]
        assert stt.sessions[0].ended is True
        assert tts.synthesized == ["reply:cześć"]
        assert _without_visual_events(microphone.sent_audio_events) == [
            StartWakeWordListening(),
            MessageEndCue(),
            AudioStart(rate=22050, width=2, channels=1, volume=1.0),
            AudioChunk(data=b"reply-audio"),
            AudioEnd(),
            StartWakeWordListening(),
        ]

    asyncio.run(run())


def test_microphone_manager_opens_configured_microphone_in_open_mic_mode() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        agent = FakeAgent()
        tts = FakeTts()
        stt = FakeStt()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=tts,
            agent=agent,
            follow_up_timeout_seconds=0.1,
            open_microphones={"office"},
        )

        await manager.start()
        await asyncio.wait_for(tts.spoke.wait(), timeout=1)
        await manager.close()

        assert agent.messages == ["cześć"]
        assert len(stt.sessions) == 0
        assert len(stt.streaming_sessions) == 1
        assert stt.streaming_sessions[0].audio_chunks == [PcmAudioChunk(data=b"audio")]
        assert stt.streaming_sessions[0].ended is True
        assert stt.streaming_sessions[0].final_transcribed is True
        assert _without_visual_events(microphone.sent_audio_events) == [
            StartOpenMicListening(),
            MessageEndCue(),
            AudioStart(rate=22050, width=2, channels=1, volume=1.0),
            AudioChunk(data=b"reply-audio"),
            AudioEnd(),
            StartOpenMicListening(),
        ]

    asyncio.run(run())


def test_microphone_manager_discards_open_mic_speech_without_wake_phrase() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        stt = FakeStt(
            streaming_sessions=[
                FakeStreamingSttSession(
                    partial_events=[
                        TextPartial(
                            text="to jest tło",
                            audio_start_seconds=0.0,
                            audio_end_seconds=1.0,
                            duration_seconds=1.0,
                        ),
                        TextEnd(),
                    ],
                    final_text="to jest tło",
                ),
                FakeStreamingSttSession(
                    partial_events=[TextEnd()],
                    final_text="",
                ),
            ]
        )
        agent = FakeAgent()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=FakeTts(),
            agent=agent,
            follow_up_timeout_seconds=0.1,
            open_microphones={"office"},
        )

        await manager.start()
        await asyncio.wait_for(
            _wait_until(lambda: len(stt.streaming_sessions) >= 2),
            timeout=1,
        )
        await manager.close()

        assert agent.messages == []
        assert not any(isinstance(event, MessageEndCue) for event in microphone.sent_audio_events)
        assert _without_visual_events(microphone.sent_audio_events) == [StartOpenMicListening()]

    asyncio.run(run())


def test_microphone_manager_resets_open_mic_state_after_rejected_wake_candidate() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        stt = FakeStt(
            streaming_sessions=[
                FakeStreamingSttSession(
                    partial_events=[
                        TextPartial(
                            text="Ryszardzie",
                            audio_start_seconds=0.0,
                            audio_end_seconds=1.0,
                            duration_seconds=1.0,
                        ),
                        TextEnd(),
                    ],
                    final_text="",
                ),
                FakeStreamingSttSession(
                    partial_events=[TextEnd()],
                    final_text="",
                ),
            ]
        )
        agent = FakeAgent()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=FakeTts(),
            agent=agent,
            follow_up_timeout_seconds=0.1,
            open_microphones={"office"},
        )

        await manager.start()
        await asyncio.wait_for(
            _wait_until(lambda: len(stt.streaming_sessions) >= 2),
            timeout=1,
        )
        await manager.close()

        assert agent.messages == []
        assert not any(isinstance(event, MessageEndCue) for event in microphone.sent_audio_events)
        assert _without_visual_events(microphone.sent_audio_events) == [
            StartOpenMicListening(),
            OpenMicWakeCandidateRejected(),
        ]

    asyncio.run(run())


def test_microphone_manager_retries_open_mic_when_audio_start_never_arrives(caplog) -> None:
    async def run() -> None:
        microphone = FakeMicrophone(events=[])
        agent = FakeAgent()
        stt = FakeStt()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=FakeTts(),
            agent=agent,
            follow_up_timeout_seconds=0.1,
            microphone_audio_start_timeouts={"office": 0.01},
            open_microphones={"office"},
        )

        with caplog.at_level(logging.DEBUG, logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(
                _wait_until(
                    lambda: "microphone still unavailable; retrying soon "
                    "error=open-mic audio start timed out after 0.01s" in caplog.text
                ),
                timeout=1,
            )
            await manager.close()

        assert agent.messages == []
        assert len(stt.streaming_sessions) == 0

    asyncio.run(run())

    assert "waiting for microphone audio start timeout_seconds=0.01 unavailable_on_timeout=True" in caplog.text
    assert "open-mic audio start timed out timeout_seconds=0.01" in caplog.text
    assert "microphone unavailable; retrying soon error=open-mic audio start timed out after 0.01s" in caplog.text
    assert "microphone still unavailable; retrying soon error=open-mic audio start timed out after 0.01s" in caplog.text
    assert "microphone available again" not in caplog.text


def test_microphone_manager_ignores_stale_events_before_open_mic_audio_start_on_debug(caplog) -> None:
    async def run() -> None:
        microphone = FakeMicrophone(
            events=[
                AudioProgress(chunks=50, bytes=51200),
                AudioChunk(data=b"stale-audio"),
                AudioEnd(),
                AudioStart(wake_word=None),
                AudioChunk(data=b"audio"),
                AudioEnd(),
            ]
        )
        agent = FakeAgent()
        tts = FakeTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=FakeStt(),
            tts=tts,
            agent=agent,
            follow_up_timeout_seconds=0.1,
            open_microphones={"office"},
        )

        with caplog.at_level(logging.DEBUG, logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(tts.spoke.wait(), timeout=1)
            await manager.close()

        assert agent.messages == ["cześć"]

    asyncio.run(run())

    assert "ignored stale microphone event before audio start event=AudioProgress count=1" in caplog.text
    assert "ignored stale microphone event before audio start event=AudioChunk count=1" in caplog.text
    assert "ignored stale microphone event before audio start event=AudioEnd count=1" in caplog.text
    assert "ignored stale microphone events before audio start counts=" in caplog.text
    assert all(
        record.levelno < logging.WARNING
        for record in caplog.records
        if "ignored" in record.getMessage() and "before audio start" in record.getMessage()
    )


def test_microphone_manager_retries_open_mic_when_stream_stops_emitting_events(caplog) -> None:
    async def run() -> None:
        microphone = FakeMicrophone(
            events=[
                AudioStart(wake_word=None),
                AudioChunk(data=b"partial-audio"),
            ]
        )
        agent = FakeAgent()
        stt = FakeStt()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=FakeTts(),
            agent=agent,
            follow_up_timeout_seconds=0.1,
            microphone_audio_event_timeouts={"office": 0.01},
            open_microphones={"office"},
        )

        with caplog.at_level(logging.DEBUG, logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(
                _wait_until(
                    lambda: "open-mic audio stream event timed out timeout_seconds=0.01" in caplog.text
                ),
                timeout=1,
            )
            await manager.close()

        assert agent.messages == []
        assert len(stt.streaming_sessions) == 1
        assert stt.streaming_sessions[0].audio_chunks == [PcmAudioChunk(data=b"partial-audio")]
        assert stt.streaming_sessions[0].closed is True

    asyncio.run(run())

    assert "open-mic audio stream event timed out timeout_seconds=0.01" in caplog.text
    assert "microphone unavailable; retrying soon error=open-mic audio stream event timed out after 0.01s" in caplog.text
    assert "microphone available again" not in caplog.text


def test_microphone_manager_rearms_open_mic_when_idle_stream_stalls(caplog) -> None:
    async def run() -> None:
        microphone = FakeMicrophone(events=[AudioStart(wake_word=None)])
        agent = FakeAgent()
        stt = FakeStt(
            streaming_sessions=[
                FakeStreamingSttSession(
                    partial_events=[TextEnd()],
                    final_text="",
                )
            ]
        )
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=FakeTts(),
            agent=agent,
            follow_up_timeout_seconds=0.1,
            microphone_audio_event_timeouts={"office": 0.01},
            open_microphones={"office"},
        )

        with caplog.at_level(logging.DEBUG, logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(
                _wait_until(
                    lambda: "open-mic audio stream stalled after idle timeouts=3 timeout_seconds=0.01"
                    in caplog.text
                ),
                timeout=1,
            )
            await manager.close()

        assert agent.messages == []
        assert len(stt.streaming_sessions) == 1
        assert _without_visual_events(microphone.sent_audio_events) == [StartOpenMicListening()]

    asyncio.run(run())

    assert "open-mic audio stream idle timeout ignored timeout_seconds=0.01" in caplog.text
    assert "open-mic audio stream stalled after idle timeouts=3 timeout_seconds=0.01" in caplog.text
    assert (
        "microphone unavailable; retrying soon error=open-mic audio stream stalled after "
        "3 idle timeouts of 0.01s"
    ) in caplog.text
    assert "open-mic audio stream event timed out timeout_seconds=0.01" not in caplog.text


def test_microphone_manager_keeps_open_mic_progress_stream_open(caplog) -> None:
    async def run() -> None:
        microphone = ProgressOnlyOpenMicMicrophone()
        agent = FakeAgent()
        stt = FakeStt(
            streaming_sessions=[
                FakeStreamingSttSession(
                    partial_events=[TextEnd()],
                    final_text="",
                )
            ]
        )
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=FakeTts(),
            agent=agent,
            follow_up_timeout_seconds=0.1,
            microphone_audio_event_timeouts={"office": 0.01},
            open_microphones={"office"},
        )

        with caplog.at_level(logging.DEBUG, logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(
                _wait_until(lambda: microphone.progress_count >= 5),
                timeout=1,
            )
            await manager.close()

        assert agent.messages == []
        assert len(stt.streaming_sessions) == 1
        assert _without_visual_events(microphone.sent_audio_events) == [StartOpenMicListening()]

    asyncio.run(run())

    assert "open-mic audio stream progress chunks=250 bytes=256000" in caplog.text
    assert "open-mic audio stream stalled" not in caplog.text
    assert "microphone unavailable; retrying soon error=open-mic audio stream stalled" not in caplog.text


def test_microphone_manager_accepts_open_mic_wake_phrase_found_only_in_final() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        stt = FakeStt(
            streaming_sessions=[
                FakeStreamingSttSession(
                    partial_events=[TextEnd()],
                    final_text="Ryszardzie, włącz muzykę",
                )
            ]
        )
        agent = FakeAgent()
        tts = FakeTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=tts,
            agent=agent,
            follow_up_timeout_seconds=0.1,
            open_microphones={"office"},
        )

        await manager.start()
        await asyncio.wait_for(tts.spoke.wait(), timeout=1)
        await manager.close()

        assert agent.messages == ["włącz muzykę"]
        assert _without_visual_events(microphone.sent_audio_events)[0] == StartOpenMicListening()
        assert _without_visual_events(microphone.sent_audio_events)[1] == MessageEndCue()

    asyncio.run(run())


def test_microphone_manager_open_mic_partial_logs_do_not_include_background_text(caplog) -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        stt = FakeStt(
            streaming_sessions=[
                FakeStreamingSttSession(
                    partial_events=[
                        TextPartial(
                            text="tajne tło",
                            audio_start_seconds=0.0,
                            audio_end_seconds=1.0,
                            duration_seconds=1.0,
                        ),
                        TextEnd(),
                    ],
                    final_text="tajne tło",
                ),
                FakeStreamingSttSession(
                    partial_events=[TextEnd()],
                    final_text="",
                ),
            ]
        )
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=FakeTts(),
            agent=FakeAgent(),
            follow_up_timeout_seconds=0.1,
            open_microphones={"office"},
        )

        with caplog.at_level(logging.DEBUG, logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(
                _wait_until(lambda: len(stt.streaming_sessions) >= 2),
                timeout=1,
            )
            await manager.close()

    asyncio.run(run())

    assert "open-mic partial transcript chars=9" in caplog.text
    assert "tajne tło" not in caplog.text
    assert all(
        record.levelno < logging.INFO or "open-mic" not in record.getMessage()
        for record in caplog.records
    )


def test_microphone_manager_rearms_wake_word_after_empty_wake_transcript() -> None:
    async def run() -> None:
        microphone = FakeMicrophone(
            events=[
                AudioStart(wake_word="Ryszardzie"),
                AudioChunk(data=b"wake-noise"),
                AudioEnd(),
                AudioStart(wake_word="Ryszardzie"),
                AudioChunk(data=b"audio"),
                AudioEnd(),
            ]
        )
        stt = FakeStt(
            session_text_events=[
                [TextEnd()],
                [TextFragment(text="cześć"), TextEnd()],
            ]
        )
        tts = FakeTts()
        agent = FakeAgent()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=tts,
            agent=agent,
            follow_up_timeout_seconds=0.1,
        )

        await manager.start()
        await asyncio.wait_for(
            _wait_until(
                lambda: sum(
                    isinstance(event, StartWakeWordListening)
                    for event in microphone.sent_audio_events
                )
                >= 3
            ),
            timeout=1,
        )
        await manager.close()

        assert agent.messages == ["cześć"]
        assert len(stt.sessions) == 2
        assert stt.sessions[0].audio_chunks == [PcmAudioChunk(data=b"wake-noise")]
        assert stt.sessions[0].ended is True
        assert stt.sessions[1].audio_chunks == [PcmAudioChunk(data=b"audio")]
        assert stt.sessions[1].ended is True
        assert tts.synthesized == ["reply:cześć"]
        assert _without_visual_events(microphone.sent_audio_events) == [
            StartWakeWordListening(),
            StartWakeWordListening(),
            MessageEndCue(),
            AudioStart(rate=22050, width=2, channels=1, volume=1.0),
            AudioChunk(data=b"reply-audio"),
            AudioEnd(),
            StartWakeWordListening(),
        ]

    asyncio.run(run())


def test_microphone_manager_adds_recognized_user_to_new_conversation() -> None:
    async def run() -> None:
        microphone = FakeMicrophone(
            events=[
                AudioStart(wake_word="Ryszardzie", rate=16000, width=2, channels=1),
                AudioChunk(data=b"audio-one"),
                AudioChunk(data=b"audio-two"),
                AudioEnd(),
            ]
        )
        agent = FakeAgent()
        recognizer = FakeSpeakerRecognitionClient(
            SpeakerRecognitionResult(
                recognized_user="Maciek",
                confidence=0.91,
                score=0.72,
                threshold=0.45,
                profile="/profiles/maciek/speaker_profile.npz",
            )
        )
        tts = FakeTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=FakeStt(),
            tts=tts,
            agent=agent,
            follow_up_timeout_seconds=0.1,
            user_settings={"Maciek": {"voice_profile": "/profiles/maciek/speaker_profile.npz"}},
            speaker_recognition=recognizer,
        )

        await manager.start()
        await asyncio.wait_for(tts.spoke.wait(), timeout=1)
        await manager.close()

        assert agent.conversations[0].attributes["user"] == "Maciek"
        assert agent.conversations[0].attributes["medium"] == "voice"
        assert agent.conversations[0].user_settings == {
            "voice_profile": "/profiles/maciek/speaker_profile.npz"
        }
        audio_format, stream = recognizer.streams[0]
        assert audio_format.sample_rate == 16000
        assert audio_format.sample_width == 2
        assert audio_format.channels == 1
        assert stream.audio_chunks == [AudioChunk(data=b"audio-one"), AudioChunk(data=b"audio-two")]
        assert stream.ended is True

    asyncio.run(run())


def test_microphone_manager_treats_empty_follow_up_as_timeout() -> None:
    async def run() -> None:
        microphone = FakeMicrophone(
            events=[
                AudioStart(wake_word="Ryszardzie"),
                AudioChunk(data=b"audio"),
                AudioEnd(),
                AudioStart(wake_word="follow_up"),
                AudioEnd(),
            ]
        )
        stt = FakeStt(
            session_text_events=[
                [TextFragment(text="cześć"), TextEnd()],
                [TextEnd()],
            ]
        )
        tts = FakeTts()
        agent = FakeFollowUpAgent()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=tts,
            agent=agent,
            follow_up_timeout_seconds=1,
        )

        await manager.start()
        await asyncio.wait_for(
            _wait_until(lambda: any(isinstance(event, ConversationTimeoutCue) for event in microphone.sent_audio_events)),
            timeout=1,
        )
        await manager.close()

        assert agent.messages == ["cześć"]
        assert tts.synthesized == ["reply:cześć"]
        assert _without_visual_events(microphone.sent_audio_events) == [
            StartWakeWordListening(),
            MessageEndCue(),
            AudioStart(rate=22050, width=2, channels=1, volume=1.0),
            AudioChunk(data=b"reply-audio"),
            AudioEnd(),
            StartFollowUpListening(),
            ConversationTimeoutCue(),
        ]

    asyncio.run(run())


def test_microphone_manager_drops_whitespace_only_follow_up_before_agent() -> None:
    async def run() -> None:
        microphone = FakeMicrophone(
            events=[
                AudioStart(wake_word="Ryszardzie"),
                AudioChunk(data=b"audio"),
                AudioEnd(),
                AudioStart(wake_word="follow_up"),
                AudioEnd(),
            ]
        )
        stt = FakeStt(
            session_text_events=[
                [TextFragment(text="cześć"), TextEnd()],
                [TextFragment(text="   "), TextEnd()],
            ]
        )
        tts = FakeTts()
        agent = FakeFollowUpAgent()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=tts,
            agent=agent,
            follow_up_timeout_seconds=1,
        )

        await manager.start()
        await asyncio.wait_for(
            _wait_until(lambda: any(isinstance(event, ConversationTimeoutCue) for event in microphone.sent_audio_events)),
            timeout=1,
        )
        await manager.close()

        assert agent.messages == ["cześć"]
        assert tts.synthesized == ["reply:cześć"]

    asyncio.run(run())


def test_microphone_manager_speaks_processing_update_at_reduced_volume(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr("ai_server.microphones.manager.random.choice", lambda choices: choices[0])
        microphone = FakeMicrophone()
        tts = FakeTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=FakeStt(),
            tts=tts,
            agent=FakeProcessingAgent(),
            follow_up_timeout_seconds=0.1,
            processing_update_spoken_cues=("zaraz...",),
        )

        await manager.start()
        await asyncio.wait_for(_wait_until(lambda: len(tts.synthesized) >= 2), timeout=1)
        await manager.close()

        assert tts.synthesized[:2] == ["zaraz...", "reply:cześć"]
        assert AudioStart(rate=22050, width=2, channels=1, volume=0.7) in microphone.sent_audio_events
        assert AudioStart(rate=22050, width=2, channels=1, volume=1.0) in microphone.sent_audio_events

    asyncio.run(run())


def test_microphone_manager_keeps_session_alive_after_tts_error() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        tts = FailingTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=FakeStt(),
            tts=tts,
            agent=FakeAgent(),
            follow_up_timeout_seconds=0.1,
        )

        await manager.start()
        await asyncio.wait_for(tts.spoke.wait(), timeout=1)
        await asyncio.sleep(0)

        assert microphone.closed is False

        await manager.close()

    asyncio.run(run())


def test_microphone_manager_logs_unavailable_microphone_without_stack_trace(caplog) -> None:
    async def run() -> None:
        microphone = UnavailableMicrophone()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=FakeStt(),
            tts=FakeTts(),
            agent=FakeAgent(),
            follow_up_timeout_seconds=0.1,
        )

        with caplog.at_level("WARNING", logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(microphone.unavailable_seen.wait(), timeout=1)
            await manager.close()

        records = [
            record
            for record in caplog.records
            if "microphone unavailable; retrying soon" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is None

    asyncio.run(run())


def test_microphone_availability_logger_warns_once_and_logs_recovery(caplog) -> None:
    logger = logging.getLogger("test.microphone.availability")
    availability = _MicrophoneAvailabilityLogger(logger)

    with caplog.at_level("DEBUG", logger="test.microphone.availability"):
        availability.unavailable(MicrophoneUnavailable("offline-one"))
        availability.unavailable(MicrophoneUnavailable("offline-two"))
        availability.available()
        availability.available()

    messages = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert messages == [
        ("WARNING", "microphone unavailable; retrying soon error=offline-one"),
        ("DEBUG", "microphone still unavailable; retrying soon error=offline-two"),
        ("INFO", "microphone available again"),
    ]
    assert all(record.exc_info is None for record in caplog.records)


def test_microphone_manager_retries_unavailable_microphone_and_logs_recovery(caplog) -> None:
    async def run() -> None:
        microphone = FlakyWakeWordMicrophone()
        tts = FakeTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=FakeStt(),
            tts=tts,
            agent=FakeAgent(),
            follow_up_timeout_seconds=0.1,
        )

        with caplog.at_level("DEBUG", logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(tts.spoke.wait(), timeout=2)
            await manager.close()

        availability_records = [
            (record.levelname, record.getMessage())
            for record in caplog.records
            if "microphone " in record.getMessage()
        ]
        assert ("WARNING", "microphone unavailable; retrying soon error=offline-1") in availability_records
        assert ("DEBUG", "microphone still unavailable; retrying soon error=offline-2") in availability_records
        assert ("INFO", "microphone available again") in availability_records
        assert microphone.wake_word_attempts == 4

    asyncio.run(run())


def test_microphone_manager_retries_prepared_reply_when_playback_is_unavailable(caplog) -> None:
    async def run() -> None:
        microphone = FlakyPlaybackMicrophone()
        tts = FakeTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=FakeStt(),
            tts=tts,
            agent=FakeAgent(),
            follow_up_timeout_seconds=0.1,
        )

        with caplog.at_level("DEBUG", logger="ai_server.microphones.manager"):
            await manager.start()
            await asyncio.wait_for(tts.spoke.wait(), timeout=2)
            await manager.close()

        assert microphone.playback_attempts == 3
        assert tts.synthesized == ["reply:cześć", "reply:cześć", "reply:cześć"]
        availability_records = [
            (record.levelname, record.getMessage())
            for record in caplog.records
            if "microphone " in record.getMessage()
        ]
        assert ("WARNING", "microphone unavailable; retrying soon error=playback-offline-1") in availability_records
        assert ("DEBUG", "microphone still unavailable; retrying soon error=playback-offline-2") in availability_records
        assert ("INFO", "microphone available again") in availability_records

    asyncio.run(run())


def test_microphone_manager_cleans_up_when_start_fails() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        stt = FakeStt()
        tts = StartFailingTts()
        manager = MicrophoneManager(
            microphones=[microphone],
            stt=stt,
            tts=tts,
            agent=FakeAgent(),
            follow_up_timeout_seconds=0.1,
        )

        with pytest.raises(RuntimeError, match="tts startup failed"):
            await manager.start()

        assert stt.started is True
        assert tts.started is True
        assert stt.closed is True
        assert tts.closed is True
        assert microphone.closed is True
        assert manager.microphone_count == 1

    asyncio.run(run())


def test_init_mics_rejects_unknown_microphone_type() -> None:
    mic_config = MicrophoneConfig(
        type="unknown",
        name="mic",
        area=None,
        options={},
    )

    with pytest.raises(ValueError, match="unsupported microphone type: unknown"):
        asyncio.run(
            init_mics(
                (mic_config,),
                SttConfig(),
                TtsConfig(),
                ConversationConfig(),
                SpeakerRecognitionConfig(),
                FakeAgent(),
            )
        )


async def _wait_until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0)
