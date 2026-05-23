import asyncio

import pytest

from ai_server.agent.assitant import AssistantAgent, ToolRoute, _parse_tool_route
from ai_server.ai_tools.interfaces import TOOL_NOT_IMPLEMENTED_REPLY
from ai_server.config import AgentConfig
from ai_server.interfaces import EndpointClosed
from ai_server.messages import MessageEvent, UserMessage, user_message_to_events
from ai_server.ollama import OllamaClient
from ai_server.streaming import send_user_message


def test_parse_tool_route() -> None:
    assert _parse_tool_route('{"tool": "time", "confidence": 0.75}') == ToolRoute(
        tool="time",
        confidence=0.75,
    )


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("not-json", "router response must be valid JSON"),
        ("[]", "router response must be a JSON object"),
        ('{"confidence": 1.0}', "tool must be a non-empty string"),
        ('{"tool": "time"}', "confidence must be a number"),
        ('{"tool": "time", "confidence": true}', "confidence must be a number"),
    ],
)
def test_parse_tool_route_rejects_invalid_response(content: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _parse_tool_route(content)


def test_assistant_routes_message_to_selected_tool() -> None:
    config = AgentConfig(type="assistant", options={"intent_router_model": "llama3.2:3b"})
    ollama_client = OllamaClient(session=FakeSession())
    agent = AssistantAgent(
        intent_router_model="llama3.2:3b",
        tools={"time": RecordingTool(config, ollama_client)},
        ollama_client=FakeOllamaClient('{"tool": "time", "confidence": 1.0}'),
        owns_ollama_client=False,
    )
    endpoint = FakeEndpoint([UserMessage(text="która godzina?")])

    with pytest.raises(EndpointClosed):
        asyncio.run(agent.run(endpoint, "session-1"))

    assert endpoint.sent == list(user_message_to_events(UserMessage(text=TOOL_NOT_IMPLEMENTED_REPLY)))


class RecordingTool:
    name = "time"
    description = "Time tool."

    def __init__(self, config: AgentConfig, ollama_client: OllamaClient) -> None:
        self._config = config
        self._ollama = ollama_client

    async def run(self, endpoint) -> None:
        await send_user_message(endpoint, UserMessage(text=TOOL_NOT_IMPLEMENTED_REPLY))


class FakeOllamaClient:
    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, payload: dict):
        return {"message": {"role": "assistant", "content": self.content}}

    async def close(self) -> None:
        pass


class FakeSession:
    def post(self, url: str, json: dict):
        raise AssertionError("unexpected HTTP request")


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
