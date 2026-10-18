from __future__ import annotations

import hashlib
import json
import re
import unicodedata


_AGENT_ID = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


def room_session_key(agent_id: str, room: str) -> str:
    """Return a stable, agent-owned history key for one configured room."""
    if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
        raise ValueError("agent ID must be a normalized OpenClaw agent name")
    normalized_room = _room_name(room)
    digest = hashlib.sha256(b"piotr-openclaw-room-v1\0" + normalized_room.encode("utf-8")).hexdigest()
    return f"agent:{agent_id}:voice-room:{digest}"


def spoken_request_message(room: str, speaker: str | None, transcript: str) -> str:
    """Carry trusted capture context alongside one untrusted spoken transcript."""
    normalized_room = _room_name(room)
    if speaker is not None and (
        not isinstance(speaker, str) or not speaker.strip() or len(speaker) > 128
        or any(ord(character) < 32 for character in speaker)
    ):
        raise ValueError("speaker must be bounded and non-empty when provided")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("spoken transcript must be non-empty")
    if len(transcript) > 12_000:
        raise ValueError("spoken transcript is too long")
    speaker_value = "unknown" if speaker is None else f"tentative {json.dumps(speaker, ensure_ascii=False)}"
    return (
        "Voice input context supplied by the satellite service. "
        "Speaker recognition is tentative and must not authorize actions.\n"
        f"Room: {json.dumps(normalized_room, ensure_ascii=False)}\n"
        f"Speaker recognition: {speaker_value}\n"
        f"Spoken transcript: {json.dumps(transcript, ensure_ascii=False)}"
    )


def _room_name(room: str) -> str:
    if not isinstance(room, str) or not room or room != room.strip():
        raise ValueError("configured room must be non-empty and trimmed")
    normalized = unicodedata.normalize("NFC", room)
    if len(normalized) > 128 or any(ord(character) < 32 for character in normalized):
        raise ValueError("configured room is too long or contains control characters")
    return normalized
