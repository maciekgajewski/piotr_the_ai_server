import asyncio

import pytest

from ai_server.agent.assitant import AssistantAgent, ToolRoute, _parse_tool_route
from ai_server.ai_tools.interfaces import TOOL_NOT_IMPLEMENTED_REPLY
from ai_server.config import AgentConfig
from ai_server.interfaces import Conversation
from ai_server.messages import TextMessage, text_message_to_events
from ai_server.ollama import OllamaClient
from conftest import FakeConversationEndpoint


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
    tool = RecordingTool(config, ollama_client)
    agent = AssistantAgent(
        intent_router_model="llama3.2:3b",
        tools={"time": tool},
        ollama_client=FakeOllamaClient('{"tool": "time", "confidence": 1.0}'),
        owns_ollama_client=False,
    )
    request = TextMessage(text="która godzina?")
    endpoint = FakeConversationEndpoint([request])
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text=TOOL_NOT_IMPLEMENTED_REPLY)))
    assert tool.request == request


class RecordingTool:
    name = "time"
    description = "Time tool."

    def __init__(self, config: AgentConfig, ollama_client: OllamaClient) -> None:
        self._config = config
        self._ollama = ollama_client

    async def run(self, endpoint, request: TextMessage) -> None:
        self.request = request
        await endpoint.send_message(TextMessage(text=TOOL_NOT_IMPLEMENTED_REPLY))


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

