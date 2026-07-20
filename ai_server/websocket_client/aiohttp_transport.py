from __future__ import annotations

from aiohttp import WSMsgType

from ai_server.websocket_messages import ClientEvent, InvalidEvent, InvalidJson
from ai_server.websocket_messages import client_event_to_json, server_event_from_json


class TransportDisconnected(Exception):
    pass


class AiohttpTransport:
    def __init__(self, websocket) -> None:
        self._websocket = websocket

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

    async def close(self) -> None:
        if not self._websocket.closed:
            await self._websocket.close()


class ClientProtocolError(Exception):
    pass
