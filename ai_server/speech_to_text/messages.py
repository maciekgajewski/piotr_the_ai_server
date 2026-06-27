from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class TextFragment:
    text: str


@dataclass(frozen=True)
class TextEnd:
    pass


TextEvent: TypeAlias = TextFragment | TextEnd
