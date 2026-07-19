from __future__ import annotations

import logging
from typing import Protocol

from ai_server.config import AgentConfig
from ai_server.conversations.agent_context import AgentExecutionContext
from ai_server.conversations.agent_runtime import AgentChannel
from ai_server.conversations.messages import UserMessage


TOOL_NOT_IMPLEMENTED_REPLY = "Nie wiem jak to zrobić"


class Tool(Protocol):
    name: str
    description: str

    async def run(self, conversation: AgentExecutionContext, channel: AgentChannel, request: UserMessage) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class BaseTool:
    name: str
    description: str

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._logger = logging.getLogger(f"{self.__module__}.{type(self).__name__}[{self.name}]")

    async def run(self, conversation: AgentExecutionContext, channel: AgentChannel, request: UserMessage) -> None:
        await channel.send_message(TOOL_NOT_IMPLEMENTED_REPLY)

    async def close(self) -> None:
        pass
