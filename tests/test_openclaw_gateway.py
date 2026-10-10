from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from openclaw_voice.gateway.client import GatewayClient
from openclaw_voice.gateway.identity import DeviceIdentity
from openclaw_voice.gateway.messages import GatewayCapacityExceeded, GatewayDisconnected
from openclaw_voice.gateway.messages import GatewayProtocolError, GatewayRejected, GatewayRequestCancelled
from openclaw_voice.gateway.messages import GatewayRequestUncertain
from openclaw_voice.gateway.settings import GatewayClientSettings, GatewaySettings


def _settings(url: str, **overrides) -> GatewaySettings:
    return dataclasses.replace(GatewaySettings(
        url=url, expected_version="2026.9.8", handshake_timeout_seconds=1,
        request_timeout_seconds=0.5, idle_timeout_seconds=2, close_timeout_seconds=0.5,
        max_frame_bytes=8192, max_pending_requests=2, event_queue_capacity=2,
    ), **overrides)


def _client_settings(role="operator") -> GatewayClientSettings:
    return GatewayClientSettings(
        name="test-satellite", role=role, platform="linux", device_family="voice-satellite",
        scopes=("operator.read", "operator.write") if role == "operator" else (),
        commands=("system.notify",) if role == "node" else (), token="test-secret",
    )


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _verify_connect(params: dict, nonce: str) -> None:
    device, client = params["device"], params["client"]
    assert params["minProtocol"] == params["maxProtocol"] == 4
    assert device["nonce"] == nonce and device["signedAt"] == 1780000000000
    assert hashlib.sha256(_decode(device["publicKey"])).hexdigest() == device["id"]
    signed = "|".join((
        "v3", device["id"], client["id"], client["mode"], params["role"],
        ",".join(params["scopes"]), str(device["signedAt"]), "test-secret", nonce,
        client["platform"], client["deviceFamily"],
    ))
    Ed25519PublicKey.from_public_bytes(_decode(device["publicKey"])).verify(
        _decode(device["signature"]), signed.encode(),
    )


@contextlib.asynccontextmanager
async def _server(handler, *, hello_override=None, challenge_override=None):
    captures = []
    sockets = []

    async def gateway(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        nonce = "server-challenge"
        await ws.send_json(challenge_override or {"type": "event", "event": "connect.challenge", "payload": {"nonce": nonce, "ts": 1780000000000}})
        raw = await ws.receive()
        if raw.type is not web.WSMsgType.TEXT:
            return ws
        frame = json.loads(raw.data)
        captures.append(frame)
        _verify_connect(frame["params"], nonce)
        hello = {
            "type": "hello-ok", "protocol": 4,
            "server": {"version": "2026.9.8", "connId": "test-connection"},
            "features": {"methods": ["chat.send", "chat.abort", "node.invoke.result"], "events": ["chat", "node.invoke.request", "tick"]},
            "auth": {"role": frame["params"]["role"], "scopes": frame["params"]["scopes"], "deviceToken": "returned-secret"},
            "policy": {"maxPayload": 8192, "maxBufferedBytes": 8192, "tickIntervalMs": 100},
        }
        if hello_override:
            hello_override(hello)
        await ws.send_json({"type": "res", "id": frame["id"], "ok": True, "payload": hello})
        await handler(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", gateway)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"ws://127.0.0.1:{runner.addresses[0][1]}/", captures
    finally:
        await asyncio.gather(*(socket.close() for socket in sockets))
        await runner.cleanup()


def _client(url, tmp_path, *, role="operator", **settings):
    return GatewayClient(
        _settings(url, **settings), _client_settings(role),
        DeviceIdentity.load_or_create(tmp_path / f"{role}.pem"),
        event_names=frozenset({"chat", "node.invoke.request"}),
    )


async def _wait_for_close(ws):
    async for _message in ws:
        pass


def test_signature_matches_installed_gateway_v3_fixture():
    fixture = json.loads((Path(__file__).parent / "fixtures/openclaw-2026.9.8-device-auth.json").read_text())
    params = fixture["params"]
    identity = DeviceIdentity(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(fixture["publicTestSeedHex"])))
    signed = identity.sign_connect(
        client_id=params["clientId"], mode=params["clientMode"], role=params["role"],
        scopes=tuple(params["scopes"]), token=params["token"], nonce=params["nonce"],
        timestamp_ms=params["signedAtMs"], platform=params["platform"], device_family=params["deviceFamily"],
    )
    assert signed["id"] == params["deviceId"]
    assert signed["publicKey"] == fixture["publicKey"]
    assert signed["signature"] == fixture["signature"]


@pytest.mark.skipif(not os.environ.get("OPENCLAW_TEST_DIST"), reason="optional installed Gateway schema/crypto probe")
def test_installed_gateway_accepts_python_connect_envelopes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    frames = [
        {"type": "req", "id": role, "method": "connect", "params": _client("ws://127.0.0.1/", tmp_path, role=role)._connect_params("fixture-nonce", 1780000000000)}
        for role in ("node", "operator")
    ]
    result = subprocess.run(
        ["node", str(root / "tools/lib/t011_gateway_contract_probe.mjs"), os.environ["OPENCLAW_TEST_DIST"]],
        input=json.dumps(frames), text=True, capture_output=True, timeout=10, check=True,
    )
    assert json.loads(result.stdout) == {"version": "2026.9.8", "protocol": 4, "checked": 2}


def test_identities_are_durable_distinct_and_private(tmp_path):
    path = tmp_path / "one.pem"
    first = DeviceIdentity.load_or_create(path)
    assert DeviceIdentity.load_or_create(path).device_id == first.device_id
    assert DeviceIdentity.load_or_create(tmp_path / "two.pem").device_id != first.device_id
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        DeviceIdentity.load_or_create(path)
    path.chmod(0o600)
    path.write_text("broken key")
    with pytest.raises(ValueError):
        DeviceIdentity.load_or_create(path)
    assert path.read_text() == "broken key"


def test_identity_rejects_symlink_and_shared_writable_directory(tmp_path):
    path = tmp_path / "link.pem"
    path.symlink_to(tmp_path / "absent.pem")
    with pytest.raises(OSError):
        DeviceIdentity.load_or_create(path)
    tmp_path.chmod(0o777)
    with pytest.raises(ValueError, match="directory"):
        DeviceIdentity.load_or_create(tmp_path / "new.pem")


@pytest.mark.parametrize("role", ["node", "operator"])
def test_signed_connection_and_role_declarations(tmp_path, role):
    async def run():
        async with _server(_wait_for_close) as (url, captures):
            client = _client(url, tmp_path, role=role)
            try:
                hello = await client.connect()
                assert client.connected
                assert hello.version == "2026.9.8"
                assert "returned-secret" not in repr(hello)
                params = captures[0]["params"]
                assert params["role"] == role
                assert params["scopes"] == ([] if role == "node" else ["operator.read", "operator.write"])
                assert params.get("commands") == (["system.notify"] if role == "node" else None)
            finally:
                await client.close()
            assert not client.connected
            with pytest.raises(AssertionError):
                await client.connect()
    asyncio.run(run())


def test_responses_can_arrive_out_of_order_among_events(tmp_path):
    async def handler(ws):
        first, second = await ws.receive_json(), await ws.receive_json()
        await ws.send_json({"type": "event", "event": "tick", "seq": 1, "payload": {}})
        await ws.send_json({"type": "event", "event": "chat", "seq": 2, "payload": {"runId": "run-1", "state": "final"}})
        for request in (second, first):
            await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": request["params"]})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            client = _client(url, tmp_path)
            try:
                await client.connect()
                replies = await asyncio.gather(client.request("chat.send", {"message": "one"}), client.request("chat.send", {"message": "two"}))
                assert replies == [{"message": "one"}, {"message": "two"}]
                event = await client.receive_event()
                assert event.name == "chat" and event.sequence == 2
                assert event.payload["runId"] == "run-1"
            finally:
                await client.close()
    asyncio.run(run())


def test_timeout_does_not_resend_or_confuse_late_reply(tmp_path):
    requests = []

    async def handler(ws):
        first = await ws.receive_json()
        requests.append(first)
        await asyncio.sleep(0.12)
        await ws.send_json({"type": "res", "id": first["id"], "ok": True, "payload": "late"})
        second = await ws.receive_json()
        requests.append(second)
        await ws.send_json({"type": "res", "id": second["id"], "ok": True, "payload": "second"})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            client = _client(url, tmp_path, request_timeout_seconds=0.1)
            try:
                await client.connect()
                with pytest.raises(GatewayRequestUncertain):
                    await client.request("chat.send", {"message": "first"})
                assert await client.request("chat.send", {"message": "second"}) == "second"
                assert len(requests) == 2 and requests[0]["id"] != requests[1]["id"]
            finally:
                await client.close()
    asyncio.run(run())


def test_disconnect_releases_requests_as_uncertain_and_drops_queued_events(tmp_path):
    async def handler(ws):
        await ws.receive_json()
        await ws.send_json({"type": "event", "event": "chat", "payload": {"state": "final"}})
        await ws.close()

    async def run():
        async with _server(handler) as (url, _captures):
            client = _client(url, tmp_path)
            try:
                await client.connect()
                with pytest.raises(GatewayRequestUncertain):
                    await client.request("chat.send", {})
                with pytest.raises(GatewayDisconnected):
                    await client.receive_event()
            finally:
                await client.close()
    asyncio.run(run())


def test_rpc_rejection_is_typed_and_does_not_expose_remote_message(tmp_path):
    async def handler(ws):
        request = await ws.receive_json()
        await ws.send_json({"type": "res", "id": request["id"], "ok": False, "error": {"code": "UNAVAILABLE", "message": "secret transcript"}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            client = _client(url, tmp_path)
            try:
                await client.connect()
                with pytest.raises(GatewayRejected) as rejected:
                    await client.request("chat.send", {})
                assert rejected.value.code == "UNAVAILABLE"
                assert "secret transcript" not in str(rejected.value)
                assert client.connected
            finally:
                await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["queue", "sequence", "json", "binary"])
def test_invalid_or_overflowing_event_stream_fails_connection(tmp_path, fault):
    async def handler(ws):
        await ws.receive_json()
        if fault == "json":
            await ws.send_str('{"type":"event","event":"chat","payload":NaN}')
        elif fault == "binary":
            await ws.send_bytes(b"wrong frame type")
        else:
            for sequence in ([1, 3] if fault == "sequence" else [1, 2, 3]):
                await ws.send_json({"type": "event", "event": "chat", "seq": sequence, "payload": {}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            client = _client(url, tmp_path)
            try:
                await client.connect()
                with pytest.raises(GatewayRequestUncertain):
                    await client.request("chat.send", {})
                assert not client.connected
            finally:
                await client.close()
    asyncio.run(run())


def test_limits_reject_before_send_and_cancelled_request_is_not_replayed(tmp_path):
    received = asyncio.Event()
    requests = []

    async def handler(ws):
        async for raw in ws:
            requests.append(json.loads(raw.data))
            received.set()

    async def run():
        async with _server(handler) as (url, _captures):
            client = _client(url, tmp_path, max_pending_requests=1)
            try:
                await client.connect()
                with pytest.raises(GatewayCapacityExceeded):
                    await client.request("chat.send", {"message": "x" * 9000})
                with pytest.raises(GatewayProtocolError):
                    await client.request("unadvertised", {})
                pending = asyncio.create_task(client.request("chat.send", {}))
                await asyncio.wait_for(received.wait(), 1)
                with pytest.raises(GatewayCapacityExceeded):
                    await client.request("chat.send", {})
                pending.cancel()
                with pytest.raises(GatewayRequestCancelled) as cancelled:
                    await pending
                assert cancelled.value.may_have_been_accepted
                assert cancelled.value.request_id == requests[0]["id"]
                assert len(requests) == 1
                assert client.connected
            finally:
                await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["version", "role", "scope", "tick"])
def test_incompatible_hello_is_rejected_before_readiness(tmp_path, fault):
    def change(hello):
        if fault == "version":
            hello["server"]["version"] = "other"
        elif fault == "role":
            hello["auth"]["role"] = "node"
        elif fault == "scope":
            hello["auth"]["scopes"] = []
        else:
            hello["policy"]["tickIntervalMs"] = 30000

    async def run():
        async with _server(_wait_for_close, hello_override=change) as (url, _captures):
            client = _client(url, tmp_path)
            with pytest.raises(GatewayProtocolError):
                await client.connect()
            assert not client.connected
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("payload", [{"nonce": "", "ts": 1}, {"nonce": "n"}, {"nonce": "n", "ts": True}])
def test_invalid_challenge_never_sends_credentials(tmp_path, payload):
    async def run():
        challenge = {"type": "event", "event": "connect.challenge", "payload": payload}
        async with _server(_wait_for_close, challenge_override=challenge) as (url, captures):
            client = _client(url, tmp_path)
            with pytest.raises(GatewayProtocolError):
                await client.connect()
            assert captures == []
            assert not client.connected
    asyncio.run(run())


def test_close_before_connect_keeps_the_generation_closed(tmp_path):
    async def run():
        client = _client("ws://127.0.0.1/", tmp_path)
        await client.close()
        with pytest.raises(AssertionError):
            await client.connect()
    asyncio.run(run())


def test_idle_timeout_releases_event_receiver(tmp_path):
    async def run():
        async with _server(_wait_for_close) as (url, _captures):
            client = _client(url, tmp_path, idle_timeout_seconds=0.2)
            try:
                await client.connect()
                with pytest.raises(GatewayDisconnected):
                    await asyncio.wait_for(client.receive_event(), 1)
            finally:
                await client.close()
    asyncio.run(run())


def test_redirect_is_rejected_before_sending_credentials(tmp_path):
    async def run():
        async with _server(_wait_for_close) as (destination, captures):
            async def redirect(request):
                raise web.HTTPFound(destination.replace("ws://", "http://"))

            app = web.Application()
            app.router.add_get("/", redirect)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            client = _client(f"ws://127.0.0.1:{runner.addresses[0][1]}/", tmp_path)
            try:
                with pytest.raises(GatewayProtocolError, match="redirect"):
                    await client.connect()
                assert captures == []
            finally:
                await client.close()
                await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("max_frame_bytes", True), ("max_pending_requests", 0), ("idle_timeout_seconds", float("inf")), ("request_timeout_seconds", 0), ("url", "ws://192.168.1.3/"), ("url", "wss://user:secret@example.org/"), ("url", "wss://example.org/?token=secret")])
def test_invalid_connection_configuration_is_rejected(field, value):
    with pytest.raises(ValueError):
        dataclasses.replace(_settings("ws://127.0.0.1/"), **{field: value})
