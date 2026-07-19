import asyncio
import json
import logging

from conftest import agent_context
from ai_server.domain_agents.media_player import MediaPlayerDomainAgent
from ai_server.domain_agents.media_player.parser import media_task_from_utterance, parse_media_command
from ai_server.orchestrator.known_utterances import collect_known_utterance_tasks, known_utterance_task


def test_media_known_utterance_routes_to_media_player() -> None:
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=FakeMediaConnection(),
        ollama_client=FakeOllamaClient([]),
    )
    task = known_utterance_task("Spotify!", collect_known_utterance_tasks({"media_player": agent}))

    assert task["domain"] == "media_player"
    assert task["command"] == {"intent": "start_last", "query": "Spotify!"}


def test_media_known_utterance_routes_volume_up_phrase_to_media_player() -> None:
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=FakeMediaConnection(),
        ollama_client=FakeOllamaClient([]),
    )
    task = known_utterance_task("Przygłośnij Muzykę", collect_known_utterance_tasks({"media_player": agent}))

    assert task["domain"] == "media_player"
    assert task["command"] == {
        "intent": "volume_delta",
        "query": "Przygłośnij Muzykę",
        "volume_delta": 0.05,
    }


def test_media_known_utterance_routes_volume_down_phrase_to_media_player() -> None:
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=FakeMediaConnection(),
        ollama_client=FakeOllamaClient([]),
    )
    task = known_utterance_task("Ścisz Muzykę", collect_known_utterance_tasks({"media_player": agent}))

    assert task["domain"] == "media_player"
    assert task["command"] == {
        "intent": "volume_delta",
        "query": "Ścisz Muzykę",
        "volume_delta": -0.05,
    }


def test_media_known_utterance_routes_tiny_volume_phrases_to_media_player() -> None:
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=FakeMediaConnection(),
        ollama_client=FakeOllamaClient([]),
    )
    known_utterances = collect_known_utterance_tasks({"media_player": agent})

    expectations = {
        "Odrobinkę głośniej": 0.02,
        "Odrobinkę ciszej": -0.02,
        "Troszkę głośniej": 0.02,
        "Troszkę ciszej": -0.02,
        "Troszeczkę głośniej": 0.02,
        "Troszeczkęciszej": -0.02,
    }
    for utterance, expected_delta in expectations.items():
        task = known_utterance_task(utterance, known_utterances)
        assert task["domain"] == "media_player"
        assert task["command"]["intent"] == "volume_delta"
        assert task["command"]["query"] == utterance
        assert task["command"]["volume_delta"] == expected_delta


def test_media_simple_short_path_parses_volume_up() -> None:
    task = media_task_from_utterance("Daj głośniej")

    assert task["domain"] == "media_player"
    assert task["command"]["intent"] == "volume_delta"
    assert task["command"]["volume_delta"] == 0.05


def test_media_parser_infers_volume_delta_details_from_query() -> None:
    down = parse_media_command({"intent": "set_volume", "query": "Ścisz muzykę.", "volume_level": 0.5})
    tiny_up = parse_media_command({"intent": "volume_delta", "query": "Odrobinkę głośniej"})
    tiny_down = parse_media_command({"intent": "volume_delta", "query": "Troszeczkęciszej"})
    little_down = parse_media_command({"intent": "volume_delta", "query": "Troszkę ciszej."})

    assert down.intent == "volume_delta"
    assert down.volume_level is None
    assert down.volume_delta == -0.05
    assert tiny_up.volume_delta == 0.02
    assert tiny_down.volume_delta == -0.02
    assert little_down.volume_delta == -0.02


def test_media_parser_normalizes_planner_percent_volume_level() -> None:
    parsed = parse_media_command({"intent": "set_volume", "query": "Ustaw głośność na 10.", "volume_level": 10.0})

    assert parsed.intent == "set_volume"
    assert parsed.volume_level == 0.10


def test_media_parser_keeps_native_volume_level() -> None:
    parsed = parse_media_command({"intent": "set_volume", "query": "Ustaw głośność na 30 procent.", "volume_level": 0.30})

    assert parsed.intent == "set_volume"
    assert parsed.volume_level == 0.30


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
    current_music = parse_media_command(
        {
            "intent": "play_media",
            "query": "Graj tę muzykę na wszystkich głośnikach w domu.",
            "all_speakers": True,
        }
    )

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
    assert current_music.intent == "transfer_playback"
    assert current_music.query == "Graj tę muzykę na wszystkich głośnikach w domu."
    assert current_music.all_speakers


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
    connection = FakeMediaConnection(player_state="idle", queue_response={})
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

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam muzykę ze Spotify."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office"], "media_id": "Liked Songs", "media_type": "playlist"}),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office"], "shuffle": True}),
    ]


def test_media_domain_agent_start_last_uses_conversation_user_media_settings() -> None:
    connection = FakeMediaConnection(player_state="idle", include_ma_duplicate=True, queue_response={})
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
    conversation = agent_context(
        "c1",
        {"medium": "voice", "area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam moje ulubione."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
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

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

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
        "command": {"intent": "volume_delta", "query": "Daj głośniej", "volume_delta": 0.05},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Głośność: 40 procent."
    assert ollama.requests == []
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("media_player_volume_delta", {"entity_ids": ["media_player.office"], "delta": 0.05}),
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
        "command": {"intent": "volume_delta", "query": "Daj głośniej", "volume_delta": 0.05},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("media_player_volume_delta", {"entity_ids": ["media_player.office"], "delta": 0.05}),
    ]


def test_media_domain_agent_interprets_planner_percent_volume_level() -> None:
    connection = FakeMediaConnection()
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "set_volume", "query": "Ustaw głośność na 10.", "volume_level": 10.0},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Ustawiłem głośność na 10 procent."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("media_player_volume_set", {"entity_ids": ["media_player.office"], "volume_level": 0.10}),
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
        "command": {"intent": "volume_delta", "query": "Przygłośnij muzykę", "volume_delta": 0.05},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "bedroom"}), task, {}))

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

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

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

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

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

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

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
    conversation = agent_context(
        "c1",
        {"medium": "voice", "area": "office", "user": "Maciek"},
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


def test_media_domain_agent_answers_configuration_query_without_service_calls() -> None:
    connection = FakeMediaConnection(include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "query_configuration", "query": "jaka jest moja domyślna muzyka?"},
    }
    conversation = agent_context(
        "c1",
        {"medium": "voice", "area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result == {
        "status": "ok",
        "text": "Mam zapisaną konfigurację mediów: domyślna muzyka: moje ulubione, polubione utwory: Liked Songs macson_g.",
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
        "final_reply_mode": "verbatim",
    }
    assert connection.calls == []


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
    conversation = agent_context(
        "c1",
        {"medium": "voice", "area": "office", "user": "Maciek"},
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


def test_media_domain_agent_tries_next_search_candidate_when_playlist_is_unplayable() -> None:
    connection = FakeMediaConnection(
        include_ma_duplicate=True,
        search_result={
            "status": "ok",
            "response": {
                "playlists": [
                    {"uri": "library://playlist/13", "name": "Post Rock Focus", "media_type": "playlist"},
                    {
                        "uri": "spotify--oonEey9Z://playlist/59pF55gB3cbu6Ih4BeMAWz",
                        "name": "Post Rock - Focus",
                        "media_type": "playlist",
                    },
                ]
            },
        },
        play_media_results_by_media_id={
            "library://playlist/13": {
                "status": "failed",
                "message": "Home Assistant command failed type=call_service error={'code': 'home_assistant_error', 'message': 'No playable item found to start playback'}",
            }
        },
    )
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "play_media", "query": "Muzyka do pracy", "all_speakers": True},
    }
    conversation = agent_context(
        "c1",
        {"medium": "voice", "area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Włączam Post Rock - Focus."
    assert connection.calls == [
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_search", {"name": "Post Rock Focus", "media_type": "playlist", "limit": 5}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office_2"], "media_id": "library://playlist/13", "media_type": "playlist"}),
        (
            "music_assistant_play_media",
            {
                "entity_ids": ["media_player.office_2"],
                "media_id": "spotify--oonEey9Z://playlist/59pF55gB3cbu6Ih4BeMAWz",
                "media_type": "playlist",
            },
        ),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office_2"], "shuffle": True}),
    ]


def test_media_domain_agent_uses_llm_to_resolve_inflected_playlist_alias() -> None:
    connection = FakeMediaConnection(include_ma_duplicate=True)
    ollama = FakeOllamaClient(
        [
            {
                "alias": "Muzyka do pracy",
                "query": "",
                "media_type": "playlist",
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
        "command": {"intent": "play_media", "query": "Graj muzykę do pracy w całym domu.", "all_speakers": True},
    }
    conversation = agent_context(
        "c1",
        {"medium": "voice", "area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert len(ollama.requests) == 1
    resolver_payload = json.loads(ollama.requests[0]["messages"][1]["content"])
    assert resolver_payload["query"] == "muzykę do pracy"
    assert resolver_payload["aliases"] == [
        {"alias": "Muzyka do pracy", "target": "Post Rock Focus", "media_type": "playlist"}
    ]
    assert connection.calls == [
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_search", {"name": "Post Rock Focus", "media_type": "playlist", "limit": 5}),
        ("music_assistant_play_media", {"entity_ids": ["media_player.office_2"], "media_id": "spotify:playlist:soft-jazz", "media_type": "playlist"}),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office_2"], "shuffle": True}),
    ]


def test_media_domain_agent_groups_whole_home_named_media_before_playback() -> None:
    connection = FakeMediaConnection(
        include_ma_duplicate=True,
        global_players=_whole_home_music_assistant_players(),
    )
    ollama = FakeOllamaClient(
        [
            {
                "alias": "Muzyka do pracy",
                "query": "",
                "media_type": "playlist",
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
        "command": {"intent": "play_media", "query": "Graj muzykę do pracy w całym domu.", "all_speakers": True},
    }
    conversation = agent_context(
        "c1",
        {"medium": "voice", "area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_search", {"name": "Post Rock Focus", "media_type": "playlist", "limit": 5}),
        (
            "media_player_join",
            {
                "entity_id": "media_player.office_2",
                "group_members": [
                    "media_player.bedroom_2",
                    "media_player.living_room_2",
                    "media_player.bathroom_2",
                ],
            },
        ),
        (
            "music_assistant_play_media",
            {"entity_ids": ["media_player.office_2"], "media_id": "spotify:playlist:soft-jazz", "media_type": "playlist"},
        ),
        ("media_player_shuffle_set", {"entity_ids": ["media_player.office_2"], "shuffle": True}),
    ]


def test_media_domain_agent_warns_when_alias_resolver_exhausts_budget(caplog) -> None:
    connection = FakeMediaConnection(include_ma_duplicate=True)
    ollama = FakeOllamaClient(
        [
            {
                "__raw_response__": {
                    "message": {"role": "assistant", "content": "", "thinking": "alias matched"},
                    "done_reason": "length",
                    "eval_count": 512,
                }
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
        "command": {"intent": "play_media", "query": "Graj muzykę do pracy w całym domu.", "all_speakers": True},
    }
    conversation = agent_context(
        "c1",
        {"medium": "voice", "area": "office", "user": "Maciek"},
        state={"user_settings": _user_media_settings()},
    )

    caplog.set_level(logging.WARNING)

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert any(
        "media query resolver returned no JSON" in record.message
        and "done_reason='length'" in record.message
        and "num_predict=512" in record.message
        and "increase num_predict" in record.message
        for record in caplog.records
    )


def test_media_domain_agent_start_last_uses_in_memory_recent_media() -> None:
    connection = FakeMediaConnection(player_state="idle", include_ma_duplicate=True, queue_response={})
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    conversation = agent_context("c1", {"medium": "voice", "area": "office", "user": "Maciek"})
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
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
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

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.living_room_2"}),
        ("media_player_join", {"entity_id": "media_player.living_room_2", "group_members": ["media_player.office_2"]}),
        ("media_player_unjoin", {"entity_ids": ["media_player.living_room_2"]}),
    ]


def test_media_domain_agent_start_last_with_explicit_target_transfers_active_queue() -> None:
    connection = FakeMediaConnection(
        player_state="idle",
        include_ma_duplicate=True,
        global_players=[
            {
                "entity_id": "media_player.office_2",
                "device_id": "device-office-ma",
                "name": "Office",
                "area_id": "office",
                "area_name": "Office",
                "state": "playing",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
            {
                "entity_id": "media_player.living_room_2",
                "device_id": "device-living-room-ma",
                "name": "Living Room",
                "area_id": "living_room",
                "area_name": "Living Room",
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
        "command": {"intent": "start_last", "query": "Graj muzykę w całym domu", "all_speakers": True},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
        ("media_player_join", {"entity_id": "media_player.office_2", "group_members": ["media_player.living_room_2"]}),
    ]


def test_media_domain_agent_relocates_current_music_reference_instead_of_searching() -> None:
    connection = FakeMediaConnection(
        player_state="idle",
        include_ma_duplicate=True,
        global_players=[
            {
                "entity_id": "media_player.office_2",
                "device_id": "device-office-ma",
                "name": "Office",
                "area_id": "office",
                "area_name": "Office",
                "state": "playing",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
            {
                "entity_id": "media_player.bedroom_2",
                "device_id": "device-bedroom-ma",
                "name": "Bedroom",
                "area_id": "bedroom",
                "area_name": "Bedroom",
                "state": "idle",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
            {
                "entity_id": "media_player.living_room_2",
                "device_id": "device-living-room-ma",
                "name": "Living Room",
                "area_id": "living_room",
                "area_name": "Living Room",
                "state": "idle",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
            {
                "entity_id": "media_player.bathroom_2",
                "device_id": "device-bathroom-ma",
                "name": "Bathroom",
                "area_id": "bathroom",
                "area_name": "Bathroom",
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
            "intent": "play_media",
            "query": "Graj tę muzykę na wszystkich głośnikach w domu.",
            "all_speakers": True,
        },
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
        (
            "media_player_join",
            {
                "entity_id": "media_player.office_2",
                "group_members": [
                    "media_player.bedroom_2",
                    "media_player.living_room_2",
                    "media_player.bathroom_2",
                ],
            },
        ),
    ]


def test_media_domain_agent_adds_requested_room_to_current_outputs() -> None:
    connection = FakeMediaConnection(
        player_state="idle",
        include_ma_duplicate=True,
        global_players=[
            {
                "entity_id": "media_player.office_2",
                "device_id": "device-office-ma",
                "name": "Office",
                "area_id": "office",
                "area_name": "Office",
                "state": "playing",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
            {
                "entity_id": "media_player.living_room_2",
                "device_id": "device-living-room-ma",
                "name": "Living Room",
                "area_id": "living_room",
                "area_name": "Living Room",
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
        "command": {"intent": "start_last", "query": "Graj muzykę w salonie", "areas": ["living room"]},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "living room", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
        ("media_player_join", {"entity_id": "media_player.office_2", "group_members": ["media_player.living_room_2"]}),
    ]


def test_media_domain_agent_treats_join_timeout_as_success_when_state_changed() -> None:
    connection = FakeMediaConnection(
        player_state="idle",
        include_ma_duplicate=True,
        join_result={"status": "failed", "error": "service_call_failed", "service": "media_player.join", "message": ""},
        join_applies=True,
        global_players=[
            {
                "entity_id": "media_player.office_2",
                "device_id": "device-office-ma",
                "name": "Office",
                "area_id": "office",
                "area_name": "Office",
                "state": "playing",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
            {
                "entity_id": "media_player.living_room_2",
                "device_id": "device-living-room-ma",
                "name": "Living Room",
                "area_id": "living_room",
                "area_name": "Living Room",
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
        "command": {"intent": "start_last", "query": "Graj muzykę w salonie", "areas": ["living room"]},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "living room", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
        ("media_player_join", {"entity_id": "media_player.office_2", "group_members": ["media_player.living_room_2"]}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
    ]


def test_media_domain_agent_falls_back_to_transfer_when_join_is_unavailable() -> None:
    connection = FakeMediaConnection(
        player_state="idle",
        include_ma_duplicate=True,
        join_result={"status": "failed", "error": "service_call_failed", "service": "media_player.join"},
        global_players=[
            {
                "entity_id": "media_player.office_2",
                "device_id": "device-office-ma",
                "name": "Office",
                "area_id": "office",
                "area_name": "Office",
                "state": "playing",
                "volume_level": 0.3,
                "is_music_assistant": True,
                "is_speaker": True,
            },
            {
                "entity_id": "media_player.living_room_2",
                "device_id": "device-living-room-ma",
                "name": "Living Room",
                "area_id": "living_room",
                "area_name": "Living Room",
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
        "command": {"intent": "start_last", "query": "Graj muzykę w salonie", "areas": ["living room"]},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert connection.calls == [
        ("list_media_players", {"area_name": "living room", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
        ("media_player_join", {"entity_id": "media_player.office_2", "group_members": ["media_player.living_room_2"]}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        (
            "music_assistant_transfer_queue",
            {
                "entity_ids": ["media_player.office_2", "media_player.living_room_2"],
                "source_player": "media_player.office_2",
                "auto_play": True,
            },
        ),
    ]


def test_media_domain_agent_start_last_with_explicit_target_resumes_when_nothing_is_playing() -> None:
    connection = FakeMediaConnection(player_state="idle", include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "start_last", "query": "Graj muzykę w salonie", "areas": ["living room"]},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Wznawiam muzykę."
    assert connection.calls == [
        ("list_media_players", {"area_name": "living room", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.living_room_2"}),
        ("media_player_play", {"entity_ids": ["media_player.living_room_2"]}),
    ]


def test_media_domain_agent_start_last_resumes_queue_instead_of_replaying_current_track() -> None:
    connection = FakeMediaConnection(
        player_state="idle",
        include_ma_duplicate=True,
        queue_response={
            "media_player.office_2": {
                "queue_id": "RINCON_F0F6C182F5EE01400",
                "active": True,
                "name": "Office",
                "items": 214,
                "current_index": 3,
                "current_item": {
                    "queue_item_id": "c1acbbd754cd47d59ff5fac5a7933dc4",
                    "name": "Gorm - Hydra",
                    "media_item": {
                        "media_type": "track",
                        "uri": "spotify://track/32KJFQ2VuhTL9r7dEbqfsA",
                        "name": "Hydra",
                        "artists": [{"name": "Gorm"}],
                    },
                },
            }
        },
    )
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {"intent": "start_last", "query": "Wznów muzykę", "areas": ["office"]},
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Wznawiam muzykę."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
        ("media_player_play", {"entity_ids": ["media_player.office_2"]}),
    ]


def test_media_domain_agent_start_last_groups_multiple_targets_and_resumes_idle_queue() -> None:
    connection = FakeMediaConnection(player_state="idle", include_ma_duplicate=True)
    agent = MediaPlayerDomainAgent(
        model="qwen3:4b-instruct",
        connection=connection,
        ollama_client=FakeOllamaClient([]),
    )
    task = {
        "id": "t1",
        "domain": "media_player",
        "command": {
            "intent": "start_last",
            "query": "Włącz muzykę w biurze i w łazience.",
            "areas": ["office", "bathroom"],
        },
    }

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Wznawiam muzykę na wybranych głośnikach."
    assert connection.calls == [
        ("list_media_players", {"area_name": "office", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "bathroom", "music_assistant_only": False, "speakers_only": True}),
        ("list_media_players", {"area_name": "", "music_assistant_only": False, "speakers_only": True}),
        ("music_assistant_get_queue", {"entity_id": "media_player.office_2"}),
        ("media_player_join", {"entity_id": "media_player.office_2", "group_members": ["media_player.bathroom_2"]}),
        ("media_player_play", {"entity_ids": ["media_player.office_2"]}),
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

    result = asyncio.run(agent.run_task(agent_context("c1", {"medium": "voice", "area": "office"}), task, {}))

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
        play_media_results_by_media_id: dict[str, dict] | None = None,
        join_result: dict | None = None,
        unjoin_result: dict | None = None,
        join_applies: bool = False,
    ) -> None:
        self.calls = []
        self.player_state = player_state
        self.search_result = search_result
        self.include_ma_duplicate = include_ma_duplicate
        self.global_players = global_players
        self.queue_response = queue_response
        self.play_media_results_by_media_id = play_media_results_by_media_id or {}
        self.join_result = join_result
        self.unjoin_result = unjoin_result
        self.join_applies = join_applies

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
        area_entities = {
            "living room": ("media_player.living_room", "living_room"),
            "bathroom": ("media_player.bathroom", "bathroom"),
            "łazience": ("media_player.bathroom", "bathroom"),
        }
        entity_id, area_id = area_entities.get(area_name, ("media_player.office", "office"))
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

    async def media_player_volume_set(self, entity_ids: list[str], volume_level: float):
        self.calls.append(("media_player_volume_set", {"entity_ids": entity_ids, "volume_level": volume_level}))
        return {"status": "ok"}

    async def media_player_shuffle_set(self, entity_ids: list[str], shuffle: bool):
        self.calls.append(("media_player_shuffle_set", {"entity_ids": entity_ids, "shuffle": shuffle}))
        return {"status": "ok"}

    async def media_player_play(self, entity_ids: list[str]):
        self.calls.append(("media_player_play", {"entity_ids": entity_ids}))
        return {"status": "ok"}

    async def media_player_join(self, entity_id: str, group_members: list[str]):
        self.calls.append(("media_player_join", {"entity_id": entity_id, "group_members": group_members}))
        result = self.join_result or {"status": "ok"}
        if result.get("status") == "ok" or self.join_applies:
            self._set_players_state([entity_id, *group_members], "playing")
        return result

    async def media_player_unjoin(self, entity_ids: list[str]):
        self.calls.append(("media_player_unjoin", {"entity_ids": entity_ids}))
        result = self.unjoin_result or {"status": "ok"}
        if result.get("status") == "ok":
            self._set_players_state(entity_ids, "idle")
        return result

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
        if media_id in self.play_media_results_by_media_id:
            return self.play_media_results_by_media_id[media_id]
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

    def _set_players_state(self, entity_ids: list[str], state: str) -> None:
        players = self.global_players
        if players is None:
            return
        entity_id_set = set(entity_ids)
        for player in players:
            if player.get("entity_id") in entity_id_set:
                player["state"] = state


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


def _whole_home_music_assistant_players() -> list[dict]:
    return [
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
        {
            "entity_id": "media_player.bedroom_2",
            "device_id": "device-bedroom-ma",
            "name": "Bedroom",
            "area_id": "bedroom",
            "area_name": "Bedroom",
            "state": "idle",
            "volume_level": 0.3,
            "is_music_assistant": True,
            "is_speaker": True,
        },
        {
            "entity_id": "media_player.living_room_2",
            "device_id": "device-living-room-ma",
            "name": "Living Room",
            "area_id": "living_room",
            "area_name": "Living Room",
            "state": "idle",
            "volume_level": 0.3,
            "is_music_assistant": True,
            "is_speaker": True,
        },
        {
            "entity_id": "media_player.bathroom_2",
            "device_id": "device-bathroom-ma",
            "name": "Bathroom",
            "area_id": "bathroom",
            "area_name": "Bathroom",
            "state": "idle",
            "volume_level": 0.3,
            "is_music_assistant": True,
            "is_speaker": True,
        },
    ]


class FakeOllamaClient:
    def __init__(self, contents: list[dict]) -> None:
        self._contents = list(contents)
        self.requests = []

    async def chat(self, payload: dict):
        self.requests.append(payload)
        content = self._contents.pop(0)
        if "__raw_response__" in content:
            return content["__raw_response__"]
        return {"message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}}

    async def close(self) -> None:
        pass
