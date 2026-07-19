from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias

from ai_server.conversations.messages import AssistantAbortReason, ContextRejectionCode
from ai_server.conversations.messages import ConversationEndReason


@dataclass(frozen=True)
class SessionStart:
    user: str | None = None
    area: str | None = None

    def __post_init__(self) -> None:
        _validate_optional_text(self.user, "user")
        _validate_optional_text(self.area, "area")


@dataclass(frozen=True)
class StartConversation:
    message: str

    def __post_init__(self) -> None:
        _validate_message(self.message)


@dataclass(frozen=True)
class FollowUpMessage:
    message: str

    def __post_init__(self) -> None:
        _validate_message(self.message)


@dataclass(frozen=True)
class FollowUpTimedOut:
    pass


@dataclass(frozen=True)
class CancelConversation:
    pass


ClientEvent: TypeAlias = SessionStart | StartConversation | FollowUpMessage | FollowUpTimedOut | CancelConversation


@dataclass(frozen=True)
class SessionAccepted:
    pass


@dataclass(frozen=True)
class ConversationReady:
    pass


@dataclass(frozen=True)
class ConversationStarted:
    conversation_id: str

    def __post_init__(self) -> None:
        _validate_required_text(self.conversation_id, "conversation_id")


@dataclass(frozen=True)
class ProcessingUpdate:
    pass


@dataclass(frozen=True)
class AssistantMessageStarted:
    message_id: str

    def __post_init__(self) -> None:
        _validate_required_text(self.message_id, "message_id")


@dataclass(frozen=True)
class AssistantTextChunk:
    message_id: str
    text: str

    def __post_init__(self) -> None:
        _validate_required_text(self.message_id, "message_id")
        _validate_required_text(self.text, "text")


@dataclass(frozen=True)
class AssistantMessageCompleted:
    message_id: str

    def __post_init__(self) -> None:
        _validate_required_text(self.message_id, "message_id")


@dataclass(frozen=True)
class AssistantMessageAborted:
    message_id: str
    reason: str
    detail: str | None = None

    def __post_init__(self) -> None:
        _validate_required_text(self.message_id, "message_id")
        if self.reason not in {reason.value for reason in AssistantAbortReason}:
            raise ValueError("invalid assistant abort reason")
        _validate_optional_text(self.detail, "detail")


@dataclass(frozen=True)
class FollowUpRequested:
    pass


@dataclass(frozen=True)
class ConversationEnded:
    reason: str
    context_rejection_code: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.reason not in {reason.value for reason in ConversationEndReason}:
            raise ValueError("invalid conversation end reason")
        needs_code = self.reason == ConversationEndReason.CONTEXT_REJECTED.value
        has_code = self.context_rejection_code is not None
        if needs_code != has_code:
            raise ValueError(
                "context_rejection_code is required exactly for context_rejected"
            )
        if self.context_rejection_code is not None and self.context_rejection_code not in {
            code.value for code in ContextRejectionCode
        }:
            raise ValueError("invalid context rejection code")
        _validate_optional_text(self.detail, "detail")


class ProtocolRejectionCode(Enum):
    INVALID_JSON = "invalid_json"
    INVALID_EVENT = "invalid_event"
    INVALID_STATE = "invalid_state"
    MESSAGE_TOO_LARGE = "message_too_large"
    DUPLICATE_FOLLOW_UP_OUTCOME = "duplicate_follow_up_outcome"
    INGRESS_OVERFLOW = "ingress_overflow"


@dataclass(frozen=True)
class ProtocolRejected:
    code: ProtocolRejectionCode
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ProtocolRejectionCode):
            raise ValueError("invalid protocol rejection code")
        _validate_optional_text(self.detail, "detail")


ServerEvent: TypeAlias = (
    SessionAccepted
    | ConversationReady
    | ConversationStarted
    | ProcessingUpdate
    | AssistantMessageStarted
    | AssistantTextChunk
    | AssistantMessageCompleted
    | AssistantMessageAborted
    | FollowUpRequested
    | ConversationEnded
    | ProtocolRejected
)


class InvalidJson(ValueError):
    pass


class InvalidEvent(ValueError):
    pass


def client_event_from_json(payload: str) -> ClientEvent:
    raw = _strict_json_object(payload)
    event_type = _event_type(raw)
    if event_type == "session_start":
        _keys(raw, {"type", "user", "area"})
        return SessionStart(user=_optional_text(raw, "user"), area=_optional_text(raw, "area"))
    if event_type == "start_conversation":
        _keys(raw, {"type", "message"})
        return StartConversation(_message(raw))
    if event_type == "follow_up_message":
        _keys(raw, {"type", "message"})
        return FollowUpMessage(_message(raw))
    if event_type == "follow_up_timed_out":
        _keys(raw, {"type"})
        return FollowUpTimedOut()
    if event_type == "cancel_conversation":
        _keys(raw, {"type"})
        return CancelConversation()
    raise InvalidEvent(f"unsupported client event type: {event_type}")


def server_event_from_json(payload: str) -> ServerEvent:
    try:
        return _server_event_from_json(payload)
    except (InvalidJson, InvalidEvent):
        raise
    except ValueError as exc:
        raise InvalidEvent(str(exc)) from exc


def _server_event_from_json(payload: str) -> ServerEvent:
    raw = _strict_json_object(payload)
    event_type = _event_type(raw)
    if event_type == "session_accepted":
        _keys(raw, {"type"})
        return SessionAccepted()
    if event_type == "conversation_ready":
        _keys(raw, {"type"})
        return ConversationReady()
    if event_type == "conversation_started":
        _keys(raw, {"type", "conversation_id"})
        return ConversationStarted(_required_text(raw, "conversation_id"))
    if event_type == "processing_update":
        _keys(raw, {"type"})
        return ProcessingUpdate()
    if event_type == "assistant_message_started":
        _keys(raw, {"type", "message_id"})
        return AssistantMessageStarted(_required_text(raw, "message_id"))
    if event_type == "assistant_text_chunk":
        _keys(raw, {"type", "message_id", "text"})
        return AssistantTextChunk(_required_text(raw, "message_id"), _required_text(raw, "text"))
    if event_type == "assistant_message_completed":
        _keys(raw, {"type", "message_id"})
        return AssistantMessageCompleted(_required_text(raw, "message_id"))
    if event_type == "assistant_message_aborted":
        _keys(raw, {"type", "message_id", "reason", "detail"})
        return AssistantMessageAborted(
            _required_text(raw, "message_id"),
            _required_text(raw, "reason"),
            _optional_text(raw, "detail"),
        )
    if event_type == "follow_up_requested":
        _keys(raw, {"type"})
        return FollowUpRequested()
    if event_type == "conversation_ended":
        _keys(raw, {"type", "reason", "context_rejection_code", "detail"})
        return ConversationEnded(
            _required_text(raw, "reason"),
            _optional_text(raw, "context_rejection_code"),
            _optional_text(raw, "detail"),
        )
    if event_type == "protocol_rejected":
        _keys(raw, {"type", "code", "detail"})
        try:
            code = ProtocolRejectionCode(_required_text(raw, "code"))
        except ValueError as exc:
            raise InvalidEvent("invalid protocol rejection code") from exc
        return ProtocolRejected(code, _optional_text(raw, "detail"))
    raise InvalidEvent(f"unsupported server event type: {event_type}")


def client_event_to_json(event: ClientEvent) -> str:
    return _to_json(client_event_to_mapping(event))


def client_event_to_mapping(event: ClientEvent) -> dict[str, Any]:
    if isinstance(event, SessionStart):
        return _without_none({"type": "session_start", "user": event.user, "area": event.area})
    if isinstance(event, StartConversation):
        return {"type": "start_conversation", "message": event.message}
    if isinstance(event, FollowUpMessage):
        return {"type": "follow_up_message", "message": event.message}
    if isinstance(event, FollowUpTimedOut):
        return {"type": "follow_up_timed_out"}
    if isinstance(event, CancelConversation):
        return {"type": "cancel_conversation"}
    raise ValueError(f"unsupported client event: {type(event).__name__}")


def server_event_to_json(event: ServerEvent) -> str:
    return _to_json(server_event_to_mapping(event))


def server_event_to_mapping(event: ServerEvent) -> dict[str, Any]:
    if isinstance(event, SessionAccepted):
        return {"type": "session_accepted"}
    if isinstance(event, ConversationReady):
        return {"type": "conversation_ready"}
    if isinstance(event, ConversationStarted):
        return {"type": "conversation_started", "conversation_id": event.conversation_id}
    if isinstance(event, ProcessingUpdate):
        return {"type": "processing_update"}
    if isinstance(event, AssistantMessageStarted):
        return {"type": "assistant_message_started", "message_id": event.message_id}
    if isinstance(event, AssistantTextChunk):
        return {"type": "assistant_text_chunk", "message_id": event.message_id, "text": event.text}
    if isinstance(event, AssistantMessageCompleted):
        return {"type": "assistant_message_completed", "message_id": event.message_id}
    if isinstance(event, AssistantMessageAborted):
        return _without_none(
            {"type": "assistant_message_aborted", "message_id": event.message_id, "reason": event.reason, "detail": event.detail}
        )
    if isinstance(event, FollowUpRequested):
        return {"type": "follow_up_requested"}
    if isinstance(event, ConversationEnded):
        return _without_none(
            {
                "type": "conversation_ended",
                "reason": event.reason,
                "context_rejection_code": event.context_rejection_code,
                "detail": event.detail,
            }
        )
    if isinstance(event, ProtocolRejected):
        return _without_none({"type": "protocol_rejected", "code": event.code.value, "detail": event.detail})
    raise ValueError(f"unsupported server event: {type(event).__name__}")


def _strict_json_object(payload: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidEvent(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        raw = json.loads(payload, object_pairs_hook=object_pairs)
    except json.JSONDecodeError as exc:
        raise InvalidJson("event must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise InvalidEvent("event must be a JSON object")
    return raw


def _event_type(raw: dict[str, Any]) -> str:
    value = raw.get("type")
    if not isinstance(value, str) or not value:
        raise InvalidEvent("event.type must be a non-empty string")
    return value


def _message(raw: dict[str, Any]) -> str:
    value = _required_text(raw, "message")
    if not value.strip():
        raise InvalidEvent("message must be non-whitespace")
    return value


def _required_text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidEvent(f"{key} must be a non-empty string")
    return value


def _optional_text(raw: dict[str, Any], key: str) -> str | None:
    if key not in raw:
        return None
    return _required_text(raw, key)


def _keys(raw: dict[str, Any], allowed: set[str]) -> None:
    extra = set(raw) - allowed
    if extra:
        raise InvalidEvent(f"unsupported event fields: {', '.join(sorted(extra))}")


def _without_none(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if value is not None}


def _to_json(raw: dict[str, Any]) -> str:
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def _validate_required_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _validate_optional_text(value: str | None, field: str) -> None:
    if value is not None:
        _validate_required_text(value, field)


def _validate_message(value: str) -> None:
    _validate_required_text(value, "message")
    if not value.strip():
        raise ValueError("message must be non-whitespace")
