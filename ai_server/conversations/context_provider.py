from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from ai_server.conversations.contexts import ConversationContext, InputConversationContext
from ai_server.conversations.messages import ContextRejectionCode


@dataclass(frozen=True)
class ContextResolved:
    context: ConversationContext


@dataclass(frozen=True)
class ContextRejected:
    code: ContextRejectionCode
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ContextRejectionCode):
            raise ValueError("context rejection code must be a ContextRejectionCode")


@dataclass(frozen=True)
class ContextUnavailable:
    detail: str | None = None


ContextResult: TypeAlias = ContextResolved | ContextRejected | ContextUnavailable


class ContextProvider(Protocol):
    def resolve(self, input_context: InputConversationContext) -> ContextResult: ...


class ConfigContextProvider:
    def __init__(self, users: dict[str, dict[str, Any]] | None = None) -> None:
        self._users = copy.deepcopy(users or {})

    def resolve(self, input_context: InputConversationContext) -> ContextResult:
        user = input_context.user
        settings: dict[str, Any] = {}
        if user is not None:
            settings = self._settings_for(user)
            if settings is None:
                return ContextRejected(
                    code=ContextRejectionCode.UNKNOWN_USER,
                    detail="configured identity is unknown",
                )
        return ContextResolved(
            ConversationContext(
                conversation_id=input_context.conversation_id,
                input_session_id=input_context.input_session_id,
                medium=input_context.medium,
                user=input_context.user,
                area=input_context.area,
                user_settings=settings,
            )
        )

    def _settings_for(self, user: str) -> dict[str, Any] | None:
        direct = self._users.get(user)
        if direct is not None:
            return copy.deepcopy(direct)
        normalized = user.casefold()
        for candidate, settings in self._users.items():
            if candidate.casefold() == normalized:
                return copy.deepcopy(settings)
        return None
