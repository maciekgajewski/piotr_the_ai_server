import asyncio
import json

import pytest

from conftest import agent_context
from ai_server.domain_agents.home_assistant import (
    HomeAssistantDomainAgent,
    _deterministic_command,
    _parse_domain_reply,
)


def _route(request: str) -> dict:
    return {
        "id": "t1",
        "domain": "home_assistant",
        "request": request,
        "depends_on": [],
    }


def test_home_assistant_domain_agent_resolves_context_reference_deterministically() -> None:
    FakeHomeAssistantConnection.calls = []
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice", "area": "office", "user": "maciek"})
    task = {
        "id": "t1",
        "domain": "home_assistant",
        "request": "ustaw ją na 22 stopnie",
        "depends_on": [],
    }

    result = asyncio.run(agent.run_task(conversation, task, {"salient_entities": ["climate.salon"]}))

    assert result["status"] == "ok"
    assert result["entities"] == ["climate.living_room"]
    assert FakeHomeAssistantConnection.calls == [
        ("find_devices", {"query": "", "device_type": "climate", "area_name": "salon"}),
        ("list_modifiable_properties", {"device": "Living room air conditioner"}),
        ("modify_device", {"device": "Living room air conditioner", "property_name": "target_temperature", "value": 22}),
    ]


@pytest.mark.parametrize(
    "request_text",
    [
        "wyłącz światło w biurze",
        "ustaw światło na 50 procent",
        "włącz wentylator w salonie",
        "turn off the light in the office",
        "turn on the fan in the living room",
        "turn off the switch in the office",
    ],
)
def test_climate_context_does_not_hijack_explicit_other_device_types(
    request_text: str,
) -> None:
    command = _deterministic_command(
        request_text,
        conversation_area="office",
        active_context={"salient_entities": ["climate.salon"]},
    )

    assert command is None


def test_climate_followup_does_not_skip_more_salient_other_device() -> None:
    command = _deterministic_command(
        "wyłącz ją",
        conversation_area="office",
        active_context={
            "salient_entities": ["light.office", "climate.salon"],
        },
    )

    assert command is None


def test_home_assistant_domain_agent_normalizes_hvac_mode_alias_before_availability_check() -> None:
    FakeHomeAssistantConnection.calls = []
    command = {
        "selection": {
            "include": [{"domain": "climate", "scope": "single", "area": "office"}],
            "exclude": [],
        },
        "operation": {
            "intent": "set_hvac_mode",
            "description": "włącz klimatyzator w tryb wentylacji",
            "parameters": {"hvac_mode": "ventilate"},
        },
    }
    fake_loop = FakeAgentLoop({}, command)
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
        loop_factory=fake_loop.factory,
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice", "area": "office"})
    task = {
        "id": "t1",
        "domain": "home_assistant",
        "request": "włącz klimatyzator w tryb wentylacji",
        "depends_on": [],
    }

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "Ustawiłem klimatyzację w biurze w trybie wentylacji."
    assert FakeHomeAssistantConnection.calls == [
        ("find_devices", {"query": "", "device_type": "climate", "area_name": "office"}),
        ("list_modifiable_properties", {"device": "Study air conditioner"}),
        ("modify_device", {"device": "Study air conditioner", "property_name": "hvac_mode", "value": "fan_only"}),
    ]


def test_home_assistant_domain_agent_answers_global_device_count_query_without_modification() -> None:
    FakeHomeAssistantConnection.calls = []
    command = {
        "selection": {
            "include": [{"domain": "climate", "scope": "all"}],
            "exclude": [],
        },
        "operation": {
            "intent": "query_state",
            "description": "ile klimatyzatorów jest w domu?",
            "parameters": {"query_type": "count"},
        },
    }
    fake_loop = FakeAgentLoop({}, command)
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
        loop_factory=fake_loop.factory,
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice", "area": "office"})
    task = {
        "id": "t1",
        "domain": "home_assistant",
        "request": "ile klimatyzatorów jest w domu?",
        "depends_on": [],
    }

    result = asyncio.run(agent.run_task(conversation, task, {}))

    assert result["status"] == "ok"
    assert result["text"] == "W domu są 3 klimatyzatory."
    assert result["needs_clarification"] is False
    assert result["final_reply_mode"] == "verbatim"
    assert FakeHomeAssistantConnection.calls == [
        ("find_devices", {"query": "", "device_type": "climate", "area_name": ""}),
    ]


def test_home_assistant_unknown_named_area_never_falls_back_to_context() -> None:
    FakeHomeAssistantConnection.calls = []
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
    )
    conversation = agent_context(
        conversation_id="conversation-1",
        attributes={"medium": "voice", "area": "office"},
    )

    result = asyncio.run(
        agent.run_task(
            conversation,
            _route("wyłącz klimatyzację w garażu"),
            {"salient_entities": ["climate.salon"]},
        )
    )

    assert result["status"] == "needs_clarification"
    assert "gara" in result["clarification_question"]
    assert FakeHomeAssistantConnection.calls == [
        ("find_devices", {"query": "", "device_type": "climate", "area_name": "garazu"}),
    ]
    resumed = asyncio.run(
        agent.resume_clarification(
            conversation,
            _route("wyłącz klimatyzację w garażu"),
            result["clarification_question"],
            "w biurze",
            {"salient_entities": ["climate.salon"]},
            None,
        )
    )
    assert resumed["status"] == "ok"
    assert FakeHomeAssistantConnection.calls[-3] == (
        "find_devices",
        {"query": "", "device_type": "climate", "area_name": "office"},
    )
    assert FakeHomeAssistantConnection.calls[-1] == (
        "modify_device",
        {
            "device": "Study air conditioner",
            "property_name": "hvac_mode",
            "value": "off",
        },
    )


def test_home_assistant_short_temperature_clarification_answer_completes() -> None:
    FakeHomeAssistantConnection.calls = []
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
    )
    conversation = agent_context(
        conversation_id="conversation-1",
        attributes={"medium": "voice", "area": "office"},
    )
    task = _route("Ustaw klimatyzację w salonie")
    first = asyncio.run(agent.run_task(conversation, task, {}))

    assert first["status"] == "needs_clarification"
    result = asyncio.run(
        agent.resume_clarification(
            conversation,
            task,
            first["clarification_question"],
            "22",
            {},
            None,
        )
    )

    assert result["status"] == "ok"
    assert FakeHomeAssistantConnection.calls[-3:] == [
        ("find_devices", {"query": "", "device_type": "climate", "area_name": "living_room"}),
        ("list_modifiable_properties", {"device": "Living room air conditioner"}),
        (
            "modify_device",
            {
                "device": "Living room air conditioner",
                "property_name": "target_temperature",
                "value": 22,
            },
        ),
    ]


def test_home_assistant_model_reply_without_tool_returns_controlled_failure() -> None:
    fake_loop = FakeNoToolAgentLoop()
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
        loop_factory=fake_loop.factory,
    )

    result = asyncio.run(
        agent.run_task(
            agent_context(conversation_id="conversation-1", attributes={"medium": "voice"}),
            _route("zmień urządzenie"),
            {},
        )
    )

    assert result == {
        "status": "failed",
        "text": "Nie mogę teraz wykonać polecenia Home Assistant.",
        "needs_clarification": False,
        "entities": [],
    }


@pytest.mark.parametrize(
    ("request_text", "property_name", "value"),
    [
        ("włącz klimatyzator w trybie chłodzenia", "hvac_mode", "cool"),
        ("wyłącz klimatyzację w tej chwili", "hvac_mode", "off"),
        ("ustaw klimatyzację w trybie auto", "hvac_mode", "auto"),
    ],
)
def test_home_assistant_non_area_w_phrases_use_current_area(
    request_text: str,
    property_name: str,
    value: str,
) -> None:
    FakeHomeAssistantConnection.calls = []
    agent = HomeAssistantDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        connection=FakeHomeAssistantConnection(),
    )

    result = asyncio.run(
        agent.run_task(
            agent_context(
                conversation_id="conversation-1",
                attributes={"medium": "voice", "area": "office"},
            ),
            _route(request_text),
            {},
        )
    )

    assert result["status"] == "ok"
    assert FakeHomeAssistantConnection.calls[0] == (
        "find_devices",
        {"query": "", "device_type": "climate", "area_name": "office"},
    )
    assert FakeHomeAssistantConnection.calls[-1] == (
        "modify_device",
        {
            "device": "Study air conditioner",
            "property_name": property_name,
            "value": value,
        },
    )


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
        if area_name == "garazu":
            return {"error": "unknown_area", "area": area_name}
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
        if device_type == "climate" and area_name == "office":
            return [
                {
                    "device_id": "study_ac",
                    "name": "Study air conditioner",
                    "type": "climate",
                    "area_id": "office",
                    "area_name": "Office",
                }
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
                "allowed_values": ["off", "cool", "fan_only", "auto"],
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
    def __init__(self, reply: dict, command: dict | None = None) -> None:
        self.reply = reply
        self.command = command or {}
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
        result = await self.tools.call_tool(
            "execute_home_assistant_task",
            {"command": self.command},
        )
        return FakeAgentReply(json.dumps(result, ensure_ascii=False), False)


class FakeNoToolAgentLoop:
    def factory(self, **kwargs):
        del kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def send_user_message(self, message: str):
        del message
        return FakeAgentReply('{"status":"ok","text":"Gotowe."}', False)


class FakeAgentReply:
    def __init__(self, reply_text: str, end_conversation: bool) -> None:
        self.reply_text = reply_text
        self.end_conversation = end_conversation
