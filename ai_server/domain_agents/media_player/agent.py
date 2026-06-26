from __future__ import annotations

import json
import logging
import time
from typing import Any

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.domain_agents.media_player.formatting import (
    format_now_playing,
    format_playing_media,
    format_resumed,
    format_started,
    format_stopped,
    format_volume_level,
)
from ai_server.domain_agents.media_player.interfaces import MediaQueueSnapshot, MediaSearchItem, MediaTarget
from ai_server.domain_agents.media_player.messages import (
    MEDIA_COMPLEX_COMMAND_SYSTEM_PROMPT,
    MEDIA_QUERY_RESOLUTION_SYSTEM_PROMPT,
)
from ai_server.domain_agents.media_player.parser import (
    DEFAULT_VOLUME_DELTA,
    TINY_VOLUME_DELTA,
    ParsedMediaCommand,
    ascii_fold,
    parse_media_command,
)
from ai_server.home_assistant import HomeAssistantConnection
from ai_server.interfaces import Conversation
from ai_server.ollama_client import OLLAMA_BASE_URL, OllamaClient
from ai_server.utils.processing import ProcessingUpdateCallback, await_with_processing_updates


PLANNING_PROMPT = """
For media_player tasks:
- For music commands without a named room, omit areas; the media player agent will use conversation.area.
- For named rooms in media_player areas, output canonical area_id values from conversation.home_assistant_areas when it is present, not the user's inflected phrase.
- For one media request naming multiple rooms, create one media_player task with all named rooms in areas; do not split one media request into one task per room.
- Use all_speakers=true only when the user explicitly asks for all speakers/everywhere/whole house/wszystkie głośniki.
- Use replace_outputs=true only when the user explicitly asks for only that room/player, e.g. "only in the office" or "tylko w biurze".
- Use intent="transfer_playback" when the user asks to move/transfer currently playing music, e.g. "Przenieś muzykę do salonu", or asks to play generic music only in a specific room, e.g. "Graj muzykę tylko w biurze".
- Use intent="transfer_playback" for references to the currently playing music plus output targeting, e.g. "Graj tę muzykę na wszystkich głośnikach"; do not treat "tę muzykę" or "obecną muzykę" as a media search query.
- For relative volume change requests such as "głośniej", "ciszej", "ścisz", "przygłośnij", "odrobinkę", "troszkę", or "troszeczkę", use intent="volume_delta", preserve the original phrase in query, and omit volume_delta unless the user gives an explicit numeric delta; the media_player agent infers the exact step.
- Use intent="set_volume" only when the user asks for an absolute volume level such as "ustaw głośność na 10".
- For "moje ulubione", use query="Liked Songs" and media_type="playlist".
- For "TOK FM", use domain="media_player", query="TOK FM", and media_type="radio".

Command shape:
{
  "intent": "start_last|stop|volume_delta|set_volume|play_media|now_playing|transfer_playback",
  "query": "original user phrase or media search text",
  "media_type": "track|album|playlist|radio|artist optional",
  "areas": ["optional named rooms"],
  "all_speakers": false,
  "replace_outputs": false,
  "volume_level": 0.0,
  "volume_delta": 0.0
}
"""

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
        processing_update_interval_seconds: float = 5.0,
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
        self._processing_update_interval_seconds = processing_update_interval_seconds
        self._recent_media_by_user: dict[str, MediaSearchItem] = {}
        self._logger = logging.getLogger(f"{__name__}.MediaPlayerDomainAgent[{model}]")

    def known_utterances(self) -> dict[str, DomainTask]:
        return {
            "Spotify!": _known_task("start_last", "Spotify!"),
            "Graj muzykę": _known_task("start_last", "Graj muzykę"),
            "Grajh muzykę": _known_task("start_last", "Grajh muzykę"),
            "Włącz muzykę": _known_task("start_last", "Włącz muzykę"),
            "Dajcie tu jakąś muzyczkę": _known_task("start_last", "Dajcie tu jakąś muzyczkę"),
            "Cisza": _known_task("stop", "Cisza"),
            "Cicho": _known_task("stop", "Cicho"),
            "Zatrzymaj muzykę": _known_task("stop", "Zatrzymaj muzykę"),
            "Wyłącz muzykę": _known_task("stop", "Wyłącz muzykę"),
            "Co to teraz gra?": _known_task("now_playing", "Co to teraz gra?"),
            "Co to za muzyka?": _known_task("now_playing", "Co to za muzyka?"),
            "Kto to gra?": _known_task("now_playing", "Kto to gra?"),
            "Daj głośniej": _known_task("volume_delta", "Daj głośniej", volume_delta=DEFAULT_VOLUME_DELTA),
            "Przygłośnij muzykę": _known_task("volume_delta", "Przygłośnij muzykę", volume_delta=DEFAULT_VOLUME_DELTA),
            "Ścisz muzykę": _known_task("volume_delta", "Ścisz muzykę", volume_delta=-DEFAULT_VOLUME_DELTA),
            "Odrobinkę głośniej": _known_task("volume_delta", "Odrobinkę głośniej", volume_delta=TINY_VOLUME_DELTA),
            "Odrobinkę ciszej": _known_task("volume_delta", "Odrobinkę ciszej", volume_delta=-TINY_VOLUME_DELTA),
            "Troszkę głośniej": _known_task("volume_delta", "Troszkę głośniej", volume_delta=TINY_VOLUME_DELTA),
            "Troszkę ciszej": _known_task("volume_delta", "Troszkę ciszej", volume_delta=-TINY_VOLUME_DELTA),
            "Troszeczkę głośniej": _known_task("volume_delta", "Troszeczkę głośniej", volume_delta=TINY_VOLUME_DELTA),
            "Troszeczkęciszej": _known_task("volume_delta", "Troszeczkęciszej", volume_delta=-TINY_VOLUME_DELTA),
            "Graj muzykę rockową": _known_task("play_media", "muzykę rockową"),
            "Graj moje ulubione": _known_task("play_media", "Liked Songs", media_type="playlist"),
            "Włącz TOK FM w całym domu": _known_task(
                "play_media",
                "Włącz TOK FM w całym domu",
                media_type="radio",
                all_speakers=True,
            ),
        }

    def planning_prompt(self) -> str:
        return PLANNING_PROMPT

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
        self._logger.info(
            "media_player start_last targets conversation_id=%s target_ids=%s explicit_outputs=%s",
            conversation.conversation_id,
            _target_ids_text(targets),
            parsed.replace_outputs or _has_explicit_output_target(parsed),
        )

        if _all_targets_playing(targets):
            media = await self._current_music_assistant_media(targets, conversation_id=conversation.conversation_id)
            if media is not None:
                self._remember_recent_media(conversation, media)
            self._logger.info(
                "media_player start_last already_playing conversation_id=%s target_ids=%s media=%s",
                conversation.conversation_id,
                _target_ids_text(targets),
                _media_text(media),
            )
            if len(targets) > 1:
                grouped_playback_targets = await self._playback_targets_for_start(conversation, targets)
                if isinstance(grouped_playback_targets, dict):
                    return grouped_playback_targets
            return _ok_result("Muzyka już gra.", targets)

        has_explicit_output_target = parsed.replace_outputs or _has_explicit_output_target(parsed)
        if has_explicit_output_target:
            relocated = await self._relocate_current_queue(conversation, targets, replace_outputs=parsed.replace_outputs)
            if relocated is not None:
                return relocated

        media_source = ""
        media = (
            None
            if has_explicit_output_target
            else await self._current_music_assistant_media(conversation_id=conversation.conversation_id)
        )
        if media is not None:
            media_source = "playing_queue"
        if media is None:
            queue_snapshot = await self._current_music_assistant_queue(targets, conversation_id=conversation.conversation_id)
            if queue_snapshot is not None:
                playback_targets = await self._playback_targets_for_start(conversation, targets)
                if isinstance(playback_targets, dict):
                    return playback_targets
                self._logger.info(
                    "media_player resume_queue request conversation_id=%s target_ids=%s queue_entity_id=%s media=%s",
                    conversation.conversation_id,
                    _target_ids_text(playback_targets),
                    queue_snapshot.entity_id,
                    _media_text(queue_snapshot.media),
                )
                result = await self._connection.media_player_play([target.entity_id for target in playback_targets])
                if result.get("status") != "ok":
                    return _failed_result("Nie udało się wznowić muzyki.")
                self._logger.info(
                    "media_player resume_queue ready conversation_id=%s target_ids=%s queue_entity_id=%s",
                    conversation.conversation_id,
                    _target_ids_text(playback_targets),
                    queue_snapshot.entity_id,
                )
                return _ok_result(format_resumed(len(targets)), targets)
        if media is None:
            media = self._recent_media(conversation)
            if media is not None:
                media_source = "recent_cache"
        if media is None:
            media = MediaSearchItem(
                media_id=_conversation_media_setting(conversation, "default_music_media_id", self._default_music_media_id),
                name=_conversation_media_setting(conversation, "default_music_name", self._default_music_name),
                media_type=_conversation_media_setting(conversation, "default_music_media_type", self._default_music_media_type),
            )
            media_source = "default"
        self._logger.info(
            "media_player start_last media conversation_id=%s source=%s media=%s",
            conversation.conversation_id,
            media_source,
            _media_text(media),
        )
        playback_targets = await self._playback_targets_for_start(conversation, targets)
        if isinstance(playback_targets, dict):
            return playback_targets
        self._logger.info(
            "media_player playback request conversation_id=%s target_ids=%s media=%s",
            conversation.conversation_id,
            _target_ids_text(playback_targets),
            _media_text(media),
        )
        result = await self._connection.music_assistant_play_media(
            [target.entity_id for target in playback_targets],
            media_id=media.media_id,
            media_type=media.media_type,
            artist=media.artist,
            album=media.album,
        )
        if result.get("status") != "ok":
            return _failed_result("Nie udało się włączyć muzyki ze Spotify.")
        self._remember_recent_media(conversation, media)
        self._logger.info(
            "media_player playback started conversation_id=%s target_ids=%s media=%s",
            conversation.conversation_id,
            _target_ids_text(playback_targets),
            _media_text(media),
        )
        if _should_shuffle_media(media.media_type, ""):
            await self._shuffle_targets(playback_targets, conversation_id=conversation.conversation_id)
        return _ok_result(format_started(len(targets), media.name), targets)

    async def _stop(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        self._logger.info(
            "media_player stop request conversation_id=%s target_ids=%s",
            conversation.conversation_id,
            _target_ids_text(targets),
        )
        result = await self._connection.media_player_stop([target.entity_id for target in targets])
        if result.get("status") != "ok":
            return _failed_result("Nie udało się zatrzymać muzyki.")
        self._logger.info(
            "media_player stopped conversation_id=%s target_ids=%s",
            conversation.conversation_id,
            _target_ids_text(targets),
        )
        return _ok_result(format_stopped(len(targets)), targets)

    async def _volume_delta(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed, allow_playing_fallback=True)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        delta = parsed.volume_delta if parsed.volume_delta is not None else DEFAULT_VOLUME_DELTA
        self._logger.info(
            "media_player volume_delta request conversation_id=%s target_ids=%s delta=%.2f",
            conversation.conversation_id,
            _target_ids_text(targets),
            delta,
        )
        result = await self._connection.media_player_volume_delta([target.entity_id for target in targets], delta)
        if result.get("status") not in {"ok", "partial"}:
            return _failed_result("Nie udało się zmienić głośności.")
        level = _result_volume_level(result) or _target_volume_after_delta(targets[0], delta)
        self._logger.info(
            "media_player volume_delta applied conversation_id=%s target_ids=%s level=%.2f",
            conversation.conversation_id,
            _target_ids_text(targets),
            level,
        )
        return _ok_result(f"Głośność: {format_volume_level(level)}.", targets)

    async def _set_volume(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        if parsed.volume_level is None:
            return _clarification_result("Na jaką głośność mam ustawić muzykę?")
        targets = await self._targets(conversation, parsed, allow_playing_fallback=True)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        self._logger.info(
            "media_player set_volume request conversation_id=%s target_ids=%s level=%.2f",
            conversation.conversation_id,
            _target_ids_text(targets),
            parsed.volume_level,
        )
        result = await self._connection.media_player_volume_set([target.entity_id for target in targets], parsed.volume_level)
        if result.get("status") != "ok":
            return _failed_result("Nie udało się ustawić głośności.")
        self._logger.info(
            "media_player set_volume applied conversation_id=%s target_ids=%s level=%.2f",
            conversation.conversation_id,
            _target_ids_text(targets),
            parsed.volume_level,
        )
        return _ok_result(f"Ustawiłem głośność na {format_volume_level(parsed.volume_level)}.", targets)

    async def _play_media(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        if not parsed.query:
            return _clarification_result("Co mam włączyć?")
        targets = await self._targets(conversation, parsed)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_music_assistant_targets(targets)
        self._logger.info(
            "media_player play_media targets conversation_id=%s target_ids=%s query=%r media_type=%s",
            conversation.conversation_id,
            _target_ids_text(targets),
            parsed.query,
            parsed.media_type,
        )

        search_items = await self._search_media(conversation, parsed)
        if not search_items:
            return _failed_result(f"Nie znalazłem muzyki: {parsed.query}.")
        self._logger.info(
            "media_player search selected conversation_id=%s candidates=%s first=%s",
            conversation.conversation_id,
            len(search_items),
            _media_text(search_items[0]),
        )
        playback_targets = await self._playback_targets_for_start(conversation, targets)
        if isinstance(playback_targets, dict):
            return playback_targets
        for index, search_item in enumerate(search_items):
            self._logger.info(
                "media_player playback request conversation_id=%s target_ids=%s media=%s candidate=%s/%s",
                conversation.conversation_id,
                _target_ids_text(playback_targets),
                _media_text(search_item),
                index + 1,
                len(search_items),
            )
            result = await self._connection.music_assistant_play_media(
                [target.entity_id for target in playback_targets],
                media_id=search_item.media_id,
                media_type=search_item.media_type or parsed.media_type,
                artist=search_item.artist,
                album=search_item.album,
            )
            if result.get("status") == "ok":
                self._remember_recent_media(conversation, search_item)
                self._logger.info(
                    "media_player playback started conversation_id=%s target_ids=%s media=%s",
                    conversation.conversation_id,
                    _target_ids_text(playback_targets),
                    _media_text(search_item),
                )
                if _should_shuffle_media(search_item.media_type, parsed.media_type):
                    await self._shuffle_targets(playback_targets, conversation_id=conversation.conversation_id)
                return _ok_result(format_playing_media(search_item.name or parsed.query, len(targets)), targets)
            if not _is_unplayable_media_result(result) or index == len(search_items) - 1:
                return _failed_result("Nie udało się włączyć muzyki.")
            self._logger.info(
                "media_player playback candidate_unplayable conversation_id=%s media=%s",
                conversation.conversation_id,
                _media_text(search_item),
            )
        return _failed_result("Nie udało się włączyć muzyki.")

    async def _now_playing(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed, allow_playing_fallback=True)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_direct_media_targets(targets)
        self._logger.info(
            "media_player now_playing request conversation_id=%s target_id=%s",
            conversation.conversation_id,
            targets[0].entity_id,
        )
        result = await self._connection.media_player_now_playing(targets[0].entity_id)
        if result.get("status") != "ok":
            return _failed_result("Nie mogę teraz sprawdzić, co gra.")
        self._logger.info(
            "media_player now_playing result conversation_id=%s target_id=%s title=%r artist=%r",
            conversation.conversation_id,
            targets[0].entity_id,
            result.get("title"),
            result.get("artist"),
        )
        return _ok_result(format_now_playing(result), targets)

    async def _transfer_playback(self, conversation: Conversation, parsed: ParsedMediaCommand) -> dict[str, Any]:
        targets = await self._targets(conversation, parsed)
        if isinstance(targets, dict):
            return targets
        targets = _prefer_music_assistant_targets(targets)
        self._logger.info(
            "media_player transfer request conversation_id=%s target_ids=%s replace_outputs=%s",
            conversation.conversation_id,
            _target_ids_text(targets),
            parsed.replace_outputs,
        )
        relocated = await self._relocate_current_queue(conversation, targets, replace_outputs=parsed.replace_outputs)
        if relocated is None:
            return _failed_result("Nie znalazłem grającej muzyki do przeniesienia.")
        return relocated

    async def _playback_targets_for_start(
        self,
        conversation: Conversation,
        targets: list[MediaTarget],
    ) -> list[MediaTarget] | dict[str, Any]:
        desired_targets = _dedupe_targets(targets)
        if len(desired_targets) <= 1:
            return desired_targets
        source = _preferred_group_leader(conversation, desired_targets)
        already_grouped_entity_ids = {source.entity_id, *source.group_members}
        join_target_ids = [
            target.entity_id
            for target in desired_targets
            if target.entity_id != source.entity_id and target.entity_id not in already_grouped_entity_ids
        ]
        if not join_target_ids:
            self._logger.info(
                "media_player grouping already_ready conversation_id=%s leader=%s target_ids=%s",
                conversation.conversation_id,
                source.entity_id,
                _target_ids_text(desired_targets),
            )
            return [source]
        self._logger.info(
            "media_player grouping request conversation_id=%s leader=%s join_target_ids=%s",
            conversation.conversation_id,
            source.entity_id,
            _entity_ids_text(join_target_ids),
        )
        join_result = await self._connection.media_player_join(source.entity_id, join_target_ids)
        if join_result.get("status") != "ok":
            self._logger.warning(
                "Home Assistant media_player join failed before starting named media source_player=%s "
                "target_entity_ids=%s join_result=%r",
                source.entity_id,
                join_target_ids,
                join_result,
            )
            return _failed_result("Nie udało się połączyć głośników.")
        self._logger.info(
            "media_player grouping ready conversation_id=%s leader=%s target_ids=%s",
            conversation.conversation_id,
            source.entity_id,
            _target_ids_text(desired_targets),
        )
        return [source]

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
            self._logger.info(
                "media_player search alias conversation_id=%s query=%r media=liked_songs",
                conversation.conversation_id,
                parsed.query,
            )
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
            self._logger.info(
                "media_player search alias conversation_id=%s query=%r target=%r media_type=%s",
                conversation.conversation_id,
                parsed.query,
                query,
                media_type,
            )
        else:
            query = resolved_query
            media_type = resolved_media_type
        self._logger.info(
            "media_player search request conversation_id=%s query=%r media_type=%s",
            conversation.conversation_id,
            query,
            media_type,
        )
        result = await self._connection.music_assistant_search(name=query, media_type=media_type, limit=5)
        if result.get("status") == "ok":
            items = _search_items(result.get("response"))
            self._logger.info(
                "media_player search result conversation_id=%s status=ok candidates=%s",
                conversation.conversation_id,
                len(items),
            )
            if items:
                return items
        if result.get("error") == "music_assistant_config_entry_not_found":
            self._logger.info(
                "media_player search unavailable conversation_id=%s fallback=raw_media_id media_id=%r",
                conversation.conversation_id,
                query,
            )
            return [MediaSearchItem(media_id=query, name=query, media_type=media_type)]
        self._logger.info(
            "media_player search result conversation_id=%s status=%s candidates=0",
            conversation.conversation_id,
            result.get("status"),
        )
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
                },
                processing_update_callback=conversation.processing_update_callback,
                purpose="media_query_resolution",
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
            self._logger.info(
                "media_player relocate skipped conversation_id=%s reason=no_playing_queue target_ids=%s",
                conversation.conversation_id,
                _target_ids_text(targets),
            )
            return None
        source = playing_targets[0]
        requested_targets = _dedupe_targets(targets)
        desired_targets = requested_targets if replace_outputs else _dedupe_targets([*playing_targets, *requested_targets])
        desired_entity_ids = {target.entity_id for target in desired_targets}
        playing_entity_ids = {target.entity_id for target in playing_targets}
        self._logger.info(
            "media_player relocate request conversation_id=%s source=%s target_ids=%s replace_outputs=%s",
            conversation.conversation_id,
            source.entity_id,
            _target_ids_text(desired_targets),
            replace_outputs,
        )
        if playing_entity_ids == desired_entity_ids:
            self._logger.info(
                "media_player relocate already_ready conversation_id=%s target_ids=%s",
                conversation.conversation_id,
                _entity_ids_text(sorted(desired_entity_ids)),
            )
            return _ok_result("Muzyka już gra.", targets)
        media = await self._current_music_assistant_media([source], conversation_id=conversation.conversation_id)
        if media is not None:
            self._remember_recent_media(conversation, media)

        join_target_ids = [
            target.entity_id
            for target in desired_targets
            if target.entity_id != source.entity_id and target.entity_id not in playing_entity_ids
        ]
        join_result: dict[str, Any] = {"status": "ok"}
        if join_target_ids:
            self._logger.info(
                "media_player relocate join request conversation_id=%s source=%s join_target_ids=%s",
                conversation.conversation_id,
                source.entity_id,
                _entity_ids_text(join_target_ids),
            )
            join_result = await self._connection.media_player_join(source.entity_id, join_target_ids)

        unjoin_result: dict[str, Any] = {"status": "ok"}
        join_reached = join_result.get("status") == "ok"
        if not join_reached and join_target_ids:
            join_reached = await self._relocation_reached_targets(
                desired_entity_ids,
                replace_outputs=False,
                conversation_id=conversation.conversation_id,
            )

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
                self._logger.info(
                    "media_player relocate unjoin request conversation_id=%s unjoin_target_ids=%s",
                    conversation.conversation_id,
                    _entity_ids_text(unjoin_entity_ids),
                )
                unjoin_result = await self._connection.media_player_unjoin(unjoin_entity_ids)

        if join_result.get("status") == "ok" and unjoin_result.get("status") == "ok":
            self._logger.info(
                "media_player relocate ready conversation_id=%s target_ids=%s",
                conversation.conversation_id,
                _target_ids_text(desired_targets),
            )
            return _ok_result(format_started(len(desired_targets), "muzykę"), desired_targets)

        if await self._relocation_reached_targets(
            desired_entity_ids,
            replace_outputs=replace_outputs,
            conversation_id=conversation.conversation_id,
        ):
            self._logger.info(
                "media_player relocate verified_ready conversation_id=%s target_ids=%s",
                conversation.conversation_id,
                _entity_ids_text(sorted(desired_entity_ids)),
            )
            return _ok_result(format_started(len(desired_targets), "muzykę"), desired_targets)

        self._logger.warning(
            "Home Assistant media_player join/unjoin did not reach requested outputs; falling back to Music Assistant transfer "
            "source_player=%s target_entity_ids=%s join_result=%r unjoin_result=%r",
            source.entity_id,
            sorted(desired_entity_ids),
            join_result,
            unjoin_result,
        )
        self._logger.info(
            "media_player transfer_queue request conversation_id=%s source=%s target_ids=%s",
            conversation.conversation_id,
            source.entity_id,
            _target_ids_text(desired_targets),
        )
        result = await self._connection.music_assistant_transfer_queue(
            [target.entity_id for target in desired_targets],
            source_player=source.entity_id,
            auto_play=True,
        )
        if result.get("status") != "ok":
            return _failed_result("Nie udało się przenieść muzyki.")
        self._logger.info(
            "media_player transfer_queue ready conversation_id=%s target_ids=%s",
            conversation.conversation_id,
            _target_ids_text(desired_targets),
        )
        return _ok_result(format_started(len(desired_targets), "muzykę"), desired_targets)

    async def _relocation_reached_targets(
        self,
        desired_entity_ids: set[str],
        *,
        replace_outputs: bool,
        conversation_id: str = "",
    ) -> bool:
        refresh_inventory = getattr(self._connection, "refresh_inventory", None)
        if callable(refresh_inventory):
            try:
                await refresh_inventory()
            except Exception:
                self._logger.info(
                    "media_player relocation inventory_refresh_failed conversation_id=%s",
                    conversation_id,
                    exc_info=True,
                )
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

    async def _current_music_assistant_media(
        self,
        targets: list[MediaTarget] | None = None,
        *,
        conversation_id: str = "",
    ) -> MediaSearchItem | None:
        queue_snapshot = await self._current_music_assistant_queue(targets, conversation_id=conversation_id)
        if queue_snapshot is None:
            return None
        return queue_snapshot.media

    async def _current_music_assistant_queue(
        self,
        targets: list[MediaTarget] | None = None,
        *,
        conversation_id: str = "",
    ) -> MediaQueueSnapshot | None:
        if targets is None:
            targets = await self._playing_music_assistant_targets()
        for target in targets:
            if not target.is_music_assistant:
                continue
            try:
                self._logger.info(
                    "media_player queue request conversation_id=%s target_id=%s",
                    conversation_id,
                    target.entity_id,
                )
                result = await self._connection.music_assistant_get_queue(target.entity_id)
            except Exception:
                self._logger.info(
                    "media_player queue unavailable conversation_id=%s target_id=%s",
                    conversation_id,
                    target.entity_id,
                    exc_info=True,
                )
                continue
            if result.get("status") != "ok":
                self._logger.info(
                    "media_player queue result conversation_id=%s target_id=%s status=%s",
                    conversation_id,
                    target.entity_id,
                    result.get("status"),
                )
                continue
            queue_snapshot = _queue_snapshot_from_response(result.get("response"), target.entity_id)
            if queue_snapshot is not None:
                self._logger.info(
                    "media_player queue current conversation_id=%s target_id=%s items=%s current_index=%s media=%s",
                    conversation_id,
                    target.entity_id,
                    queue_snapshot.item_count,
                    queue_snapshot.current_index,
                    _media_text(queue_snapshot.media),
                )
                return queue_snapshot
        return None

    async def _shuffle_targets(self, targets: list[MediaTarget], *, conversation_id: str = "") -> None:
        self._logger.info(
            "media_player shuffle request conversation_id=%s target_ids=%s enabled=true",
            conversation_id,
            _target_ids_text(targets),
        )
        result = await self._connection.media_player_shuffle_set([target.entity_id for target in targets], True)
        if result.get("status") != "ok":
            self._logger.warning("failed to enable shuffle for media targets result=%r", result)
            return
        self._logger.info(
            "media_player shuffle enabled conversation_id=%s target_ids=%s",
            conversation_id,
            _target_ids_text(targets),
        )

    def _remember_recent_media(self, conversation: Conversation, media: MediaSearchItem) -> None:
        self._recent_media_by_user[_conversation_user_key(conversation)] = media
        self._logger.info(
            "media_player recent_media remembered conversation_id=%s user=%s media=%s",
            conversation.conversation_id,
            _conversation_user_key(conversation),
            _media_text(media),
        )

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
            },
            processing_update_callback=conversation.processing_update_callback,
            purpose="complex_command_parse",
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

    async def _chat_with_fallback(
        self,
        payload: dict[str, Any],
        *,
        processing_update_callback: ProcessingUpdateCallback | None = None,
        purpose: str = "media_player_chat",
    ) -> dict[str, Any]:
        model = self._fallback_model if self._fallback_model and time.monotonic() < self._fallback_until else self._model
        try:
            return await self._chat_once_with_logging(
                payload,
                model=model,
                purpose=purpose,
                processing_update_callback=processing_update_callback,
            )
        except Exception:
            if self._fallback_model is None or model == self._fallback_model:
                raise
            self._fallback_until = time.monotonic() + self._fallback_backoff_seconds
            self._logger.warning("media_player DSA model failed, retrying fallback_model=%s", self._fallback_model, exc_info=True)
            return await self._chat_once_with_logging(
                payload,
                model=self._fallback_model,
                purpose=purpose,
                processing_update_callback=processing_update_callback,
            )

    async def _chat_once_with_logging(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        purpose: str,
        processing_update_callback: ProcessingUpdateCallback | None,
    ) -> dict[str, Any]:
        self._logger.info("media_player LLM request purpose=%s model=%s", purpose, model)
        started_at = time.monotonic()
        response = await await_with_processing_updates(
            self._ollama.chat({**payload, "model": model}),
            callback=processing_update_callback,
            logger=self._logger,
            interval_seconds=self._processing_update_interval_seconds,
        )
        duration_ms = int((time.monotonic() - started_at) * 1000)
        prompt_tokens, completion_tokens, total_tokens = _ollama_token_counts(response)
        self._logger.info(
            "media_player LLM reply purpose=%s model=%s duration_ms=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
            purpose,
            model,
            duration_ms,
            prompt_tokens,
            completion_tokens,
            total_tokens,
        )
        return response


def _target_ids_text(targets: list[MediaTarget]) -> str:
    return _entity_ids_text([target.entity_id for target in targets])


def _entity_ids_text(entity_ids: list[str]) -> str:
    return ",".join(entity_ids)


def _media_text(media: MediaSearchItem | None) -> str:
    if media is None:
        return "none"
    label = media.name or media.media_id
    if media.media_type:
        return f"{media.media_type}:{label}"
    return label


def _ollama_token_counts(response: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    prompt_tokens = _int_count_or_none(response.get("prompt_eval_count"))
    completion_tokens = _int_count_or_none(response.get("eval_count"))
    total_tokens = prompt_tokens + completion_tokens if prompt_tokens is not None and completion_tokens is not None else None
    return prompt_tokens, completion_tokens, total_tokens


def _int_count_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


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


def _preferred_group_leader(conversation: Conversation, targets: list[MediaTarget]) -> MediaTarget:
    if conversation.area:
        normalized_area = ascii_fold(conversation.area).lower()
        for target in targets:
            if normalized_area in {ascii_fold(target.area_id).lower(), ascii_fold(target.area_name).lower()}:
                return target
    return targets[0]


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
    queue_snapshot = _queue_snapshot_from_response(response, entity_id)
    if queue_snapshot is None:
        return None
    return queue_snapshot.media


def _queue_snapshot_from_response(response: Any, entity_id: str) -> MediaQueueSnapshot | None:
    queue = _queue_mapping(response, entity_id)
    if queue is None:
        return None
    item_count = _queue_item_count(queue)
    current_index = _queue_current_index(queue)
    item = queue.get("current_item")
    if not isinstance(item, dict) and (item_count is None or item_count <= 0):
        return None
    media = _media_from_queue_mapping(queue)
    if media is None:
        media = MediaSearchItem(
            media_id=_first_string(queue.get("queue_id"), entity_id),
            name=_first_string(queue.get("name"), "kolejkę"),
            media_type="queue",
        )
    return MediaQueueSnapshot(
        entity_id=entity_id,
        media=media,
        item_count=item_count,
        current_index=current_index,
    )


def _media_from_queue_mapping(queue: dict[str, Any]) -> MediaSearchItem | None:
    item = queue.get("current_item")
    if not isinstance(item, dict):
        return None
    media_item = item.get("media_item") if isinstance(item.get("media_item"), dict) else {}
    media_id = _first_string(
        media_item.get("uri"),
        item.get("uri"),
        media_item.get("media_id"),
        item.get("media_id"),
        media_item.get("item_id"),
        item.get("item_id"),
        media_item.get("id"),
        item.get("id"),
        item.get("name"),
        media_item.get("name"),
    )
    name = _first_string(item.get("name"), item.get("title"), media_item.get("name"), media_item.get("title"), media_id)
    if not media_id or not name:
        return None
    media_type = _first_string(media_item.get("media_type"), item.get("media_type"), media_item.get("type"), item.get("type"))
    artist = _first_string(
        media_item.get("artist"),
        item.get("artist"),
        media_item.get("artists"),
        item.get("artists"),
        media_item.get("album_artist"),
        item.get("album_artist"),
    )
    album_mapping = media_item.get("album") if isinstance(media_item.get("album"), dict) else {}
    album = _first_string(
        album_mapping.get("name"),
        media_item.get("album"),
        item.get("album"),
        media_item.get("album_name"),
        item.get("album_name"),
    )
    return MediaSearchItem(media_id=media_id, name=name, media_type=media_type, artist=artist, album=album)


def _queue_item_count(queue: dict[str, Any]) -> int | None:
    items = queue.get("items")
    if isinstance(items, bool):
        return None
    if isinstance(items, int):
        return items
    if isinstance(items, list):
        return len(items)
    return None


def _queue_current_index(queue: dict[str, Any]) -> int | None:
    current_index = queue.get("current_index")
    if isinstance(current_index, bool) or not isinstance(current_index, int):
        return None
    return current_index


def _queue_mapping(response: Any, entity_id: str) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    queue = response.get(entity_id)
    if isinstance(queue, dict):
        return queue
    if isinstance(response.get("current_item"), dict) or isinstance(response.get("items"), (int, list)):
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


def _known_task(intent: str, query: str, **command_options: Any) -> DomainTask:
    command = {"intent": intent, "query": query, **command_options}
    return {
        "id": "t1",
        "domain": "media_player",
        "command": command,
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


def _clarification_result(question: str) -> dict[str, Any]:
    return {
        "status": "needs_clarification",
        "text": question,
        "needs_clarification": True,
        "clarification_question": question,
        "entities": [],
    }
