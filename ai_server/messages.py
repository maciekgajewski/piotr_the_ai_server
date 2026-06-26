from __future__ import annotations

import json
from dataclasses import dataclass
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

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ConversationEnded)


@dataclass(frozen=True)
class WaitForNewConversation:
    pass


@dataclass(frozen=True)
class WaitForNewMessage:
    pass


@dataclass(frozen=True)
class RequestFollowUp:
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class ProcessingUpdate:
    pass


@dataclass(frozen=True)
class SessionRejected:
    reason: str


@dataclass(frozen=True)
class MessageBegin:
    pass


@dataclass(frozen=True)
class MessageFragment:
    text: str


@dataclass(frozen=True)
class MessageEnd:
    pass


ConversationInputEvent: TypeAlias = MessageBegin | MessageFragment | MessageEnd
ConversationOutputEvent: TypeAlias = MessageBegin | MessageFragment | MessageEnd | RequestFollowUp | ProcessingUpdate
EndpointToSessionEvent: TypeAlias = (
    SessionAttributes | NewConversation | ConversationEnded | MessageBegin | MessageFragment | MessageEnd
)
SessionToEndpointEvent: TypeAlias = (
    WaitForNewConversation
    | WaitForNewMessage
    | RequestFollowUp
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
        _reject_extra_keys(raw_event, {"type"})
        return ConversationEnded()
    if event_type == "message_begin":
        _reject_extra_keys(raw_event, {"type"})
        return MessageBegin()
    if event_type == "message_fragment":
        text = raw_event.get("text")
        if not isinstance(text, str):
            raise ValueError("message_fragment.text must be a string")
        _reject_extra_keys(raw_event, {"type", "text"})
        return MessageFragment(text=text)
    if event_type == "message_end":
        _reject_extra_keys(raw_event, {"type"})
        return MessageEnd()

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

    if event_type == "wait_for_new_conversation":
        _reject_extra_keys(raw_event, {"type"})
        return WaitForNewConversation()
    if event_type == "wait_for_new_message":
        _reject_extra_keys(raw_event, {"type"})
        return WaitForNewMessage()
    if event_type == "request_follow_up":
        timeout_seconds = raw_event.get("timeout_seconds")
        if timeout_seconds is None:
            _reject_extra_keys(raw_event, {"type"})
            return RequestFollowUp()
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("request_follow_up.timeout_seconds must be a positive number")
        _reject_extra_keys(raw_event, {"type", "timeout_seconds"})
        return RequestFollowUp(timeout_seconds=float(timeout_seconds))
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
        _reject_extra_keys(raw_event, {"type"})
        return MessageBegin()
    if event_type == "message_fragment":
        text = raw_event.get("text")
        if not isinstance(text, str):
            raise ValueError("message_fragment.text must be a string")
        _reject_extra_keys(raw_event, {"type", "text"})
        return MessageFragment(text=text)
    if event_type == "message_end":
        _reject_extra_keys(raw_event, {"type"})
        return MessageEnd()

    raise ValueError(f"unsupported session event type: {event_type}")


def endpoint_event_to_json(event: EndpointToSessionEvent) -> str:
    return json.dumps(endpoint_event_to_mapping(event), ensure_ascii=False)


def endpoint_event_to_mapping(event: EndpointToSessionEvent) -> dict[str, Any]:
    if isinstance(event, SessionAttributes):
        return {"type": "session_attributes", "attributes": dict(event.attributes)}
    if isinstance(event, NewConversation):
        return {"type": "new_conversation", "attributes": dict(event.attributes)}
    if isinstance(event, ConversationEnded):
        return {"type": "conversation_ended"}
    if isinstance(event, MessageBegin):
        return {"type": "message_begin"}
    if isinstance(event, MessageFragment):
        return {"type": "message_fragment", "text": event.text}
    if isinstance(event, MessageEnd):
        return {"type": "message_end"}

    raise ValueError(f"unsupported endpoint event: {type(event).__name__}")


def session_event_to_json(event: SessionToEndpointEvent) -> str:
    return json.dumps(session_event_to_mapping(event), ensure_ascii=False)


def session_event_to_mapping(event: SessionToEndpointEvent) -> dict[str, Any]:
    if isinstance(event, WaitForNewConversation):
        return {"type": "wait_for_new_conversation"}
    if isinstance(event, WaitForNewMessage):
        return {"type": "wait_for_new_message"}
    if isinstance(event, RequestFollowUp):
        payload = {"type": "request_follow_up"}
        if event.timeout_seconds is not None:
            payload["timeout_seconds"] = event.timeout_seconds
        return payload
    if isinstance(event, ProcessingUpdate):
        return {"type": "processing_update"}
    if isinstance(event, SessionRejected):
        return {"type": "session_rejected", "reason": event.reason}
    if isinstance(event, MessageBegin):
        return {"type": "message_begin"}
    if isinstance(event, MessageFragment):
        return {"type": "message_fragment", "text": event.text}
    if isinstance(event, MessageEnd):
        return {"type": "message_end"}

    raise ValueError(f"unsupported session event: {type(event).__name__}")


def text_message_to_events(message: TextMessage) -> tuple[ConversationOutputEvent, ...]:
    return (
        MessageBegin(),
        MessageFragment(text=message.text),
        MessageEnd(),
    )


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
