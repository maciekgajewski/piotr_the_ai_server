import asyncio
import json

from ai_server.domain_agents.media_player import MediaPlayerDomainAgent
from ai_server.domain_agents.media_player.parser import media_task_from_utterance, parse_media_command
from ai_server.interfaces import Conversation
from ai_server.orchestrator.known_utterances import known_utterance_task


def test_media_known_utterance_routes_to_media_player() -> None:
    task = known_utterance_task("Spotify!")

    assert task["domain"] == "media_player"
    assert task["command"] == {"intent": "start_last", "query": "Spotify!"}


def test_media_simple_short_path_parses_volume_up() -> None:
    task = media_task_from_utterance("Daj głośniej")

    assert task["domain"] == "media_player"
    assert task["command"]["intent"] == "volume_delta"
    assert task["command"]["volume_delta"] == 0.10


def test_media_simple_short_path_routes_transfer_phrases() -> None:
    transfer = media_task_from_utterance("Przenieś muzykę do Salonu")
    only = media_task_from_utterance("Graj muzykę tylko w biurze")
    multi_room = media_task_from_utterance("Graj muzykę w sypialni i łazience")

    assert transfer is not None
    assert transfer["command"] == {
        "intent": "transfer_playback",
        "query": "Przenieś muzykę do Salonu",
        "areas": ["Salonu"],
    }
    assert only is not None
    assert only["command"] == {
        "intent": "transfer_playback",
        "query": "Graj muzykę tylko w biurze",
        "areas": ["biurze"],
        "replace_outputs": True,
    }
    assert multi_room is not None
    assert multi_room["command"] == {
        "intent": "start_last",
        "query": "Graj muzykę w sypialni i łazience",
        "areas": ["sypialni", "łazience"],
    }


def test_media_parser_extracts_query_area_and_all_speakers() -> None:
    room = parse_media_command({"query": "Graj soft jazz w łazience"})
    all_speakers = parse_media_command({"query": "Graj moje ulubione na wszystkich głośnikach"})
    only_office = parse_media_command({"query": "Play music only in office"})
    transfer = parse_media_command({"query": "Przenieś muzykę do Salonu"})
    multi_room = parse_media_command({"query": "Graj muzykę w sypialni i łazience"})

    assert room.intent == "play_media"
    assert room.query == "soft jazz"
    assert room.areas == ("łazience",)
    assert all_speakers.query == "Liked Songs"
    assert all_speakers.media_type == "playlist"
    assert all_speakers.all_speakers
    assert only_office.intent == "transfer_playback"
    assert only_office.areas == ("office",)
    assert only_office.replace_outputs
    assert transfer.intent == "transfer_playback"
    assert transfer.areas == ("Salonu",)
    assert multi_room.intent == "start_last"
    assert multi_room.areas == ("sypialni", "łazience")


def test_media_parser_handles_provider_phrase_and_stop_suffix() -> None:
    play = parse_media_command({"query": "'Zagraj hip-hop na Spotify."})
    stop = parse_media_command({"query": "Zatrzymaj muzykę. Ok, na pół."})
    turn_off = media_task_from_utterance("Wyłącz muzykę.")

    assert play.intent == "play_media"
    assert play.query == "hip-hop"
    assert stop.intent == "stop"
    assert stop.simple
    assert turn_off is not None
    assert turn_off["domain"] == "media_player"
    assert turn_off["command"]["intent"] == "stop"


def test_media_parser_handles_tok_fm_radio_and_whole_home() -> None:
    commands = [
        parse_media_command({"query": "Graj TOK FM"}),
        parse_media_command({"query": "Pusć TOK FM"}),
        parse_media_command({"query": "Włacz TOK FM"}),
    ]
    whole_home = parse_media_command({"query": "Włącz TOK FM w całym domu"})

    for command in commands:
        assert command.intent == "play_media"
        assert command.query == "TOK FM"
        assert command.media_type == "radio"
    assert whole_home.intent == "play_media"
    assert whole_home.query == "TOK FM"
    assert whole_home.media_type == "radio"
    assert whole_home.all_speakers


def test_media_domain_agent_start_last_uses_default_music_when_queue_and_memory_are_empty() -> None:
    connection = FakeMediaConnection(player_state="idle")
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "start_last", "query": "Spotify!"},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam muzykę ze Spotify."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office"], "media_id": "Liked Songs", "media_type": "playlist"}),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office"], "shuffle": True}),
    ]


def test_media_domain_agent_start_last_uses_conversation_user_media_settings() -> None:
    connection = FakeMediaConnection(player_state="idle", include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "start_last", "query": "Spotify!"},
    }
    conversation = Conversation(
        "c1",
        {"area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam moje ulubione."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        (
            "music_assistant_play_media",
            {"entity_ids": ["media_player.office_2"], "media_id": "library://playlist/7", "media_type": "playlist"},
        ),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office_2"], "shuffle": True}),
    ]


def test_media_domain_agent_start_last_is_noop_when_target_is_already_playing() -> None:
    connection = FakeMediaConnection(player_state="playing", include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "start_last", "query": "Spotify!"},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Muzyka już gra."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
    ]


def test_media_domain_agent_executes_simple_volume_without_llm() -> None:
    connection = FakeMediaConnection()
    ollama = FakeOllamaClient([])
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=ollama,
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "volume_delta", "query": "Daj głośniej", "volume_delta": 0.10},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Głośność: 40 procent."
    assert ollama.requests == []
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("media_player_volume_delta", {"entity_ids": ["media_player.office"], "delta": 0.1}),
    ]


def test_media_domain_agent_keeps_volume_on_direct_entity_when_ma_duplicate_exists() -> None:
    connection = FakeMediaConnection(include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "volume_delta", "query": "Daj głośniej", "volume_delta": 0.10},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("media_player_volume_delta", {"entity_ids": ["media_player.office"], "delta": 0.1}),
    ]


def test_media_domain_agent_does_not_clarify_when_current_area_has_no_speaker() -> None:
    connection = FakeMediaConnection(player_state="idle")
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "volume_delta", "query": "Przygłośnij muzykę", "volume_delta": 0.10},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "bedroom"}), task, {}))

    assert result == {
        "status": "failed",
        "text": "Nie znalazłem głośnika w tym pokoju.",
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
    }
    assert connection.calls == [
        ("list_media_players", {"area_name": "bedroom", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
    ]


def test_media_domain_agent_uses_llm_for_complex_command() -> None:
    connection = FakeMediaConnection()
    ollama = FakeOllamaClient(
        [
            {
                "intent": "play_media",
                "query": "soft jazz",
                "media_type": "playlist",
                "areas": ["living room"],
                "all_speakers": False,
            }
        ]
    )
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=ollama,
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"query": "zapuść coś do kolacji w salonie"},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam Soft Jazz."
    assert len(ollama.requests) == 1
    assert connection.calls == [
        ("list_media_players", {"area_name": "living room", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_search", {"name": "soft jazz", "media_type": "playlist", "limit": 5}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.living_room"], "media_id": "spotify:playlist:soft-jazz", "media_type": "playlist"}),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.living_room"], "shuffle": True}),
    ]


def test_media_domain_agent_falls_back_to_raw_play_media_when_search_config_entry_is_missing() -> None:
    connection = FakeMediaConnection(search_result={"status": "failed", "error": "music_assistant_config_entry_not_found"})
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "play_media", "query": "Włącz hip-hop na Spotify."},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam hip-hop."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_search", {"name": "hip-hop", "media_type": "", "limit": 5}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office"], "media_id": "hip-hop", "media_type": ""}),
    ]


def test_media_domain_agent_prefers_ma_duplicate_for_play_media() -> None:
    connection = FakeMediaConnection(include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "play_media", "query": "Włącz hip-hop na Spotify."},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_search", {"name": "hip-hop", "media_type": "", "limit": 5}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office_2"], "media_id": "spotify:playlist:soft-jazz", "media_type": "playlist"}),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office_2"], "shuffle": True}),
    ]


def test_media_domain_agent_uses_conversation_user_liked_songs_without_searching() -> None:
    connection = FakeMediaConnection(include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "play_media", "query": "Graj moje ulubione"},
    }
    conversation = Conversation(
        "c1",
        {"area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam Liked Songs macson_g."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        (
            "music_assistant_play_media",
            {"entity_ids": ["media_player.office_2"], "media_id": "library://playlist/7", "media_type": "playlist"},
        ),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office_2"], "shuffle": True}),
    ]


def test_media_domain_agent_resolves_user_playlist_alias() -> None:
    connection = FakeMediaConnection(include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "play_media", "query": "Muzyka do pracy"},
    }
    conversation = Conversation(
        "c1",
        {"area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_search", {"name": "Post Rock Focus", "media_type": "playlist", "limit": 5}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office_2"], "media_id": "spotify:playlist:soft-jazz", "media_type": "playlist"}),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office_2"], "shuffle": True}),
    ]


def test_media_domain_agent_start_last_uses_in_memory_recent_media() -> None:
    connection = FakeMediaConnection(player_state="idle", include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    conversation = Conversation("c1", {"area": "office", "user": "Maciek"})
    play_task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "play_media", "query": "soft jazz", "media_type": "playlist"},
    }
    start_last_task = {
        "id": "t2",
        "domain": "media_player",
        "command": {"intent": "start_last", "query": "play music"},
    }

    play_result = asyncio.run(agent.run_task(conversation, play_task, {}))
    connection.calls = []
    start_last_result = asyncio.run(agent.run_task(conversation, start_last_task, {}))

    assert play_result["status"] == "ok"
    assert start_last_result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office_2"], "media_id": "spotify:playlist:soft-jazz", "media_type": "playlist"}),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office_2"], "shuffle": True}),
    ]


def test_media_domain_agent_relocates_current_queue_for_only_request() -> None:
    connection = FakeMediaConnection(
        player_state="idle",
        include_ma_duplicate=True,
        global_players=[
            {
                "entity_id": "media_player.living_room_2",
                "device_id": "device-living-room-ma",
                "name": "Living Room",
                "area_id": "living_room",
                "area_name": "Living Room",
                "state": "playing",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
            {
                "entity_id": "media_player.office_2",
                "device_id": "device-office-ma",
                "name": "Office",
                "area_id": "office",
                "area_name": "Office",
                "state": "idle",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
        ],
    )
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {
            "intent": "transfer_playback",
            "query": "Graj muzykę tylko w office",
            "areas": ["office"],
            "replace_outputs": True,
        },
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.living_room_2"}),
        (
            "music_assistant_transfer_queue",
            {"entity_ids": ["media_player.office_2"], "source_player": "media_player.living_room_2", "auto_play": True},
        ),
    ]


def test_media_domain_agent_plays_tok_fm_radio_from_music_assistant_search() -> None:
    connection = FakeMediaConnection(
        search_result={
            "status": "ok",
            "response": {"radio": [{"uri": "tunein://station/tok-fm", "name": "TOK FM", "media_type": "radio"}]},
        }
    )
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "play_media", "query": "Włącz TOK FM w całym domu"},
    }

    result = asyncio.run(agent.run_task(Conversation("c1", {"area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam TOK FM."
    assert connection.calls == [
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_search", {"name": "TOK FM", "media_type": "radio", "limit": 5}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office"], "media_id": "tunein://station/tok-fm", "media_type": "radio"}),
    ]


class FakeMediaConnection:
    def __init__(
        self,
        player_state: str = "playing",
        search_result: dict | None = None,
        include_ma_duplicate: bool = False,
        global_players: list[dict] | None = None,
        queue_response: dict | None = None,
    ) -> None:
        self.calls = []
        self.player_state = player_state
        self.search_result = search_result
        self.include_ma_duplicate = include_ma_duplicate
        self.global_players = global_players
        self.queue_response = queue_response

    async def list_media_players(self, *, area_name: str = "", music_assistant_only: bool = True, speakers_only: bool = True):
        self.calls.append(
            (
                "list_media_players",
                {"area_name": area_name, "music_assistant_only": music_assistant_only, "speakers_only": speakers_only},
            )
        )
        if not area_name and self.global_players is not None:
            return self.global_players
        if area_name == "bedroom":
            return []
        entity_id = "media_player.living_room" if area_name == "living room" else "media_player.office"
        area_id = "living_room" if area_name == "living room" else "office"
        players = [
            {
                "entity_id": entity_id,
                "device_id": f"device-{area_id}",
                "name": area_name or "Office speaker",
                "area_id": area_id,
                "area_name": area_name or "Office",
                "state": self.player_state,
                "volume_level": 0.3,
                "is_music_assistant": False,
                "is_speaker": True,
            }
        ]
        if self.include_ma_duplicate:
            players.append(
                {
                    "entity_id": f"{entity_id}_2",
                    "device_id": f"device-{area_id}-ma",
                    "name": area_name or "Office speaker",
                    "area_id": area_id,
                    "area_name": area_name or "Office",
                    "state": self.player_state,
                    "volume_level": 0.3,
                    "is_music_assistant": True,
                    "is_speaker": True,
                }
            )
        return players

    async def media_player_volume_delta(self, entity_ids: list[str], delta: float):
        self.calls.append(("media_player_volume_delta", {"entity_ids": entity_ids, "delta": delta}))
        return {"status": "ok", "results": [{"entity_id": entity_ids[0], "volume_level": 0.4}]}

    async def media_player_shuffle_set(self, entity_ids: list[str], shuffle: bool):
        self.calls.append(("media_player_shuffle_set", {"entity_ids": entity_ids, "shuffle": shuffle}))
        return {"status": "ok"}

    async def music_assistant_search(self, *, name: str, media_type: str = "", limit: int = 5, library_only: bool = False):
        del library_only
        self.calls.append(("music_assistant_search", {"name": name, "media_type": media_type, "limit": limit}))
        if self.search_result is not None:
            return self.search_result
        return {
            "status": "ok",
            "response": {"items": [{"uri": "spotify:playlist:soft-jazz", "name": "Soft Jazz", "media_type": "playlist"}]},
        }

    async def music_assistant_play_media(
        self,
        entity_ids: list[str],
        *,
        media_id: str,
        media_type: str = "",
        artist: str = "",
        album: str = "",
    ):
        del artist, album
        self.calls.append(
            (
                "music_assistant_play_media",
                {"entity_ids": entity_ids, "media_id": media_id, "media_type": media_type},
            )
        )
        return {"status": "ok"}

    async def music_assistant_get_queue(self, entity_id: str):
        self.calls.append(("music_assistant_get_queue", {"entity_id": entity_id}))
        if self.queue_response is not None:
            return {"status": "ok", "response": self.queue_response}
        return {
            "status": "ok",
            "response": {
                entity_id: {
                    "current_item": {
                        "uri": "spotify:playlist:current-focus",
                        "name": "Current Focus",
                        "media_type": "playlist",
                    }
                }
            },
        }

    async def music_assistant_transfer_queue(
        self,
        entity_ids: list[str],
        *,
        source_player: str = "",
        auto_play: bool = True,
    ):
        self.calls.append(
            (
                "music_assistant_transfer_queue",
                {"entity_ids": entity_ids, "source_player": source_player, "auto_play": auto_play},
            )
        )
        return {"status": "ok"}


def _user_media_settings() -> dict:
    return {
        "media": {
            "liked_songs_media_id": "library://playlist/7",
            "liked_songs_media_type": "playlist",
            "liked_songs_name": "Liked Songs macson_g",
            "default_music_media_id": "library://playlist/7",
            "default_music_media_type": "playlist",
            "default_music_name": "moje ulubione",
            "playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"},
        }
    }


class FakeOllamaClient:
    def __init__(self, contents: list[dict]) -> None:
        self._contents = list(contents)
        self.requests = []

    async def chat(self, payload: dict):
        self.requests.append(payload)
        content = self._contents.pop(0)
        return {"message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}}

    async def close(self) -> None:
        pass
