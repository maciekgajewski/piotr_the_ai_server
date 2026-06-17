import asyncio
import logging

import pytest

from ai_server.config import ConversationConfig, MicrophoneConfig, SpeakerRecognitionConfig, SttConfig, TtsConfig
from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, RequestFollowUp, TextMessage, WaitForNewConversation
from ai_server.microphones.agent_endpoint import MicrophoneAgentEndpoint
from ai_server.microphones.interfaces import MicrophoneUnavailable
from ai_server.microphones.manager import MicrophoneManager, _MicrophoneAvailabilityLogger, init_mics
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioStart, ConversationTimeoutCue, MessageEndCue
from ai_server.microphones.messages import StartFollowUpListening
from ai_server.microphones.messages import StartWakeWordListening
from ai_server.microphones.messages import TextEnd, TextFragment
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget
from ai_server.speaker_recognition.client import SpeakerRecognitionResult


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
            await endpoint.send(RequestFollowUp())


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
        self._events = list(events or [
            AudioStart(wake_word="Ryszardzie"),
            AudioChunk(data=b"audio"),
            AudioEnd(),
        ])

    async def wait_for_event(self):
        if not self._events:
            await asyncio.sleep(3600)
        return self._events.pop(0)

    async def send_output_event(self, event) -> None:
        self.sent_audio_events.append(event)

    async def close(self) -> None:
        self.closed = True


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


class FakeStt:
    def __init__(self, session_text_events=None) -> None:
        self.started = False
        self.closed = False
        self.sessions = []
        self._session_text_events = list(session_text_events or [])

    async def start(self) -> None:
        self.started = True

    async def create_session(self, session_id: str):
        text_events = self._session_text_events.pop(0) if self._session_text_events else None
        session = FakeSttSession(text_events)
        self.sessions.append(session)
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

        await endpoint.send_to_session(MessageBegin())
        await endpoint.send_to_session(MessageFragment(text="hello"))
        await endpoint.send_to_session(MessageEnd())

        assert await endpoint.receive() == MessageBegin()
        assert await endpoint.receive() == MessageFragment(text="hello")
        assert await endpoint.receive() == MessageEnd()

        await endpoint.send(WaitForNewConversation())
        assert await endpoint.receive_from_session() == WaitForNewConversation()

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
        assert stt.sessions[0].audio_chunks == [AudioChunk(data=b"audio")]
        assert stt.sessions[0].ended is True
        assert tts.synthesized == ["reply:cześć"]
        assert microphone.sent_audio_events == [
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
        assert microphone.sent_audio_events == [
            StartWakeWordListening(),
            MessageEndCue(),
            AudioStart(rate=22050, width=2, channels=1, volume=1.0),
            AudioChunk(data=b"reply-audio"),
            AudioEnd(),
            StartFollowUpListening(),
            ConversationTimeoutCue(),
        ]

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
