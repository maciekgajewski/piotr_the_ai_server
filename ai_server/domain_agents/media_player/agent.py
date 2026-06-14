from __future__ import annotations

import json
import logging
import time
from typing import Any

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.domain_agents.media_player.formatting import (
    format_now_playing,
    format_playing_media,
    format_started,
    format_stopped,
    format_volume_level,
)
from ai_server.domain_agents.media_player.interfaces import MediaSearchItem, MediaTarget
from ai_server.domain_agents.media_player.messages import MEDIA_COMPLEX_COMMAND_SYSTEM_PROMPT
from ai_server.domain_agents.media_player.parser import DEFAULT_VOLUME_DELTA, ParsedMediaCommand, parse_media_command
from ai_server.home_assistant import HomeAssistantConnection
from ai_server.interfaces import Conversation
from ai_server.ollama_client import OLLAMA_BASE_URL, OllamaClient


class MediaPlayerDomainAgent:
    def __init__(
        self,
        *,
        model: str,
        connection: HomeAssistantConnection,
        ollama_url: str = OLLAMA_BASE_URL,
        fallback_model: str | None = None,
        fallback_backoff_seconds: float = 300.0,
        ollama_client: OllamaClient | None = None,
        liked_songs_media_id: str = "Liked Songs",
        liked_songs_media_type: str = "playlist",
        default_music_media_id: str = "Liked Songs",
        default_music_media_type: str = "playlist",
        default_music_name: str = "muzykę ze Spotify",
    ) -> None:
        self._model = model
        self._connection = connection
        self._ollama_url = ollama_url
        self._fallback_model = fallback_model
        self._fallback_backoff_seconds = fallback_backoff_seconds
        self._ollama = ollama_client or OllamaClient(base_url=ollama_url)
        self._owns_ollama = ollama_client is None
        self._fallback_until = 0.0
        self._liked_songs_media_id = liked_songs_media_id
        self._liked_songs_media_type = liked_songs_media_type
        self._default_music_media_id = default_music_media_id
        self._default_music_media_type = default_music_media_type
        self._default_music_name = default_music_name
        self._logger = logging.getLogger(f"{__name__}.MediaPlayerDomainAgent[{model}]")

    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        command = task.get("command", {})
        command = command if isinstance(command, dict) else {}
        parsed = parse_media_command(command)
        logger = logging.getLogger(
            f"{__name__}.MediaPlayerDomainAgent[{self._model}:{conversation.conversation_id}:{task.get('id', 'unknown')}]"
        )
        logger.info(
            "running media_player task intent=%s simple=%s query=%r areas=%s all_speakers=%s",
            parsed.intent,
            parsed.simple,
            parsed.query,
            parsed.areas,
            parsed.all_speakers,
        )

        if not parsed.simple:
            parsed = await self._complex_command(
                command=command,
                conversation=conversation,
                active_context=active_context,
            )
            logger.info(
                "media_player complex command parsed intent=%s query=%r areas=%s all_speakers=%s",
                parsed.intent,
                parsed.query,
                parsed.areas,
                parsed.all_speakers,
            )

        if parsed.intent == "start_last":
            return await self._start_last(conversation, parsed)
        if parsed.intent == "stop":
            return await self._stop(conversation, parsed)
        if parsed.intent == "volume_delta":
            return await self._volume_delta(conversation, parsed)
        if parsed.intent == "set_volume":
            return await self._set_volume(conversation, parsed)
        if parsed.intent == "play_media":
            return await self._play_media(conversation, parsed)
        if parsed.intent == "now_playing":
            return await self._now_playing(conversation, parsed)
        return _clarification_result("Jaką muzykę albo który głośnik mam obsłużyć?")

    async def close(self) -> None:
        if self._owns_ollama:
            await self._ollama.close()

    async def _start_last(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_music_assistant_targets(targets)
        media_id = _conversation_media_setting(conversation, "default_music_media_id", self._default_music_media_id)
        media_type = _conversation_media_setting(conversation, "default_music_media_type", self._default_music_media_type)
        media_name = _conversation_media_setting(conversation, "default_music_name", self._default_music_name)
        result = await self._connection.music_assistant_play_media(
            [target.entity_id for target in targets],
            media_id=media_id,
            media_type=media_type,
        )
        if result.get("status") != "ok":
            return _failed_result("Nie udało się włączyć muzyki ze Spotify.")
        return _ok_result(format_started(len(targets), media_name), targets)

    async def _stop(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        result = await self._connection.media_player_stop([target.entity_id for target in targets])
        if result.get("status") != "ok":
            return _failed_result("Nie udało się zatrzymać muzyki.")
        return _ok_result(format_stopped(len(targets)), targets)

    async def _volume_delta(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed, allow_playing_fallback=True)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        delta = parsed.volume_delta if parsed.volume_delta is not None else DEFAULT_VOLUME_DELTA
        result = await self._connection.media_player_volume_delta([target.entity_id for target in targets], delta)
        if result.get("status") not in {"ok", "partial"}:
            return _failed_result("Nie udało się zmienić głośności.")
        level = _result_volume_level(result) or _target_volume_after_delta(targets[0], delta)
        return _ok_result(f"Głośność: {format_volume_level(level)}.", targets)

    async def _set_volume(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        if parsed.volume_level is None:
            return _clarification_result("Na jaką głośność mam ustawić muzykę?")
        targets = await self._targets(conversation, parsed, allow_playing_fallback=True)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        result = await self._connection.media_player_volume_set([target.entity_id for target in targets], parsed.volume_level)
        if result.get("status") != "ok":
            return _failed_result("Nie udało się ustawić głośności.")
        return _ok_result(f"Ustawiłem głośność na {format_volume_level(parsed.volume_level)}.", targets)

    async def _play_media(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        if not parsed.query:
            return _clarification_result("Co mam włączyć?")
        targets = await self._targets(conversation, parsed)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_music_assistant_targets(targets)

        search_item = await self._search_media(conversation, parsed)
        if search_item is None:
            return _failed_result(f"Nie znalazłem muzyki: {parsed.query}.")
        result = await self._connection.music_assistant_play_media(
            [target.entity_id for target in targets],
            media_id=search_item.media_id,
            media_type=search_item.media_type or parsed.media_type,
            artist=search_item.artist,
            album=search_item.album,
        )
        if result.get("status") != "ok":
            return _failed_result("Nie udało się włączyć muzyki.")
        return _ok_result(format_playing_media(search_item.name or parsed.query, len(targets)), targets)

    async def _now_playing(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed, allow_playing_fallback=True)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        result = await self._connection.media_player_now_playing(targets[0].entity_id)
        if result.get("status") != "ok":
            return _failed_result("Nie mogę teraz sprawdzić, co gra.")
        return _ok_result(format_now_playing(result), targets)

    async def _targets(
        self,
        conversation: Conversation,
        parsed: ParsedMediaCommand,
        *,
        allow_playing_fallback: bool = False,
    ) -> list[MediaTarget] | dict[str, Any]:
        if parsed.all_speakers:
            result = await self._list_speaker_players()
            return _targets_from_result(result)

        if parsed.areas:
            targets: list[MediaTarget] = []
            for area in parsed.areas:
                result = await self._list_speaker_players(area_name=area)
                if isinstance(result, dict):
                    return _failed_result(f"Nie znam pokoju: {area}.")
                targets.extend(_targets_from_player_mappings(result))
            if targets:
                return _dedupe_targets(targets)
            return _failed_result("Nie znalazłem głośnika w tym pokoju.")

        if conversation.area:
            result = await self._list_speaker_players(area_name=conversation.area)
            targets = _targets_from_result(result)
            if not isinstance(targets, dict) and targets:
                return targets

        if allow_playing_fallback:
            result = await self._list_speaker_players()
            if isinstance(result, list):
                playing = [target for target in _targets_from_player_mappings(result) if _player_state(result, target.entity_id) == "playing"]
                if len(playing) == 1:
                    return playing
        if conversation.area:
            return _failed_result("Nie znalazłem głośnika w tym pokoju.")
        return _clarification_result("W którym pokoju mam użyć głośnika?")

    async def _list_speaker_players(self, *, area_name: str = "") -> list[dict[str, Any]] | dict[str, Any]:
        return await self._connection.list_media_players(
            area_name=area_name,
            music_assistant_only=False,
            speakers_only=True,
        )

    async def _search_media(self, conversation: Conversation, parsed: ParsedMediaCommand) -> MediaSearchItem | None:
        if parsed.query == "Liked Songs":
            return MediaSearchItem(
                media_id=_conversation_media_setting(conversation, "liked_songs_media_id", self._liked_songs_media_id),
                name=_conversation_media_setting(conversation, "liked_songs_name", "Liked Songs"),
                media_type=_conversation_media_setting(conversation, "liked_songs_media_type", self._liked_songs_media_type),
            )
        query = parsed.query
        result = await self._connection.music_assistant_search(name=query, media_type=parsed.media_type, limit=5)
        if result.get("status") == "ok":
            item = _best_search_item(result.get("response"))
            if item is not None:
                return item
        if result.get("error") == "music_assistant_config_entry_not_found":
            self._logger.info("Music Assistant search unavailable; using raw play_media media_id=%r", query)
            return MediaSearchItem(media_id=query, name=query, media_type=parsed.media_type)
        return None

    async def _complex_command(
        self,
        *,
        command: dict[str, Any],
        conversation: Conversation,
        active_context: dict[str, Any],
    ) -> ParsedMediaCommand:
        payload = {
            "command": command,
            "conversation": {
                "area": conversation.area,
                "user": conversation.user,
                "user_settings": conversation.user_settings,
            },
            "active_context": active_context,
        }
        response = await self._chat_with_fallback(
            {
                "raw": False,
                "think": False,
                "format": "json",
                "stream": False,
                "keep_alive": "1h",
                "options": {"num_predict": 256, "temperature": 0, "num_ctx": 4096},
                "messages": [
                    {"role": "system", "content": MEDIA_COMPLEX_COMMAND_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            }
        )
        message = response.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            return parse_media_command(command, force_simple=True)
        try:
            parsed_command = json.loads(content)
        except json.JSONDecodeError:
            return parse_media_command(command, force_simple=True)
        if not isinstance(parsed_command, dict):
            return parse_media_command(command, force_simple=True)
        return parse_media_command(parsed_command, force_simple=True)

    async def _chat_with_fallback(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = self._fallback_model if self._fallback_model and time.monotonic() < self._fallback_until else self._model
        try:
            return await self._ollama.chat({**payload, "model": model})
        except Exception:
            if self._fallback_model is None or model == self._fallback_model:
                raise
            self._fallback_until = time.monotonic() + self._fallback_backoff_seconds
            self._logger.warning("media_player DSA model failed, retrying fallback_model=%s", self._fallback_model, exc_info=True)
            return await self._ollama.chat({**payload, "model": self._fallback_model})


def _targets_from_result(result: list[dict[str, Any]] | dict[str, Any]) -> list[MediaTarget] | dict[str, Any]:
    if isinstance(result, dict):
        return _failed_result("Nie mogę odczytać głośników z Home Assistant.")
    targets = _targets_from_player_mappings(result)
    if not targets:
        return _clarification_result("Nie znalazłem pasującego głośnika.")
    return targets


def _targets_from_player_mappings(players: list[dict[str, Any]]) -> list[MediaTarget]:
    targets = []
    for player in players:
        entity_id = player.get("entity_id")
        name = player.get("name")
        area_id = player.get("area_id")
        area_name = player.get("area_name")
        volume_level = player.get("volume_level")
        if not all(isinstance(value, str) and value for value in (entity_id, name, area_id, area_name)):
            continue
        targets.append(
            MediaTarget(
                entity_id=entity_id,
                name=name,
                area_id=area_id,
                area_name=area_name,
                volume_level=volume_level if isinstance(volume_level, (int, float)) else None,
                is_music_assistant=player.get("is_music_assistant") is True,
            )
        )
    return targets


def _dedupe_targets(targets: list[MediaTarget]) -> list[MediaTarget]:
    seen = set()
    deduped = []
    for target in targets:
        if target.entity_id in seen:
            continue
        seen.add(target.entity_id)
        deduped.append(target)
    return deduped


def _prefer_music_assistant_targets(targets: list[MediaTarget]) -> list[MediaTarget]:
    return _prefer_targets_by_area(targets, prefer_music_assistant=True)


def _prefer_direct_media_targets(targets: list[MediaTarget]) -> list[MediaTarget]:
    return _prefer_targets_by_area(targets, prefer_music_assistant=False)


def _prefer_targets_by_area(targets: list[MediaTarget], *, prefer_music_assistant: bool) -> list[MediaTarget]:
    preferred: list[MediaTarget] = []
    seen_area_ids = set()
    for target in targets:
        if target.area_id in seen_area_ids:
            continue
        seen_area_ids.add(target.area_id)
        area_targets = [candidate for candidate in targets if candidate.area_id == target.area_id]
        preferred_targets = [
            candidate for candidate in area_targets if candidate.is_music_assistant is prefer_music_assistant
        ]
        preferred.extend(preferred_targets or area_targets)
    return _dedupe_targets(preferred)


def _conversation_media_setting(conversation: Conversation, key: str, default: str) -> str:
    media_settings = conversation.user_settings.get("media")
    if not isinstance(media_settings, dict):
        return default
    value = media_settings.get(key)
    return value if isinstance(value, str) and value else default


def _player_state(players: list[dict[str, Any]], entity_id: str) -> str:
    for player in players:
        if player.get("entity_id") == entity_id and isinstance(player.get("state"), str):
            return player["state"]
    return ""


def _target_volume_after_delta(target: MediaTarget, delta: float) -> float:
    volume = target.volume_level if target.volume_level is not None else 0.5
    return min(1.0, max(0.0, volume + delta))


def _result_volume_level(result: dict[str, Any]) -> float | None:
    results = result.get("results")
    if not isinstance(results, list) or not results:
        volume = result.get("volume_level")
        return volume if isinstance(volume, (int, float)) else None
    volume = results[0].get("volume_level") if isinstance(results[0], dict) else None
    return volume if isinstance(volume, (int, float)) else None


def _best_search_item(response: Any) -> MediaSearchItem | None:
    for item in _iter_search_items(response):
        media_id = _first_string(item.get("uri"), item.get("item_id"), item.get("media_id"), item.get("id"))
        name = _first_string(item.get("name"), item.get("title"), item.get("sort_name"), media_id)
        if not media_id or not name:
            continue
        media_type = _first_string(item.get("media_type"), item.get("type"))
        artist = _first_string(item.get("artist"), item.get("artists"), item.get("album_artist"))
        album = _first_string(item.get("album"), item.get("album_name"))
        return MediaSearchItem(media_id=media_id, name=name, media_type=media_type, artist=artist, album=album)
    return None


def _iter_search_items(value: Any):
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(value, dict):
        return
    items = value.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                yield item
    for key in ("tracks", "albums", "artists", "playlists", "radio"):
        typed_items = value.get(key)
        if isinstance(typed_items, list):
            for item in typed_items:
                if isinstance(item, dict):
                    yield item


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item:
                    return item
                if isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str) and name:
                        return name
    return ""


def _ok_result(text: str, targets: list[MediaTarget]) -> dict[str, Any]:
    return {
        "status": "ok",
        "text": text,
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [f"media_player.{target.area_id}" for target in targets],
        "final_reply_mode": "verbatim",
    }


def _failed_result(text: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "text": text,
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
    }


def _clarification_result(question: str) -> dict[str, Any]:
    return {
        "status": "needs_clarification",
        "text": question,
        "needs_clarification": True,
        "clarification_question": question,
        "entities": [],
    }
