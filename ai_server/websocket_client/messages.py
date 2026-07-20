from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class InputKind(Enum):
    START = "start"
    FOLLOW_UP = "follow_up"


class InputResultKind(Enum):
    TEXT = "text"
    EMPTY = "empty"
    HELP = "help"
    EXIT = "exit"
    EOF = "eof"
    UNKNOWN_COMMAND = "unknown_command"
    BATCH_COMPLETE = "batch_complete"


@dataclass(frozen=True)
class InputResult:
    kind: InputResultKind
    text: str | None
    submitted_at: float


class ExitKind(Enum):
    COMPLETED = "completed"
    EXITED = "exited"
    INTERRUPTED = "interrupted"
    CONNECTION_FAILED = "connection_failed"
    CONNECTION_LOST = "connection_lost"
    REJECTED = "rejected"
    PROTOCOL_ERROR = "protocol_error"
    LOCAL_FAILURE = "local_failure"
    KNOWN_UNSENT = "known_unsent"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    USAGE_ERROR = "usage_error"


@dataclass(frozen=True)
class ExitResult:
    kind: ExitKind
    detail: str | None = None

    @property
    def code(self) -> int:
        if self.kind in (ExitKind.COMPLETED, ExitKind.EXITED):
            return 0
        if self.kind is ExitKind.INTERRUPTED:
            return 130
        if self.kind is ExitKind.USAGE_ERROR:
            return 2
        return 1
