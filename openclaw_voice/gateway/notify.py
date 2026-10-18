from __future__ import annotations

import json
from typing import Any

from openclaw_voice.gateway.messages import GatewayEvent, GatewayNotification, GatewayNotifyRejected
from openclaw_voice.gateway.messages import GatewayNotifyUncertain
from openclaw_voice.gateway.messages import GatewayProtocolError
from openclaw_voice.gateway.settings import GatewayNotifySettings


def decode_notify_request(
    event: GatewayEvent, *, node_id: str, settings: GatewayNotifySettings,
) -> GatewayNotification:
    """Decode one installed-version node invoke event without touching media.

    The remaining budget and length checks are only early rejection. The media
    owner must also prove that synthesis, playback, cleanup, and the result
    round trip fit the actual remaining deadline before PlaybackBegin.
    """
    if event.name != "node.invoke.request" or not isinstance(event.payload, dict):
        raise GatewayProtocolError("expected node.invoke.request event")
    payload = event.payload
    invocation_id, actual_node_id = _invocation_identity(event, node_id)
    if payload.get("command") != "system.notify":
        raise GatewayNotifyRejected("UNSUPPORTED_COMMAND", "unsupported node command")
    idempotency_key = _required_string(payload.get("idempotencyKey"), "idempotency key")
    remaining_ms = payload.get("timeoutMs")
    if type(remaining_ms) is not int or remaining_ms < 0:
        raise GatewayProtocolError("invalid notification timeout")
    if remaining_ms < settings.minimum_remaining_ms:
        raise GatewayNotifyRejected("DEADLINE_TOO_SHORT", "notification deadline is too short")
    params_json = payload.get("paramsJSON")
    if not isinstance(params_json, str):
        raise GatewayNotifyRejected("INVALID_REQUEST", "notification parameters are missing")
    if len(params_json.encode("utf-8")) > settings.max_params_bytes:
        raise GatewayNotifyRejected("TOO_LONG", "notification parameters are too long")
    try:
        params = json.loads(params_json, parse_constant=_invalid_json_constant)
    except (ValueError, TypeError) as exc:
        raise GatewayNotifyRejected("INVALID_REQUEST", "notification parameters are invalid") from exc
    if not isinstance(params, dict):
        raise GatewayNotifyRejected("INVALID_REQUEST", "notification parameters must be an object")
    if set(params) - {"title", "body", "sound", "priority", "delivery"}:
        raise GatewayNotifyRejected("INVALID_REQUEST", "unsupported notification parameter")
    title = _optional_text(params.get("title"), "title")
    body = _optional_text(params.get("body"), "body")
    if not title and not body:
        raise GatewayNotifyRejected("INVALID_REQUEST", "notification title or body is required")
    if len(title) > settings.max_title_characters or len(body) > settings.max_body_characters:
        raise GatewayNotifyRejected("TOO_LONG", "notification text is too long")
    if len(title) + len(body) > settings.max_speech_characters:
        raise GatewayNotifyRejected("TOO_LONG", "notification speech is too long")
    sound = _optional_hint(params.get("sound"), "sound")
    priority = _optional_hint(params.get("priority"), "priority")
    delivery = _optional_hint(params.get("delivery"), "delivery")
    if sound is not None or priority is not None or delivery not in (None, "system"):
        raise GatewayNotifyRejected("UNSUPPORTED_HINT", "notification hint is not supported by spoken playback")
    return GatewayNotification(
        invocation_id=invocation_id, node_id=actual_node_id,
        idempotency_key=idempotency_key, title=title, body=body,
        sound=sound, priority=priority, delivery=delivery,
        remaining_ms=remaining_ms,
    )


def completed_notify_result(notification: GatewayNotification) -> dict[str, Any]:
    """Call only after matching PlaybackFinished and manager cleanup."""
    return {"id": notification.invocation_id, "nodeId": notification.node_id, "ok": True}


def rejected_notify_result(
    event: GatewayEvent, *, node_id: str, rejection: GatewayNotifyRejected,
) -> dict[str, Any]:
    """Call only when the manager proves no PlaybackBegin occurred."""
    invocation_id, actual_node_id = _invocation_identity(event, node_id)
    return {
        "id": invocation_id, "nodeId": actual_node_id,
        "ok": False, "error": {"code": rejection.code, "message": str(rejection)},
    }


def uncertain_notify_result(notification: GatewayNotification, error: GatewayNotifyUncertain) -> dict[str, Any]:
    """Report a possibly spoken notification without implying pre-playback rejection."""
    return {
        "id": notification.invocation_id, "nodeId": notification.node_id,
        "ok": False,
        "error": {"code": "UNCERTAIN_PLAYBACK", "message": str(error)},
    }


def _invocation_identity(event: GatewayEvent, node_id: str) -> tuple[str, str]:
    if event.name != "node.invoke.request" or not isinstance(event.payload, dict):
        raise GatewayProtocolError("expected node.invoke.request event")
    invocation_id = _required_string(event.payload.get("id"), "invocation ID")
    actual_node_id = _required_string(event.payload.get("nodeId"), "node ID")
    if actual_node_id != node_id:
        raise GatewayProtocolError("notification addressed to a different node")
    return invocation_id, actual_node_id


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewayProtocolError(f"invalid notification {label}")
    return value


def _optional_text(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GatewayNotifyRejected("INVALID_REQUEST", f"notification {label} must be text")
    return value.strip()


def _optional_hint(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GatewayNotifyRejected("INVALID_REQUEST", f"notification {label} must be text")
    value = value.strip()
    if not value or len(value) > 128:
        raise GatewayNotifyRejected("INVALID_REQUEST", f"notification {label} is invalid")
    return value


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")
