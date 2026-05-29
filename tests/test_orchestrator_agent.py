import asyncio
import json

import pytest

from ai_server.agent.orchestrator import OrchestratorAgent, _parse_plan
from ai_server.interfaces import Conversation
from ai_server.messages import TextMessage, text_message_to_events
from conftest import FakeConversationEndpoint


def test_parse_plan_validates_home_assistant_command_envelope() -> None:
    plan = _parse_plan(
        json.dumps(
            {
                "kind": "single_task",
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


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("not-json", "orchestrator plan must be valid JSON"),
        ("[]", "orchestrator plan must be a JSON object"),
        ('{"tasks": []}', "kind must be a non-empty string"),
        ('{"kind": "single_task", "tasks": [{"id": "t1", "domain": "home_assistant", "command": {}}]}', "selection must be an object"),
    ],
)
def test_parse_plan_rejects_invalid_response(content: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _parse_plan(content)


def test_orchestrator_dispatches_tasks_and_uses_followup_context() -> None:
    plan_one = {
        "kind": "single_task",
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
        model="qwen3:4b-instruct",
        domain_agents={"home_assistant": domain_agent},
        ollama_client=ollama,
        owns_ollama_client=False,
    )
    endpoint = FakeConversationEndpoint([TextMessage(text="włącz klimę w salonie"), TextMessage(text="ustaw ją na 26")])
    conversation = Conversation(conversation_id="conversation-1", attributes={"location": "office"})

    asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(
        text_message_to_events(TextMessage(text="Włączyłem klimatyzację w salonie."))
    ) + list(text_message_to_events(TextMessage(text="Ustawiłem klimatyzację w salonie na 26 stopni.")))
    assert [task["id"] for task in domain_agent.tasks] == ["t1", "t2"]
    second_planning_payload = json.loads(ollama.requests[2]["messages"][-1]["content"])
    assert second_planning_payload["active_context"]["salient_entities"] == ["climate.salon"]
    assert conversation.state["orchestrator"]["active_domain"] == "home_assistant"


def test_orchestrator_reports_unsupported_domain_to_final_synthesis() -> None:
    plan = {
        "kind": "single_task",
        "tasks": [{"id": "t1", "domain": "wikipedia", "command": {"topic": "Albert Einstein"}}],
        "context_updates": {"salient_entities": [], "active_domain": None},
        "needs_clarification": False,
        "clarification_question": None,
    }
    ollama = FakeOllamaClient([json.dumps(plan), "Wikipedia nie jest jeszcze podłączona."])
    agent = OrchestratorAgent(model="qwen3:4b-instruct", ollama_client=ollama, owns_ollama_client=False)
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


def _ha_command(intent: str, description: str, parameters=None):
    return {
        "selection": {"include": [{"domain": "climate", "scope": "single", "area": "salon"}], "exclude": []},
        "operation": {"intent": intent, "description": description, "parameters": parameters or {}},
    }


class FakeOllamaClient:
    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.requests = []

    async def chat(self, payload: dict):
        self.requests.append(payload)
        return {"message": {"role": "assistant", "content": self._contents.pop(0)}}

    async def close(self) -> None:
        pass


class RecordingDomainAgent:
    def __init__(self) -> None:
        self.tasks = []

    async def run_task(self, conversation, task, active_context):
        self.tasks.append(task)
        return {"status": "ok", "text": "Gotowe."}

    async def close(self) -> None:
        pass
