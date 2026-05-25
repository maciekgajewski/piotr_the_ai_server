from __future__ import annotations

import asyncio

from ai_server.interfaces import CommunicationEndpoint, EndpointClosed
from ai_server.messages import EndpointToSessionEvent, SessionToEndpointEvent


class MicrophoneAgentEndpoint(CommunicationEndpoint):
    def __init__(self) -> None:
        self._incoming: asyncio.Queue[EndpointToSessionEvent | None] = asyncio.Queue()
        self._outgoing: asyncio.Queue[SessionToEndpointEvent] = asyncio.Queue()
        self._closed = False

    async def receive(self) -> EndpointToSessionEvent:
        event = await self._incoming.get()
        if event is None:
            raise EndpointClosed()
        return event

    async def send(self, event: SessionToEndpointEvent) -> None:
        if self._closed:
            raise EndpointClosed()
        await self._outgoing.put(event)

    async def send_to_session(self, event: EndpointToSessionEvent) -> None:
        if self._closed:
            raise EndpointClosed()
        await self._incoming.put(event)

    async def receive_from_session(self) -> SessionToEndpointEvent:
        if self._closed:
            raise EndpointClosed()
        return await self._outgoing.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._incoming.put_nowait(None)
