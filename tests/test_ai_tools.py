import asyncio
import logging
import locale

import pytest

from ai_server.agent_loop import AgentReply
from ai_server.ai_tools import create_tools
from ai_server.ai_tools.calculator import CalculatorTool
from ai_server.ai_tools.interfaces import TOOL_NOT_IMPLEMENTED_REPLY
from ai_server.ai_tools.clarify import ClarifyTool
from ai_server.ai_tools.home_assistant import HomeAssistantTool
from ai_server.ai_tools.time import TimeTool
from ai_server.ai_tools.weather import WeatherTool
from ai_server.ai_tools.web_search import WebSearchTool
from ai_server.ai_tools.wikipedia import WikipediaTool
from ai_server.config import AgentConfig
from ai_server.home_assistant import HomeAssistantConnection, HomeAssistantServiceCall, parse_home_assistant_options
from ai_server.home_assistant.connection import _build_inventory, _call_home_assistant_service
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
    tool = HomeAssistantTool(config, connection=_sample_connection(_sample_inventory()))
    endpoint = FakeConversationEndpoint([])
    conversation = Conversation(
        conversation_id="conversation-1",
        attributes={"area": "office", "user": "maciek"},
    )

    asyncio.run(tool.run(conversation, endpoint, TextMessage(text="włącz klimę")))

    assert fake_loop.config.model == "qwen3:8b"
    assert fake_loop.config.ollama_url == "http://ollama:11434"
    assert "Current user: maciek" in fake_loop.system_prompt
    assert "Current area: office" in fake_loop.system_prompt
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
        "ai_server.home_assistant.connection._call_home_assistant_service",
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


def test_home_assistant_modify_device_resolves_domain_area_context_reference(monkeypatch) -> None:
    calls = []

    async def fake_call_home_assistant_service(options, service_call: HomeAssistantServiceCall, logger) -> None:
        calls.append(service_call)

    monkeypatch.setattr(
        "ai_server.home_assistant.connection._call_home_assistant_service",
        fake_call_home_assistant_service,
    )
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.modify_device("climate.living_room", "target_temperature", 22))

    assert result == {
        "status": "ok",
        "service": "climate.set_temperature",
        "entity_id": "climate.living_room_air_conditioner",
    }
    assert calls == [
        HomeAssistantServiceCall(
            domain="climate",
            service="set_temperature",
            entity_id="climate.living_room_air_conditioner",
            service_data={"temperature": 22},
        )
    ]


def test_home_assistant_modify_device_rejects_invalid_value() -> None:
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.modify_device("klima", "target_temperature", 99))

    assert result["error"] == "invalid_property_value"
    assert result["property_name"] == "target_temperature"
    assert result["message"] == "target_temperature must be at most 30.0"


def test_home_assistant_find_devices_searches_globally_by_query_and_type() -> None:
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.find_devices(query="klimatyzator klima klimatyzacja", device_type="climate"))

    assert [device["device_id"] for device in result] == ["device-living-ac", "device-ac"]


def test_home_assistant_find_devices_infers_type_from_query_alias() -> None:
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.find_devices(query="klima"))

    assert [device["device_id"] for device in result] == ["device-living-ac", "device-ac"]


def test_home_assistant_find_devices_can_filter_by_area_alias() -> None:
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.find_devices(device_type="climate", area_name="salon"))

    assert [device["device_id"] for device in result] == ["device-living-ac"]


def test_home_assistant_lists_music_assistant_media_players() -> None:
    connection = _sample_connection(_sample_media_inventory())

    result = asyncio.run(connection.list_media_players(area_name="office"))

    assert result == [
        {
            "entity_id": "media_player.office_speaker",
            "device_id": "device-office-speaker",
            "name": "Office speaker",
            "aliases": ["office music"],
            "area_id": "office",
            "area_name": "Office",
            "state": "playing",
            "volume_level": 0.25,
            "is_music_assistant": True,
            "is_speaker": True,
            "group_members": ["media_player.living_room_speaker"],
        }
    ]


def test_home_assistant_media_player_join_calls_ws(monkeypatch) -> None:
    calls = []

    async def fake_call_home_assistant_service(options, service_call: HomeAssistantServiceCall, logger) -> None:
        calls.append(service_call)

    monkeypatch.setattr(
        "ai_server.home_assistant.connection._call_home_assistant_service",
        fake_call_home_assistant_service,
    )
    connection = _sample_connection(_sample_media_inventory())

    result = asyncio.run(
        connection.media_player_join("media_player.office_speaker", ["media_player.living_room_speaker"])
    )

    assert result == {
        "status": "ok",
        "service": "media_player.join",
        "entity_id": ["media_player.office_speaker"],
    }
    assert calls == [
        HomeAssistantServiceCall(
            domain="media_player",
            service="join",
            entity_id=["media_player.office_speaker"],
            service_data={"group_members": ["media_player.living_room_speaker"]},
        )
    ]


def test_home_assistant_media_player_unjoin_calls_ws(monkeypatch) -> None:
    calls = []

    async def fake_call_home_assistant_service(options, service_call: HomeAssistantServiceCall, logger) -> None:
        calls.append(service_call)

    monkeypatch.setattr(
        "ai_server.home_assistant.connection._call_home_assistant_service",
        fake_call_home_assistant_service,
    )
    connection = _sample_connection(_sample_media_inventory())

    result = asyncio.run(connection.media_player_unjoin(["media_player.office_speaker"]))

    assert result == {
        "status": "ok",
        "service": "media_player.unjoin",
        "entity_id": ["media_player.office_speaker"],
    }
    assert calls == [
        HomeAssistantServiceCall(
            domain="media_player",
            service="unjoin",
            entity_id=["media_player.office_speaker"],
            service_data={},
        )
    ]


def test_home_assistant_call_service_supports_return_response(monkeypatch) -> None:
    fake_socket = FakeHomeAssistantWebSocket({"response": {"items": [{"name": "Soft Jazz"}]}})
    monkeypatch.setattr("ai_server.home_assistant.connection._HomeAssistantWebSocket", fake_socket.factory)
    options = parse_home_assistant_options(_sample_config().options)

    result = asyncio.run(
        _call_home_assistant_service(
            options,
            HomeAssistantServiceCall(
                "music_assistant",
                "search",
                None,
                {"config_entry_id": "ma-1", "name": "soft jazz"},
                return_response=True,
            ),
            logging.getLogger("test"),
        )
    )

    assert result == {"response": {"items": [{"name": "Soft Jazz"}]}}
    assert fake_socket.payloads == [
        {
            "type": "call_service",
            "domain": "music_assistant",
            "service": "search",
            "service_data": {"config_entry_id": "ma-1", "name": "soft jazz"},
            "return_response": True,
        }
    ]


def test_home_assistant_connection_updates_cached_state_from_state_changed_event() -> None:
    connection = HomeAssistantConnection(parse_home_assistant_options(_sample_config().options))
    connection._raw_areas = [{"area_id": "office", "name": "Office", "aliases": ["Biuro"]}]
    connection._raw_devices = [{"id": "device-ac", "name": "Office AC", "name_by_user": None, "area_id": "office"}]
    connection._raw_entity_details = [
        {
            "entity_id": "climate.study_air_conditioner",
            "device_id": "device-ac",
            "area_id": None,
            "name": None,
            "original_name": "Study air conditioner",
            "aliases": ["klima"],
        }
    ]
    connection._states_by_entity_id = {
        "climate.study_air_conditioner": {
            "entity_id": "climate.study_air_conditioner",
            "state": "off",
            "attributes": {"friendly_name": "Study air conditioner", "hvac_modes": ["off", "cool"]},
        }
    }
    connection._rebuild_inventory_locked()

    asyncio.run(
        connection._handle_state_changed(
            {
                "event_type": "state_changed",
                "data": {
                    "entity_id": "climate.study_air_conditioner",
                    "new_state": {
                        "entity_id": "climate.study_air_conditioner",
                        "state": "cool",
                        "attributes": {
                            "friendly_name": "Study air conditioner",
                            "hvac_modes": ["off", "cool"],
                            "temperature": 22,
                        },
                    },
                },
            }
        )
    )

    device = connection.inventory.devices_by_id["device-ac"]
    assert device.entities[0].state == "cool"
    assert device.entities[0].attributes["temperature"] == 22


def test_home_assistant_common_properties_intersect_device_capabilities() -> None:
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.list_common_modifiable_properties(["klima", "salon ac"]))

    assert result["errors"] == []
    assert [device["device_id"] for device in result["devices"]] == ["device-ac", "device-living-ac"]
    hvac_mode = next(property_info for property_info in result["common_properties"] if property_info["property_name"] == "hvac_mode")
    assert hvac_mode["allowed_values"] == ["off", "cool"]


def test_home_assistant_modify_devices_returns_per_device_results_and_skips_unsupported(monkeypatch) -> None:
    calls = []

    async def fake_call_home_assistant_service(options, service_call: HomeAssistantServiceCall, logger) -> None:
        calls.append(service_call)

    monkeypatch.setattr(
        "ai_server.home_assistant.connection._call_home_assistant_service",
        fake_call_home_assistant_service,
    )
    tool = _sample_tool(_sample_inventory())

    result = asyncio.run(tool.modify_devices(["klima", "salon lampka"], "hvac_mode", "off"))

    assert result["status"] == "partial"
    assert result["errors"] == []
    assert result["results"] == [
        {
            "status": "ok",
            "service": "climate.set_hvac_mode",
            "entity_id": "climate.study_air_conditioner",
            "device_id": "device-ac",
            "device_name": "Office AC",
        },
        {
            "status": "skipped",
            "error": "unsupported_property",
            "property_name": "hvac_mode",
            "known_properties": ["brightness_percent", "on"],
            "device_id": "device-living-light",
            "device_name": "Living room lamp",
        },
    ]
    assert calls == [
        HomeAssistantServiceCall(
            domain="climate",
            service="set_hvac_mode",
            entity_id="climate.study_air_conditioner",
            service_data={"hvac_mode": "off"},
        )
    ]


def test_home_assistant_modify_devices_rejects_over_broad_batch_without_global_wording(monkeypatch) -> None:
    calls = []

    async def fake_call_home_assistant_service(options, service_call: HomeAssistantServiceCall, logger) -> None:
        calls.append(service_call)

    monkeypatch.setattr(
        "ai_server.home_assistant.connection._call_home_assistant_service",
        fake_call_home_assistant_service,
    )
    tool = _sample_tool(_sample_inventory())
    tool.set_request_context(user_message="ustaw klimę na 26 stopni", area="office")

    with pytest.raises(ValueError, match="The user did not ask for all matching devices"):
        asyncio.run(tool.modify_devices(["klima", "salon ac"], "target_temperature", 26))

    assert calls == []


def test_home_assistant_modify_devices_allows_batch_with_global_wording(monkeypatch) -> None:
    calls = []

    async def fake_call_home_assistant_service(options, service_call: HomeAssistantServiceCall, logger) -> None:
        calls.append(service_call)

    monkeypatch.setattr(
        "ai_server.home_assistant.connection._call_home_assistant_service",
        fake_call_home_assistant_service,
    )
    tool = _sample_tool(_sample_inventory())
    tool.set_request_context(user_message="wyłącz wszystkie klimatyzacje", area="office")

    result = asyncio.run(tool.modify_devices(["klima", "salon ac"], "hvac_mode", "off"))

    assert result["status"] == "ok"
    assert [call.entity_id for call in calls] == ["climate.study_air_conditioner", "climate.living_room_air_conditioner"]


class FakeSession:
    def post(self, url: str, json: dict):
        raise AssertionError("unexpected HTTP request")


class FakeOllamaClient:
    async def chat(self, payload: dict):
        return {"message": {"role": "assistant", "content": "Jest południe."}}


class FakeHomeAssistantWebSocket:
    def __init__(self, result):
        self.result = result
        self.payloads = []

    def factory(self, options, logger, *, log_traffic: bool):
        del options, logger, log_traffic
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def command(self, payload: dict):
        self.payloads.append(payload)
        return self.result


class FakeAgentLoop:
    def __init__(self) -> None:
        self.config = None
        self.system_prompt = None
        self.tools = None
        self.messages = []
        self.eval_count = 0

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
    return HomeAssistantTool(_sample_config(), connection=_sample_connection(inventory))


def _sample_connection(inventory):
    connection = HomeAssistantConnection(parse_home_assistant_options(_sample_config().options))
    connection._inventory = inventory
    return connection


def _sample_config():
    return AgentConfig(
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


def _sample_inventory():
    return _build_inventory(
        raw_areas=[
            {"area_id": "office", "name": "Office", "aliases": ["Biuro", "Pracownia"]},
            {"area_id": "living_room", "name": "Living room", "aliases": ["Salon"]},
        ],
        raw_devices=[
            {"id": "device-ac", "name": "Office AC", "name_by_user": None, "area_id": "office"},
            {"id": "device-living-ac", "name": "Living AC", "name_by_user": None, "area_id": "living_room"},
            {"id": "device-living-light", "name": "Living room lamp", "name_by_user": None, "area_id": "living_room"},
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
                "entity_id": "climate.living_room_air_conditioner",
                "device_id": "device-living-ac",
                "area_id": None,
                "name": None,
                "original_name": "Living room air conditioner",
                "aliases": ["salon ac"],
            },
            {
                "entity_id": "light.living_room_lamp",
                "device_id": "device-living-light",
                "area_id": None,
                "name": None,
                "original_name": "Living room lamp",
                "aliases": ["salon lampka"],
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
            {
                "entity_id": "climate.living_room_air_conditioner",
                "state": "off",
                "attributes": {
                    "friendly_name": "Living room air conditioner",
                    "min_temp": 16,
                    "max_temp": 30,
                    "target_temp_step": 1,
                    "hvac_modes": ["off", "cool"],
                    "fan_modes": ["auto", "low", "high"],
                },
            },
            {
                "entity_id": "light.living_room_lamp",
                "state": "off",
                "attributes": {"friendly_name": "Living room lamp", "supported_color_modes": ["brightness"]},
            },
            {"entity_id": "light.unassigned", "state": "off", "attributes": {"friendly_name": "Unassigned light"}},
        ],
        controllable_domains=("climate", "light", "switch", "fan", "cover"),
    )


def _sample_media_inventory():
    return _build_inventory(
        raw_areas=[
            {"area_id": "office", "name": "Office", "aliases": ["Biuro"]},
            {"area_id": "living_room", "name": "Living room", "aliases": ["Salon"]},
        ],
        raw_devices=[
            {"id": "device-office-speaker", "name": "Office speaker", "name_by_user": None, "area_id": "office"},
            {"id": "device-tv", "name": "TV", "name_by_user": None, "area_id": "living_room"},
        ],
        raw_entity_details=[
            {
                "entity_id": "media_player.office_speaker",
                "device_id": "device-office-speaker",
                "area_id": None,
                "name": None,
                "original_name": "Office speaker",
                "aliases": ["office music"],
                "platform": "music_assistant",
                "config_entry_id": "ma-1",
            },
            {
                "entity_id": "media_player.tv",
                "device_id": "device-tv",
                "area_id": None,
                "name": None,
                "original_name": "TV",
                "aliases": [],
                "platform": "cast",
                "config_entry_id": "cast-1",
            },
        ],
        raw_states=[
            {
                "entity_id": "media_player.office_speaker",
                "state": "playing",
                "attributes": {
                    "friendly_name": "Office speaker",
                    "volume_level": 0.25,
                    "device_class": "speaker",
                    "group_members": ["media_player.living_room_speaker"],
                },
            },
            {
                "entity_id": "media_player.tv",
                "state": "idle",
                "attributes": {
                    "friendly_name": "TV",
                    "device_class": "tv",
                },
            },
        ],
        controllable_domains=("climate", "light", "switch", "fan", "cover"),
    )
