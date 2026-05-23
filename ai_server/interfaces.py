from __future__ import annotations

from abc import ABC, abstractmethod

from ai_server.messages import MessageEvent


class CommunicationEndpoint(ABC):
    @abstractmethod
    async def receive(self) -> MessageEvent:
        raise NotImplementedError

    @abstractmethod
    async def send(self, event: MessageEvent) -> None:
        raise NotImplementedError


class EndpointClosed(Exception):
    """Raised when a communication endpoint cannot receive more messages."""
