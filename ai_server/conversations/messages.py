from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class ContextRejectionCode(Enum):
    UNKNOWN_USER = "unknown_user"
    NOT_AUTHORIZED = "not_authorized"
    UNSUPPORTED_INPUT_CONTEXT = "unsupported_input_context"


class ConversationEndReason(Enum):
    COMPLETED = "completed"
    INPUT_CANCELLED = "input_cancelled"
    FOLLOW_UP_TIMEOUT = "follow_up_timeout"
    CONTEXT_REJECTED = "context_rejected"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    AGENT_FAILED = "agent_failed"
    INPUT_FAILED = "input_failed"
    INPUT_SESSION_CLOSED = "input_session_closed"
    INTERNAL_FAILURE = "internal_failure"


class AssistantAbortReason(Enum):
    INPUT_CANCELLED = "input_cancelled"
    AGENT_FAILED = "agent_failed"
    INPUT_FAILED = "input_failed"
    INPUT_SESSION_CLOSED = "input_session_closed"
    INTERNAL_FAILURE = "internal_failure"


class AgentCancellationReason(Enum):
    INPUT_CANCELLED = "input_cancelled"
    INPUT_FAILED = "input_failed"
    INPUT_SESSION_CLOSED = "input_session_closed"
    INTERNAL_FAILURE = "internal_failure"


class TurnDispositionKind(Enum):
    END_CONVERSATION = "end_conversation"
    REQUEST_FOLLOW_UP = "request_follow_up"


class AssistantSinkTerminalResult(Enum):
    COMPLETED = "completed"
    ABORTED = "aborted"
    INPUT_SESSION_CLOSED = "input_session_closed"


@dataclass(frozen=True)
class UserMessage:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("user message text must be non-whitespace")


@dataclass(frozen=True)
class FollowUpTimedOut:
    pass


@dataclass(frozen=True)
class ConversationCancelled:
    pass


@dataclass(frozen=True)
class InputConversationFailed:
    detail: str | None = None


class InputSessionClosed(Exception):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or "input session closed")
        self.detail = detail


InputControlEvent: TypeAlias = (
    UserMessage | FollowUpTimedOut | ConversationCancelled | InputConversationFailed | InputSessionClosed
)


@dataclass(frozen=True)
class ProcessingUpdate:
    pass


@dataclass(frozen=True)
class AssistantMessageStarted:
    pass


@dataclass(frozen=True)
class AssistantTextChunk:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("assistant text chunk must be non-empty")


@dataclass(frozen=True)
class AssistantMessageCompleted:
    pass


@dataclass(frozen=True)
class TurnDisposition:
    kind: TurnDispositionKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TurnDispositionKind):
            raise ValueError("turn disposition kind must be a TurnDispositionKind")


@dataclass(frozen=True)
class AgentConversationFailed:
    detail: str | None = None


AgentEvent: TypeAlias = (
    ProcessingUpdate
    | AssistantMessageStarted
    | AssistantTextChunk
    | AssistantMessageCompleted
    | TurnDisposition
    | AgentConversationFailed
)


@dataclass(frozen=True)
class AgentInputAccepted:
    pass


@dataclass(frozen=True)
class AgentCancellationAcknowledged:
    reason: AgentCancellationReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, AgentCancellationReason):
            raise ValueError("agent cancellation reason must be an AgentCancellationReason")


@dataclass(frozen=True)
class AssistantSinkStarted:
    pass


@dataclass(frozen=True)
class AssistantTextAccepted:
    pass


@dataclass(frozen=True)
class FollowUpRequestCommitted:
    token: str

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("follow-up token must be a non-empty string")


@dataclass(frozen=True)
class ConversationEnded:
    reason: ConversationEndReason
    context_rejection_code: ContextRejectionCode | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ConversationEndReason):
            raise ValueError("conversation end reason must be a ConversationEndReason")
        if self.context_rejection_code is not None and not isinstance(
            self.context_rejection_code,
            ContextRejectionCode,
        ):
            raise ValueError("context rejection code must be a ContextRejectionCode")
        has_code = self.context_rejection_code is not None
        needs_code = self.reason is ConversationEndReason.CONTEXT_REJECTED
        if has_code != needs_code:
            raise ValueError("context_rejection_code is required exactly for context_rejected")
