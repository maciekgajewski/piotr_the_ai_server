from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from ai_server.ai_tools import create_tools
from ai_server.agent.assistant import AssistantAgent
from ai_server.agent.echo import EchoAgent
from ai_server.agent.interrogator import InterrogatorAgent
from ai_server.agent.polite_reply import PoliteReplyAgent
from ai_server.orchestrator import OrchestratorAgent

from ai_server.config import AgentConfig, DEFAULT_CACHE_DIR, ServerConfig
from ai_server.domain_agents import create_domain_agents
from ai_server.home_assistant import HomeAssistantConnection
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.ollama_client import OllamaClient


class Agent(Protocol):
    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


async def create_agent(
    config: AgentConfig,
    ollama_url: str,
    home_assistant_connection: HomeAssistantConnection | None = None,
    server_config: ServerConfig = ServerConfig(),
    cache_dir: Path = Path(DEFAULT_CACHE_DIR).expanduser(),
) -> Agent:
    logger = logging.getLogger(f"{__name__}.factory[{config.type}]")
    logger.info("Creating agent type=%s", config.type)
    if config.type == "echo":
        logger.info("Created agent type=echo")
        return EchoAgent()

    if config.type == "interrogator":
        logger.info("Created agent type=interrogator")
        return InterrogatorAgent()

    if config.type == "polite_reply":
        model = config.options["model"]
        logger.info("Creating polite_reply agent model=%s", model)
        ollama_client = OllamaClient(base_url=ollama_url)
        agent = PoliteReplyAgent(model=model, ollama_client=ollama_client)
        try:
            logger.info("Preloading polite_reply agent model=%s", model)
            await agent.preload()
        except BaseException:
            await agent.close()
            raise
        logger.info("Created polite_reply agent model=%s", model)
        return agent

    if config.type == "assistant":
        intent_router_model = config.options["intent_router_model"]
        logger.info("Creating assistant agent intent_router_model=%s", intent_router_model)
        ollama_client = OllamaClient(base_url=ollama_url)
        tool_config = AgentConfig(type=config.type, options={**config.options, "ollama_url": ollama_url})
        tools = create_tools(tool_config, home_assistant_connection=home_assistant_connection)
        logger.info("Loaded %s assistant tools", len(tools))
        agent = AssistantAgent(intent_router_model=intent_router_model, tools=tools, ollama_client=ollama_client)
        try:
            logger.info("Preloading assistant agent intent_router_model=%s", intent_router_model)

            await agent.preload()
        except BaseException:
            await agent.close()
            raise
        logger.info("Created assistant agent intent_router_model=%s", intent_router_model)
        return agent

    if config.type == "orchestrator":
        orchestrator_model = config.options["orchestrator_model"]
        clarification_model = config.options.get("clarification_model")
        logger.info(
            "Creating orchestrator agent orchestrator_model=%s clarification_model=%s",
            orchestrator_model,
            clarification_model,
        )
        ollama_client = OllamaClient(base_url=ollama_url)
        domain_agents = create_domain_agents(
            config,
            ollama_url,
            home_assistant_connection=home_assistant_connection,
            server_config=server_config,
            cache_dir=cache_dir,
        )
        logger.info("Loaded %s orchestrator domain agents", len(domain_agents))
        agent = OrchestratorAgent(
            orchestrator_model=orchestrator_model,
            clarification_model=clarification_model,
            domain_agents=domain_agents,
            ollama_client=ollama_client,
            server_config=server_config,
        )
        try:
            logger.info("Preloading orchestrator agent orchestrator_model=%s", orchestrator_model)
            await agent.preload()
        except BaseException:
            await agent.close()
            raise
        logger.info("Created orchestrator agent orchestrator_model=%s", orchestrator_model)
        return agent

    raise ValueError(f"unsupported agent type: {config.type}")
