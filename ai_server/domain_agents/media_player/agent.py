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
from ai_server.domain_agents.media_player.messages import (
    MEDIA_COMPLEX_COMMAND_SYSTEM_PROMPT,
    MEDIA_QUERY_RESOLUTION_SYSTEM_PROMPT,
)
from ai_server.domain_agents.media_player.parser import DEFAULT_VOLUME_DELTA, ParsedMediaCommand, ascii_fold, parse_media_command
from ai_server.home_assistant import HomeAssistantConnection
from ai_server.interfaces import Conversation
from ai_server.ollama_client import OLLAMA_BASE_URL, OllamaClient


MEDIA_QUERY_RESOLUTION_NUM_PREDICT = 512


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
        self._recent_media_by_user: dict[str, MediaSearchItem] = {}
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
        if parsed.intent == "transfer_playback":
            return await self._transfer_playback(conversation, parsed)
        return _clarification_result("Jaką muzykę albo który głośnik mam obsłużyć?")

    async def close(self) -> None:
        if self._owns_ollama:
            await self._ollama.close()

    async def _start_last(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_music_assistant_targets(targets)

        if _all_targets_playing(targets):
            media = await self._current_music_assistant_media(targets)
            if media is not None:
                self._remember_recent_media(conversation, media)
            return _ok_result("Muzyka już gra.", targets)

        if parsed.replace_outputs or _has_explicit_output_target(parsed):
            relocated = await self._relocate_current_queue(conversation, targets, replace_outputs=parsed.replace_outputs)
            if relocated is not None:
                return relocated

        media = await self._current_music_assistant_media()
        if media is None:
            media = self._recent_media(conversation)
        if media is None:
            media = MediaSearchItem(
                media_id=_conversation_media_setting(conversation, "default_music_media_id", self._default_music_media_id),
                name=_conversation_media_setting(conversation, "default_music_name", self._default_music_name),
                media_type=_conversation_media_setting(conversation, "default_music_media_type", self._default_music_media_type),
            )
        result = await self._connection.music_assistant_play_media(
            [target.entity_id for target in targets],
            media_id=media.media_id,
            media_type=media.media_type,
            artist=media.artist,
            album=media.album,
        )
        if result.get("status") != "ok":
            return _failed_result("Nie udało się włączyć muzyki ze Spotify.")
        self._remember_recent_media(conversation, media)
        if _should_shuffle_media(media.media_type, ""):
            await self._shuffle_targets(targets)
        return _ok_result(format_started(len(targets), media.name), targets)

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

        search_items = await self._search_media(conversation, parsed)
        if not search_items:
            return _failed_result(f"Nie znalazłem muzyki: {parsed.query}.")
        for index, search_item in enumerate(search_items):
            result = await self._connection.music_assistant_play_media(
                [target.entity_id for target in targets],
                media_id=search_item.media_id,
                media_type=search_item.media_type or parsed.media_type,
                artist=search_item.artist,
                album=search_item.album,
            )
            if result.get("status") == "ok":
                self._remember_recent_media(conversation, search_item)
                if _should_shuffle_media(search_item.media_type, parsed.media_type):
                    await self._shuffle_targets(targets)
                return _ok_result(format_playing_media(search_item.name or parsed.query, len(targets)), targets)
            if not _is_unplayable_media_result(result) or index == len(search_items) - 1:
                return _failed_result("Nie udało się włączyć muzyki.")
            self._logger.info(
                "Music Assistant could not play search candidate; trying next media_id=%r name=%r",
                search_item.media_id,
                search_item.name,
            )
        return _failed_result("Nie udało się włączyć muzyki.")

    async def _now_playing(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed, allow_playing_fallback=True)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        result = await self._connection.media_player_now_playing(targets[0].entity_id)
        if result.get("status") != "ok":
            return _failed_result("Nie mogę teraz sprawdzić, co gra.")
        return _ok_result(format_now_playing(result), targets)

    async def _transfer_playback(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_music_assistant_targets(targets)
        relocated = await self._relocate_current_queue(conversation, targets, replace_outputs=parsed.replace_outputs)
        if relocated is None:
            return _failed_result("Nie znalazłem grającej muzyki do przeniesienia.")
        return relocated

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

    async def _search_media(self, conversation: Conversation, parsed: ParsedMediaCommand) -> list[MediaSearchItem]:
        if parsed.query == "Liked Songs":
            return [
                MediaSearchItem(
                    media_id=_conversation_media_setting(conversation, "liked_songs_media_id", self._liked_songs_media_id),
                    name=_conversation_media_setting(conversation, "liked_songs_name", "Liked Songs"),
                    media_type=_conversation_media_setting(conversation, "liked_songs_media_type", self._liked_songs_media_type),
                )
            ]
        aliases = _conversation_media_aliases(conversation)
        resolved_alias = _matching_media_alias(aliases, parsed.query)
        resolved_query = parsed.query
        resolved_media_type = parsed.media_type
        if resolved_alias is None and aliases:
            resolved = await self._resolve_media_query(conversation, parsed, aliases)
            resolved_alias = _alias_by_name(aliases, _string_or_empty(resolved.get("alias")))
            resolved_query = _string_or_empty(resolved.get("query")) or parsed.query
            resolved_media_type = _string_or_empty(resolved.get("media_type")) or parsed.media_type
        if resolved_alias is not None:
            query = resolved_alias["target"]
            media_type = resolved_alias["media_type"]
        else:
            query = resolved_query
            media_type = resolved_media_type
        result = await self._connection.music_assistant_search(name=query, media_type=media_type, limit=5)
        if result.get("status") == "ok":
            items = _search_items(result.get("response"))
            if items:
                return items
        if result.get("error") == "music_assistant_config_entry_not_found":
            self._logger.info("Music Assistant search unavailable; using raw play_media media_id=%r", query)
            return [MediaSearchItem(media_id=query, name=query, media_type=media_type)]
        return []

    async def _resolve_media_query(
        self,
        conversation: Conversation,
        parsed: ParsedMediaCommand,
        aliases: list[dict[str, str]],
    ) -> dict[str, Any]:
        payload = {
            "query": parsed.query,
            "media_type": parsed.media_type,
            "aliases": aliases,
            "conversation": {
                "area": conversation.area,
                "user": conversation.user,
            },
        }
        try:
            response = await self._chat_with_fallback(
                {
                    "raw": False,
                    "think": False,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "1h",
                    "options": {
                        "num_predict": MEDIA_QUERY_RESOLUTION_NUM_PREDICT,
                        "temperature": 0,
                        "num_ctx": 4096,
                    },
                    "messages": [
                        {"role": "system", "content": MEDIA_QUERY_RESOLUTION_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                }
            )
        except Exception:
            self._logger.info("media query resolver failed; using original query=%r", parsed.query, exc_info=True)
            return {}
        message = response.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        done_reason = response.get("done_reason")
        if not isinstance(content, str) or not content.strip():
            self._logger.warning(
                "media query resolver returned no JSON; using original query=%r done_reason=%r num_predict=%s suggestion='increase num_predict'",
                parsed.query,
                done_reason,
                MEDIA_QUERY_RESOLUTION_NUM_PREDICT,
            )
            return {}
        try:
            resolved = json.loads(content)
        except json.JSONDecodeError:
            self._logger.warning(
                "media query resolver returned invalid JSON; using original query=%r done_reason=%r num_predict=%s suggestion='increase num_predict'",
                parsed.query,
                done_reason,
                MEDIA_QUERY_RESOLUTION_NUM_PREDICT,
            )
            return {}
        if not isinstance(resolved, dict):
            self._logger.warning(
                "media query resolver returned non-object JSON; using original query=%r done_reason=%r num_predict=%s suggestion='increase num_predict'",
                parsed.query,
                done_reason,
                MEDIA_QUERY_RESOLUTION_NUM_PREDICT,
            )
            return {}
        return resolved

    async def _relocate_current_queue(
        self,
        conversation: Conversation,
        targets: list[MediaTarget],
        *,
        replace_outputs: bool = False,
    ) -> dict[str, Any] | None:
        playing_targets = await self._playing_music_assistant_targets()
        if not playing_targets:
            return None
        source = playing_targets[0]
        requested_targets = _dedupe_targets(targets)
        desired_targets = requested_targets if replace_outputs else _dedupe_targets([*playing_targets, *requested_targets])
        desired_entity_ids = {target.entity_id for target in desired_targets}
        playing_entity_ids = {target.entity_id for target in playing_targets}
        if playing_entity_ids == desired_entity_ids:
            return _ok_result("Muzyka już gra.", targets)
        media = await self._current_music_assistant_media([source])
        if media is not None:
            self._remember_recent_media(conversation, media)

        join_target_ids = [
            target.entity_id
            for target in desired_targets
            if target.entity_id != source.entity_id and target.entity_id not in playing_entity_ids
        ]
        join_result: dict[str, Any] = {"status": "ok"}
        if join_target_ids:
            join_result = await self._connection.media_player_join(source.entity_id, join_target_ids)

        unjoin_result: dict[str, Any] = {"status": "ok"}
        join_reached = join_result.get("status") == "ok"
        if not join_reached and join_target_ids:
            join_reached = await self._relocation_reached_targets(desired_entity_ids, replace_outputs=False)

        if replace_outputs and join_reached:
            unjoin_entity_ids = _dedupe_strings(
                [
                    target.entity_id
                    for target in playing_targets
                    if target.entity_id not in desired_entity_ids
                ]
                + [
                    member
                    for target in playing_targets
                    for member in target.group_members
                    if member not in desired_entity_ids
                ]
            )
            if unjoin_entity_ids:
                unjoin_result = await self._connection.media_player_unjoin(unjoin_entity_ids)

        if join_result.get("status") == "ok" and unjoin_result.get("status") == "ok":
            return _ok_result(format_started(len(desired_targets), "muzykę"), desired_targets)

        if await self._relocation_reached_targets(desired_entity_ids, replace_outputs=replace_outputs):
            return _ok_result(format_started(len(desired_targets), "muzykę"), desired_targets)

        self._logger.warning(
            "Home Assistant media_player join/unjoin did not reach requested outputs; falling back to Music Assistant transfer "
            "source_player=%s target_entity_ids=%s join_result=%r unjoin_result=%r",
            source.entity_id,
            sorted(desired_entity_ids),
            join_result,
            unjoin_result,
        )
        result = await self._connection.music_assistant_transfer_queue(
            [target.entity_id for target in desired_targets],
            source_player=source.entity_id,
            auto_play=True,
        )
        if result.get("status") != "ok":
            return _failed_result("Nie udało się przenieść muzyki.")
        return _ok_result(format_started(len(desired_targets), "muzykę"), desired_targets)

    async def _relocation_reached_targets(self, desired_entity_ids: set[str], *, replace_outputs: bool) -> bool:
        refresh_inventory = getattr(self._connection, "refresh_inventory", None)
        if callable(refresh_inventory):
            try:
                await refresh_inventory()
            except Exception:
                self._logger.info("could not refresh Home Assistant inventory after media_player join/unjoin", exc_info=True)
        result = await self._list_speaker_players()
        if not isinstance(result, list):
            return False
        targets = _prefer_music_assistant_targets(_targets_from_player_mappings(result))
        targets_by_entity_id = {target.entity_id: target for target in targets}
        if not all(_entity_is_active_output(entity_id, targets_by_entity_id) for entity_id in desired_entity_ids):
            return False
        if not replace_outputs:
            return True
        return not any(
            _entity_is_active_output(target.entity_id, targets_by_entity_id)
            for target in targets
            if target.entity_id not in desired_entity_ids
        )

    async def _playing_music_assistant_targets(self) -> list[MediaTarget]:
        result = await self._list_speaker_players()
        if not isinstance(result, list):
            return []
        targets = _prefer_music_assistant_targets(_targets_from_player_mappings(result))
        return [target for target in targets if target.is_music_assistant and target.state == "playing"]

    async def _current_music_assistant_media(self, targets: list[MediaTarget] | None = None) -> MediaSearchItem | None:
        if targets is None:
            targets = await self._playing_music_assistant_targets()
        for target in targets:
            if not target.is_music_assistant:
                continue
            try:
                result = await self._connection.music_assistant_get_queue(target.entity_id)
            except Exception:
                self._logger.info("Music Assistant get_queue unavailable for entity_id=%s", target.entity_id, exc_info=True)
                continue
            if result.get("status") != "ok":
                continue
            media = _media_from_queue_response(result.get("response"), target.entity_id)
            if media is not None:
                return media
        return None

    async def _shuffle_targets(self, targets: list[MediaTarget]) -> None:
        result = await self._connection.media_player_shuffle_set([target.entity_id for target in targets], True)
        if result.get("status") != "ok":
            self._logger.warning("failed to enable shuffle for media targets result=%r", result)

    def _remember_recent_media(self, conversation: Conversation, media: MediaSearchItem) -> None:
        self._recent_media_by_user[_conversation_user_key(conversation)] = media

    def _recent_media(self, conversation: Conversation) -> MediaSearchItem | None:
        return self._recent_media_by_user.get(_conversation_user_key(conversation))

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
        raw_group_members = player.get("group_members")
        group_members = (
            tuple(member for member in raw_group_members if isinstance(member, str))
            if isinstance(raw_group_members, list)
            else ()
        )
        targets.append(
            MediaTarget(
                entity_id=entity_id,
                name=name,
                area_id=area_id,
                area_name=area_name,
                volume_level=volume_level if isinstance(volume_level, (int, float)) else None,
                is_music_assistant=player.get("is_music_assistant") is True,
                state=player.get("state") if isinstance(player.get("state"), str) else "",
                group_members=group_members,
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


def _dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
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


def _conversation_media_aliases(conversation: Conversation) -> list[dict[str, str]]:
    media_settings = conversation.user_settings.get("media")
    if not isinstance(media_settings, dict):
        return []
    aliases: list[dict[str, str]] = []
    for key, raw_aliases in media_settings.items():
        if not isinstance(key, str) or not key.endswith("_aliases") or not isinstance(raw_aliases, dict):
            continue
        media_type = key[: -len("_aliases")]
        if media_type == "media":
            media_type = ""
        for raw_alias, raw_target in raw_aliases.items():
            if not isinstance(raw_alias, str) or not raw_alias or not isinstance(raw_target, str) or not raw_target:
                continue
            aliases.append({"alias": raw_alias, "target": raw_target, "media_type": media_type})
    return aliases


def _matching_media_alias(aliases: list[dict[str, str]], query: str) -> dict[str, str] | None:
    normalized_query = _normalize_media_lookup(query)
    for alias in aliases:
        if _normalize_media_lookup(alias["alias"]) == normalized_query:
            return alias
    return None


def _alias_by_name(aliases: list[dict[str, str]], alias_name: str) -> dict[str, str] | None:
    if not alias_name:
        return None
    for alias in aliases:
        if alias["alias"] == alias_name:
            return alias
    return None


def _string_or_empty(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _conversation_user_key(conversation: Conversation) -> str:
    return conversation.user or "__default__"


def _has_explicit_output_target(parsed: ParsedMediaCommand) -> bool:
    return parsed.all_speakers or bool(parsed.areas)


def _all_targets_playing(targets: list[MediaTarget]) -> bool:
    return bool(targets) and all(target.state == "playing" for target in targets)


def _entity_is_active_output(entity_id: str, targets_by_entity_id: dict[str, MediaTarget]) -> bool:
    target = targets_by_entity_id.get(entity_id)
    if target is not None and target.state == "playing":
        return True
    return any(target.state == "playing" and entity_id in target.group_members for target in targets_by_entity_id.values())


def _should_shuffle_media(*media_types: str) -> bool:
    return any(media_type == "playlist" for media_type in media_types)


def _media_from_queue_response(response: Any, entity_id: str) -> MediaSearchItem | None:
    queue = _queue_mapping(response, entity_id)
    if queue is None:
        return None
    item = queue.get("current_item")
    if not isinstance(item, dict):
        return None
    media_id = _first_string(item.get("uri"), item.get("media_id"), item.get("item_id"), item.get("id"), item.get("name"))
    name = _first_string(item.get("name"), item.get("title"), media_id)
    if not media_id or not name:
        return None
    media_type = _first_string(item.get("media_type"), item.get("type"))
    artist = _first_string(item.get("artist"), item.get("artists"), item.get("album_artist"))
    album = _first_string(item.get("album"), item.get("album_name"))
    return MediaSearchItem(media_id=media_id, name=name, media_type=media_type, artist=artist, album=album)


def _queue_mapping(response: Any, entity_id: str) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    queue = response.get(entity_id)
    if isinstance(queue, dict):
        return queue
    if isinstance(response.get("current_item"), dict):
        return response
    return None


def _normalize_media_lookup(value: str) -> str:
    return ascii_fold(value).casefold().strip()


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


def _search_items(response: Any) -> list[MediaSearchItem]:
    items = []
    seen_media_ids = set()
    for item in _iter_search_items(response):
        media_id = _first_string(item.get("uri"), item.get("item_id"), item.get("media_id"), item.get("id"))
        name = _first_string(item.get("name"), item.get("title"), item.get("sort_name"), media_id)
        if not media_id or not name or media_id in seen_media_ids:
            continue
        seen_media_ids.add(media_id)
        media_type = _first_string(item.get("media_type"), item.get("type"))
        artist = _first_string(item.get("artist"), item.get("artists"), item.get("album_artist"))
        album = _first_string(item.get("album"), item.get("album_name"))
        items.append(MediaSearchItem(media_id=media_id, name=name, media_type=media_type, artist=artist, album=album))
    return items


def _is_unplayable_media_result(result: dict[str, Any]) -> bool:
    if result.get("status") == "ok":
        return False
    message = result.get("message")
    return isinstance(message, str) and "No playable item found to start playback" in message


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
