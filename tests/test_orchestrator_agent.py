import asyncio
import json
import logging

import pytest

from ai_server.agent.orchestrator import GENERATION_FAILURE_MESSAGE, OrchestratorAgent, _parse_plan
from ai_server.config import ServerConfig
from ai_server.interfaces import Conversation
from ai_server.messages import TextMessage, text_message_to_events
from ai_server.ollama import OllamaError
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


def test_orchestrator_dispatches_tasks_and_uses_followup_context(caplog) -> None:
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
    plan_two = {
        "kind": "followup",
        "confidence": 0.9,
        "tasks": [
            {
                "id": "t2",
                "domain": "home_assistant",
                "command": _ha_command("set_temperature", "set it to 26 degrees", {"temperature": 26}),
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
            json.dumps(plan_two),
            "Ustawiłem klimatyzację w salonie na 26 stopni.",
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

    with caplog.at_level(logging.INFO, logger="ai_server.agent.orchestrator"):
        asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(
        text_message_to_events(TextMessage(text="Włączyłem klimatyzację w salonie."))
    ) + list(text_message_to_events(TextMessage(text="Ustawiłem klimatyzację w salonie na 26 stopni.")))
    assert [task["id"] for task in domain_agent.tasks] == ["t1", "t2"]
    second_planning_payload = json.loads(ollama.requests[2]["messages"][-1]["content"])
    assert second_planning_payload["conversation"]["area"] == "office"
    assert second_planning_payload["conversation"]["server_location"] == "Wrocław"
    assert second_planning_payload["conversation"]["server_timezone"] == "Europe/Warsaw"
    assert "location" not in second_planning_payload["conversation"]
    assert "room" not in second_planning_payload["conversation"]
    assert second_planning_payload["active_context"]["salient_entities"] == ["climate.salon"]
    assert conversation.state["orchestrator"]["active_domain"] == "home_assistant"
    assert "received message text='włącz klimę w salonie'" in caplog.text
    assert "planning output model=qwen3:4b-instruct kind=single_task confidence=0.9" in caplog.text
    assert "dispatching task task=" in caplog.text
    assert "task result task_id=t1" in caplog.text
    assert "final reply output model=qwen3:4b-instruct text='Włączyłem klimatyzację w salonie.'" in caplog.text
    assert "produced reply text='Ustawiłem klimatyzację w salonie na 26 stopni.'" in caplog.text


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

    with caplog.at_level(logging.WARNING, logger="ai_server.agent.orchestrator"):
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

    with caplog.at_level(logging.WARNING, logger="ai_server.agent.orchestrator"):
        asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Które światło mam włączyć?"))) + list(
        text_message_to_events(TextMessage(text="Włączyłem światło w salonie."))
    )
    assert [task["id"] for task in domain_agent.tasks] == ["t1", "t1"]
    assert [request["model"] for request in ollama.requests] == ["small", "small", "big", "small"]
    assert conversation.state["orchestrator"]["pending_clarification"] is None
    assert "utterance caused clarification source=dsa conversation_id=c1 utterance='włącz światło'" in caplog.text


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
