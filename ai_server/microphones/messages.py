from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


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
class TextFragment:
    text: str


@dataclass(frozen=True)
class TextEnd:
    pass


AudioEvent: TypeAlias = AudioStart | AudioChunk | AudioEnd
TextEvent: TypeAlias = TextFragment | TextEnd
