from __future__ import annotations

import logging
from typing import Protocol

from ai_server.config import AgentConfig
from ai_server.endpoint import CommunicationEndpoint

logger = logging.getLogger(f"{__name__}.factory")


class Agent(Protocol):
    async def run(self, endpoint: CommunicationEndpoint, session_id: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


async def create_agent(config: AgentConfig, ollama_url: str) -> Agent:
    logger.info("Creating agent type=%s", config.type)
    if config.type == "echo":
        from ai_server.agent.echo import EchoAgent

        logger.info("Created agent type=echo")
        return EchoAgent()

    if config.type == "polite_reply":
        from ai_server.agent.polite_reply import PoliteReplyAgent

        model = config.options["model"]
        logger.info("Creating polite_reply agent model=%s", model)
        agent = PoliteReplyAgent(model=model, base_url=ollama_url)
        try:
            logger.info("Preloading polite_reply agent model=%s", model)
            await agent.preload()
        except BaseException:
            await agent.close()
            raise
        logger.info("Created polite_reply agent model=%s", model)
        return agent

    raise ValueError(f"unsupported agent type: {config.type}")
