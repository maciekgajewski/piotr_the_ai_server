import asyncio

import pytest

from ai_server.agent.assistant import AssistantAgent, ToolRoute, _parse_tool_route
from ai_server.ai_tools.interfaces import TOOL_NOT_IMPLEMENTED_REPLY
from ai_server.config import AgentConfig
from conftest import TextMessage, text_message_to_events
from conftest import FakeAgentChannel, agent_context, run_agent


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
    tool = RecordingTool(config)
    agent = AssistantAgent(
        intent_router_model="llama3.2:3b",
        tools={"time": tool},
        ollama_client=FakeOllamaClient('{"tool": "time", "confidence": 1.0}'),
        owns_ollama_client=False,
    )
    request = TextMessage(text="która godzina?")
    endpoint = FakeAgentChannel([request])
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice"})

    asyncio.run(run_agent(agent, conversation, endpoint))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text=TOOL_NOT_IMPLEMENTED_REPLY)))
    assert tool.request == request
    assert tool.conversation == conversation


class RecordingTool:
    name = "time"
    description = "Time tool."

    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    async def run(self, conversation, endpoint, request: TextMessage) -> None:
        self.conversation = conversation
        self.request = request
        await endpoint.send_message(TextMessage(text=TOOL_NOT_IMPLEMENTED_REPLY))

    async def close(self) -> None:
        pass


class FakeOllamaClient:
    def __init__(self, content: str) -> None:
        self.content = content

    async def chat(self, payload: dict):
        return {"message": {"role": "assistant", "content": self.content}}

    async def close(self) -> None:
        pass
