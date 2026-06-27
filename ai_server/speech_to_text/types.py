from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PcmAudioFormat:
    rate: int
    width: int
    channels: int

    @property
    def byte_rate(self) -> int:
        return self.rate * self.width * self.channels


@dataclass(frozen=True)
class PcmAudioChunk:
    data: bytes


DEFAULT_STT_AUDIO_FORMAT = PcmAudioFormat(rate=16000, width=2, channels=1)


def audio_seconds(byte_count: int, audio_format: PcmAudioFormat) -> float:
    if audio_format.byte_rate <= 0:
        return 0.0
    return byte_count / audio_format.byte_rate
