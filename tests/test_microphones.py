import asyncio

import pytest

from ai_server.config import MicrophoneConfig, SttConfig, TtsConfig
from ai_server.messages import UserMessage
from ai_server.microphones.agent_endpoint import MicrophoneAgentEndpoint
from ai_server.microphones.manager import MicrophoneManager, init_mics
from ai_server.microphones.types import MicrophoneContext, MicrophoneUtterance, PlaybackTarget


class FakeAgent:
    async def run(self, endpoint, session_id: str) -> None:
        while True:
            message = await endpoint.receive()
            await endpoint.send(UserMessage(text=f"reply:{message.text}"))

    async def close(self) -> None:
        pass


class FakeDriver:
    def __init__(self) -> None:
        self.context = MicrophoneContext(type="fake", name="office", location="office")
        self.playback_target = PlaybackTarget(
            type="fake",
            name="office",
            address="box.local",
            api_key="key",
        )
        self.closed = False
        self._sent = False

    async def wait_for_utterance(self, capture_seconds: float) -> MicrophoneUtterance:
        if self._sent:
            await asyncio.sleep(3600)
        self._sent = True
        return MicrophoneUtterance(audio_chunks=(b"audio",), wake_word="Ryszardzie")

    async def close(self) -> None:
        self.closed = True


class FakeStt:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.utterances = []

    async def start(self) -> None:
        self.started = True

    async def transcribe(self, utterance: MicrophoneUtterance) -> str:
        self.utterances.append(utterance)
        return "cześć"

    async def close(self) -> None:
        self.closed = True


class FakeTts:
    def __init__(self) -> None:
        self.spoken = []
        self.closed = False
        self.spoke = asyncio.Event()

    async def speak(self, target: PlaybackTarget, text: str) -> None:
        self.spoken.append((target, text))
        self.spoke.set()

    async def close(self) -> None:
        self.closed = True


class FailingTts(FakeTts):
    async def speak(self, target: PlaybackTarget, text: str) -> None:
        self.spoken.append((target, text))
        self.spoke.set()
        raise RuntimeError("speaker unavailable")


def test_microphone_agent_endpoint_exchanges_one_message() -> None:
    async def run() -> None:
        endpoint = MicrophoneAgentEndpoint()

        async def agent() -> None:
            message = await endpoint.receive()
            await endpoint.send(UserMessage(text=f"reply:{message.text}"))

        task = asyncio.create_task(agent())
        reply = await endpoint.exchange(UserMessage(text="hello"))
        await task

        assert reply == UserMessage(text="reply:hello")

    asyncio.run(run())


def test_microphone_manager_sends_transcript_to_agent_and_speaks_reply() -> None:
    async def run() -> None:
        driver = FakeDriver()
        stt = FakeStt()
        tts = FakeTts()
        manager = MicrophoneManager(
            drivers=[driver],
            stt=stt,
            tts=tts,
            agent=FakeAgent(),
            capture_seconds=5.0,
        )

        await manager.start()
        await asyncio.wait_for(tts.spoke.wait(), timeout=1)
        await manager.close()

        assert stt.started is True
        assert stt.closed is True
        assert driver.closed is True
        assert tts.closed is True
        assert stt.utterances == [MicrophoneUtterance(audio_chunks=(b"audio",), wake_word="Ryszardzie")]
        assert tts.spoken == [(driver.playback_target, "reply:cześć")]

    asyncio.run(run())


def test_microphone_manager_keeps_session_alive_after_tts_error() -> None:
    async def run() -> None:
        driver = FakeDriver()
        tts = FailingTts()
        manager = MicrophoneManager(
            drivers=[driver],
            stt=FakeStt(),
            tts=tts,
            agent=FakeAgent(),
            capture_seconds=5.0,
        )

        await manager.start()
        await asyncio.wait_for(tts.spoke.wait(), timeout=1)
        await asyncio.sleep(0)

        assert driver.closed is False

        await manager.close()

    asyncio.run(run())


def test_init_mics_rejects_unknown_microphone_type() -> None:
    mic_config = MicrophoneConfig(type="unknown", name="mic", location=None, options={})

    with pytest.raises(ValueError, match="unsupported microphone type: unknown"):
        asyncio.run(init_mics((mic_config,), SttConfig(), TtsConfig(), FakeAgent()))
