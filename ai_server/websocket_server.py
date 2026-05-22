from __future__ import annotations

import logging
from typing import ClassVar

from aiohttp import ClientConnectionResetError, WSMsgType, web

from ai_server.agent import Agent
from ai_server.config import Config
from ai_server.endpoint import CommunicationEndpoint, EndpointClosed
from ai_server.messages import UserMessage, user_message_from_json, user_message_to_json
from ai_server.sessions import SessionManager


class WebsocketCommunicationEndpoint(CommunicationEndpoint):
    _logger: ClassVar[logging.Logger] = logging.getLogger(
        f"{__name__}.WebsocketCommunicationEndpoint"
    )

    def __init__(self, websocket: web.WebSocketResponse, peer: str) -> None:
        self._websocket = websocket
        self._log_prefix = f"WebsocketCommunicationEndpoint[{peer}]"

    async def receive(self) -> UserMessage:
        message = await self._websocket.receive()

        if message.type == WSMsgType.TEXT:
            self._logger.debug("%s Received websocket message: %s", self._log_prefix, message.data)
            return user_message_from_json(message.data)

        if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise EndpointClosed()

        if message.type == WSMsgType.ERROR:
            raise EndpointClosed() from self._websocket.exception()

        raise ValueError(f"unsupported websocket message type: {message.type}")

    async def send(self, msg: UserMessage) -> None:
        payload = user_message_to_json(msg)
        self._logger.debug("%s Sending websocket message: %s", self._log_prefix, payload)
        try:
            await self._websocket.send_str(payload)
        except ClientConnectionResetError as exc:
            raise EndpointClosed() from exc


def create_app(
    config: Config,
    agent: Agent,
    session_manager: SessionManager | None = None,
) -> web.Application:
    logger = logging.getLogger(f"{__name__}.WebsocketServer")
    manager = session_manager or SessionManager(agent)
    app = web.Application()

    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        peer = _format_peer(request)
        logger.info("WebsocketServer[%s] Accepted websocket connection %s", peer, request.path)

        endpoint = WebsocketCommunicationEndpoint(websocket, peer)
        await manager.run_session(endpoint)
        return websocket

    app.router.add_get(config.websocket.path, websocket_handler)
    app["session_manager"] = manager
    return app


def _format_peer(request: web.Request) -> str:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"

    return request.remote or "unknown"
