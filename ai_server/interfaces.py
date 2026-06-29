from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ai_server.messages import ConversationInputEvent, ConversationOutputEvent, EndpointToSessionEvent
from ai_server.messages import SessionToEndpointEvent, TextMessage
from ai_server.utils.processing import ProcessingUpdateCallback
from ai_server.utils.processing import DEFAULT_PROCESSING_UPDATE_INTERVAL_SECONDS


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


class ConversationMedium(Enum):
    VOICE = "voice"
    TEXT = "text"


@dataclass
class Conversation:
    conversation_id: str
    attributes: dict[str, str]
    state: dict[str, Any] = field(default_factory=dict)
    processing_update_callback: ProcessingUpdateCallback | None = None
    processing_update_interval_seconds: float = DEFAULT_PROCESSING_UPDATE_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        self.medium

    @property
    def user(self) -> str | None:
        return self.attributes.get("user")

    @property
    def area(self) -> str | None:
        return self.attributes.get("area")

    @property
    def medium(self) -> ConversationMedium:
        raw_medium = self.attributes.get("medium")
        try:
            assert raw_medium is not None
            return ConversationMedium(raw_medium)
        except (AssertionError, ValueError) as exc:
            raise AssertionError(f"conversation.medium must be one of: voice, text; got {raw_medium!r}") from exc

    @property
    def user_settings(self) -> dict[str, Any]:
        settings = self.state.get("user_settings")
        return settings if isinstance(settings, dict) else {}


class EndpointClosed(Exception):
    """Raised when a communication endpoint cannot receive more messages."""
