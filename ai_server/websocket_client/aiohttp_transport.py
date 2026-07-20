from __future__ import annotations

import asyncio
from collections import deque

from aiohttp import WSMsgType

from ai_server.websocket_messages import ClientEvent, InvalidEvent, InvalidJson
from ai_server.websocket_messages import client_event_to_json, server_event_from_json


class TransportDisconnected(Exception):
    pass


class AiohttpTransport:
    def __init__(self, websocket) -> None:
        self._websocket = websocket
        self._pending: deque[tuple[str, asyncio.Future]] = deque()
        self._writer_task: asyncio.Task | None = None

    async def receive(self):
        message = await self._websocket.receive()
        if message.type is WSMsgType.TEXT:
            try:
                return server_event_from_json(message.data)
            except (InvalidJson, InvalidEvent) as exc:
                raise ClientProtocolError(str(exc)) from exc
        if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise TransportDisconnected("websocket closed")
        if message.type is WSMsgType.ERROR:
            raise TransportDisconnected("websocket connection failed") from message.data
        raise ClientProtocolError(f"unsupported websocket message type: {message.type}")

    async def send(self, event: ClientEvent) -> None:
        await self._websocket.send_str(client_event_to_json(event))

    def commit_send(self, event: ClientEvent) -> asyncio.Task:
        payload = client_event_to_json(event)
        completion = asyncio.get_running_loop().create_future()
        self._pending.append((payload, completion))
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._drain_committed_frames())
        return completion

    async def _drain_committed_frames(self) -> None:
        while self._pending:
            payload, completion = self._pending.popleft()
            try:
                await self._websocket.send_str(payload)
            except Exception as exc:
                if not completion.done():
                    completion.set_exception(exc)
            else:
                if not completion.done():
                    completion.set_result(None)

    async def close(self) -> None:
        if self._writer_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._writer_task), timeout=1.0)
            except Exception:
                self._writer_task.cancel()
        if not self._websocket.closed:
            try:
                await asyncio.wait_for(self._websocket.close(), timeout=1.0)
            except Exception:
                return None


class ClientProtocolError(Exception):
    pass
