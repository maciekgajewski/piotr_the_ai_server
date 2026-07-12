from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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
class AudioProgress:
    chunks: int
    bytes: int


@dataclass(frozen=True)
class MessageEndCue:
    pass


@dataclass(frozen=True)
class StartWakeWordListening:
    pass


@dataclass(frozen=True)
class StartOpenMicListening:
    pass


@dataclass(frozen=True)
class StartFollowUpListening:
    pass


@dataclass(frozen=True)
class ConversationTimeoutCue:
    pass


@dataclass(frozen=True)
class OpenMicWakeCandidateRejected:
    pass


class VisualState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"


@dataclass(frozen=True)
class SetVisualState:
    state: VisualState


AudioEvent: TypeAlias = AudioStart | AudioChunk | AudioEnd | AudioProgress
MicrophoneOutputEvent: TypeAlias = (
    AudioStart
    | AudioChunk
    | AudioEnd
    | MessageEndCue
    | StartWakeWordListening
    | StartOpenMicListening
    | StartFollowUpListening
    | ConversationTimeoutCue
    | OpenMicWakeCandidateRejected
    | SetVisualState
)
