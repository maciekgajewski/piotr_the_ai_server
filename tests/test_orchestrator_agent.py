import asyncio
import json
import logging

import pytest

from ai_server.orchestrator import GENERATION_FAILURE_MESSAGE, OrchestratorAgent, _parse_plan
from ai_server.config import ServerConfig
from ai_server.home_assistant.interfaces import HomeAssistantArea, HomeAssistantInventory
from ai_server.interfaces import Conversation
from ai_server.messages import TextMessage, text_message_to_events
from ai_server.ollama_client import OllamaError
from conftest import FakeConversationEndpoint


def test_parse_plan_validates_home_assistant_command_envelope() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "kind": "single_task",
                "confidence": 0.9,
                "tasks": [
                    {
                        "id": "t1",
                        "domain": "home_assistant",
                        "command": {
                            "selection": {
                                "include": [{"domain": "light", "scope": "all"}],
                                "exclude": [{"name": "lampka nocna"}],
                            },
                            "operation": {
                                "intent": "turn_off",
                                "description": "turn them off gently",
                                "parameters": {},
                            },
                        },
                    }
                ],
                "context_updates": {"salient_entities": [], "active_domain": None},
                "needs_clarification": False,
                "clarification_question": None,
            }
        )
    )

    assert plan["tasks"][0]["command"]["operation"]["intent"] == "turn_off"


def test_parse_plan_validates_media_player_command() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "kind": "single_task",
                "confidence": 0.9,
                "tasks": [
                    {
                        "id": "t1",
                        "domain": "media_player",
                        "command": {
                            "intent": "play_media",
                            "query": "Liked Songs",
                            "media_type": "playlist",
                            "all_speakers": True,
                        },
                    }
                ],
                "context_updates": {"salient_entities": [], "active_domain": "media_player"},
                "needs_clarification": False,
                "clarification_question": None,
            }
        )
    )

    assert plan["tasks"][0]["command"] == {
        "intent": "play_media",
        "query": "Liked Songs",
        "media_type": "playlist",
        "all_speakers": True,
    }


def test_parse_plan_validates_media_player_transfer_command() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "kind": "single_task",
                "confidence": 0.9,
                "tasks": [
                    {
                        "id": "t1",
                        "domain": "media_player",
                        "command": {
                            "intent": "transfer_playback",
                            "query": "Graj muzykę tylko w biurze",
                            "areas": ["office"],
                            "replace_outputs": True,
                        },
                    }
                ],
                "context_updates": {"salient_entities": [], "active_domain": "media_player"},
                "needs_clarification": False,
                "clarification_question": None,
            }
        )
    )

    assert plan["tasks"][0]["command"] == {
        "intent": "transfer_playback",
        "query": "Graj muzykę tylko w biurze",
        "areas": ["office"],
        "replace_outputs": True,
    }


def test_parse_plan_allows_system_status_command() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "kind": "single_task",
                "confidence": 0.9,
                "tasks": [
                    {
                        "id": "t1",
                        "domain": "system_status",
                        "command": {"intent": "summary", "query": "status systemu"},
                    }
                ],
                "context_updates": {"salient_entities": [], "active_domain": "system_status"},
                "needs_clarification": False,
                "clarification_question": None,
            }
        )
    )

    assert plan["tasks"][0]["domain"] == "system_status"
    assert plan["tasks"][0]["command"] == {"intent": "summary", "query": "status systemu"}


def test_parse_plan_ignores_embedded_string_fragments_in_tasks() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "kind": "single_task",
                "confidence": 0.9,
                "tasks": [
                    {
                        "id": "t1",
                        "domain": "home_assistant",
                        "command": _ha_command("turn_on", "włącz klimatyzację w salonie"),
                    },
                    'context_updates":{"salient_entities":["climate.salon"],"active_domain":"home_assistant"},',
                ],
                "needs_clarification": False,
                "clarification_question": None,
            }
        )
    )

    assert len(plan["tasks"]) == 1
    assert plan["context_updates"] == {
        "salient_entities": ["climate.salon"],
        "active_domain": "home_assistant",
    }


def test_parse_plan_repairs_quoted_plan_with_malformed_embedded_context() -> None:
    plan = _parse_plan(
        '"{\\"kind\\":\\"single_task\\",\\"tasks\\":[{\\"id\\":\\"t1\\",\\"domain\\":\\"home_assistant\\",'
        '\\"confidence\\":0.9,'
        '\\"command\\":{\\"selection\\":{\\"include\\":[{\\"domain\\":\\"climate\\",\\"scope\\":\\"all\\"}]},'
        '\\"operation\\":{\\"intent\\":\\"turn_off\\",\\"description\\":\\"wyłącz wszystkie klimatyzacje\\",'
        '\\"parameters\\":{}}},\\"depends_on\\":[],\\"status\\":\\"ready\\",\\"clarification_question\\":null},'
        '\\"context_updates\\\\\\":{\\\\\\"salient_entities\\\\\\":[], "'
    )

    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["command"]["selection"]["include"] == [{"domain": "climate", "scope": "all"}]
    assert plan["context_updates"] == {}


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("not-json", "orchestrator plan must be valid JSON"),
        ("[]", "orchestrator plan must be a JSON object"),
        ('{"tasks": []}', "kind must be a non-empty string"),
        ('{"kind": "single_task", "tasks": []}', "confidence must be a number"),
        ('{"kind": "single_task", "confidence": 2, "tasks": []}', "confidence must be a number"),
        ('{"kind": "single_task", "confidence": 0.9, "tasks": [{"id": "t1", "domain": "home_assistant", "command": {}}]}', "selection must be an object"),
    ],
)
def test_parse_plan_rejects_invalid_response(content: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _parse_plan(content)


def test_orchestrator_does_not_read_followup_without_explicit_request(caplog) -> None:
    plan_one = {
        "kind": "single_task",
        "confidence": 0.9,
        "tasks": [
            {
                "id": "t1",
                "domain": "home_assistant",
                "command": _ha_command("turn_on", "turn on the air conditioning in the living room"),
            }
        ],
        "context_updates": {"salient_entities": ["climate.salon"], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient(
        [
            json.dumps(plan_one),
            "Włączyłem klimatyzację w salonie.",
        ]
    )
    domain_agent = RecordingDomainAgent()
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
        server_config=ServerConfig(timezone="Europe/Warsaw", location="Wrocław"),
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="włącz klimę w salonie"), TextMessage(text="ustaw ją na 26")])
    conversation = Conversation(conversation_id="conversation-1", attributes={"area": "office"})

    with caplog.at_level(logging.INFO, logger="ai_server.orchestrator"):
        asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(
        text_message_to_events(TextMessage(text="Włączyłem klimatyzację w salonie."))
    )
    assert endpoint.control_events == []
    assert [task["id"] for task in domain_agent.tasks] == ["t1"]
    planning_payload = json.loads(ollama.requests[0]["messages"][-1]["content"])
    assert planning_payload["conversation"]["area"] == "office"
    assert planning_payload["conversation"]["server_location"] == "Wrocław"
    assert planning_payload["conversation"]["server_timezone"] == "Europe/Warsaw"
    assert "location" not in planning_payload["conversation"]
    assert "room" not in planning_payload["conversation"]
    assert conversation.state["orchestrator"]["active_domain"] == "home_assistant"
    assert "received message text='włącz klimę w salonie'" in caplog.text
    assert "planning output model=qwen3:4b-instruct kind=single_task confidence=0.9" in caplog.text
    assert "dispatching task task=" in caplog.text
    assert "task result task_id=t1" in caplog.text
    assert "final reply output model=qwen3:4b-instruct text='Włączyłem klimatyzację w salonie.'" in caplog.text
    assert "produced reply text='Ustawiłem klimatyzację w salonie na 26 stopni.'" not in caplog.text


def test_orchestrator_reports_unsupported_domain_to_final_synthesis() -> None:
    plan = {
        "kind": "single_task",
        "confidence": 0.9,
        "tasks": [{"id": "t1", "domain": "wikipedia", "command": {"topic": "Albert Einstein"}}],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan), "Wikipedia nie jest jeszcze podłączona."])
    agent = OrchestratorAgent(orchestrator_model="qwen3:4b-instruct", ollama_client=ollama, owns_ollama_client=False)
    endpoint = FakeConversationEndpoint([TextMessage(text="sprawdź Einsteina na Wikipedii")])
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    asyncio.run(agent.run_conversation(conversation, endpoint))

    final_payload = json.loads(ollama.requests[1]["messages"][-1]["content"])
    assert final_payload["task_results"] == [
        {
            "task_id": "t1",
            "domain": "wikipedia",
            "status": "unsupported_domain",
            "message": "Domain agent is not available: wikipedia",
        }
    ]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Wikipedia nie jest jeszcze podłączona.")))


def test_orchestrator_planning_prompt_uses_only_loaded_domain_prompts() -> None:
    plan = {
        "kind": "chat",
        "confidence": 0.95,
        "tasks": [],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan), "Cześć."])
    domain_agent = RecordingDomainAgent(planning_prompt="For time tasks only.")
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"time": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="co u ciebie?")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    system_prompt = ollama.requests[0]["messages"][0]["content"]
    assert "Available task domains: time" in system_prompt
    assert "For time tasks only." in system_prompt
    assert "system_status" not in system_prompt
    assert "home_assistant" not in system_prompt
    assert "general" not in system_prompt


def test_orchestrator_planning_output_logs_token_counts(caplog) -> None:
    plan = {
        "kind": "chat",
        "confidence": 0.95,
        "tasks": [],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient(
        [
            {
                "message": {"role": "assistant", "content": json.dumps(plan)},
                "prompt_eval_count": 123,
                "eval_count": 12,
            },
            "Cześć.",
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="hej")])

    with caplog.at_level(logging.INFO, logger="ai_server.orchestrator"):
        asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert "planning output model=qwen3:4b-instruct kind=chat confidence=0.95" in caplog.text
    assert "prompt_tokens=123 completion_tokens=12 total_tokens=135" in caplog.text


def test_orchestrator_returns_single_verbatim_task_result_without_final_synthesis() -> None:
    plan = {
        "kind": "single_task",
        "confidence": 0.9,
        "tasks": [{"id": "t1", "domain": "wikipedia", "command": {"topic": "Albert Einstein"}}],
        "context_updates": {"salient_entities": [], "active_domain": "wikipedia"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan)])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Tekst kontrolowany przez DSA.", "final_reply_mode": "verbatim"}]
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"wikipedia": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="sprawdź Einsteina na Wikipedii")])
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Tekst kontrolowany przez DSA.")))
    assert len(ollama.requests) == 1


def test_orchestrator_short_path_dispatches_known_time_utterance_without_ollama() -> None:
    ollama = FakeOllamaClient([])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "czternasta zero pięć", "final_reply_mode": "verbatim"}],
        known_utterances={
            "Która godzina?": {
                "id": "t1",
                "domain": "time",
                "command": {"query": "Która godzina?"},
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
        },
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"time": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="Która godzina?")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert ollama.requests == []
    assert domain_agent.tasks == [
        {
            "id": "t1",
            "domain": "time",
            "command": {"query": "Która godzina?"},
            "depends_on": [],
            "status": "ready",
            "clarification_question": None,
        }
    ]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="czternasta zero pięć")))


def test_orchestrator_short_path_dispatches_system_status_check_in_without_ollama() -> None:
    ollama = FakeOllamaClient([])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Wszystko działa dobrze, Krzysztofie.", "final_reply_mode": "verbatim"}],
        known_utterances={
            "Jak się masz?": {
                "id": "t1",
                "domain": "system_status",
                "command": {"intent": "quick_check", "query": "Jak się masz?"},
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
        },
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"system_status": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="Jak się masz?")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"user": "Krzysztof"}), endpoint))

    assert ollama.requests == []
    assert domain_agent.tasks == [
        {
            "id": "t1",
            "domain": "system_status",
            "command": {"intent": "quick_check", "query": "Jak się masz?"},
            "depends_on": [],
            "status": "ready",
            "clarification_question": None,
        }
    ]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Wszystko działa dobrze, Krzysztofie.")))


def test_orchestrator_short_path_dispatches_weather_utterance_without_ollama() -> None:
    ollama = FakeOllamaClient([])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "We Wrocławiu jest 16 stopni.", "final_reply_mode": "verbatim"}],
        known_utterances={
            "Jaka pogoda na weekend?": {
                "id": "t1",
                "domain": "weather",
                "command": {
                    "tool": "get_weather_forecast",
                    "query": "Jaka pogoda na weekend?",
                    "horizon": "weekend",
                    "granularity": "daily",
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
        },
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"weather": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="Jaka pogoda na weekend?")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert ollama.requests == []
    assert domain_agent.tasks == [
        {
            "id": "t1",
            "domain": "weather",
            "command": {
                "tool": "get_weather_forecast",
                "query": "Jaka pogoda na weekend?",
                "horizon": "weekend",
                "granularity": "daily",
            },
            "depends_on": [],
            "status": "ready",
            "clarification_question": None,
        }
    ]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="We Wrocławiu jest 16 stopni.")))


def test_orchestrator_plans_pronoun_temperature_followup_instead_of_weather_short_path() -> None:
    initial_plan = {
        "kind": "single_task",
        "confidence": 0.95,
        "tasks": [
            {
                "id": "t1",
                "domain": "home_assistant",
                "command": {
                    "selection": {
                        "include": [{"domain": "climate", "scope": "single", "area": "living_room"}],
                        "exclude": [],
                    },
                    "operation": {
                        "intent": "turn_on",
                        "description": "Włącz klimatyzację w salonie",
                        "parameters": {},
                    },
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            }
        ],
        "context_updates": {"salient_entities": ["climate.living_room"], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    followup_plan = {
        "kind": "followup",
        "confidence": 0.95,
        "tasks": [
            {
                "id": "t2",
                "domain": "home_assistant",
                "command": {
                    "selection": {"include": [], "exclude": []},
                    "operation": {
                        "intent": "set_temperature",
                        "description": "ustaw ją na 22 stopnie",
                        "parameters": {"temperature": 22},
                    },
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(initial_plan), json.dumps(followup_plan)])
    domain_agent = RecordingDomainAgent(
        [
            {"status": "ok", "text": "Włączono klimatyzację w salonie.", "final_reply_mode": "verbatim"},
            {"status": "ok", "text": "Ustawiono klimatyzację w salonie na 22 stopnie.", "final_reply_mode": "verbatim"},
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="big",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
        home_assistant_inventory_provider=FakeAreaInventoryProvider(),
    )
    conversation = Conversation(conversation_id="c1", attributes={"area": "office"})
    first_endpoint = FakeConversationEndpoint([TextMessage(text="Włącz klimatyzację w salonie")])
    second_endpoint = FakeConversationEndpoint([TextMessage(text="Super! I ustaw ją na 22 stopnie")])

    asyncio.run(agent.run_conversation(conversation, first_endpoint))
    asyncio.run(agent.run_conversation(conversation, second_endpoint))

    assert len(ollama.requests) == 2
    followup_payload = json.loads(ollama.requests[1]["messages"][1]["content"])
    assert followup_payload["active_context"]["salient_entities"] == ["climate.living_room"]
    assert [task["domain"] for task in domain_agent.tasks] == ["home_assistant", "home_assistant"]
    assert domain_agent.tasks[1]["command"]["operation"]["intent"] == "set_temperature"
    assert domain_agent.tasks[1]["command"]["selection"]["include"] == [
        {"domain": "climate", "scope": "single", "area": "living_room"}
    ]
    assert first_endpoint.sent == list(text_message_to_events(TextMessage(text="Włączono klimatyzację w salonie.")))
    assert second_endpoint.sent == list(
        text_message_to_events(TextMessage(text="Ustawiono klimatyzację w salonie na 22 stopnie."))
    )


def test_orchestrator_plans_media_stop_with_extra_tail() -> None:
    plan = {
        "kind": "single_task",
        "confidence": 0.95,
        "tasks": [
            {
                "id": "t1",
                "domain": "media_player",
                "command": {"intent": "stop", "query": "Zatrzymaj muzykę. Ok, na pół."},
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "media_player"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan)])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Zatrzymałem muzykę.", "final_reply_mode": "verbatim"}]
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"media_player": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="Zatrzymaj muzykę. Ok, na pół.")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "bedroom"}), endpoint))

    assert len(ollama.requests) == 1
    assert domain_agent.tasks[0]["domain"] == "media_player"
    assert domain_agent.tasks[0]["command"]["intent"] == "stop"
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Zatrzymałem muzykę.")))


def test_orchestrator_plans_composite_after_known_media_phrase_prefix() -> None:
    plan = {
        "kind": "multi_task",
        "confidence": 0.95,
        "tasks": [
            {
                "id": "t1",
                "domain": "media_player",
                "command": {"intent": "stop", "query": "Wyłącz muzykę"},
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
            {
                "id": "t2",
                "domain": "home_assistant",
                "command": {
                    "selection": {
                        "include": [{"domain": "climate", "scope": "single", "area": "office"}],
                        "exclude": [],
                    },
                    "operation": {
                        "intent": "turn_off",
                        "description": "wyłącz klimatyzator",
                        "parameters": {},
                    },
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
        ],
        "context_updates": {"salient_entities": ["climate.office"], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan), "Zatrzymałem muzykę i wyłączyłem klimatyzację."])
    media_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Zatrzymałem muzykę.", "final_reply_mode": "verbatim"}],
        known_utterances={
            "Wyłącz muzykę": {
                "id": "t1",
                "domain": "media_player",
                "command": {"intent": "stop", "query": "Wyłącz muzykę"},
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
        },
    )
    home_assistant_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Wyłączyłem klimatyzację w biurze.", "final_reply_mode": "verbatim"}]
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"media_player": media_agent, "home_assistant": home_assistant_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="Wyłącz muzykę i wyłącz klimatyzator.")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "office"}), endpoint))

    assert len(ollama.requests) == 2
    assert media_agent.tasks[0]["domain"] == "media_player"
    assert home_assistant_agent.tasks[0]["domain"] == "home_assistant"
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Zatrzymałem muzykę i wyłączyłem klimatyzację.")))


def test_orchestrator_uses_area_inventory_for_named_media_room_instead_of_short_path() -> None:
    plan = {
        "kind": "single_task",
        "confidence": 0.95,
        "tasks": [
            {
                "id": "t1",
                "domain": "media_player",
                "command": {"intent": "stop", "query": "zatrzymaj muzykę w pracowni", "areas": ["office"]},
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "media_player"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan)])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Zatrzymałem muzykę.", "final_reply_mode": "verbatim"}]
    )
    agent = OrchestratorAgent(
        orchestrator_model="big",
        domain_agents={"media_player": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
        home_assistant_inventory_provider=FakeAreaInventoryProvider(),
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="zatrzymaj muzykę w pracowni")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "bedroom"}), endpoint))

    planning_payload = json.loads(ollama.requests[0]["messages"][1]["content"])
    assert planning_payload["conversation"]["home_assistant_areas"] == [
        {"area_id": "living_room", "name": "Living room", "aliases": ["Salon"]},
        {"area_id": "office", "name": "Office", "aliases": ["Biuro", "Pracownia"]},
    ]
    assert domain_agent.tasks[0]["command"]["areas"] == ["office"]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Zatrzymałem muzykę.")))


def test_orchestrator_merges_split_media_room_tasks_before_dispatch() -> None:
    plan = {
        "kind": "multi_task",
        "confidence": 0.95,
        "tasks": [
            {
                "id": "t1",
                "domain": "media_player",
                "command": {
                    "intent": "play_media",
                    "query": "Muzyka do pracy",
                    "media_type": "playlist",
                    "areas": ["salon"],
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
            {
                "id": "t2",
                "domain": "media_player",
                "command": {
                    "intent": "play_media",
                    "query": "Muzyka do pracy",
                    "media_type": "playlist",
                    "areas": ["Pracownia"],
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
        ],
        "context_updates": {"salient_entities": ["living_room", "office"], "active_domain": "media_player"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan), "Włączam muzykę."])
    domain_agent = RecordingDomainAgent([{"status": "ok", "text": "Włączam muzykę."}])
    agent = OrchestratorAgent(
        orchestrator_model="big",
        domain_agents={"media_player": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
        home_assistant_inventory_provider=FakeAreaInventoryProvider(),
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="graj muzykę do pracy w salonie i pracowni")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "office"}), endpoint))

    assert len(domain_agent.tasks) == 1
    assert domain_agent.tasks[0]["id"] == "t1"
    assert domain_agent.tasks[0]["command"] == {
        "intent": "play_media",
        "query": "Muzyka do pracy",
        "media_type": "playlist",
        "areas": ["living_room", "office"],
    }
    final_reply_payload = json.loads(ollama.requests[1]["messages"][1]["content"])
    assert final_reply_payload["plan"]["kind"] == "single_task"
    assert len(final_reply_payload["plan"]["tasks"]) == 1


def test_orchestrator_canonicalizes_home_assistant_room_alias_from_inventory() -> None:
    plan = {
        "kind": "single_task",
        "confidence": 0.95,
        "tasks": [
            {
                "id": "t1",
                "domain": "home_assistant",
                "command": {
                    "selection": {
                        "include": [{"domain": "climate", "scope": "single", "area": "Pracownia"}],
                        "exclude": [],
                    },
                    "operation": {
                        "intent": "turn_off",
                        "description": "wyłącz klimatyzację w pracowni",
                        "parameters": {},
                    },
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            }
        ],
        "context_updates": {"salient_entities": ["climate.office"], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan)])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Wyłączyłem klimatyzację.", "final_reply_mode": "verbatim"}]
    )
    agent = OrchestratorAgent(
        orchestrator_model="big",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
        home_assistant_inventory_provider=FakeAreaInventoryProvider(),
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="wyłącz klimatyzację w pracowni")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "bedroom"}), endpoint))

    planning_payload = json.loads(ollama.requests[0]["messages"][1]["content"])
    assert planning_payload["conversation"]["home_assistant_areas"] == [
        {"area_id": "living_room", "name": "Living room", "aliases": ["Salon"]},
        {"area_id": "office", "name": "Office", "aliases": ["Biuro", "Pracownia"]},
    ]
    assert domain_agent.tasks[0]["command"]["selection"]["include"][0]["area"] == "office"
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Wyłączyłem klimatyzację.")))


def test_orchestrator_blocks_unknown_planned_area_before_dispatch() -> None:
    plan = {
        "kind": "single_task",
        "confidence": 0.95,
        "tasks": [
            {
                "id": "t1",
                "domain": "media_player",
                "command": {"intent": "stop", "query": "zatrzymaj muzykę w pracowni", "areas": ["pracowni"]},
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "media_player"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan), "O który pokój chodzi?"])
    domain_agent = RecordingDomainAgent()
    agent = OrchestratorAgent(
        orchestrator_model="big",
        domain_agents={"media_player": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
        home_assistant_inventory_provider=FakeAreaInventoryProvider(),
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="zatrzymaj muzykę w pracowni")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "bedroom"}), endpoint))

    assert domain_agent.tasks == []
    assert "Nie znam pokoju" in json.loads(ollama.requests[1]["messages"][1]["content"])["task_results"][0]["clarification_question"]
    assert endpoint.control_events
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="O który pokój chodzi?")))


def test_orchestrator_short_path_dispatches_tok_fm_without_ollama() -> None:
    ollama = FakeOllamaClient([])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Włączam TOK FM.", "final_reply_mode": "verbatim"}],
        known_utterances={
            "Włącz TOK FM w całym domu": {
                "id": "t1",
                "domain": "media_player",
                "command": {
                    "intent": "play_media",
                    "query": "Włącz TOK FM w całym domu",
                    "media_type": "radio",
                    "all_speakers": True,
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
        },
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"media_player": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="Włącz TOK FM w całym domu")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "bedroom"}), endpoint))

    assert ollama.requests == []
    assert domain_agent.tasks[0]["domain"] == "media_player"
    assert domain_agent.tasks[0]["command"]["intent"] == "play_media"
    assert domain_agent.tasks[0]["command"]["media_type"] == "radio"
    assert domain_agent.tasks[0]["command"]["all_speakers"] is True
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Włączam TOK FM.")))


def test_orchestrator_short_path_dispatches_media_volume_up_without_ollama() -> None:
    ollama = FakeOllamaClient([])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "Głośność: 40 procent.", "final_reply_mode": "verbatim"}],
        known_utterances={
            "Przygłośnij muzykę": {
                "id": "t1",
                "domain": "media_player",
                "command": {
                    "intent": "volume_delta",
                    "query": "Przygłośnij muzykę",
                    "volume_delta": 0.05,
                },
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
        },
    )
    agent = OrchestratorAgent(
        orchestrator_model="qwen3:4b-instruct",
        domain_agents={"media_player": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="Przygłośnij Muzykę")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "office"}), endpoint))

    assert ollama.requests == []
    assert domain_agent.tasks[0]["domain"] == "media_player"
    assert domain_agent.tasks[0]["command"]["intent"] == "volume_delta"
    assert domain_agent.tasks[0]["command"]["query"] == "Przygłośnij Muzykę"
    assert domain_agent.tasks[0]["command"]["volume_delta"] == 0.05
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Głośność: 40 procent.")))


def test_orchestrator_retries_low_confidence_plan_with_clarification_model() -> None:
    low_confidence_plan = {
        "kind": "chat",
        "confidence": 0.3,
        "tasks": [],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    high_confidence_plan = {
        "kind": "single_task",
        "confidence": 0.95,
        "tasks": [{"id": "t1", "domain": "wikipedia", "command": {"topic": "Maria Skłodowska-Curie"}}],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(low_confidence_plan), json.dumps(high_confidence_plan), "Jest odpowiedź."])
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="opowiedz o Marii Curie")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert [request["model"] for request in ollama.requests] == ["small", "big", "small"]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Jest odpowiedź.")))


def test_orchestrator_retries_invalid_plan_with_clarification_model() -> None:
    valid_plan = {
        "kind": "chat",
        "confidence": 0.9,
        "tasks": [],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient(["not-json", json.dumps(valid_plan), "Dobrze."])
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="pogadajmy")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert [request["model"] for request in ollama.requests] == ["small", "big", "small"]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Dobrze.")))


def test_orchestrator_returns_error_phrase_when_clarification_model_fails() -> None:
    low_confidence_plan = {
        "kind": "chat",
        "confidence": 0.2,
        "tasks": [],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(low_confidence_plan), OllamaError("cloud failed")])
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="coś trudnego")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text=GENERATION_FAILURE_MESSAGE)))


def test_orchestrator_retries_empty_final_reply_with_clarification_model() -> None:
    plan = {
        "kind": "chat",
        "confidence": 0.9,
        "tasks": [],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan), "", "Duża odpowiedź."])
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="odpowiedz")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert [request["model"] for request in ollama.requests] == ["small", "small", "big"]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Duża odpowiedź.")))


def test_orchestrator_clarification_answer_can_correct_home_assistant_selection_domain() -> None:
    initial_plan = {
        "kind": "single_task",
        "confidence": 0.9,
        "tasks": [
            {
                "id": "t1",
                "domain": "home_assistant",
                "command": {
                    "selection": {
                        "include": [{"domain": "light", "scope": "single", "area": "office"}],
                        "exclude": [],
                    },
                    "operation": {
                        "intent": "turn_on",
                        "description": "włącz klimatyzację.",
                        "parameters": {},
                    },
                },
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    resolved_task = {
        "confidence": 0.95,
        "task": {
            "id": "t1",
            "domain": "home_assistant",
            "command": {
                "selection": {
                    "include": [{"domain": "climate", "scope": "single", "area": "office"}],
                    "exclude": [],
                },
                "operation": {
                    "intent": "turn_on",
                    "description": "Włącz klimatyzację.",
                    "parameters": {},
                },
            },
            "depends_on": [],
            "status": "ready",
            "clarification_question": None,
        },
    }
    ollama = FakeOllamaClient(
        [
            json.dumps(initial_plan),
            "Jaką dokładnie zmianę mam wykonać?",
            json.dumps(resolved_task),
            "Włączyłem klimatyzator.",
        ]
    )
    domain_agent = RecordingDomainAgent(
        [
            {
                "status": "needs_clarification",
                "text": "Jaką dokładnie zmianę mam wykonać?",
                "needs_clarification": True,
                "clarification_question": "Jaką dokładnie zmianę mam wykonać?",
            },
            {"status": "ok", "text": "Włączyłem klimatyzator."},
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="sprawdź klimatyzator"), TextMessage(text="Włącz klimatyzator")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={"area": "office"}), endpoint))

    assert [request["model"] for request in ollama.requests] == ["small", "small", "big", "small"]
    assert [task["command"]["selection"]["include"][0]["domain"] for task in domain_agent.tasks] == ["light", "climate"]
    assert [task["command"]["operation"]["intent"] for task in domain_agent.tasks] == ["turn_on", "turn_on"]
    assert domain_agent.tasks[1]["command"]["selection"]["include"] == [
        {"domain": "climate", "scope": "single", "area": "office"}
    ]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Jaką dokładnie zmianę mam wykonać?"))) + list(
        text_message_to_events(TextMessage(text="Włączyłem klimatyzator."))
    )


def test_orchestrator_retries_empty_clarification_response_with_orchestrator_model() -> None:
    initial_plan = {
        "kind": "single_task",
        "confidence": 0.9,
        "tasks": [
            {
                "id": "t1",
                "domain": "home_assistant",
                "command": _ha_command("turn_on", "włącz światło"),
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    resolved_task = {
        "confidence": 0.92,
        "task": {
            "id": "t1",
            "domain": "home_assistant",
            "command": _ha_command("turn_on", "włącz światło w salonie"),
            "depends_on": [],
            "status": "ready",
            "clarification_question": None,
        },
    }
    ollama = FakeOllamaClient(
        [
            json.dumps(initial_plan),
            "Które światło mam włączyć?",
            "",
            json.dumps(resolved_task),
            "Włączyłem światło w salonie.",
        ]
    )
    domain_agent = RecordingDomainAgent(
        [
            {
                "status": "needs_clarification",
                "text": "Które światło mam włączyć?",
                "needs_clarification": True,
                "clarification_question": "Które światło mam włączyć?",
            },
            {"status": "ok", "text": "Włączyłem światło w salonie."},
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="włącz światło"), TextMessage(text="w salonie")])

    asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert [request["model"] for request in ollama.requests] == ["small", "small", "big", "small", "small"]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Które światło mam włączyć?"))) + list(
        text_message_to_events(TextMessage(text="Włączyłem światło w salonie."))
    )


def test_orchestrator_logs_blocked_task_clarification_utterance(caplog) -> None:
    plan = {
        "kind": "single_task",
        "confidence": 0.9,
        "tasks": [
            {
                "id": "t1",
                "domain": "home_assistant",
                "command": _ha_command("turn_on", "włącz światło"),
                "depends_on": [],
                "status": "blocked",
                "clarification_question": "Które światło mam włączyć?",
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan), "Które światło mam włączyć?"])
    agent = OrchestratorAgent(orchestrator_model="small", ollama_client=ollama, owns_ollama_client=False)
    endpoint = FakeConversationEndpoint([TextMessage(text="włącz światło")])

    with caplog.at_level(logging.WARNING, logger="ai_server.orchestrator"):
        asyncio.run(agent.run_conversation(Conversation(conversation_id="c1", attributes={}), endpoint))

    assert "utterance caused clarification source=orchestrator conversation_id=c1 utterance='włącz światło'" in caplog.text


def test_orchestrator_stores_dsa_clarification_and_resumes_same_domain(caplog) -> None:
    initial_plan = {
        "kind": "single_task",
        "confidence": 0.9,
        "tasks": [
            {
                "id": "t1",
                "domain": "home_assistant",
                "command": _ha_command("turn_on", "włącz światło"),
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    resolved_task = {
        "confidence": 0.92,
        "task": {
            "id": "t1",
            "domain": "home_assistant",
            "command": _ha_command("turn_on", "włącz światło w salonie"),
            "depends_on": [],
            "status": "ready",
            "clarification_question": None,
        },
    }
    ollama = FakeOllamaClient(
        [
            json.dumps(initial_plan),
            "Które światło mam włączyć?",
            json.dumps(resolved_task),
            "Włączyłem światło w salonie.",
        ]
    )
    domain_agent = RecordingDomainAgent(
        [
            {
                "status": "needs_clarification",
                "text": "Które światło mam włączyć?",
                "needs_clarification": True,
                "clarification_question": "Które światło mam włączyć?",
            },
            {"status": "ok", "text": "Włączyłem światło w salonie."},
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="włącz światło"), TextMessage(text="w salonie")])
    conversation = Conversation(conversation_id="c1", attributes={})

    with caplog.at_level(logging.WARNING, logger="ai_server.orchestrator"):
        asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Które światło mam włączyć?"))) + list(
        text_message_to_events(TextMessage(text="Włączyłem światło w salonie."))
    )
    assert [task["id"] for task in domain_agent.tasks] == ["t1", "t1"]
    assert [request["model"] for request in ollama.requests] == ["small", "small", "big", "small"]
    assert conversation.state["orchestrator"]["pending_clarification"] is None
    assert "utterance caused clarification source=dsa conversation_id=c1 utterance='włącz światło'" in caplog.text


def test_orchestrator_pending_clarification_takes_priority_over_short_path() -> None:
    resolved_task = {
        "confidence": 0.92,
        "task": {
            "id": "t1",
            "domain": "home_assistant",
            "command": _ha_command("turn_on", "włącz światło w salonie"),
            "depends_on": [],
            "status": "ready",
            "clarification_question": None,
        },
    }
    ollama = FakeOllamaClient([json.dumps(resolved_task), "Włączyłem światło w salonie."])
    domain_agent = RecordingDomainAgent([{"status": "ok", "text": "Włączyłem światło w salonie."}])
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    conversation = Conversation(conversation_id="c1", attributes={})
    conversation.state["orchestrator"] = {
        "last_turns": [],
        "salient_entities": [],
        "active_domain": "home_assistant",
        "pending_tasks": [],
        "pending_clarification": {
            "domain": "home_assistant",
            "task": {
                "id": "t1",
                "domain": "home_assistant",
                "command": _ha_command("turn_on", "włącz światło"),
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
            "task_result": {
                "task_id": "t1",
                "domain": "home_assistant",
                "status": "needs_clarification",
                "text": "Które światło mam włączyć?",
                "needs_clarification": True,
                "clarification_question": "Które światło mam włączyć?",
            },
            "clarification_question": "Które światło mam włączyć?",
        },
    }
    endpoint = FakeConversationEndpoint([TextMessage(text="Która godzina?")])

    asyncio.run(agent.run_conversation(conversation, endpoint))

    assert [request["model"] for request in ollama.requests] == ["big", "small"]
    assert [task["domain"] for task in domain_agent.tasks] == ["home_assistant"]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Włączyłem światło w salonie.")))


def test_orchestrator_clears_pending_clarification_on_thanks() -> None:
    ollama = FakeOllamaClient([])
    domain_agent = RecordingDomainAgent()
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        domain_agents={"media_player": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    conversation = Conversation(conversation_id="c1", attributes={"area": "bedroom"})
    conversation.state["orchestrator"] = {
        "last_turns": [{"user": "Przygłośnij muzykę.", "assistant": "W którym pokoju mam użyć głośnika?"}],
        "salient_entities": [],
        "active_domain": "media_player",
        "pending_tasks": [],
        "pending_clarification": {
            "domain": "media_player",
            "task": {
                "id": "t1",
                "domain": "media_player",
                "command": {"intent": "volume_delta", "query": "Przygłośnij muzykę.", "volume_delta": 0.05},
                "depends_on": [],
                "status": "ready",
                "clarification_question": None,
            },
            "task_result": {
                "task_id": "t1",
                "domain": "media_player",
                "status": "needs_clarification",
                "text": "W którym pokoju mam użyć głośnika?",
                "needs_clarification": True,
                "clarification_question": "W którym pokoju mam użyć głośnika?",
            },
            "clarification_question": "W którym pokoju mam użyć głośnika?",
        },
    }
    endpoint = FakeConversationEndpoint([TextMessage(text="Dziękuję.")])

    asyncio.run(agent.run_conversation(conversation, endpoint))

    assert ollama.requests == []
    assert domain_agent.tasks == []
    assert conversation.state["orchestrator"]["pending_clarification"] is None
    assert endpoint.control_events == []
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="")))


def test_orchestrator_drops_unanswered_clarification_when_conversation_ends() -> None:
    initial_plan = {
        "kind": "single_task",
        "confidence": 0.9,
        "tasks": [
            {
                "id": "t1",
                "domain": "home_assistant",
                "command": _ha_command("turn_on", "włącz światło"),
            }
        ],
        "context_updates": {"salient_entities": [], "active_domain": "home_assistant"},
        "needs_clarification": False,
        "clarification_question": None,
    }
    second_plan = {
        "kind": "chat",
        "confidence": 0.9,
        "tasks": [],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient(
        [
            json.dumps(initial_plan),
            "Które światło mam włączyć?",
            json.dumps(second_plan),
            "Nowa rozmowa.",
        ]
    )
    domain_agent = RecordingDomainAgent(
        [
            {
                "status": "needs_clarification",
                "text": "Które światło mam włączyć?",
                "needs_clarification": True,
                "clarification_question": "Które światło mam włączyć?",
            },
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    conversation = Conversation(conversation_id="c1", attributes={})

    asyncio.run(agent.run_conversation(conversation, FakeConversationEndpoint([TextMessage(text="włącz światło")])))

    assert conversation.state["orchestrator"]["pending_clarification"] is None

    asyncio.run(agent.run_conversation(conversation, FakeConversationEndpoint([TextMessage(text="w salonie")])))

    system_prompts = [request["messages"][0]["content"] for request in ollama.requests]
    assert len(system_prompts) == 4
    assert "You are an orchestration planner" in system_prompts[2]
    assert "resolving a pending domain-agent clarification" not in "\n".join(system_prompts)
    assert [task["id"] for task in domain_agent.tasks] == ["t1"]


def _ha_command(intent: str, description: str, parameters=None):
    return {
        "selection": {"include": [{"domain": "climate", "scope": "single", "area": "salon"}], "exclude": []},
        "operation": {"intent": intent, "description": description, "parameters": parameters or {}},
    }


class FakeOllamaClient:
    def __init__(self, contents: list[str | dict | Exception]) -> None:
        self._contents = list(contents)
        self.requests = []

    async def chat(self, payload: dict):
        self.requests.append(payload)
        content = self._contents.pop(0)
        if isinstance(content, Exception):
            raise content
        if isinstance(content, dict):
            return content
        return {"message": {"role": "assistant", "content": content}}

    async def close(self) -> None:
        pass


class RecordingDomainAgent:
    def __init__(
        self,
        results: list[dict] | None = None,
        *,
        known_utterances: dict[str, dict] | None = None,
        planning_prompt: str = "For test_domain tasks.",
    ) -> None:
        self.tasks = []
        self._results = list(results or [{"status": "ok", "text": "Gotowe."}])
        self._known_utterances = known_utterances or {}
        self._planning_prompt = planning_prompt

    def known_utterances(self):
        return self._known_utterances

    def planning_prompt(self):
        return self._planning_prompt

    async def run_task(self, conversation, task, active_context):
        self.tasks.append(task)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]

    async def close(self) -> None:
        pass


class FakeAreaInventoryProvider:
    @property
    def inventory(self) -> HomeAssistantInventory:
        office = HomeAssistantArea(area_id="office", name="Office", aliases=("Biuro", "Pracownia"))
        living_room = HomeAssistantArea(area_id="living_room", name="Living room", aliases=("Salon",))
        return HomeAssistantInventory(
            areas_by_id={
                office.area_id: office,
                living_room.area_id: living_room,
            },
            devices_by_id={},
            devices_by_area={
                office.area_id: (),
                living_room.area_id: (),
            },
            area_lookup={},
            device_lookup={},
        )
