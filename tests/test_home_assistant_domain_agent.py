import asyncio
import json

import pytest

from ai_server.domain_agents.home_assistant import HomeAssistantDomainAgent, HomeAssistantDomainToolSet, _parse_domain_reply
from ai_server.interfaces import Conversation


def test_home_assistant_domain_agent_runs_one_agent_loop_task() -> None:
    FakeHomeAssistantConnection.calls = []
    fake_loop = FakeAgentLoop(
        {
            "status": "ok",
            "text": "Ustawiłem klimatyzację w salonie na 22 stopni.",
            "needs_clarification": False,
            "clarification_question": None,
            "entities": ["climate.salon"],
        }
    )
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
        loop_factory=fake_loop.factory,
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={"medium": "voice", "area": "office", "user": "maciek"})
    task = {
        "id": "t1",
        "domain": "home_assistant",
        "command": {
            "selection": {
                "include": [{"domain": "climate", "scope": "single", "area": "salon"}],
                "exclude": [],
            },
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

    result = asyncio.run(agent.run_task(conversation, task, {"salient_entities": ["climate.salon"]}))

    assert result["status"] == "ok"
    assert result["entities"] == ["climate.living_room"]
    assert fake_loop.config.model == "qwen3:4b-instruct"
    assert fake_loop.config.ollama_url == "http://ollama:11434"
    assert fake_loop.config.fallback_model is None
    assert isinstance(fake_loop.tools, HomeAssistantDomainToolSet)
    assert "Current area: office" in fake_loop.system_prompt
    assert "Reply style for conversation.medium=voice" in fake_loop.system_prompt
    payload = json.loads(fake_loop.messages[0])
    assert payload["task"] == task
    assert payload["conversation"]["medium"] == "voice"
    assert "numbers and dates as Polish words" in payload["conversation"]["reply_style"]
    assert "active_context" not in payload
    assert "execution_hints" in payload
    assert fake_loop.tools._tools._current_user_message == "ustaw ją na 22 stopnie"
    assert fake_loop.tools._tools._current_area == "office"
    assert FakeHomeAssistantConnection.calls == [
        ("find_devices", {"query": "", "device_type": "climate", "area_name": "salon"}),
        ("list_modifiable_properties", {"device": "Living room air conditioner"}),
        ("modify_device", {"device": "Living room air conditioner", "property_name": "target_temperature", "value": 22}),
    ]


def test_home_assistant_domain_agent_normalizes_hvac_mode_alias_before_availability_check() -> None:
    FakeHomeAssistantConnection.calls = []
    fake_loop = FakeAgentLoop({})
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
        loop_factory=fake_loop.factory,
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={"medium": "voice", "area": "office"})
    task = {
        "id": "t1",
        "domain": "home_assistant",
        "command": {
            "selection": {
                "include": [{"domain": "climate", "scope": "single", "area": "office"}],
                "exclude": [],
            },
            "operation": {
                "intent": "set_hvac_mode",
                "description": "włącz klimatyzator w tryb wentylacji",
                "parameters": {"hvac_mode": "ventilate"},
            },
        },
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Ustawiłem klimatyzację w salonie w trybie wentylacji."
    assert FakeHomeAssistantConnection.calls == [
        ("find_devices", {"query": "", "device_type": "climate", "area_name": "office"}),
        ("list_modifiable_properties", {"device": "Living room air conditioner"}),
        ("modify_device", {"device": "Living room air conditioner", "property_name": "hvac_mode", "value": "fan_only"}),
    ]


def test_home_assistant_domain_agent_answers_global_device_count_query_without_modification() -> None:
    FakeHomeAssistantConnection.calls = []
    fake_loop = FakeAgentLoop({})
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
        loop_factory=fake_loop.factory,
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={"medium": "voice", "area": "office"})
    task = {
        "id": "t1",
        "domain": "home_assistant",
        "command": {
            "selection": {
                "include": [{"domain": "climate", "scope": "all"}],
                "exclude": [],
            },
            "operation": {
                "intent": "query_state",
                "description": "ile klimatyzatorów jest w domu?",
                "parameters": {"query_type": "count"},
            },
        },
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "W domu są 3 klimatyzatory."
    assert result["needs_clarification"] is False
    assert result["final_reply_mode"] == "verbatim"
    assert FakeHomeAssistantConnection.calls == [
        ("find_devices", {"query": "", "device_type": "climate", "area_name": ""}),
    ]


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ("not-json", "reply must be valid JSON"),
        ("[]", "reply must be a JSON object"),
        ('{"text":"ok"}', "status must be a non-empty string"),
        ('{"status":"ok","text":123}', "text must be a string"),
    ],
)
def test_parse_domain_reply_rejects_invalid_json(reply: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _parse_domain_reply(reply)


class FakeHomeAssistantConnection:
    inventory = object()
    calls = []

    def system_prompt_context(self, *, user: str | None, area: str | None) -> str:
        return f"Current user: {user or 'unknown'}\nCurrent area: {area or 'unknown'}"

    async def find_devices(self, query: str = "", device_type: str = "", area_name: str = ""):
        self.calls.append(("find_devices", {"query": query, "device_type": device_type, "area_name": area_name}))
        if device_type == "climate" and area_name == "":
            return [
                {
                    "device_id": "study_ac",
                    "name": "Study air conditioner",
                    "type": "climate",
                    "area_id": "office",
                    "area_name": "Office",
                },
                {
                    "device_id": "living_room_ac",
                    "name": "Living room air conditioner",
                    "type": "climate",
                    "area_id": "living_room",
                    "area_name": "Living room",
                },
                {
                    "device_id": "bedroom_ac",
                    "name": "Bedroom air conditioner",
                    "type": "climate",
                    "area_id": "bedroom",
                    "area_name": "Bedroom",
                },
            ]
        return [
            {
                "device_id": "living_room_ac",
                "name": "Living room air conditioner",
                "type": "climate",
                "area_id": "living_room",
                "area_name": "Living room",
            }
        ]

    async def list_modifiable_properties(self, device: str):
        self.calls.append(("list_modifiable_properties", {"device": device}))
        return [
            {
                "property_name": "target_temperature",
                "domain": "climate",
                "value_type": "number",
            },
            {
                "property_name": "hvac_mode",
                "domain": "climate",
                "value_type": "string",
                "allowed_values": ["off", "cool", "fan_only"],
                "value_aliases": {
                    "fan_only": ["wentylacja", "tryb wentylacji", "nawiew", "wiatrak", "fan", "ventilate", "ventilation"]
                },
            },
        ]

    async def list_common_modifiable_properties(self, devices: list[str]):
        self.calls.append(("list_common_modifiable_properties", {"devices": devices}))
        return {"devices": devices, "properties": []}

    async def modify_device(self, device: str, property_name: str, value):
        self.calls.append(("modify_device", {"device": device, "property_name": property_name, "value": value}))
        return {"status": "ok"}

    async def modify_devices(self, devices: list[str], property_name: str, value, *, user_message: str = "", current_area: str | None = None):
        self.calls.append(("modify_devices", {"devices": devices, "property_name": property_name, "value": value}))
        return {"status": "ok"}


class FakeAgentLoop:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.config = None
        self.system_prompt = None
        self.tools = None
        self.messages = []

    def factory(self, *, config, system_prompt, tools, ollama_connection=None, **kwargs):
        self.config = config
        self.system_prompt = system_prompt
        self.tools = tools
        self.ollama_connection = ollama_connection
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def send_user_message(self, message: str):
        self.messages.append(message)
        result = await self.tools.call_tool("execute_home_assistant_task", {})
        return FakeAgentReply(json.dumps(result, ensure_ascii=False), False)


class FakeAgentReply:
    def __init__(self, reply_text: str, end_conversation: bool) -> None:
        self.reply_text = reply_text
        self.end_conversation = end_conversation
