from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ai_server.conversations.contexts import ConversationContext, ConversationMedium
from ai_server.utils.processing import ProcessingUpdateCallback


@dataclass
class AgentExecutionContext:
    """Private mutable state owned by one active AgentConversation."""

    conversation: ConversationContext
    agent_state: dict[str, Any] = field(default_factory=dict)
    processing_update_callback: ProcessingUpdateCallback | None = None
    processing_update_interval_seconds: float = 5.0

    @property
    def conversation_id(self) -> str:
        return self.conversation.conversation_id

    @property
    def input_session_id(self) -> str:
        return self.conversation.input_session_id

    @property
    def medium(self) -> ConversationMedium:
        return self.conversation.medium

    @property
    def user(self) -> str | None:
        return self.conversation.user

    @property
    def area(self) -> str | None:
        return self.conversation.area

    @property
    def user_settings(self):
        return _thaw(self.conversation.user_settings)


def _thaw(value):
    if hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)
