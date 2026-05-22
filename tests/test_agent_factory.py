import asyncio

import pytest

from ai_server.agent import create_agent
from ai_server.agent.echo import EchoAgent
from ai_server.agent.polite_reply import PoliteReplyAgent
from ai_server.config import AgentConfig


def test_create_agent_returns_echo_agent() -> None:
    agent = asyncio.run(create_agent(AgentConfig(type="echo", options={}), "http://ollama:11434"))

    assert isinstance(agent, EchoAgent)


def test_create_agent_returns_polite_reply_agent(monkeypatch) -> None:
    async def fake_preload(self) -> None:
        pass

    async def create_and_check_agent() -> None:
        agent = await create_agent(
            AgentConfig(
                type="polite_reply",
                options={"model": "qwen3:4b"},
            ),
            "http://ollama:11434",
        )

        try:
            assert isinstance(agent, PoliteReplyAgent)
            assert agent._base_url == "http://ollama:11434"
        finally:
            await agent.close()

    monkeypatch.setattr(PoliteReplyAgent, "preload", fake_preload)

    asyncio.run(create_and_check_agent())


def test_create_agent_rejects_unknown_agent_type() -> None:
    with pytest.raises(ValueError, match="unsupported agent type: unknown"):
        asyncio.run(create_agent(AgentConfig(type="unknown", options={}), "http://ollama:11434"))
