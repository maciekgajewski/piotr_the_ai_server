from __future__ import annotations

import logging

from aiohttp import ClientConnectionResetError, WSCloseCode, WSMsgType, web

from ai_server.agent import Agent
from ai_server.config import Config
from ai_server.interfaces import CommunicationEndpoint, EndpointClosed
from ai_server.messages import EndpointToSessionEvent, SessionToEndpointEvent
from ai_server.messages import endpoint_event_from_json, session_event_to_json
from ai_server.sessions import SessionManager


class WebsocketCommunicationEndpoint(CommunicationEndpoint):
    def __init__(self, websocket: web.WebSocketResponse, peer: str) -> None:
        self._websocket = websocket
        self._logger = logging.getLogger(f"{__name__}.WebsocketCommunicationEndpoint[{peer}]")

    async def receive(self) -> EndpointToSessionEvent:
        message = await self._websocket.receive()

        if message.type == WSMsgType.TEXT:
            try:
                event = endpoint_event_from_json(message.data)
            except ValueError as exc:
                self._logger.warning("closing websocket after invalid protocol event: %s", exc)
                await self._websocket.close(code=WSCloseCode.PROTOCOL_ERROR, message=str(exc).encode())
                raise EndpointClosed() from exc

            self._logger.debug("received websocket event: %s", message.data)
            return event

        if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise EndpointClosed()

        if message.type == WSMsgType.ERROR:
            raise EndpointClosed() from self._websocket.exception()

        raise ValueError(f"unsupported websocket message type: {message.type}")

    async def send(self, event: SessionToEndpointEvent) -> None:
        payload = session_event_to_json(event)
        self._logger.debug("sending websocket event: %s", payload)
        try:
            await self._websocket.send_str(payload)
        except ClientConnectionResetError as exc:
            raise EndpointClosed() from exc


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
            await manager.run_session(endpoint, require_session_attributes=True)
            return websocket
        except AssertionError as exc:
            connection_logger.warning("websocket protocol violation: %s", exc)
            await websocket.close(code=WSCloseCode.PROTOCOL_ERROR, message=str(exc).encode())
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
