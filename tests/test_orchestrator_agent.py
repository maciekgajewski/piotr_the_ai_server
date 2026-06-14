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
        [{"status": "ok", "text": "czternasta zero pięć", "final_reply_mode": "verbatim"}]
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


def test_orchestrator_short_path_dispatches_weather_utterance_without_ollama() -> None:
    ollama = FakeOllamaClient([])
    domain_agent = RecordingDomainAgent(
        [{"status": "ok", "text": "We Wrocławiu jest 16 stopni.", "final_reply_mode": "verbatim"}]
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


def test_orchestrator_short_path_dispatches_media_stop_with_wake_word_tail_without_ollama() -> None:
    ollama = FakeOllamaClient([])
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

    assert ollama.requests == []
    assert domain_agent.tasks[0]["domain"] == "media_player"
    assert domain_agent.tasks[0]["command"]["intent"] == "stop"
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Zatrzymałem muzykę.")))


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
        [{"status": "ok", "text": "Włączam TOK FM.", "final_reply_mode": "verbatim"}]
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
                "command": {"intent": "volume_delta", "query": "Przygłośnij muzykę.", "volume_delta": 0.1},
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
    def __init__(self, contents: list[str | Exception]) -> None:
        self._contents = list(contents)
        self.requests = []

    async def chat(self, payload: dict):
        self.requests.append(payload)
        content = self._contents.pop(0)
        if isinstance(content, Exception):
            raise content
        return {"message": {"role": "assistant", "content": content}}

    async def close(self) -> None:
        pass


class RecordingDomainAgent:
    def __init__(self, results: list[dict] | None = None) -> None:
        self.tasks = []
        self._results = list(results or [{"status": "ok", "text": "Gotowe."}])

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
