from __future__ import annotations

from typing import Any


def format_volume_level(volume_level: float) -> str:
    return f"{round(volume_level * 100)} procent"


def format_now_playing(now_playing: dict[str, Any]) -> str:
    title = _text(now_playing.get("title"))
    artist = _text(now_playing.get("artist"))
    album = _text(now_playing.get("album"))
    if not title and not artist:
        return "Teraz nic nie gra."
    if title and artist and album:
        return f"Teraz gra {artist} - {title}, z albumu {album}."
    if title and artist:
        return f"Teraz gra {artist} - {title}."
    if title:
        return f"Teraz gra {title}."
    return f"Teraz gra {artist}."


def format_started(target_count: int, media_name: str = "muzykę") -> str:
    if target_count > 1:
        return f"Włączam {media_name} na wybranych głośnikach."
    return f"Włączam {media_name}."


def format_resumed(target_count: int) -> str:
    if target_count > 1:
        return "Wznawiam muzykę na wybranych głośnikach."
    return "Wznawiam muzykę."


def format_stopped(target_count: int) -> str:
    if target_count > 1:
        return "Zatrzymałem muzykę na wybranych głośnikach."
    return "Zatrzymałem muzykę."


def format_playing_media(query: str, target_count: int) -> str:
    if target_count > 1:
        return f"Włączam {query} na wybranych głośnikach."
    return f"Włączam {query}."


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
