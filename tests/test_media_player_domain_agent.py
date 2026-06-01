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
        ("list_media_players", {"area_name": "office", "music_assistant_only": True, "speakers_only": True}),
        ("media_player_volume_delta", {"entity_ids": ["media_player.office"], "delta": 0.1}),
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
        ("list_media_players", {"area_name": "living room", "music_assistant_only": True, "speakers_only": True}),
        ("music_assistant_search", {"name": "soft jazz", "media_type": "playlist", "limit": 5}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.living_room"], "media_id": "spotify:playlist:soft-jazz", "media_type": "playlist"}),
    ]


class FakeMediaConnection:
    def __init__(self) -> None:
        self.calls = []

    async def list_media_players(self, *, area_name: str = "", music_assistant_only: bool = True, speakers_only: bool = True):
        self.calls.append(
            (
                "list_media_players",
                {"area_name": area_name, "music_assistant_only": music_assistant_only, "speakers_only": speakers_only},
            )
        )
        entity_id = "media_player.living_room" if area_name == "living room" else "media_player.office"
        area_id = "living_room" if area_name == "living room" else "office"
        return [
            {
                "entity_id": entity_id,
                "device_id": f"device-{area_id}",
                "name": area_name or "Office speaker",
                "area_id": area_id,
                "area_name": area_name or "Office",
                "state": "playing",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            }
        ]

    async def media_player_volume_delta(self, entity_ids: list[str], delta: float):
        self.calls.append(("media_player_volume_delta", {"entity_ids": entity_ids, "delta": delta}))
        return {"status": "ok", "results": [{"entity_id": entity_ids[0], "volume_level": 0.4}]}

    async def music_assistant_search(self, *, name: str, media_type: str = "", limit: int = 5, library_only: bool = False):
        del library_only
        self.calls.append(("music_assistant_search", {"name": name, "media_type": media_type, "limit": limit}))
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
