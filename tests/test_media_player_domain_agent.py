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


def test_media_parser_extracts_query_area_and_all_speakers() -> None:
    room = parse_media_command({"query": "Graj soft jazz w łazience"})
    all_speakers = parse_media_command({"query": "Graj moje ulubione na wszystkich głośnikach"})

    assert room.intent == "play_media"
    assert room.query == "soft jazz"
    assert room.areas == ("łazience",)
    assert all_speakers.query == "Liked Songs"
    assert all_speakers.media_type == "playlist"
    assert all_speakers.all_speakers


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


def test_media_domain_agent_start_last_uses_default_spotify_music_not_resume() -> None:
    connection = FakeMediaConnection()
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
        ("music_assistant_play_media", {"entity_ids": ["media_player.office"], "media_id": "Liked Songs", "media_type": "playlist"}),
    ]


def test_media_domain_agent_start_last_uses_conversation_user_media_settings() -> None:
    connection = FakeMediaConnection(include_ma_duplicate=True)
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
        (
            "music_assistant_play_media",
            {"entity_ids": ["media_player.office_2"], "media_id": "library://playlist/7", "media_type": "playlist"},
        ),
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
    ) -> None:
        self.calls = []
        self.player_state = player_state
        self.search_result = search_result
        self.include_ma_duplicate = include_ma_duplicate

    async def list_media_players(self, *, area_name: str = "", music_assistant_only: bool = True, speakers_only: bool = True):
        self.calls.append(
            (
                "list_media_players",
                {"area_name": area_name, "music_assistant_only": music_assistant_only, "speakers_only": speakers_only},
            )
        )
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


def _user_media_settings() -> dict:
    return {
        "media": {
            "liked_songs_media_id": "library://playlist/7",
            "liked_songs_media_type": "playlist",
            "liked_songs_name": "Liked Songs macson_g",
            "default_music_media_id": "library://playlist/7",
            "default_music_media_type": "playlist",
            "default_music_name": "moje ulubione",
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
