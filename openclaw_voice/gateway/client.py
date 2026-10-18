from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from typing import Any

import aiohttp

from openclaw_voice.gateway.identity import DeviceIdentity
from openclaw_voice.gateway.messages import GatewayCapacityExceeded, GatewayDisconnected, GatewayError
from openclaw_voice.gateway.messages import GatewayEvent, GatewayHello, GatewayProtocolError, GatewayRejected
from openclaw_voice.gateway.messages import GatewayRequestCancelled, GatewayRequestUncertain
from openclaw_voice.gateway.settings import GatewayClientSettings, GatewaySettings


class GatewayClient:
    """One signed connection generation. Never retries or replays a request.

    A fresh instance is required after a disconnect. Callers own reconnect policy
    and must discard unresolved run/session bindings before exposing readiness.
    The single reader routes responses without waiting for event consumers.
    """

    def __init__(
        self, settings: GatewaySettings, client: GatewayClientSettings,
        identity: DeviceIdentity, *, event_names: frozenset[str],
    ) -> None:
        self._settings = settings
        self._client = client
        self._identity = identity
        self._event_names = event_names
        self._http: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[GatewayEvent] = asyncio.Queue(settings.event_queue_capacity)
        self._failed = asyncio.Event()
        self._failure: GatewayError | None = None
        self._hello: GatewayHello | None = None
        self._started = False
        self._last_sequence: int | None = None
        self._send_limit = settings.max_frame_bytes
        self._logger = logging.getLogger(f"{__name__}.GatewayClient[{client.name}:{identity.device_id[:12]}]")

    @property
    def connected(self) -> bool:
        return self._hello is not None and not self._failed.is_set()

    def observes_event(self, name: str) -> bool:
        return name in self._event_names

    @property
    def hello(self) -> GatewayHello:
        if self._hello is None:
            raise GatewayDisconnected("Gateway handshake has not completed")
        return self._hello

    async def connect(self) -> GatewayHello:
        if self._started or self._failed.is_set():
            raise AssertionError("GatewayClient represents one connection generation")
        self._started = True
        self._logger.debug("connecting role=%s", self._client.role)
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(_reject_redirect)
        self._http = aiohttp.ClientSession(trace_configs=[trace])
        try:
            async with asyncio.timeout(self._settings.handshake_timeout_seconds):
                self._websocket = await self._http.ws_connect(
                    self._settings.url,
                    max_msg_size=self._settings.max_frame_bytes,
                    timeout=aiohttp.ClientWSTimeout(ws_close=self._settings.close_timeout_seconds),
                )
                challenge = await self._receive_frame()
                payload = challenge.get("payload")
                if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge" or not isinstance(payload, dict):
                    raise GatewayProtocolError("expected Gateway connect challenge")
                nonce, timestamp = payload.get("nonce"), payload.get("ts")
                if not isinstance(nonce, str) or not nonce or "|" in nonce or type(timestamp) is not int or timestamp < 0:
                    raise GatewayProtocolError("invalid Gateway challenge nonce or timestamp")
                request_id = uuid.uuid4().hex
                await self._send_frame({"type": "req", "id": request_id, "method": "connect", "params": self._connect_params(nonce, timestamp)})
                response = await self._receive_frame()
                if response.get("type") != "res" or response.get("id") != request_id:
                    raise GatewayProtocolError("expected correlated Gateway connect response")
                self._hello = self._parse_hello(_response_payload(response))
            self._reader = asyncio.create_task(self._read(), name=f"gateway-reader-{self._client.name}")
            self._logger.info("connected role=%s version=%s connection=%s", self._client.role, self._hello.version, self._hello.connection_id)
            return self._hello
        except BaseException:
            await self.close()
            raise

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if not self.connected:
            raise self._failure or GatewayDisconnected("Gateway is not connected")
        if method not in self.hello.methods:
            raise GatewayProtocolError("Gateway did not advertise the requested method")
        if len(self._pending) >= self._settings.max_pending_requests:
            raise GatewayCapacityExceeded("Gateway pending request limit reached")
        request_id = uuid.uuid4().hex
        frame = _encode_frame({"type": "req", "id": request_id, "method": method, "params": params}, self._send_limit)
        response = asyncio.get_running_loop().create_future()
        self._pending[request_id] = response
        self._logger.debug("sending request method=%s id=%s", method, request_id)
        try:
            async with asyncio.timeout(self._settings.request_timeout_seconds):
                assert self._websocket is not None
                # Once send begins, even a cancelled/failed await can be uncertain.
                await self._websocket.send_str(frame)
                return await response
        except asyncio.CancelledError as exc:
            raise GatewayRequestCancelled(request_id) from exc
        except (TimeoutError, aiohttp.ClientError, ConnectionError) as exc:
            raise GatewayRequestUncertain("Gateway request may have been accepted; do not replay") from exc
        finally:
            self._pending.pop(request_id, None)
            if not response.done():
                response.cancel()
            elif not response.cancelled():
                response.exception()  # Observe a concurrent close even if send failed.

    async def receive_event(self) -> GatewayEvent:
        if self._failed.is_set():
            raise self._failure or GatewayDisconnected("Gateway closed")
        event = asyncio.create_task(self._events.get())
        failure = asyncio.create_task(self._failed.wait())
        try:
            await asyncio.wait({event, failure}, return_when=asyncio.FIRST_COMPLETED)
            if self._failed.is_set():
                raise self._failure or GatewayDisconnected("Gateway closed")
            return event.result()
        finally:
            for task in (event, failure):
                task.cancel()
            await asyncio.gather(event, failure, return_exceptions=True)

    async def close(self) -> None:
        self._fail(GatewayDisconnected("Gateway connection closed"))
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._websocket is not None:
            await self._websocket.close()
        if self._http is not None:
            await self._http.close()
        self._logger.debug("connection closed; pending work released")

    def _connect_params(self, nonce: str, timestamp: int) -> dict[str, Any]:
        client = self._client
        mode = "node" if client.role == "node" else "backend"
        client_id = "node-host" if client.role == "node" else "gateway-client"
        auth = {key: value for key, value in (("token", client.token), ("deviceToken", client.device_token)) if value is not None}
        params: dict[str, Any] = {
            "minProtocol": 4, "maxProtocol": 4,
            "client": {"id": client_id, "displayName": client.name, "version": "t011-1", "platform": client.platform, "deviceFamily": client.device_family, "mode": mode},
            "role": client.role, "scopes": list(client.scopes),
            "caps": list(client.capabilities),
            "device": self._identity.sign_connect(
                client_id=client_id, mode=mode, role=client.role, scopes=client.scopes,
                token=client.token or client.device_token, nonce=nonce, timestamp_ms=timestamp,
                platform=client.platform, device_family=client.device_family,
            ),
        }
        if client.role == "node":
            params["commands"] = list(client.commands)
        if auth:
            params["auth"] = auth
        return params

    def _parse_hello(self, raw: Any) -> GatewayHello:
        if not isinstance(raw, dict) or raw.get("type") != "hello-ok" or raw.get("protocol") != 4:
            raise GatewayProtocolError("Gateway did not negotiate protocol 4")
        server, features, auth, policy = (raw.get(key) for key in ("server", "features", "auth", "policy"))
        if not all(isinstance(value, dict) for value in (server, features, auth, policy)):
            raise GatewayProtocolError("invalid Gateway hello metadata")
        if server.get("version") != self._settings.expected_version:
            raise GatewayProtocolError("Gateway version differs from the configured verified version")
        connection_id = server.get("connId")
        if not isinstance(connection_id, str) or not connection_id:
            raise GatewayProtocolError("Gateway connection ID is missing")
        if auth.get("role") != self._client.role:
            raise GatewayProtocolError("Gateway granted a different role")
        scopes = _string_set(auth.get("scopes"))
        if not set(self._client.scopes).issubset(scopes):
            raise GatewayProtocolError("Gateway did not grant the requested scopes")
        for name in ("maxPayload", "maxBufferedBytes", "tickIntervalMs"):
            if type(policy.get(name)) is not int or policy[name] <= 0:
                raise GatewayProtocolError("invalid Gateway connection policy")
        if self._settings.idle_timeout_seconds * 1000 <= policy["tickIntervalMs"]:
            raise GatewayProtocolError("Gateway idle timeout must exceed the advertised tick interval")
        self._send_limit = min(self._send_limit, policy["maxPayload"], policy["maxBufferedBytes"])
        device_token = auth.get("deviceToken")
        if device_token is not None and (not isinstance(device_token, str) or not device_token):
            raise GatewayProtocolError("invalid Gateway device token receipt")
        return GatewayHello(server["version"], connection_id, self._client.role, _string_set(features.get("methods")), _string_set(features.get("events")), scopes, device_token)

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        assert self._websocket is not None
        await self._websocket.send_str(_encode_frame(frame, self._send_limit))

    async def _receive_frame(self) -> dict[str, Any]:
        assert self._websocket is not None
        async with asyncio.timeout(self._settings.idle_timeout_seconds):
            message = await self._websocket.receive()
        if message.type is not aiohttp.WSMsgType.TEXT:
            raise GatewayDisconnected("Gateway closed or sent a non-text frame")
        try:
            value = json.loads(message.data, parse_constant=_invalid_json_constant)
        except (ValueError, RecursionError) as exc:
            raise GatewayProtocolError("Gateway sent invalid JSON") from exc
        if not isinstance(value, dict):
            raise GatewayProtocolError("Gateway frame must be an object")
        return value

    async def _read(self) -> None:
        try:
            while True:
                frame = await self._receive_frame()
                if frame.get("type") == "res":
                    request_id = frame.get("id")
                    if not isinstance(request_id, str) or not request_id:
                        raise GatewayProtocolError("invalid Gateway response ID")
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        self._logger.debug("discarding late or unrelated RPC response")
                        continue
                    try:
                        result = _response_payload(frame)
                    except GatewayRejected as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(result)
                elif frame.get("type") == "event":
                    name, sequence = frame.get("event"), frame.get("seq")
                    if not isinstance(name, str) or not name:
                        raise GatewayProtocolError("invalid Gateway event name")
                    if sequence is not None:
                        if type(sequence) is not int or sequence < 0:
                            raise GatewayProtocolError("invalid Gateway event sequence")
                        if self._last_sequence is not None and sequence != self._last_sequence + 1:
                            raise GatewayProtocolError("Gateway event sequence gap or replay")
                        self._last_sequence = sequence
                    if name == "connect.challenge":
                        raise GatewayProtocolError("unexpected challenge after Gateway handshake")
                    if name in self._event_names:
                        self._events.put_nowait(GatewayEvent(name, frame.get("payload"), sequence))
                else:
                    raise GatewayProtocolError("unsupported Gateway frame type")
        except asyncio.CancelledError:
            raise
        except (GatewayError, TimeoutError, aiohttp.ClientError, ConnectionError, asyncio.QueueFull) as exc:
            if isinstance(exc, GatewayError):
                failure = exc
            elif isinstance(exc, asyncio.QueueFull):
                failure = GatewayCapacityExceeded("Gateway event queue overflow")
            else:
                failure = GatewayDisconnected("Gateway receive failed or timed out")
            self._logger.warning("connection failed type=%s", type(failure).__name__)
            self._fail(failure)
            assert self._websocket is not None
            await self._websocket.close()

    def _fail(self, failure: GatewayError) -> None:
        if self._failed.is_set():
            return
        self._failure = failure
        self._failed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(GatewayRequestUncertain("Gateway disconnected after dispatch; do not replay"))
        while not self._events.empty():
            self._events.get_nowait()


def _response_payload(frame: dict[str, Any]) -> Any:
    if type(frame.get("ok")) is not bool:
        raise GatewayProtocolError("Gateway response requires a boolean ok field")
    if frame["ok"]:
        return frame.get("payload")
    error = frame.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise GatewayProtocolError("Gateway failure requires an error code")
    code = error["code"]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code):
        raise GatewayProtocolError("Gateway error code is invalid")
    raise GatewayRejected(code, error.get("details"))


def _encode_frame(frame: dict[str, Any], limit: int) -> str:
    value = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(value.encode("utf-8")) > limit:
        raise GatewayCapacityExceeded("Gateway frame exceeds the configured or negotiated limit")
    return value


def _string_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise GatewayProtocolError("Gateway hello requires arrays of non-empty strings")
    return frozenset(value)


def _invalid_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


async def _reject_redirect(
    session: aiohttp.ClientSession, context: Any, params: aiohttp.TraceRequestRedirectParams,
) -> None:
    # ws_connect follows HTTP redirects by default and exposes no redirect flag.
    # Never send the signed credentials to an endpoint selected by a redirect.
    raise GatewayProtocolError("Gateway WebSocket redirects are not supported")
