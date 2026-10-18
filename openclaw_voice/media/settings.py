from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class NotificationAudioSettings:
    max_audio_bytes: int
    max_audio_seconds: float
    title_body_pause_seconds: float
    synthesis_timeout_seconds: float
    synthesis_cleanup_timeout_seconds: float
    cleanup_and_receipt_margin_seconds: float

    def __post_init__(self) -> None:
        if type(self.max_audio_bytes) is not int or self.max_audio_bytes <= 0:
            raise ValueError("notification max_audio_bytes must be a positive integer")
        for name in (
            "max_audio_seconds", "title_body_pause_seconds",
            "synthesis_timeout_seconds", "synthesis_cleanup_timeout_seconds",
            "cleanup_and_receipt_margin_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"notification {name} must be positive and finite")
