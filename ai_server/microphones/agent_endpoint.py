from __future__ import annotations

import asyncio

from ai_server.interfaces import CommunicationEndpoint, EndpointClosed
from ai_server.messages import MessageEvent, UserMessage, user_message_to_events
from ai_server.streaming import receive_user_message


class MicrophoneAgentEndpoint(CommunicationEndpoint):
    def __init__(self) -> None:
        self._incoming: asyncio.Queue[MessageEvent | None] = asyncio.Queue()
        self._outgoing: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self._closed = False

    async def exchange(self, message: UserMessage) -> UserMessage:
        if self._closed:
            raise EndpointClosed()

        for event in user_message_to_events(message):
            await self._incoming.put(event)
        return await receive_user_message(_QueueEndpoint(self._outgoing))

    async def receive(self) -> MessageEvent:
        event = await self._incoming.get()
        if event is None:
            raise EndpointClosed()
        return event

    async def send(self, event: MessageEvent) -> None:
        if self._closed:
            raise EndpointClosed()
        await self._outgoing.put(event)

    async def send_to_agent(self, event: MessageEvent) -> None:
        if self._closed:
            raise EndpointClosed()
        await self._incoming.put(event)

    async def receive_reply(self) -> MessageEvent:
        if self._closed:
            raise EndpointClosed()
        return await self._outgoing.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._incoming.put_nowait(None)


class _QueueEndpoint(CommunicationEndpoint):
    def __init__(self, queue: asyncio.Queue[MessageEvent]) -> None:
        self._queue = queue

    async def receive(self) -> MessageEvent:
        return await self._queue.get()

    async def send(self, event: MessageEvent) -> None:
        raise NotImplementedError
