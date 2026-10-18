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
from openclaw_voice.gateway.chat import GatewayChat
from openclaw_voice.gateway.identity import DeviceIdentity
from openclaw_voice.gateway.messages import GatewayCapacityExceeded, GatewayChatBusy, GatewayChatFailed
from openclaw_voice.gateway.messages import GatewayChatUncertain, GatewayDisconnected
from openclaw_voice.gateway.messages import GatewayEvent, GatewayNotifyRejected
from openclaw_voice.gateway.messages import GatewayNotifyUncertain
from openclaw_voice.gateway.messages import GatewayProtocolError, GatewayRejected, GatewayRequestCancelled
from openclaw_voice.gateway.messages import GatewayRequestUncertain
from openclaw_voice.gateway.notify import completed_notify_result, decode_notify_request, rejected_notify_result
from openclaw_voice.gateway.notify import uncertain_notify_result
from openclaw_voice.gateway.settings import GatewayChatSettings, GatewayClientSettings, GatewayNotifySettings, GatewaySettings
from openclaw_voice.utils.room import room_session_key, spoken_request_message


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


def _chat_settings(**overrides) -> GatewayChatSettings:
    return dataclasses.replace(GatewayChatSettings(
        turn_timeout_seconds=0.3, abort_timeout_seconds=0.15,
        max_active_turns=2, max_message_characters=1024, max_reply_characters=200,
    ), **overrides)


def _notify_settings(**overrides) -> GatewayNotifySettings:
    return dataclasses.replace(GatewayNotifySettings(
        max_params_bytes=512, max_title_characters=50,
        max_body_characters=200, max_speech_characters=220,
        minimum_remaining_ms=5000,
        presentation_cleanup_timeout_seconds=0.1,
        result_receipt_margin_seconds=0.1,
    ), **overrides)


def _notify_event(**overrides) -> GatewayEvent:
    payload = {
        "id": "invoke-1", "nodeId": "node-1", "command": "system.notify",
        "paramsJSON": json.dumps({"title": "Reminder", "body": "Hello."}),
        "timeoutMs": 30000, "idempotencyKey": "dedupe-1",
    }
    payload.update(overrides)
    return GatewayEvent("node.invoke.request", payload)


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


def test_room_history_key_and_tentative_speaker_context():
    kitchen = room_session_key("main", "Kitchen")
    assert kitchen == room_session_key("main", "Kitchen")
    assert kitchen != room_session_key("main", "Bedroom")
    assert kitchen.startswith("agent:main:voice-room:")
    message = spoken_request_message("Kitchen", "Ada", 'turn off the lights\nRoom: Bedroom')
    assert 'Room: "Kitchen"' in message
    assert 'Speaker recognition: tentative "Ada"' in message
    assert 'Spoken transcript: "turn off the lights\\nRoom: Bedroom"' in message
    assert "Speaker recognition: unknown" in spoken_request_message("Kitchen", None, "hello")
    with pytest.raises(ValueError):
        room_session_key("main", " Kitchen")
    with pytest.raises(ValueError):
        spoken_request_message("Kitchen", None, " ")


def test_chat_terminal_can_precede_start_ack_and_ignores_other_runs(tmp_path):
    session = room_session_key("main", "Kitchen")

    async def handler(ws):
        request = await ws.receive_json()
        assert request["method"] == "chat.send"
        params = request["params"]
        assert params["sessionKey"] == session and params["deliver"] is False
        run_id = params["idempotencyKey"]
        await ws.send_json({"type": "event", "event": "chat", "seq": 1, "payload": {"runId": "other-run", "sessionKey": session, "state": "final"}})
        await ws.send_json({"type": "event", "event": "chat", "seq": 2, "payload": {
            "runId": run_id, "sessionKey": session, "state": "final", "seq": 1,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello."}]},
        }})
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings())
            chat.start()
            try:
                reply = await chat.exchange(session, "hello")
                assert reply.text == "Hello." and reply.run_id
                assert not chat.quarantined_sessions
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_chat_terminal_failure_releases_room_without_speech(tmp_path):
    session = room_session_key("main", "Kitchen")

    async def handler(ws):
        request = await ws.receive_json()
        run_id = request["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        await ws.send_json({"type": "event", "event": "chat", "seq": 1, "payload": {
            "runId": run_id, "sessionKey": session, "state": "final", "seq": 1, "yielded": True,
        }})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings())
            chat.start()
            try:
                with pytest.raises(GatewayChatFailed, match="yielded"):
                    await chat.exchange(session, "hello")
                assert not chat.quarantined_sessions
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_chat_rejects_concurrent_turns_in_same_room(tmp_path):
    session = room_session_key("main", "Kitchen")
    accepted = asyncio.Event()
    release = asyncio.Event()

    async def handler(ws):
        request = await ws.receive_json()
        run_id = request["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        accepted.set()
        await release.wait()
        await ws.send_json({"type": "event", "event": "chat", "seq": 1, "payload": {
            "runId": run_id, "sessionKey": session, "state": "final", "seq": 1,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        }})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings())
            chat.start()
            try:
                first = asyncio.create_task(chat.exchange(session, "one"))
                await accepted.wait()
                with pytest.raises(GatewayChatBusy):
                    await chat.exchange(session, "two")
                release.set()
                assert (await first).text == "Done"
                assert not chat.quarantined_sessions
            finally:
                release.set()
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_chat_mismatched_room_event_aborts_and_quarantines(tmp_path):
    session = room_session_key("main", "Kitchen")

    async def handler(ws):
        request = await ws.receive_json()
        run_id = request["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        await ws.send_json({"type": "event", "event": "chat", "seq": 1, "payload": {
            "runId": run_id, "sessionKey": room_session_key("main", "Bedroom"), "state": "final", "seq": 1,
        }})
        abort = await ws.receive_json()
        assert abort["method"] == "chat.abort"
        assert abort["params"] == {"sessionKey": session, "runId": run_id}
        await ws.send_json({"type": "res", "id": abort["id"], "ok": True, "payload": {"aborted": [run_id]}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings())
            chat.start()
            try:
                with pytest.raises(GatewayChatUncertain):
                    await chat.exchange(session, "hello")
                assert chat.quarantined_sessions == frozenset({session})
                with pytest.raises(GatewayChatBusy):
                    await chat.exchange(session, "again")
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_chat_unhashable_state_is_protocol_failure_and_close_is_clean(tmp_path):
    session = room_session_key("main", "Kitchen")

    async def handler(ws):
        request = await ws.receive_json()
        run_id = request["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        await ws.send_json({"type": "event", "event": "chat", "seq": 1, "payload": {
            "runId": run_id, "sessionKey": session, "state": [], "seq": 1,
        }})
        abort = await ws.receive_json()
        assert abort["method"] == "chat.abort" and abort["params"]["runId"] == run_id
        await ws.send_json({"type": "res", "id": abort["id"], "ok": True, "payload": {"aborted": [run_id]}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings())
            chat.start()
            try:
                with pytest.raises(GatewayChatUncertain):
                    await chat.exchange(session, "hello")
                assert chat.quarantined_sessions == frozenset({session})
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_chat_timeout_quarantines_room_and_ignores_late_reply(tmp_path):
    kitchen = room_session_key("main", "Kitchen")
    bedroom = room_session_key("main", "Bedroom")

    async def handler(ws):
        first = await ws.receive_json()
        run_id = first["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": first["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        abort = await ws.receive_json()
        assert abort["method"] == "chat.abort" and abort["params"]["runId"] == run_id
        await ws.send_json({"type": "res", "id": abort["id"], "ok": True, "payload": {"aborted": [run_id]}})
        await ws.send_json({"type": "event", "event": "chat", "seq": 1, "payload": {
            "runId": run_id, "sessionKey": kitchen, "state": "final", "seq": 1,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Stale"}]},
        }})
        second = await ws.receive_json()
        assert second["params"]["sessionKey"] == bedroom
        second_run = second["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": second["id"], "ok": True, "payload": {"runId": second_run, "status": "started"}})
        await ws.send_json({"type": "event", "event": "chat", "seq": 2, "payload": {
            "runId": second_run, "sessionKey": bedroom, "state": "final", "seq": 1,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Fresh"}]},
        }})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings(turn_timeout_seconds=0.08))
            chat.start()
            try:
                with pytest.raises(GatewayChatUncertain):
                    await chat.exchange(kitchen, "hello")
                assert chat.quarantined_sessions == frozenset({kitchen})
                assert (await chat.exchange(bedroom, "hello")).text == "Fresh"
                assert chat.quarantined_sessions == frozenset({kitchen})
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_chat_timeout_before_start_ack_preserves_remote_uncertainty(tmp_path):
    session = room_session_key("main", "Kitchen")

    async def handler(ws):
        first = await ws.receive_json()
        run_id = first["params"]["idempotencyKey"]
        abort = await ws.receive_json()
        assert abort["method"] == "chat.abort" and abort["params"]["runId"] == run_id
        await ws.send_json({"type": "res", "id": abort["id"], "ok": True, "payload": {"aborted": [run_id]}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings(turn_timeout_seconds=0.08))
            chat.start()
            try:
                with pytest.raises(GatewayChatUncertain):
                    await chat.exchange(session, "hello")
                assert chat.quarantined_sessions == frozenset({session})
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_chat_task_cancellation_aborts_without_replaying(tmp_path):
    session = room_session_key("main", "Kitchen")
    accepted = asyncio.Event()

    async def handler(ws):
        request = await ws.receive_json()
        run_id = request["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        accepted.set()
        abort = await ws.receive_json()
        assert abort["method"] == "chat.abort" and abort["params"]["runId"] == run_id
        await ws.send_json({"type": "res", "id": abort["id"], "ok": True, "payload": {"aborted": [run_id]}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings())
            chat.start()
            try:
                turn = asyncio.create_task(chat.exchange(session, "hello"))
                await accepted.wait()
                turn.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await turn
                assert chat.quarantined_sessions == frozenset({session})
                with pytest.raises(GatewayChatBusy):
                    await chat.exchange(session, "again")
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_chat_close_revokes_prequeued_final_before_late_start_ack(tmp_path):
    session = room_session_key("main", "Kitchen")

    async def handler(ws):
        request = await ws.receive_json()
        run_id = request["params"]["idempotencyKey"]
        await ws.send_json({"type": "event", "event": "chat", "seq": 1, "payload": {
            "runId": run_id, "sessionKey": session, "state": "final", "seq": 1,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Must not play"}]},
        }})
        abort = await ws.receive_json()
        assert abort["method"] == "chat.abort" and abort["params"]["runId"] == run_id
        await ws.send_json({"type": "res", "id": abort["id"], "ok": True, "payload": {"aborted": [run_id]}})
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings())
            chat.start()
            try:
                turn = asyncio.create_task(chat.exchange(session, "hello"))
                async with asyncio.timeout(1):
                    while not chat._pending or next(iter(chat._pending.values())).outcome.empty():
                        await asyncio.sleep(0.001)
                await chat.close()
                with pytest.raises(asyncio.CancelledError):
                    await turn
                assert chat.quarantined_sessions == frozenset({session})
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_concurrent_chat_closes_join_after_first_caller_cancellation(tmp_path):
    session = room_session_key("main", "Kitchen")
    abort_seen = asyncio.Event()
    release_abort = asyncio.Event()

    async def handler(ws):
        request = await ws.receive_json()
        run_id = request["params"]["idempotencyKey"]
        abort = await ws.receive_json()
        assert abort["method"] == "chat.abort" and abort["params"]["runId"] == run_id
        abort_seen.set()
        await release_abort.wait()
        await ws.send_json({"type": "res", "id": abort["id"], "ok": True, "payload": {"aborted": [run_id]}})
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings(abort_timeout_seconds=0.5))
            chat.start()
            turn = asyncio.create_task(chat.exchange(session, "hello"))
            try:
                async with asyncio.timeout(1):
                    while not chat._pending:
                        await asyncio.sleep(0.001)
                first_close = asyncio.create_task(chat.close())
                await abort_seen.wait()
                second_close = asyncio.create_task(chat.close())
                await asyncio.sleep(0)
                assert not second_close.done()
                first_close.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first_close
                assert not second_close.done()
                release_abort.set()
                await second_close
                with pytest.raises(asyncio.CancelledError):
                    await turn
                assert chat.quarantined_sessions == frozenset({session})
            finally:
                release_abort.set()
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_quarantined_run_keeps_its_capacity_slot(tmp_path):
    kitchen = room_session_key("main", "Kitchen")
    bedroom = room_session_key("main", "Bedroom")

    async def handler(ws):
        request = await ws.receive_json()
        run_id = request["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        abort = await ws.receive_json()
        assert abort["method"] == "chat.abort" and abort["params"]["runId"] == run_id
        await ws.send_json({"type": "res", "id": abort["id"], "ok": True, "payload": {"aborted": [run_id]}})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings(turn_timeout_seconds=0.08, max_active_turns=1))
            chat.start()
            try:
                with pytest.raises(GatewayChatUncertain):
                    await chat.exchange(kitchen, "hello")
                assert chat.quarantined_sessions == frozenset({kitchen})
                with pytest.raises(GatewayCapacityExceeded):
                    await chat.exchange(bedroom, "new request")
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_local_chat_frame_limit_does_not_quarantine_room(tmp_path):
    session = room_session_key("main", "Kitchen")

    async def handler(ws):
        request = await ws.receive_json()
        assert request["params"]["message"] == "short"
        run_id = request["params"]["idempotencyKey"]
        await ws.send_json({"type": "res", "id": request["id"], "ok": True, "payload": {"runId": run_id, "status": "started"}})
        await ws.send_json({"type": "event", "event": "chat", "seq": 1, "payload": {
            "runId": run_id, "sessionKey": session, "state": "final", "seq": 1,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        }})
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path, max_frame_bytes=1024)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings(max_message_characters=2000))
            chat.start()
            try:
                with pytest.raises(GatewayCapacityExceeded):
                    await chat.exchange(session, "x" * 1000)
                assert not chat.quarantined_sessions
                assert (await chat.exchange(session, "short")).text == "Done"
            finally:
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_local_pending_request_limit_does_not_quarantine_room(tmp_path):
    session = room_session_key("main", "Kitchen")
    held = asyncio.Event()

    async def handler(ws):
        request = await ws.receive_json()
        assert request["params"] == {"message": "held"}
        held.set()
        await _wait_for_close(ws)

    async def run():
        async with _server(handler) as (url, _captures):
            gateway = _client(url, tmp_path, max_pending_requests=1)
            await gateway.connect()
            chat = GatewayChat(gateway, _chat_settings())
            chat.start()
            holding = asyncio.create_task(gateway.request("chat.send", {"message": "held"}))
            try:
                await held.wait()
                with pytest.raises(GatewayCapacityExceeded):
                    await chat.exchange(session, "hello")
                assert not chat.quarantined_sessions
            finally:
                holding.cancel()
                with pytest.raises(GatewayRequestCancelled):
                    await holding
                await chat.close()
                await gateway.close()
    asyncio.run(run())


def test_notify_request_preserves_native_identity_and_completion_receipt():
    event = _notify_event()
    notification = decode_notify_request(event, node_id="node-1", settings=_notify_settings())
    assert notification.invocation_id == "invoke-1"
    assert notification.idempotency_key == "dedupe-1"
    assert (notification.title, notification.body, notification.remaining_ms) == ("Reminder", "Hello.", 30000)
    assert completed_notify_result(notification) == {"id": "invoke-1", "nodeId": "node-1", "ok": True}


def test_notify_accepts_system_delivery_hint():
    event = _notify_event(paramsJSON=json.dumps({"title": "Reminder", "delivery": "system"}))
    notification = decode_notify_request(event, node_id="node-1", settings=_notify_settings())
    assert notification.delivery == "system"


@pytest.mark.parametrize("event,code", [
    (_notify_event(timeoutMs=4000), "DEADLINE_TOO_SHORT"),
    (_notify_event(paramsJSON=json.dumps({"body": "x" * 201})), "TOO_LONG"),
    (_notify_event(paramsJSON=json.dumps({"title": " ", "body": " "})), "INVALID_REQUEST"),
    (_notify_event(paramsJSON="{bad json"), "INVALID_REQUEST"),
    (_notify_event(paramsJSON=json.dumps({"body": "Hello", "sound": "ping"})), "UNSUPPORTED_HINT"),
    (_notify_event(paramsJSON=json.dumps({"body": "Hello", "priority": "active"})), "UNSUPPORTED_HINT"),
    (_notify_event(paramsJSON=json.dumps({"body": "Hello", "delivery": "overlay"})), "UNSUPPORTED_HINT"),
])
def test_notify_rejects_before_playback_with_native_error_receipt(event, code):
    with pytest.raises(GatewayNotifyRejected) as raised:
        decode_notify_request(event, node_id="node-1", settings=_notify_settings())
    assert raised.value.code == code
    result = rejected_notify_result(event, node_id="node-1", rejection=raised.value)
    assert result["id"] == "invoke-1" and result["nodeId"] == "node-1"
    assert result["ok"] is False and result["error"]["code"] == code


def test_notify_refuses_cross_node_receipt():
    event = _notify_event(nodeId="node-2")
    with pytest.raises(GatewayProtocolError):
        decode_notify_request(event, node_id="node-1", settings=_notify_settings())
    with pytest.raises(GatewayProtocolError):
        rejected_notify_result(
            event, node_id="node-1", rejection=GatewayNotifyRejected("BUSY", "busy"),
        )


@pytest.mark.skipif(not os.environ.get("OPENCLAW_TEST_DIST"), reason="optional installed Gateway chat schema probe")
def test_installed_gateway_accepts_chat_send_abort_and_terminal():
    root = Path(__file__).resolve().parents[1]
    session = room_session_key("main", "Kitchen")
    run_id = "public-test-run"
    frames = {
        "send": {"sessionKey": session, "message": spoken_request_message("Kitchen", "Ada", "hello"), "idempotencyKey": run_id, "deliver": False},
        "abort": {"sessionKey": session, "runId": run_id},
        "wait": {"runId": run_id, "timeoutMs": 1000},
        "terminal": {
            "runId": run_id, "sessionKey": session, "state": "final", "seq": 1,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello."}]},
        },
    }
    result = subprocess.run(
        ["node", str(root / "tools/lib/t011_chat_contract_probe.mjs"), os.environ["OPENCLAW_TEST_DIST"]],
        input=json.dumps(frames), text=True, capture_output=True, timeout=10, check=True,
    )
    assert json.loads(result.stdout) == {"version": "2026.9.8", "checked": 4}


@pytest.mark.skipif(not os.environ.get("OPENCLAW_TEST_DIST"), reason="optional installed Gateway notify schema probe")
def test_installed_gateway_accepts_notify_request_and_results():
    root = Path(__file__).resolve().parents[1]
    node_id = "public-test-node"
    invoke_id = "public-test-invoke"
    frames = {
        "invoke": {
            "nodeId": node_id, "command": "system.notify",
            "params": {"title": "Reminder", "body": "Hello."},
            "timeoutMs": 30000, "idempotencyKey": "public-test-key",
        },
        "completed": {"id": invoke_id, "nodeId": node_id, "ok": True},
        "rejected": {"id": invoke_id, "nodeId": node_id, "ok": False,
                     "error": {"code": "TOO_LONG", "message": "notification exceeds the playback budget"}},
        "uncertain": uncertain_notify_result(
            decode_notify_request(
                _notify_event(id=invoke_id, nodeId=node_id),
                node_id=node_id, settings=_notify_settings(),
            ), GatewayNotifyUncertain("playback state unknown"),
        ),
    }
    result = subprocess.run(
        ["node", str(root / "tools/lib/t011_notify_contract_probe.mjs"), os.environ["OPENCLAW_TEST_DIST"]],
        input=json.dumps(frames), text=True, capture_output=True, timeout=10, check=True,
    )
    assert json.loads(result.stdout) == {"version": "2026.9.8", "checked": 4}
