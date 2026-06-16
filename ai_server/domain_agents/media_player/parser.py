from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.utils.text import normalize_text


DEFAULT_VOLUME_DELTA = 0.10
TRIM_CHARACTERS = " \t\r\n?.!,;:'\"“”‘’"
VALID_INTENTS = {
    "start_last",
    "stop",
    "volume_delta",
    "set_volume",
    "play_media",
    "now_playing",
    "transfer_playback",
}
PLAY_VERBS = ("graj", "zagraj", "wlacz", "włącz", "odtworz", "odtwórz", "pusc", "puść", "play")
TRANSFER_VERBS = ("przenies", "przenieś", "przenieście", "move", "transfer")
REPLACE_OUTPUT_MARKERS = (
    "only",
    "only in",
    "only on",
    "tylko",
    "tylko w",
    "tylko we",
    "tylko na",
)
ALL_SPEAKERS_MARKERS = (
    "na wszystkich glosnikach",
    "wszystkie glosniki",
    "all speakers",
    "every speaker",
    "everywhere",
    "wszędzie",
    "wszedzie",
    "caly dom",
    "cały dom",
    "calym domu",
    "całym domu",
    "w calym domu",
    "w całym domu",
)
RADIO_ALIASES = {
    "tok fm": "TOK FM",
}
START_LAST_QUERIES = {
    "spotify",
    "graj muzyke",
    "grajh muzyke",
    "wlacz muzyke",
    "włącz muzykę",
    "dajcie tu jakas muzyczke",
    "dajcie tu jakąś muzyczkę",
}
STOP_QUERIES = {
    "cisza",
    "cicho",
    "zatrzymaj muzyke",
    "zatrzymaj muzykę",
    "wylacz muzyke",
    "wyłącz muzykę",
}
NOW_PLAYING_QUERIES = {
    "co to teraz gra",
    "co to za muzyka",
    "kto to gra",
}


@dataclass(frozen=True)
class ParsedMediaCommand:
    intent: str
    query: str
    media_type: str
    areas: tuple[str, ...]
    all_speakers: bool
    volume_level: float | None
    volume_delta: float | None
    replace_outputs: bool
    simple: bool


def parse_media_command(command: dict[str, Any], *, force_simple: bool = False) -> ParsedMediaCommand:
    query = _string_or_empty(command.get("query"))
    original_query = query
    normalized_query = normalize_text(query)
    ascii_query = ascii_fold(normalized_query)
    intent = _normalize_intent(_string_or_empty(command.get("intent"))) or _intent_from_query(ascii_query)
    all_speakers = _bool(command.get("all_speakers")) or _has_all_speakers_marker(ascii_query)
    replace_outputs = _bool(command.get("replace_outputs")) or _has_replace_output_marker(ascii_query)
    areas = _areas_from_command(command) or _areas_from_query(
        original_query,
        ascii_query,
        allow_destination_prepositions=intent == "transfer_playback",
    )
    media_type = _normalize_media_type(_string_or_empty(command.get("media_type"))) or _media_type_from_query(ascii_query)
    volume_level = _volume_level_from_command(command)
    volume_delta = _volume_delta_from_command(command)

    if intent == "set_volume" and volume_level is None:
        volume_level = _volume_level_from_query(ascii_query)
    if intent == "volume_delta" and volume_delta is None:
        volume_delta = -DEFAULT_VOLUME_DELTA if _is_volume_down(ascii_query) else DEFAULT_VOLUME_DELTA
    if intent == "play_media":
        query = _media_query_from_command(command) or _media_query_from_query(original_query, ascii_query)
        query = _canonical_radio_query(query)
        if _is_liked_songs_query(query):
            query = "Liked Songs"
            media_type = "playlist"
        if _is_known_radio_query(query):
            media_type = "radio"

    simple = force_simple or _is_simple_command(
        intent=intent,
        query=query,
        volume_level=volume_level,
        volume_delta=volume_delta,
    )
    return ParsedMediaCommand(
        intent=intent,
        query=query,
        media_type=media_type,
        areas=areas,
        all_speakers=all_speakers,
        volume_level=volume_level,
        volume_delta=volume_delta,
        replace_outputs=replace_outputs,
        simple=simple,
    )


def media_task_from_utterance(user_input: str) -> DomainTask | None:
    normalized_query = normalize_text(user_input)
    ascii_query = ascii_fold(normalized_query)
    if not _looks_like_media_query(ascii_query):
        return None
    parsed = parse_media_command({"query": user_input})
    if not parsed.simple:
        return None
    command = _command_from_parsed(parsed, user_input)
    return {
        "id": "t1",
        "domain": "media_player",
        "command": command,
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


def known_media_task(intent: str, query: str) -> DomainTask:
    return {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": intent, "query": query},
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


def ascii_fold(value: str) -> str:
    value = value.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    folded = unicodedata.normalize("NFKD", value)
    return "".join(character for character in folded if not unicodedata.combining(character))


def _command_from_parsed(parsed: ParsedMediaCommand, query: str) -> dict[str, Any]:
    command: dict[str, Any] = {
        "intent": parsed.intent,
        "query": query,
    }
    if parsed.media_type:
        command["media_type"] = parsed.media_type
    if parsed.areas:
        command["areas"] = list(parsed.areas)
    if parsed.all_speakers:
        command["all_speakers"] = True
    if parsed.replace_outputs:
        command["replace_outputs"] = True
    if parsed.volume_level is not None:
        command["volume_level"] = parsed.volume_level
    if parsed.volume_delta is not None:
        command["volume_delta"] = parsed.volume_delta
    return command


def _intent_from_query(ascii_query: str) -> str:
    if _is_transfer_playback_query(ascii_query):
        return "transfer_playback"
    if ascii_query in {ascii_fold(normalize_text(value)) for value in START_LAST_QUERIES}:
        return "start_last"
    if _starts_with_known_query(ascii_query, STOP_QUERIES):
        return "stop"
    if _starts_with_known_query(ascii_query, NOW_PLAYING_QUERIES):
        return "now_playing"
    if _volume_level_from_query(ascii_query) is not None:
        return "set_volume"
    if _is_volume_up(ascii_query) or _is_volume_down(ascii_query):
        return "volume_delta"
    if _starts_with_play_verb(ascii_query):
        media_query = _media_query_from_query(ascii_query, ascii_query)
        if media_query and ascii_fold(normalize_text(media_query)) not in {"music", "muzyka", "muzyke"}:
            return "play_media"
        if _has_replace_output_marker(ascii_query):
            return "transfer_playback"
        return "start_last"
    return ""


def _looks_like_media_query(ascii_query: str) -> bool:
    if not ascii_query:
        return False
    if ascii_query in {ascii_fold(normalize_text(value)) for value in START_LAST_QUERIES | STOP_QUERIES | NOW_PLAYING_QUERIES}:
        return True
    if _starts_with_known_query(ascii_query, STOP_QUERIES | NOW_PLAYING_QUERIES):
        return True
    if _is_volume_up(ascii_query) or _is_volume_down(ascii_query) or _volume_level_from_query(ascii_query) is not None:
        return True
    if _starts_with_music_play_verb(ascii_query):
        return True
    if _is_transfer_playback_query(ascii_query):
        return True
    return "muzyka" in ascii_query or "spotify" in ascii_query or _contains_known_radio(ascii_query)


def _is_simple_command(
    *,
    intent: str,
    query: str,
    volume_level: float | None,
    volume_delta: float | None,
) -> bool:
    if intent in {"start_last", "stop", "now_playing", "transfer_playback"}:
        return True
    if intent == "set_volume":
        return volume_level is not None
    if intent == "volume_delta":
        return volume_delta is not None
    if intent == "play_media":
        return bool(query)
    return False


def _media_query_from_command(command: dict[str, Any]) -> str:
    for key in ("media_id", "media", "name"):
        value = command.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _media_query_from_query(query: str, ascii_query: str) -> str:
    text = query.strip(TRIM_CHARACTERS)
    normalized_text = ascii_fold(normalize_text(text))
    for verb in PLAY_VERBS:
        normalized_verb = ascii_fold(normalize_text(verb))
        if normalized_text.startswith(f"{normalized_verb} "):
            text = text[len(verb) :].strip()
            break
    text = _strip_media_type_words(text)
    text = _strip_provider_phrases(text)
    text = _strip_target_phrases(text)
    return text.strip(TRIM_CHARACTERS)


def _strip_media_type_words(text: str) -> str:
    return re.sub(
        r"^(piosenke|piosenkę|utwor|utwór|album|playliste|playlistę|playlist|radio)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _strip_provider_phrases(text: str) -> str:
    text = re.sub(r"\s+(?:na|z|ze|on)\s+spotify\s*$", "", text, flags=re.IGNORECASE)
    return text


def _strip_target_phrases(text: str) -> str:
    text = re.sub(r"\s+na\s+wszystkich\s+głośnikach.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+na\s+wszystkich\s+glosnikach.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+tylko\s+(?:w|we|do|na)\s+.+$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:w|we)\s+.+$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+only\s+(?:in|on)\s+.+$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+in\s+.+$", "", text, flags=re.IGNORECASE)
    return text


def _areas_from_command(command: dict[str, Any]) -> tuple[str, ...]:
    raw_areas = command.get("areas", [])
    if isinstance(raw_areas, str) and raw_areas.strip():
        return (raw_areas.strip(),)
    if isinstance(raw_areas, list):
        return tuple(area.strip() for area in raw_areas if isinstance(area, str) and area.strip())
    return ()


def _areas_from_query(query: str, ascii_query: str, *, allow_destination_prepositions: bool = False) -> tuple[str, ...]:
    if _has_all_speakers_marker(ascii_query):
        return ()
    prepositions = r"w|we|in"
    if allow_destination_prepositions:
        prepositions = r"w|we|do|to|in"
    match = re.search(rf"\b(?:tylko\s+)?(?:{prepositions})\s+(.+?)[?.!]*$", query.strip(), flags=re.IGNORECASE)
    if match is None:
        return ()
    raw_area = match.group(1).strip(" ?.!")
    if not raw_area:
        return ()
    parts = re.split(r"\s+i\s+|,| oraz ", raw_area)
    return tuple(part.strip() for part in parts if part.strip())


def _has_all_speakers_marker(ascii_query: str) -> bool:
    return any(ascii_fold(normalize_text(marker)) in ascii_query for marker in ALL_SPEAKERS_MARKERS)


def _has_replace_output_marker(ascii_query: str) -> bool:
    words = set(ascii_query.split())
    if "only" in words or "tylko" in words:
        return True
    return any(ascii_fold(normalize_text(marker)) in ascii_query for marker in REPLACE_OUTPUT_MARKERS)


def _is_transfer_playback_query(ascii_query: str) -> bool:
    if not any(ascii_query.startswith(ascii_fold(normalize_text(verb))) for verb in TRANSFER_VERBS):
        return False
    return any(marker in ascii_query for marker in ("muzyka", "muzyke", "music", "playback"))


def _volume_level_from_query(ascii_query: str) -> float | None:
    match = re.search(r"(?:glosnosc|głośność).*?(?:na\s+)?(\d{1,3})", ascii_query)
    if match is None:
        return None
    value = int(match.group(1))
    if value > 100:
        value = 100
    return value / 100


def _volume_level_from_command(command: dict[str, Any]) -> float | None:
    value = command.get("volume_level")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _clamp_volume(float(value))
    return None


def _volume_delta_from_command(command: dict[str, Any]) -> float | None:
    value = command.get("volume_delta")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _media_type_from_query(ascii_query: str) -> str:
    if _contains_known_radio(ascii_query):
        return "radio"
    if "album" in ascii_query:
        return "album"
    if "playlista" in ascii_query or "playliste" in ascii_query or "playlist" in ascii_query:
        return "playlist"
    if "radio" in ascii_query:
        return "radio"
    if "piosen" in ascii_query or "utwor" in ascii_query:
        return "track"
    return ""


def _normalize_media_type(value: str) -> str:
    normalized = ascii_fold(normalize_text(value))
    if normalized in {"song", "track", "utwor", "piosenka"}:
        return "track"
    if normalized in {"album", "playlist", "radio", "artist"}:
        return normalized
    if normalized in {"playlista", "playliste"}:
        return "playlist"
    return ""


def _normalize_intent(value: str) -> str:
    if value in VALID_INTENTS:
        return value
    normalized = ascii_fold(normalize_text(value))
    return normalized if normalized in VALID_INTENTS else ""


def _starts_with_known_query(ascii_query: str, known_queries: set[str]) -> bool:
    return any(
        ascii_query == normalized_known or ascii_query.startswith(f"{normalized_known} ")
        for normalized_known in {ascii_fold(normalize_text(value)) for value in known_queries}
    )


def _is_volume_up(ascii_query: str) -> bool:
    return any(marker in ascii_query for marker in ("glosniej", "przyglosnij", "daj glosniej", "podglosnij"))


def _is_volume_down(ascii_query: str) -> bool:
    return any(marker in ascii_query for marker in ("ciszej", "scisz", "przycisz"))


def _starts_with_play_verb(ascii_query: str) -> bool:
    return any(ascii_query.startswith(ascii_fold(normalize_text(verb))) for verb in PLAY_VERBS)


def _starts_with_music_play_verb(ascii_query: str) -> bool:
    for verb in ("graj", "zagraj", "odtworz", "odtwórz", "pusc", "puść"):
        if ascii_query.startswith(ascii_fold(normalize_text(verb))):
            return True
    if not any(ascii_query.startswith(ascii_fold(normalize_text(verb))) for verb in ("wlacz", "włącz")):
        return False
    return any(marker in ascii_query for marker in ("muzyk", "spotify", "album", "playlist", "piosen", "utwor", "radio")) or _contains_known_radio(ascii_query)


def _is_liked_songs_query(query: str) -> bool:
    normalized = ascii_fold(normalize_text(query))
    return any(marker in normalized for marker in ("moje ulubione", "my favourites", "my favorites", "liked songs"))


def _canonical_radio_query(query: str) -> str:
    normalized = ascii_fold(normalize_text(query))
    return RADIO_ALIASES.get(normalized, query)


def _is_known_radio_query(query: str) -> bool:
    return ascii_fold(normalize_text(query)) in RADIO_ALIASES


def _contains_known_radio(ascii_query: str) -> bool:
    return any(alias in ascii_query for alias in RADIO_ALIASES)


def _clamp_volume(value: float) -> float:
    return min(1.0, max(0.0, value))


def _bool(value: Any) -> bool:
    return value is True


def _string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""
