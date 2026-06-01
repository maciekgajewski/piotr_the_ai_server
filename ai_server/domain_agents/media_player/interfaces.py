from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaTarget:
    entity_id: str
    name: str
    area_id: str
    area_name: str
    volume_level: float | None = None


@dataclass(frozen=True)
class MediaSearchItem:
    media_id: str
    name: str
    media_type: str
    artist: str = ""
    album: str = ""
