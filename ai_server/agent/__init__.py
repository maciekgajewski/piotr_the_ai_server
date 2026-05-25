from __future__ import annotations

import logging
from typing import Protocol

from ai_server.ai_tools import create_tools
from ai_server.agent.assitant import AssistantAgent
from ai_server.agent.echo import EchoAgent
from ai_server.agent.interrogator import InterrogatorAgent
from ai_server.agent.polite_reply import PoliteReplyAgent
from ai_server.config import AgentConfig
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.ollama import OllamaClient


class Agent(Protocol):
    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


async def create_agent(config: AgentConfig, ollama_url: str) -> Agent:
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
        tools = create_tools(config, ollama_client)
        logger.info("Loaded %s assistant tools", len(tools))
        agent = AssistantAgent(intent_router_model=intent_router_model, tools=tools, ollama_client=ollama_client)
        try:
            logger.info("Preloading assistant agent intent_router_model=%s", intent_router_model)

            await agent.preload()
        except BaseException:
            await agent.close()
            raise
        logger.info("Created asistant agent intent_router_model=%s", intent_router_model)
        return agent

    raise ValueError(f"unsupported agent type: {config.type}")
