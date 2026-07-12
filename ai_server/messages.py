from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, TypeAlias


@dataclass(frozen=True)
class TextMessage:
    text: str


@dataclass(frozen=True)
class SessionAttributes:
    attributes: dict[str, str]


@dataclass(frozen=True)
class NewConversation:
    attributes: dict[str, str]


class ConversationEnded(Exception):
    """Raised or sent when the input side ends the active conversation."""

    def __init__(self, reason: str = "endpoint_ended") -> None:
        super().__init__(reason)
        self.reason = reason

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ConversationEnded) and self.reason == other.reason


@dataclass(frozen=True)
class ReadyForConversation:
    pass


@dataclass(frozen=True)
class FollowUpRequested:
    timeout_seconds: float


@dataclass(frozen=True)
class ProcessingUpdate:
    pass


@dataclass(frozen=True)
class SessionRejected:
    reason: str


@dataclass(frozen=True)
class MessageBegin:
    message_id: str = field(compare=False)


@dataclass(frozen=True)
class MessageFragment:
    message_id: str = field(compare=False)
    text: str


@dataclass(frozen=True)
class MessageEnd:
    message_id: str = field(compare=False)


ConversationInputEvent: TypeAlias = MessageBegin | MessageFragment | MessageEnd
ConversationOutputEvent: TypeAlias = MessageBegin | MessageFragment | MessageEnd | ProcessingUpdate
EndpointToSessionEvent: TypeAlias = (
    SessionAttributes | NewConversation | ConversationEnded | MessageBegin | MessageFragment | MessageEnd
)
SessionToEndpointEvent: TypeAlias = (
    ReadyForConversation
    | FollowUpRequested
    | ConversationEnded
    | ProcessingUpdate
    | SessionRejected
    | MessageBegin
    | MessageFragment
    | MessageEnd
)
ProtocolEvent: TypeAlias = EndpointToSessionEvent | SessionToEndpointEvent


def endpoint_event_from_json(payload: str) -> EndpointToSessionEvent:
    try:
        raw_event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("event must be valid JSON") from exc

    if not isinstance(raw_event, dict):
        raise ValueError("event must be a JSON object")

    return endpoint_event_from_mapping(raw_event)


def endpoint_event_from_mapping(raw_event: dict[str, Any]) -> EndpointToSessionEvent:
    event_type = raw_event.get("type")
    if not isinstance(event_type, str):
        raise ValueError("event.type must be a string")

    if event_type == "session_attributes":
        return SessionAttributes(attributes=_parse_attributes(raw_event, "session_attributes.attributes"))
    if event_type == "new_conversation":
        return NewConversation(attributes=_parse_attributes(raw_event, "new_conversation.attributes"))
    if event_type == "conversation_ended":
        return ConversationEnded(_parse_non_empty_string(raw_event, "reason", "conversation_ended.reason"))
    if event_type == "message_begin":
        return MessageBegin(message_id=_parse_message_id(raw_event, "message_begin"))
    if event_type == "message_fragment":
        text = raw_event.get("text")
        if not isinstance(text, str):
            raise ValueError("message_fragment.text must be a string")
        return MessageFragment(
            message_id=_parse_message_id(raw_event, "message_fragment", {"text"}),
            text=text,
        )
    if event_type == "message_end":
        return MessageEnd(message_id=_parse_message_id(raw_event, "message_end"))

    raise ValueError(f"unsupported endpoint event type: {event_type}")


def session_event_from_json(payload: str) -> SessionToEndpointEvent:
    try:
        raw_event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("event must be valid JSON") from exc

    if not isinstance(raw_event, dict):
        raise ValueError("event must be a JSON object")

    return session_event_from_mapping(raw_event)


def session_event_from_mapping(raw_event: dict[str, Any]) -> SessionToEndpointEvent:
    event_type = raw_event.get("type")
    if not isinstance(event_type, str):
        raise ValueError("event.type must be a string")

    if event_type == "ready_for_conversation":
        _reject_extra_keys(raw_event, {"type"})
        return ReadyForConversation()
    if event_type == "follow_up_requested":
        timeout_seconds = raw_event.get("timeout_seconds")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("follow_up_requested.timeout_seconds must be a positive finite number")
        _reject_extra_keys(raw_event, {"type", "timeout_seconds"})
        return FollowUpRequested(timeout_seconds=float(timeout_seconds))
    if event_type == "processing_update":
        _reject_extra_keys(raw_event, {"type"})
        return ProcessingUpdate()
    if event_type == "session_rejected":
        reason = raw_event.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError("session_rejected.reason must be a non-empty string")
        _reject_extra_keys(raw_event, {"type", "reason"})
        return SessionRejected(reason=reason)
    if event_type == "message_begin":
        return MessageBegin(message_id=_parse_message_id(raw_event, "message_begin"))
    if event_type == "message_fragment":
        text = raw_event.get("text")
        if not isinstance(text, str):
            raise ValueError("message_fragment.text must be a string")
        return MessageFragment(
            message_id=_parse_message_id(raw_event, "message_fragment", {"text"}),
            text=text,
        )
    if event_type == "message_end":
        return MessageEnd(message_id=_parse_message_id(raw_event, "message_end"))
    if event_type == "conversation_ended":
        return ConversationEnded(_parse_non_empty_string(raw_event, "reason", "conversation_ended.reason"))

    raise ValueError(f"unsupported session event type: {event_type}")


def endpoint_event_to_json(event: EndpointToSessionEvent) -> str:
    return json.dumps(endpoint_event_to_mapping(event), ensure_ascii=False)


def endpoint_event_to_mapping(event: EndpointToSessionEvent) -> dict[str, Any]:
    if isinstance(event, SessionAttributes):
        return {"type": "session_attributes", "attributes": dict(event.attributes)}
    if isinstance(event, NewConversation):
        return {"type": "new_conversation", "attributes": dict(event.attributes)}
    if isinstance(event, ConversationEnded):
        return {"type": "conversation_ended", "reason": event.reason}
    if isinstance(event, MessageBegin):
        return {"type": "message_begin", "message_id": event.message_id}
    if isinstance(event, MessageFragment):
        return {"type": "message_fragment", "message_id": event.message_id, "text": event.text}
    if isinstance(event, MessageEnd):
        return {"type": "message_end", "message_id": event.message_id}

    raise ValueError(f"unsupported endpoint event: {type(event).__name__}")


def session_event_to_json(event: SessionToEndpointEvent) -> str:
    return json.dumps(session_event_to_mapping(event), ensure_ascii=False)


def session_event_to_mapping(event: SessionToEndpointEvent) -> dict[str, Any]:
    if isinstance(event, ReadyForConversation):
        return {"type": "ready_for_conversation"}
    if isinstance(event, FollowUpRequested):
        return {"type": "follow_up_requested", "timeout_seconds": event.timeout_seconds}
    if isinstance(event, ConversationEnded):
        return {"type": "conversation_ended", "reason": event.reason}
    if isinstance(event, ProcessingUpdate):
        return {"type": "processing_update"}
    if isinstance(event, SessionRejected):
        return {"type": "session_rejected", "reason": event.reason}
    if isinstance(event, MessageBegin):
        return {"type": "message_begin", "message_id": event.message_id}
    if isinstance(event, MessageFragment):
        return {"type": "message_fragment", "message_id": event.message_id, "text": event.text}
    if isinstance(event, MessageEnd):
        return {"type": "message_end", "message_id": event.message_id}

    raise ValueError(f"unsupported session event: {type(event).__name__}")


def text_message_to_events(message: TextMessage, message_id: str | None = None) -> tuple[ConversationOutputEvent, ...]:
    effective_message_id = message_id or str(uuid.uuid4())
    return (
        MessageBegin(message_id=effective_message_id),
        MessageFragment(message_id=effective_message_id, text=message.text),
        MessageEnd(message_id=effective_message_id),
    )


def _parse_message_id(raw_event: dict[str, Any], event_name: str, extra_keys: set[str] | None = None) -> str:
    return _parse_non_empty_string(raw_event, "message_id", f"{event_name}.message_id", extra_keys)


def _parse_non_empty_string(
    raw_event: dict[str, Any],
    key: str,
    field_name: str,
    extra_keys: set[str] | None = None,
) -> str:
    value = raw_event.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    _reject_extra_keys(raw_event, {"type", key} | (extra_keys or set()))
    return value


def _parse_attributes(raw_event: dict[str, Any], field_name: str) -> dict[str, str]:
    attributes = raw_event.get("attributes", {})
    if not isinstance(attributes, dict):
        raise ValueError(f"{field_name} must be an object")

    parsed_attributes = {}
    for key, value in attributes.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_name} keys must be non-empty strings")
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name}.{key} must be a non-empty string")
        parsed_attributes[key] = value

    _reject_extra_keys(raw_event, {"type", "attributes"})
    return parsed_attributes


def _reject_extra_keys(raw_event: dict[str, Any], allowed_keys: set[str]) -> None:
    extra_keys = set(raw_event) - allowed_keys
    if extra_keys:
        raise ValueError(f"unsupported event fields: {', '.join(sorted(extra_keys))}")
