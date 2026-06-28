from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class TextFragment:
    text: str


@dataclass(frozen=True)
class TextPartial:
    text: str
    audio_start_seconds: float
    audio_end_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class TextEnd:
    pass


TextEvent: TypeAlias = TextFragment | TextEnd
StreamingTextEvent: TypeAlias = TextPartial | TextEnd
