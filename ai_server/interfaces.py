from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ai_server.messages import ConversationInputEvent, ConversationOutputEvent, EndpointToSessionEvent
from ai_server.messages import SessionToEndpointEvent, TextMessage


class CommunicationEndpoint(ABC):
    @abstractmethod
    async def receive(self) -> EndpointToSessionEvent:
        raise NotImplementedError

    @abstractmethod
    async def send(self, event: SessionToEndpointEvent) -> None:
        raise NotImplementedError


class ConversationEndpoint(ABC):
    @abstractmethod
    async def receive(self) -> ConversationInputEvent:
        raise NotImplementedError

    @abstractmethod
    async def send(self, event: ConversationOutputEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    async def messages(self) -> AsyncIterator[TextMessage]:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, message: TextMessage) -> None:
        raise NotImplementedError


@dataclass
class Conversation:
    conversation_id: str
    attributes: dict[str, str]
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def user(self) -> str | None:
        return self.attributes.get("user")

    @property
    def area(self) -> str | None:
        return self.attributes.get("area")

    @property
    def user_settings(self) -> dict[str, Any]:
        settings = self.state.get("user_settings")
        return settings if isinstance(settings, dict) else {}


class EndpointClosed(Exception):
    """Raised when a communication endpoint cannot receive more messages."""
