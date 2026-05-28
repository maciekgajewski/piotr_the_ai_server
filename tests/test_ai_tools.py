import asyncio
import locale

import pytest

from ai_server.agent_loop import AgentReply
from ai_server.ai_tools import create_tools
from ai_server.ai_tools.calculator import CalculatorTool
from ai_server.ai_tools.interfaces import TOOL_NOT_IMPLEMENTED_REPLY
from ai_server.ai_tools.clarify import ClarifyTool
from ai_server.ai_tools.home_assistant import HomeAssistantTool
from ai_server.ai_tools.home_assistant.home_assistant import (
    HomeAssistantServiceCall,
    _build_inventory,
)
from ai_server.ai_tools.time import TimeTool
from ai_server.ai_tools.weather import WeatherTool
from ai_server.ai_tools.web_search import WebSearchTool
from ai_server.ai_tools.wikipedia import WikipediaTool
from ai_server.config import AgentConfig
from ai_server.interfaces import Conversation
from ai_server.messages import TextMessage, text_message_to_events
from conftest import FakeConversationEndpoint


def test_create_tools_builds_static_dictionary(caplog) -> None:
    config = AgentConfig(
        type="assistant",
        options={
            "intent_router_model": "llama3.2:3b",
            "model": "qwen3:8b",
            "ollama_url": "http://ollama:11434",
            "home_assistant": {
                "url": "http://ha.local:8123",
                "token": "secret-token",
            },
        },
    )

    with caplog.at_level("INFO"):
        tools = create_tools(config)

    assert tools.keys() == {
        "calculator",
        "clarify",
        "home_assistant",
        "time",
        "weather",
        "web_search",
        "wikipedia",
    }
    assert isinstance(tools["calculator"], CalculatorTool)
    assert isinstance(tools["clarify"], ClarifyTool)
    assert isinstance(tools["home_assistant"], HomeAssistantTool)
    assert isinstance(tools["time"], TimeTool)
    assert isinstance(tools["weather"], WeatherTool)
    assert isinstance(tools["web_search"], WebSearchTool)
    assert isinstance(tools["wikipedia"], WikipediaTool)
    assert all(tool._config is config for tool in tools.values())
    assert tools["time"]._ollama_url == "http://ollama:11434"
    assert "Loaded AI tool name=calculator class=CalculatorTool" in caplog.text


def test_tool_run_stubs_send_default_reply() -> None:
    config = AgentConfig(type="assistant", options={"intent_router_model": "llama3.2:3b"})
    tool = CalculatorTool(config)
    endpoint = FakeConversationEndpoint([])
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    asyncio.run(tool.run(conversation, endpoint, TextMessage(text="zrób coś")))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text=TOOL_NOT_IMPLEMENTED_REPLY)))


def test_time_tool_logs_locale_failure(monkeypatch, caplog) -> None:
    def fake_setlocale(category, value=None):
        if value is None:
            return "C"
        if value == "pl_PL.utf8":
            raise locale.Error("unsupported locale")
        return value

    monkeypatch.setattr(locale, "setlocale", fake_setlocale)
    monkeypatch.setattr("ai_server.ai_tools.time.time.OllamaClient", lambda base_url: FakeOllamaClient())
    config = AgentConfig(
        type="assistant",
        options={"intent_router_model": "llama3.2:3b", "ollama_url": "http://ollama:11434"},
    )
    tool = TimeTool(config)
    endpoint = FakeConversationEndpoint([])
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    with caplog.at_level("ERROR"):
        asyncio.run(tool.run(conversation, endpoint, TextMessage(text="która godzina?")))

    assert "failed to set locale pl_PL.utf8" in caplog.text


def test_home_assistant_tool_runs_local_agent_loop_with_context(monkeypatch) -> None:
    fake_loop = FakeAgentLoop()
    monkeypatch.setattr("ai_server.ai_tools.home_assistant.home_assistant.AgentLoop", fake_loop.factory)
    monkeypatch.setattr(HomeAssistantTool, "_start_background_refresh", lambda self: None)
    config = AgentConfig(
        type="assistant",
        options={
            "intent_router_model": "llama3.2:3b",
            "model": "qwen3:8b",
            "ollama_url": "http://ollama:11434",
            "home_assistant": {
                "url": "http://ha.local:8123/",
                "token": "secret-token",
            },
        },
    )
    tool = HomeAssistantTool(config)
    tool._inventory = _sample_inventory()
    endpoint = FakeConversationEndpoint([])
    conversation = Conversation(
        conversation_id="conversation-1",
        attributes={"location": "office", "user": "maciek"},
    )

    asyncio.run(tool.run(conversation, endpoint, TextMessage(text="włącz klimę")))

    assert fake_loop.config.model == "qwen3:8b"
    assert fake_loop.config.ollama_url == "http://ollama:11434"
    assert "Current user: maciek" in fake_loop.system_prompt
    assert "Current location: office" in fake_loop.system_prompt
    assert "area_id=office; name=Office; aliases=Biuro, Pracownia" in fake_loop.system_prompt
    assert "hvac_mode: fan_only aliases:" in fake_loop.system_prompt
    assert fake_loop.messages == ["włącz klimę"]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Gotowe.")))


def test_home_assistant_tool_requires_config() -> None:
    config = AgentConfig(type="assistant", options={"intent_router_model": "llama3.2:3b"})

    with pytest.raises(ValueError, match="agent.home_assistant must be a mapping"):
        HomeAssistantTool(config)


def test_home_assistant_inventory_uses_area_and_entity_aliases() -> None:
    inventory = _sample_inventory()

    assert [device["device_id"] for device in asyncio.run(_sample_tool(inventory).list_devices("biuro"))] == [
        "device-ac"
    ]
    assert [device["device_id"] for device in asyncio.run(_sample_tool(inventory).list_devices("Pracownia"))] == [
        "device-ac"
    ]
    assert [device["device_id"] for device in asyncio.run(_sample_tool(inventory).list_devices("office"))] == [
        "device-ac"
    ]


def test_home_assistant_properties_resolve_device_aliases() -> None:
    tool = _sample_tool(_sample_inventory())

    properties = asyncio.run(tool.list_modifiable_properties("klima"))

    assert {property_info["property_name"] for property_info in properties} == {
        "on",
        "target_temperature",
        "hvac_mode",
        "fan_mode",
    }
    hvac_mode = next(property_info for property_info in properties if property_info["property_name"] == "hvac_mode")
    assert hvac_mode["allowed_values"] == ["off", "cool", "fan_only"]
    assert hvac_mode["value_aliases"]["fan_only"][:2] == ["wentylacja", "tryb wentylacji"]


def test_home_assistant_modify_device_normalizes_vocabulary_and_calls_ws(monkeypatch) -> None:
    calls = []

    async def fake_call_home_assistant_service(options, service_call: HomeAssistantServiceCall, logger) -> None:
        calls.append(service_call)

    monkeypatch.setattr(
        "ai_server.ai_tools.home_assistant.home_assistant._call_home_assistant_service",
        fake_call_home_assistant_service,
    )
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.modify_device("klima", "hvac_mode", "tryb wentylacji"))

    assert result == {
        "status": "ok",
        "service": "climate.set_hvac_mode",
        "entity_id": "climate.study_air_conditioner",
    }
    assert calls == [
        HomeAssistantServiceCall(
            domain="climate",
            service="set_hvac_mode",
            entity_id="climate.study_air_conditioner",
            service_data={"hvac_mode": "fan_only"},
        )
    ]


def test_home_assistant_modify_device_rejects_invalid_value() -> None:
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.modify_device("klima", "target_temperature", 99))

    assert result["error"] == "invalid_property_value"
    assert result["property_name"] == "target_temperature"
    assert result["message"] == "target_temperature must be at most 30.0"


class FakeSession:
    def post(self, url: str, json: dict):
        raise AssertionError("unexpected HTTP request")


class FakeOllamaClient:
    async def chat(self, payload: dict):
        return {"message": {"role": "assistant", "content": "Jest południe."}}


class FakeAgentLoop:
    def __init__(self) -> None:
        self.config = None
        self.system_prompt = None
        self.tools = None
        self.messages = []

    def factory(self, config, system_prompt, tools):
        self.config = config
        self.system_prompt = system_prompt
        self.tools = tools
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def send_user_message(self, message: str) -> AgentReply:
        self.messages.append(message)
        return AgentReply(reply_text="Gotowe.", end_conversation=False)


def _sample_tool(inventory):
    config = AgentConfig(
        type="assistant",
        options={
            "intent_router_model": "llama3.2:3b",
            "model": "qwen3:8b",
            "ollama_url": "http://ollama:11434",
            "home_assistant": {
                "url": "http://ha.local:8123",
                "token": "secret-token",
            },
        },
    )
    tool = HomeAssistantTool(config)
    tool._inventory = inventory
    return tool


def _sample_inventory():
    return _build_inventory(
        raw_areas=[
            {"area_id": "office", "name": "Office", "aliases": ["Biuro", "Pracownia"]},
            {"area_id": "living_room", "name": "Living room", "aliases": ["Salon"]},
        ],
        raw_devices=[
            {"id": "device-ac", "name": "Office AC", "name_by_user": None, "area_id": "office"},
            {"id": "device-unassigned", "name": "Unassigned", "name_by_user": None, "area_id": None},
        ],
        raw_entity_details=[
            {
                "entity_id": "climate.study_air_conditioner",
                "device_id": "device-ac",
                "area_id": None,
                "name": None,
                "original_name": "Study air conditioner",
                "aliases": ["klima", "klimatyzacja w biurze"],
            },
            {
                "entity_id": "light.unassigned",
                "device_id": "device-unassigned",
                "area_id": None,
                "name": "Unassigned light",
                "original_name": None,
                "aliases": [],
            },
        ],
        raw_states=[
            {
                "entity_id": "climate.study_air_conditioner",
                "state": "off",
                "attributes": {
                    "friendly_name": "Study air conditioner",
                    "min_temp": 16,
                    "max_temp": 30,
                    "target_temp_step": 0.5,
                    "hvac_modes": ["off", "cool", "fan_only"],
                    "fan_modes": ["auto", "low", "high"],
                },
            },
            {"entity_id": "light.unassigned", "state": "off", "attributes": {"friendly_name": "Unassigned light"}},
        ],
        controllable_domains=("climate", "light", "switch", "fan", "cover"),
    )
