from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from ai_server.speech_to_text.messages import TextEnd, TextEvent, TextFragment


@dataclass(frozen=True)
class AudioStart:
    wake_word: str | None = None
    rate: int | None = None
    width: int | None = None
    channels: int | None = None
    volume: float | None = None


@dataclass(frozen=True)
class AudioChunk:
    data: bytes


@dataclass(frozen=True)
class AudioEnd:
    pass


@dataclass(frozen=True)
class MessageEndCue:
    pass


@dataclass(frozen=True)
class StartWakeWordListening:
    pass


@dataclass(frozen=True)
class StartFollowUpListening:
    pass


@dataclass(frozen=True)
class ConversationTimeoutCue:
    pass


AudioEvent: TypeAlias = AudioStart | AudioChunk | AudioEnd
MicrophoneOutputEvent: TypeAlias = (
    AudioStart
    | AudioChunk
    | AudioEnd
    | MessageEndCue
    | StartWakeWordListening
    | StartFollowUpListening
    | ConversationTimeoutCue
)
