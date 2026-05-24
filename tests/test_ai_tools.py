import asyncio
import locale

import pytest

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
from ai_server.interfaces import EndpointClosed
from ai_server.messages import MessageEvent, UserMessage, user_message_to_events
from ai_server.ollama import OllamaClient


def test_create_tools_builds_static_dictionary(caplog) -> None:
    config = AgentConfig(type="assistant", options={"intent_router_model": "llama3.2:3b"})
    ollama_client = OllamaClient(session=FakeSession())

    with caplog.at_level("INFO"):
        tools = create_tools(config, ollama_client)

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
    assert all(tool._ollama is ollama_client for tool in tools.values())
    assert "Loaded AI tool name=calculator class=CalculatorTool" in caplog.text


def test_tool_run_stubs_send_default_reply() -> None:
    config = AgentConfig(type="assistant", options={"intent_router_model": "llama3.2:3b"})
    ollama_client = OllamaClient(session=FakeSession())
    tool = CalculatorTool(config, ollama_client)
    endpoint = FakeEndpoint([])

    asyncio.run(tool.run(endpoint, UserMessage(text="zrób coś")))

    assert endpoint.sent == list(user_message_to_events(UserMessage(text=TOOL_NOT_IMPLEMENTED_REPLY)))


def test_time_tool_logs_locale_failure(monkeypatch, caplog) -> None:
    def fake_setlocale(category, value=None):
        if value is None:
            return "C"
        if value == "pl_PL.utf8":
            raise locale.Error("unsupported locale")
        return value

    monkeypatch.setattr(locale, "setlocale", fake_setlocale)
    config = AgentConfig(type="assistant", options={"intent_router_model": "llama3.2:3b"})
    tool = TimeTool(config, FakeOllamaClient())
    endpoint = FakeEndpoint([])

    with caplog.at_level("ERROR"):
        asyncio.run(tool.run(endpoint, UserMessage(text="która godzina?")))

    assert "failed to set locale pl_PL.utf8" in caplog.text


class FakeSession:
    def post(self, url: str, json: dict):
        raise AssertionError("unexpected HTTP request")


class FakeOllamaClient:
    async def chat(self, payload: dict):
        return {"message": {"role": "assistant", "content": "Jest południe."}}


class FakeEndpoint:
    def __init__(self, incoming: list[UserMessage]) -> None:
        self._incoming: list[MessageEvent] = []
        for message in incoming:
            self._incoming.extend(user_message_to_events(message))
        self.sent = []

    async def receive(self) -> MessageEvent:
        if not self._incoming:
            raise EndpointClosed()
        return self._incoming.pop(0)

    async def send(self, event: MessageEvent) -> None:
        self.sent.append(event)
