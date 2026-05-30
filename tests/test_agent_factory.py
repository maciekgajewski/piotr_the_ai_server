import asyncio
from pathlib import Path

import pytest

from ai_server.agent import create_agent
from ai_server.agent.assistant import AssistantAgent, _build_user_prompt_template
from ai_server.agent.echo import EchoAgent
from ai_server.agent.interrogator import InterrogatorAgent
from ai_server.agent.orchestrator import OrchestratorAgent
from ai_server.agent.polite_reply import PoliteReplyAgent
from ai_server.ai_tools.calculator import CalculatorTool
from ai_server.ai_tools.home_assistant import HomeAssistantTool
from ai_server.config import AgentConfig, ServerConfig
from ai_server.domain_agents.current_time import CurrentTimeDomainAgent
from ai_server.domain_agents.wikipedia import WikipediaDomainAgent
from ai_server.home_assistant import HomeAssistantConnection, parse_home_assistant_options


def test_create_agent_returns_echo_agent() -> None:
    agent = asyncio.run(create_agent(AgentConfig(type="echo", options={}), "http://ollama:11434"))

    assert isinstance(agent, EchoAgent)


def test_create_agent_returns_interrogator_agent() -> None:
    agent = asyncio.run(create_agent(AgentConfig(type="interrogator", options={}), "http://ollama:11434"))

    assert isinstance(agent, InterrogatorAgent)


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
            assert agent._ollama._base_url == "http://ollama:11434"
        finally:
            await agent.close()

    monkeypatch.setattr(PoliteReplyAgent, "preload", fake_preload)

    asyncio.run(create_and_check_agent())


def test_create_agent_returns_assistant_agent_with_loaded_tools(monkeypatch) -> None:
    async def fake_preload(self) -> None:
        pass

    async def create_and_check_agent() -> None:
        config = AgentConfig(
            type="assistant",
            options={
                "intent_router_model": "llama3.2:3b",
                "model": "qwen3:8b",
                "home_assistant": {
                    "url": "http://ha.local:8123",
                    "token": "secret-token",
                },
            },
        )
        home_assistant_connection = HomeAssistantConnection(parse_home_assistant_options(config.options))
        agent = await create_agent(
            config,
            "http://ollama:11434",
            home_assistant_connection=home_assistant_connection,
        )

        try:
            assert isinstance(agent, AssistantAgent)
            assert agent._tools["home_assistant"]._config.options["ollama_url"] == "http://ollama:11434"
            assert agent._tools["home_assistant"]._connection is home_assistant_connection
            assert "calculator" in agent._tools
            assert "- calculator: A tool for performing mathematical calculations." in agent._user_prompt_template
            assert "User input: {user_input}" in agent._user_prompt_template
        finally:
            await agent.close()

    monkeypatch.setattr(AssistantAgent, "preload", fake_preload)

    asyncio.run(create_and_check_agent())


def test_create_agent_returns_orchestrator_agent(monkeypatch) -> None:
    async def fake_preload(self) -> None:
        pass

    async def create_and_check_agent() -> None:
        config = AgentConfig(
            type="orchestrator",
            options={
                "model": "qwen3:4b-instruct",
                "domain_agents": {
                    "home_assistant": {"model": "qwen3:8b"},
                    "time": {},
                    "wikipedia": {},
                },
                "home_assistant": {
                    "url": "http://ha.local:8123",
                    "token": "secret-token",
                },
            },
        )
        home_assistant_connection = HomeAssistantConnection(parse_home_assistant_options(config.options))
        agent = await create_agent(
            AgentConfig(
                type="orchestrator",
                options=config.options,
            ),
            "http://ollama:11434",
            home_assistant_connection=home_assistant_connection,
            server_config=ServerConfig(timezone="Europe/Warsaw", location="Wrocław"),
            cache_dir=Path("/tmp/piotr-test-cache"),
        )

        try:
            assert isinstance(agent, OrchestratorAgent)
            assert agent._model == "qwen3:4b-instruct"
            assert agent._ollama._base_url == "http://ollama:11434"
            assert agent._server_config == ServerConfig(timezone="Europe/Warsaw", location="Wrocław")
            assert agent._domain_agents["home_assistant"]._model == "qwen3:8b"
            assert isinstance(agent._domain_agents["time"], CurrentTimeDomainAgent)
            assert agent._domain_agents["time"]._timezone == "Europe/Warsaw"
            assert agent._domain_agents["time"]._location == "Wrocław"
            assert isinstance(agent._domain_agents["wikipedia"], WikipediaDomainAgent)
            assert agent._domain_agents["wikipedia"]._languages == ("pl", "en")
        finally:
            await agent.close()

    monkeypatch.setattr(OrchestratorAgent, "preload", fake_preload)

    asyncio.run(create_and_check_agent())


def test_assistant_prompt_template_preserves_json_schema_braces() -> None:
    config = AgentConfig(type="assistant", options={"intent_router_model": "llama3.2:3b"})
    tool = CalculatorTool(config)
    template = _build_user_prompt_template({"calculator": tool})

    prompt = template.format(user_input="która godzina?")

    assert '{"tool": "...","confidence": 0.0}' in prompt
    assert "User input: która godzina?" in prompt


def test_create_agent_rejects_unknown_agent_type() -> None:
    with pytest.raises(ValueError, match="unsupported agent type: unknown"):
        asyncio.run(create_agent(AgentConfig(type="unknown", options={}), "http://ollama:11434"))


class FakeSession:
    def post(self, url: str, json: dict):
        raise AssertionError("unexpected HTTP request")
