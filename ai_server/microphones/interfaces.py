from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from ai_server.microphones.messages import AudioChunk, AudioEvent, MicrophoneOutputEvent, TextEvent
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget


class MicrophoneUnavailable(Exception):
    """Raised when a microphone is temporarily unreachable."""


class Microphone(Protocol):
    context: MicrophoneContext
    playback_target: PlaybackTarget

    async def wait_for_event(self) -> AudioEvent:
        raise NotImplementedError

    async def send_output_event(self, event: MicrophoneOutputEvent) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class SttSession(Protocol):
    async def send_audio(self, chunk: AudioChunk) -> None:
        raise NotImplementedError

    async def end_audio(self) -> None:
        raise NotImplementedError

    async def receive_text(self) -> TextEvent:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class SpeechToText(Protocol):
    async def start(self) -> None:
        raise NotImplementedError

    async def create_session(self, session_id: str) -> SttSession:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class TextToSpeech(Protocol):
    async def start(self) -> None:
        raise NotImplementedError

    def synthesize(self, text: str) -> AsyncIterator[AudioEvent]:
        raise NotImplementedError

    async def speak(self, target: PlaybackTarget, text: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
