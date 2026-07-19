from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ai_server.conversations.contexts import ConversationContext, InputConversationContext, InputSessionContext
from ai_server.conversations.messages import AgentCancellationAcknowledged, AgentCancellationReason, AgentEvent
from ai_server.conversations.messages import AgentInputAccepted, AssistantAbortReason, AssistantSinkStarted
from ai_server.conversations.messages import AssistantSinkTerminalResult, AssistantTextAccepted, ConversationEnded
from ai_server.conversations.messages import FollowUpRequestCommitted, InputControlEvent, UserMessage


class AssistantOutputSink(ABC):
    @abstractmethod
    async def start(self) -> AssistantSinkStarted:
        raise NotImplementedError

    @abstractmethod
    async def send_text(self, chunk: str) -> AssistantTextAccepted:
        raise NotImplementedError

    @abstractmethod
    async def complete(self) -> AssistantSinkTerminalResult:
        raise NotImplementedError

    @abstractmethod
    async def abort(
        self,
        reason: AssistantAbortReason,
        detail: str | None = None,
    ) -> AssistantSinkTerminalResult:
        raise NotImplementedError


class InputConversation(ABC):
    @property
    @abstractmethod
    def context(self) -> InputConversationContext:
        raise NotImplementedError

    @property
    @abstractmethod
    def initial_message(self) -> UserMessage:
        raise NotImplementedError

    @property
    @abstractmethod
    def assistant_output(self) -> AssistantOutputSink:
        raise NotImplementedError

    @abstractmethod
    async def receive_control(self) -> InputControlEvent:
        raise NotImplementedError

    @abstractmethod
    async def processing_update(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def request_follow_up(self) -> FollowUpRequestCommitted:
        raise NotImplementedError

    @abstractmethod
    def acknowledge_follow_up_ready(self, token: FollowUpRequestCommitted) -> None:
        raise NotImplementedError

    @abstractmethod
    async def end_conversation(self, event: ConversationEnded) -> None:
        raise NotImplementedError


class InputSession(ABC):
    @property
    @abstractmethod
    def context(self) -> InputSessionContext:
        raise NotImplementedError

    @property
    @abstractmethod
    def closed(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def accept_conversation(self) -> AbstractAsyncContextManager[InputConversation]:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class InputAdapter(Protocol):
    def open_session(self) -> AbstractAsyncContextManager[InputSession]: ...


class AgentConversation(ABC):
    @abstractmethod
    async def send_user_message(self, message: UserMessage) -> AgentInputAccepted:
        raise NotImplementedError

    @abstractmethod
    async def receive_event(self) -> AgentEvent:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, reason: AgentCancellationReason) -> AgentCancellationAcknowledged:
        raise NotImplementedError


class Agent(Protocol):
    def open_conversation(
        self,
        context: ConversationContext,
    ) -> AbstractAsyncContextManager[AgentConversation]: ...

    async def close(self) -> None: ...
