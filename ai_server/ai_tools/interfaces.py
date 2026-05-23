from __future__ import annotations

from typing import Protocol

from ai_server.config import AgentConfig
from ai_server.interfaces import CommunicationEndpoint
from ai_server.messages import UserMessage
from ai_server.ollama import OllamaClient
from ai_server.streaming import send_user_message


TOOL_NOT_IMPLEMENTED_REPLY = "Nie wiem jak to zrobić"


class Tool(Protocol):
    name: str
    description: str

    async def run(self, endpoint: CommunicationEndpoint, request: UserMessage) -> None:
        raise NotImplementedError


class BaseTool:
    name: str
    description: str

    def __init__(self, config: AgentConfig, ollama_client: OllamaClient) -> None:
        self._config = config
        self._ollama = ollama_client

    async def run(self, endpoint: CommunicationEndpoint, request: UserMessage) -> None:
        await send_user_message(endpoint, UserMessage(text=TOOL_NOT_IMPLEMENTED_REPLY))
