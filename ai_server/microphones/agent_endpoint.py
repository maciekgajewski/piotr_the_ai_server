from __future__ import annotations

import asyncio

from ai_server.endpoint import CommunicationEndpoint, EndpointClosed
from ai_server.messages import UserMessage


class MicrophoneAgentEndpoint(CommunicationEndpoint):
    def __init__(self) -> None:
        self._incoming: asyncio.Queue[UserMessage | None] = asyncio.Queue()
        self._outgoing: asyncio.Queue[UserMessage] = asyncio.Queue()
        self._closed = False

    async def exchange(self, message: UserMessage) -> UserMessage:
        if self._closed:
            raise EndpointClosed()

        await self._incoming.put(message)
        return await self._outgoing.get()

    async def receive(self) -> UserMessage:
        message = await self._incoming.get()
        if message is None:
            raise EndpointClosed()
        return message

    async def send(self, msg: UserMessage) -> None:
        if self._closed:
            raise EndpointClosed()
        await self._outgoing.put(msg)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._incoming.put_nowait(None)
