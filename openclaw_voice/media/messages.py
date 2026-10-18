from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreparedSpeechSegment:
    rate: int
    width: int
    channels: int
    volume: float | None
    pcm: bytes

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm) / (self.rate * self.width * self.channels)


@dataclass(frozen=True)
class PreparedNotificationAudio:
    segments: tuple[PreparedSpeechSegment, ...]
    pause_seconds: float

    @property
    def duration_seconds(self) -> float:
        return sum(segment.duration_seconds for segment in self.segments) + self.pause_seconds
