from __future__ import annotations

import asyncio
import json

import pytest

from openclaw_voice.gateway.messages import GatewayDisconnected, GatewayEvent, GatewayNotifyReceiptUncertain
from openclaw_voice.gateway.messages import GatewayNotifyRejected, GatewayNotifyUncertain
from openclaw_voice.gateway.messages import GatewayRequestUncertain
from openclaw_voice.gateway.node_notify import handle_notify_invocation
from openclaw_voice.gateway.settings import GatewayNotifySettings


def _settings() -> GatewayNotifySettings:
    return GatewayNotifySettings(
        max_params_bytes=512, max_title_characters=50, max_body_characters=100,
        max_speech_characters=150, minimum_remaining_ms=100,
        presentation_cleanup_timeout_seconds=0.01,
        result_receipt_margin_seconds=0.01,
    )


def _event(**changes) -> GatewayEvent:
    payload = {
        "id": "inv-1", "nodeId": "node-1", "command": "system.notify",
        "idempotencyKey": "key-1", "timeoutMs": 1000,
        "paramsJSON": json.dumps({"title": "Title", "body": "Body"}),
    }
    payload.update(changes)
    return GatewayEvent("node.invoke.request", payload)


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict):
        self.calls.append((method, params))
        return {"ok": True}


class _FatalExit(Exception):
    pass


class _Fatal:
    async def terminate(self, detail: str):
        raise _FatalExit(detail)


class _Presenter:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.calls = []

    async def present(self, notification, *, deadline):
        self.calls.append((notification, deadline))
        if self.outcome is not None:
            raise self.outcome


@pytest.mark.parametrize("outcome,expected", [
    (None, (True, None)),
    (GatewayNotifyRejected("BUSY", "satellite busy"), (False, "BUSY")),
    (GatewayNotifyUncertain("playback may have begun"), (False, "UNCERTAIN_PLAYBACK")),
])
def test_node_result_reflects_presenter_completion_boundary(outcome, expected):
    async def run():
        gateway = _Gateway()
        presenter = _Presenter(outcome)
        now = asyncio.get_running_loop().time()
        await handle_notify_invocation(
            gateway, _event(), node_id="node-1", settings=_settings(),
            presenter=presenter, received_at=now, fatal_termination=_Fatal(),
        )
        assert len(presenter.calls) == 1
        assert presenter.calls[0][1] == pytest.approx(now + 0.98)
        assert len(gateway.calls) == 1
        method, result = gateway.calls[0]
        assert method == "node.invoke.result"
        assert (result["ok"], result.get("error", {}).get("code")) == expected
        assert result["id"] == "inv-1" and result["nodeId"] == "node-1"

    asyncio.run(run())


def test_node_rejects_unsupported_hint_without_calling_presenter():
    async def run():
        gateway = _Gateway()
        presenter = _Presenter()
        event = _event(paramsJSON=json.dumps({"body": "Body", "sound": "ping"}))
        await handle_notify_invocation(
            gateway, event, node_id="node-1", settings=_settings(),
            presenter=presenter, received_at=asyncio.get_running_loop().time(),
            fatal_termination=_Fatal(),
        )
        assert presenter.calls == []
        assert gateway.calls[0][1]["error"]["code"] == "UNSUPPORTED_HINT"

    asyncio.run(run())


def test_node_does_not_claim_success_after_result_deadline():
    class _SlowGateway(_Gateway):
        async def request(self, method, params):
            self.calls.append((method, params))
            await asyncio.Event().wait()

    async def run():
        gateway = _SlowGateway()
        with pytest.raises(GatewayNotifyReceiptUncertain):
            await handle_notify_invocation(
                gateway, _event(timeoutMs=110), node_id="node-1", settings=_settings(),
                presenter=_Presenter(), received_at=asyncio.get_running_loop().time(),
                fatal_termination=_Fatal(),
            )
        assert gateway.calls[0][1]["ok"] is True

    asyncio.run(run())


@pytest.mark.parametrize("response", [{"ok": True, "ignored": True}, {}, {"ok": False}])
def test_node_requires_exact_native_result_receipt(response):
    class _ResultGateway(_Gateway):
        async def request(self, method, params):
            self.calls.append((method, params))
            return response

    async def run():
        with pytest.raises(GatewayNotifyReceiptUncertain):
            await handle_notify_invocation(
                _ResultGateway(), _event(), node_id="node-1", settings=_settings(),
                presenter=_Presenter(), received_at=asyncio.get_running_loop().time(),
                fatal_termination=_Fatal(),
            )

    asyncio.run(run())


@pytest.mark.parametrize("failure", [
    GatewayDisconnected("gateway disconnected"),
    GatewayRequestUncertain("result request may have reached gateway"),
])
def test_node_result_transport_failure_preserves_receipt_uncertainty(failure):
    class _FailingGateway(_Gateway):
        async def request(self, method, params):
            self.calls.append((method, params))
            raise failure

    async def run():
        gateway = _FailingGateway()
        presenter = _Presenter()
        with pytest.raises(GatewayNotifyReceiptUncertain):
            await handle_notify_invocation(
                gateway, _event(), node_id="node-1", settings=_settings(),
                presenter=presenter, received_at=asyncio.get_running_loop().time(),
                fatal_termination=_Fatal(),
            )
        assert len(presenter.calls) == 1
        assert len(gateway.calls) == 1

    asyncio.run(run())


def test_stalled_presenter_is_joined_then_reports_uncertain():
    cleaned = asyncio.Event()

    class _StalledPresenter:
        async def present(self, notification, *, deadline):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    async def run():
        gateway = _Gateway()
        await handle_notify_invocation(
            gateway, _event(timeoutMs=110), node_id="node-1", settings=_settings(),
            presenter=_StalledPresenter(), received_at=asyncio.get_running_loop().time(),
            fatal_termination=_Fatal(),
        )
        assert cleaned.is_set()
        assert gateway.calls[0][1]["error"]["code"] == "UNCERTAIN_PLAYBACK"

    asyncio.run(run())


def test_unjoinable_presenter_triggers_fatal_containment():
    release = asyncio.Event()

    class _UnjoinablePresenter:
        async def present(self, notification, *, deadline):
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()

    async def run():
        before = asyncio.all_tasks()
        try:
            with pytest.raises(_FatalExit, match="did not stop"):
                await handle_notify_invocation(
                    _Gateway(), _event(timeoutMs=110), node_id="node-1", settings=_settings(),
                    presenter=_UnjoinablePresenter(), received_at=asyncio.get_running_loop().time(),
                    fatal_termination=_Fatal(),
                )
        finally:
            release.set()
            workers = asyncio.all_tasks() - before
            if workers:
                await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), 1)

    asyncio.run(run())


def test_repeated_caller_cancellation_joins_presenter_before_exit():
    entered = asyncio.Event()
    finalizing = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    class _DelayedPresenter:
        async def present(self, notification, *, deadline):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                finalizing.set()
                await release.wait()
                cleaned.set()

    async def run():
        gateway = _Gateway()
        caller = asyncio.create_task(handle_notify_invocation(
            gateway, _event(), node_id="node-1", settings=GatewayNotifySettings(
                max_params_bytes=512, max_title_characters=50, max_body_characters=100,
                max_speech_characters=150, minimum_remaining_ms=100,
                presentation_cleanup_timeout_seconds=0.5,
                result_receipt_margin_seconds=0.01,
            ), presenter=_DelayedPresenter(),
            received_at=asyncio.get_running_loop().time(), fatal_termination=_Fatal(),
        ))
        await entered.wait()
        caller.cancel()
        await finalizing.wait()
        caller.cancel()
        assert not caller.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert cleaned.is_set()
        assert gateway.calls == []

    asyncio.run(run())
