from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ConversationMedium(Enum):
    TEXT = "text"
    VOICE = "voice"


@dataclass(frozen=True)
class InputSessionContext:
    input_session_id: str
    medium: ConversationMedium
    user: str | None = None
    area: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.input_session_id, "input_session_id")
        _require_medium(self.medium)
        _require_optional_text(self.user, "user")
        _require_optional_text(self.area, "area")


@dataclass(frozen=True)
class InputConversationContext:
    conversation_id: str
    input_session_id: str
    medium: ConversationMedium
    user: str | None = None
    area: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.conversation_id, "conversation_id")
        _require_id(self.input_session_id, "input_session_id")
        _require_medium(self.medium)
        _require_optional_text(self.user, "user")
        _require_optional_text(self.area, "area")


@dataclass(frozen=True)
class ConversationContext:
    conversation_id: str
    input_session_id: str
    medium: ConversationMedium
    user_settings: Mapping[str, Any]
    user: str | None = None
    area: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.conversation_id, "conversation_id")
        _require_id(self.input_session_id, "input_session_id")
        _require_medium(self.medium)
        _require_optional_text(self.user, "user")
        _require_optional_text(self.area, "area")
        object.__setattr__(self, "user_settings", _freeze_mapping(self.user_settings))


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_value(item) for key, item in copy.deepcopy(dict(value)).items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _require_id(value: str, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _require_optional_text(value: str | None, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{field} must be a non-empty string when provided")


def _require_medium(value: ConversationMedium) -> None:
    if not isinstance(value, ConversationMedium):
        raise ValueError("medium must be a ConversationMedium")
