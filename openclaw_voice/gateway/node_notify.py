from __future__ import annotations

import asyncio
import logging

from openclaw_voice.gateway.client import GatewayClient
from openclaw_voice.gateway.interfaces import NotificationPresenter
from openclaw_voice.gateway.messages import GatewayError, GatewayEvent, GatewayNotifyReceiptUncertain
from openclaw_voice.gateway.messages import GatewayNotifyRejected, GatewayNotifyUncertain
from openclaw_voice.gateway.notify import completed_notify_result, decode_notify_request
from openclaw_voice.gateway.notify import rejected_notify_result, uncertain_notify_result
from openclaw_voice.gateway.settings import GatewayNotifySettings
from openclaw_voice.utils.interfaces import FatalTermination
from openclaw_voice.utils.tasks import cancel_and_join_or_terminate


async def handle_notify_invocation(
    gateway: GatewayClient,
    event: GatewayEvent,
    *,
    node_id: str,
    settings: GatewayNotifySettings,
    presenter: NotificationPresenter,
    received_at: float,
    fatal_termination: FatalTermination,
) -> None:
    """Handle one native invocation; never replay its result after uncertainty.

    The presenter owns media reservation, playback, and readiness. Its normal
    return proves matching playback completion and cleanup. A typed rejection
    proves that playback never began. Any post-commit uncertainty is distinct.
    """
    logger = logging.getLogger(f"{__name__}.NodeNotification[{node_id}]")
    timeout_ms = event.payload.get("timeoutMs") if isinstance(event.payload, dict) else None
    deadline = received_at + timeout_ms / 1000 if type(timeout_ms) is int and timeout_ms >= 0 else None
    try:
        notification = decode_notify_request(event, node_id=node_id, settings=settings)
    except GatewayNotifyRejected as rejection:
        payload = rejected_notify_result(event, node_id=node_id, rejection=rejection)
        logger.info("notification rejected before playback invocation=%s code=%s", payload["id"], rejection.code)
        assert deadline is not None
        await _send_result(gateway, payload, deadline)
        return
    assert deadline is not None
    presentation_deadline = deadline - settings.result_receipt_margin_seconds - settings.presentation_cleanup_timeout_seconds
    if presentation_deadline <= asyncio.get_running_loop().time():
        rejection = GatewayNotifyRejected("DEADLINE_TOO_SHORT", "notification deadline is too short")
        await _send_result(
            gateway,
            rejected_notify_result(event, node_id=node_id, rejection=rejection),
            deadline,
        )
        return
    presentation = asyncio.create_task(presenter.present(notification, deadline=presentation_deadline))
    try:
        done, _ = await asyncio.wait(
            {presentation},
            timeout=max(0.0, presentation_deadline - asyncio.get_running_loop().time()),
        )
        if not done:
            cancelled = await cancel_and_join_or_terminate(
                presentation, timeout=settings.presentation_cleanup_timeout_seconds,
                fatal_termination=fatal_termination,
                detail="notification presenter did not stop after cancellation",
            )
            if cancelled:
                raise asyncio.CancelledError
            raise GatewayNotifyUncertain("notification presentation exceeded the native deadline")
        presentation.result()
    except asyncio.CancelledError:
        if not presentation.done():
            await cancel_and_join_or_terminate(
                presentation, timeout=settings.presentation_cleanup_timeout_seconds,
                fatal_termination=fatal_termination,
                detail="notification presenter did not stop after cancellation",
            )
        raise
    except GatewayNotifyRejected as rejection:
        payload = rejected_notify_result(event, node_id=node_id, rejection=rejection)
        logger.info("notification rejected before playback invocation=%s code=%s", notification.invocation_id, rejection.code)
    except GatewayNotifyUncertain as error:
        payload = uncertain_notify_result(notification, error)
        logger.warning("notification playback uncertain invocation=%s", notification.invocation_id)
    else:
        payload = completed_notify_result(notification)
        logger.info("notification playback and cleanup completed invocation=%s", notification.invocation_id)
    await _send_result(gateway, payload, deadline)


async def _send_result(gateway: GatewayClient, payload: dict, deadline: float) -> None:
    try:
        async with asyncio.timeout_at(deadline):
            response = await gateway.request("node.invoke.result", payload)
    except (TimeoutError, GatewayError) as exc:
        raise GatewayNotifyReceiptUncertain("native notification result was not confirmed") from exc
    if response != {"ok": True}:
        raise GatewayNotifyReceiptUncertain("native notification result was ignored or malformed")
