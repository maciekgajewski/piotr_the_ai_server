from __future__ import annotations

from abc import ABC, abstractmethod

from ai_server.messages import UserMessage


class CommunicationEndpoint(ABC):
    @abstractmethod
    async def receive(self) -> UserMessage:
        raise NotImplementedError

    @abstractmethod
    async def send(self, msg: UserMessage) -> None:
        raise NotImplementedError


class EndpointClosed(Exception):
    """Raised when a communication endpoint cannot receive more messages."""

