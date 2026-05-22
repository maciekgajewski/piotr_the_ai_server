from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MicrophoneContext:
    type: str
    name: str
    location: str | None = None

    @property
    def log_prefix(self) -> str:
        if self.location is None:
            return f"Microphone[{self.type}:{self.name}]"
        return f"Microphone[{self.type}:{self.name}@{self.location}]"


@dataclass(frozen=True)
class PlaybackTarget:
    type: str
    name: str
    address: str
    api_key: str
    expected_name: str | None = None


@dataclass(frozen=True)
class MicrophoneUtterance:
    audio_chunks: tuple[bytes, ...]
    wake_word: str | None = None

    @property
    def byte_count(self) -> int:
        return sum(len(chunk) for chunk in self.audio_chunks)


class MicrophoneDriver(Protocol):
    context: MicrophoneContext
    playback_target: PlaybackTarget

    async def wait_for_utterance(self, capture_seconds: float) -> MicrophoneUtterance:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class SpeechToText(Protocol):
    async def start(self) -> None:
        raise NotImplementedError

    async def transcribe(self, utterance: MicrophoneUtterance) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class TextToSpeech(Protocol):
    async def speak(self, target: PlaybackTarget, text: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
