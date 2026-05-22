import pytest

from ai_server.agent import create_agent
from ai_server.agent.echo import EchoAgent
from ai_server.config import AgentConfig


def test_create_agent_returns_echo_agent() -> None:
    assert isinstance(create_agent(AgentConfig(type="echo", options={})), EchoAgent)


def test_create_agent_rejects_unknown_agent_type() -> None:
    with pytest.raises(ValueError, match="unsupported agent type: unknown"):
        create_agent(AgentConfig(type="unknown", options={}))
