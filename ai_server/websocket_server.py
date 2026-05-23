from __future__ import annotations

import logging
from collections import deque

from aiohttp import ClientConnectionResetError, WSCloseCode, WSMsgType, web

from ai_server.agent import Agent
from ai_server.config import Config
from ai_server.interfaces import CommunicationEndpoint, EndpointClosed
from ai_server.messages import MessageBegin, MessageEnd, MessageEvent, MessageFragment, UserMessage
from ai_server.messages import user_message_from_json, user_message_to_json
from ai_server.sessions import SessionManager


class WebsocketCommunicationEndpoint(CommunicationEndpoint):
    def __init__(self, websocket: web.WebSocketResponse, peer: str) -> None:
        self._websocket = websocket
        self._logger = logging.getLogger(f"{__name__}.WebsocketCommunicationEndpoint[{peer}]")
        self._incoming_events: deque[MessageEvent] = deque()
        self._outgoing_text_parts: list[str] = []

    async def receive(self) -> MessageEvent:
        if self._incoming_events:
            return self._incoming_events.popleft()

        message = await self._websocket.receive()

        if message.type == WSMsgType.TEXT:
            self._logger.debug("received websocket message: %s", message.data)
            user_message = user_message_from_json(message.data)
            self._incoming_events.append(MessageFragment(text=user_message.text))
            self._incoming_events.append(MessageEnd())
            return MessageBegin()

        if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise EndpointClosed()

        if message.type == WSMsgType.ERROR:
            raise EndpointClosed() from self._websocket.exception()

        raise ValueError(f"unsupported websocket message type: {message.type}")

    async def send(self, event: MessageEvent) -> None:
        if isinstance(event, MessageBegin):
            self._outgoing_text_parts.clear()
            return
        if isinstance(event, MessageFragment):
            self._outgoing_text_parts.append(event.text)
            return
        if isinstance(event, MessageEnd):
            payload = user_message_to_json(UserMessage(text="".join(self._outgoing_text_parts)))
            self._logger.debug("sending websocket message: %s", payload)
            try:
                await self._websocket.send_str(payload)
            except ClientConnectionResetError as exc:
                raise EndpointClosed() from exc
            finally:
                self._outgoing_text_parts.clear()
            return

        raise ValueError(f"unsupported message event: {type(event).__name__}")


def create_app(
    config: Config,
    agent: Agent,
    session_manager: SessionManager | None = None,
) -> web.Application:
    manager = session_manager or SessionManager(agent)
    app = web.Application()
    websockets: set[web.WebSocketResponse] = set()

    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        websockets.add(websocket)
        peer = _format_peer(request)
        connection_logger = logging.getLogger(f"{__name__}.WebsocketServer[{peer}]")
        connection_logger.info("accepted websocket connection %s", request.path)

        try:
            endpoint = WebsocketCommunicationEndpoint(websocket, peer)
            await manager.run_session(endpoint)
            return websocket
        finally:
            websockets.discard(websocket)

    async def close_websockets(_app: web.Application) -> None:
        for websocket in set(websockets):
            await websocket.close(
                code=WSCloseCode.GOING_AWAY,
                message=b"server shutdown",
            )

    app.router.add_get(config.websocket.path, websocket_handler)
    app.on_shutdown.append(close_websockets)
    app["session_manager"] = manager
    app["websockets"] = websockets
    return app


def _format_peer(request: web.Request) -> str:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"

    return request.remote or "unknown"
