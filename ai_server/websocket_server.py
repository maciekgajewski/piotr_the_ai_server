from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientConnectionResetError, WSCloseCode, WSMsgType, web

from ai_server.agent import Agent
from ai_server.config import Config
from ai_server.interfaces import CommunicationEndpoint, EndpointClosed
from ai_server.messages import ConversationEnded, EndpointToSessionEvent, RequestFollowUp, SessionRejected, SessionToEndpointEvent
from ai_server.messages import endpoint_event_from_json, session_event_to_json
from ai_server.sessions import SessionManager
from ai_server.user_settings import UserSettingsProvider


class WebsocketCommunicationEndpoint(CommunicationEndpoint):
    def __init__(self, websocket: web.WebSocketResponse, peer: str, follow_up_timeout_seconds: float) -> None:
        self._websocket = websocket
        self._follow_up_timeout_seconds = follow_up_timeout_seconds
        self._next_receive_timeout_seconds: float | None = None
        self._logger = logging.getLogger(f"{__name__}.WebsocketCommunicationEndpoint[{peer}]")

    async def receive(self) -> EndpointToSessionEvent:
        try:
            if self._next_receive_timeout_seconds is None:
                message = await self._websocket.receive()
            else:
                timeout_seconds = self._next_receive_timeout_seconds
                self._next_receive_timeout_seconds = None
                message = await asyncio.wait_for(self._websocket.receive(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self._logger.info("follow-up timed out; ending conversation")
            return ConversationEnded()

        if message.type == WSMsgType.TEXT:
            try:
                event = endpoint_event_from_json(message.data)
            except ValueError as exc:
                self._logger.warning("closing websocket after invalid protocol event: %s", exc)
                await _reject_websocket(self._websocket, str(exc))
                raise EndpointClosed() from exc

            self._logger.debug("received websocket event: %s", message.data)
            return event

        if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise EndpointClosed()

        if message.type == WSMsgType.ERROR:
            raise EndpointClosed() from self._websocket.exception()

        raise ValueError(f"unsupported websocket message type: {message.type}")

    async def send(self, event: SessionToEndpointEvent) -> None:
        if isinstance(event, RequestFollowUp):
            self._next_receive_timeout_seconds = self._follow_up_timeout_seconds
            event = RequestFollowUp(timeout_seconds=self._follow_up_timeout_seconds)
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
    user_settings_provider: UserSettingsProvider | None = None,
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
            endpoint = WebsocketCommunicationEndpoint(
                websocket,
                peer,
                follow_up_timeout_seconds=config.websocket.follow_up_timeout_seconds,
            )
            await manager.run_session(
                endpoint,
                require_session_attributes=True,
                user_settings=config.users,
                user_settings_provider=user_settings_provider,
            )
            return websocket
        except AssertionError as exc:
            connection_logger.warning("websocket protocol violation: %s", exc)
            await _reject_websocket(websocket, str(exc))
            return websocket
        finally:
            websockets.discard(websocket)

    async def close_websockets(_app: web.Application) -> None:
        for websocket in set(websockets):
            await websocket.close(
                code=WSCloseCode.GOING_AWAY,
                message=b"server shutdown",
            )

    async def status_handler(_request: web.Request) -> web.Response:
        provider_status = {"mode": "config"}
        if user_settings_provider is not None and hasattr(user_settings_provider, "status"):
            provider_status = user_settings_provider.status()
        return web.json_response(
            {
                "status": "ok",
                "websocket": {
                    "host": config.websocket.host,
                    "port": config.websocket.port,
                    "path": config.websocket.path,
                    "active_connections": len(websockets),
                    "active_sessions": manager.session_count,
                },
                "user_settings": provider_status,
            }
        )

    app.router.add_get(config.websocket.path, websocket_handler)
    app.router.add_get("/api/status", status_handler)
    app.on_shutdown.append(close_websockets)
    app["session_manager"] = manager
    app["websockets"] = websockets
    return app


def _format_peer(request: web.Request) -> str:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"

    return request.remote or "unknown"


async def _reject_websocket(websocket: web.WebSocketResponse, reason: str) -> None:
    await websocket.send_str(session_event_to_json(SessionRejected(reason=reason)))
    await websocket.close(code=WSCloseCode.PROTOCOL_ERROR, message=reason.encode())
