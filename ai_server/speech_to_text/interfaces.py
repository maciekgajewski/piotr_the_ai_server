from __future__ import annotations

from typing import Protocol

from ai_server.speech_to_text.messages import StreamingTextEvent, TextEvent
from ai_server.speech_to_text.types import PcmAudioChunk


class SttSession(Protocol):
    async def send_audio(self, chunk: PcmAudioChunk) -> None:
        raise NotImplementedError

    async def end_audio(self) -> None:
        raise NotImplementedError

    async def receive_text(self) -> TextEvent:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class StreamingSttSession(Protocol):
    async def send_audio(self, chunk: PcmAudioChunk) -> None:
        raise NotImplementedError

    async def end_audio(self) -> None:
        raise NotImplementedError

    async def receive_text(self) -> StreamingTextEvent:
        raise NotImplementedError

    async def transcribe_final(self) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class SpeechToText(Protocol):
    async def start(self) -> None:
        raise NotImplementedError

    async def create_session(self, session_id: str) -> SttSession:
        raise NotImplementedError

    async def create_streaming_session(self, session_id: str) -> StreamingSttSession:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
