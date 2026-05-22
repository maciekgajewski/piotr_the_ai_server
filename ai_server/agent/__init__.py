from __future__ import annotations

from typing import Protocol

from ai_server.config import AgentConfig
from ai_server.endpoint import CommunicationEndpoint


class Agent(Protocol):
    async def run(self, endpoint: CommunicationEndpoint, session_id: str) -> None:
        raise NotImplementedError


def create_agent(config: AgentConfig) -> Agent:
    if config.type == "echo":
        from ai_server.agent.echo import EchoAgent

        return EchoAgent()

    raise ValueError(f"unsupported agent type: {config.type}")
