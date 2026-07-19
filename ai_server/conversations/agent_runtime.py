from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from enum import Enum

from ai_server.conversations.contexts import ConversationContext
from ai_server.conversations.interfaces import AgentConversation
from ai_server.conversations.messages import AgentCancellationAcknowledged, AgentCancellationReason, AgentEvent
from ai_server.conversations.messages import AgentConversationFailed, AgentInputAccepted, AssistantMessageCompleted
from ai_server.conversations.messages import AssistantMessageStarted, AssistantTextChunk, ProcessingUpdate, TurnDisposition
from ai_server.conversations.messages import TurnDispositionKind, UserMessage
from ai_server.conversations.rendezvous import Rendezvous, RendezvousClosed


class AgentChannel:
    """Agent-side directional API; the bridge never receives this object."""

    def __init__(self, incoming: Rendezvous[UserMessage], outgoing: Rendezvous[AgentEvent]) -> None:
        self._incoming = incoming
        self._outgoing = outgoing
        self._stream_open = False
        self._disposition_sent = False
        self._phase = _AgentChannelPhase.WAITING_FOR_INITIAL_INPUT

    async def receive_user_message(self) -> UserMessage:
        if self._phase not in (
            _AgentChannelPhase.WAITING_FOR_INITIAL_INPUT,
            _AgentChannelPhase.WAITING_FOR_FOLLOW_UP,
        ):
            raise AssertionError("user input requested in invalid Agent turn phase")
        message = await self._incoming.receive()
        self._phase = _AgentChannelPhase.ACTIVE
        self._disposition_sent = False
        return message

    async def processing_update(self) -> None:
        self._require_active_turn("processing update")
        if self._stream_open:
            raise AssertionError("processing update emitted while assistant stream is open")
        await self._outgoing.send(ProcessingUpdate())

    async def start_assistant_message(self) -> None:
        self._require_active_turn("assistant stream")
        if self._stream_open:
            raise AssertionError("assistant stream already open")
        self._stream_open = True
        await self._outgoing.send(AssistantMessageStarted())

    async def send_text(self, text: str) -> None:
        self._require_active_turn("assistant text")
        if not self._stream_open:
            raise AssertionError("assistant text emitted before stream start")
        await self._outgoing.send(AssistantTextChunk(text))

    async def complete_assistant_message(self) -> None:
        self._require_active_turn("assistant completion")
        if not self._stream_open:
            raise AssertionError("assistant stream completed before start")
        self._stream_open = False
        await self._outgoing.send(AssistantMessageCompleted())

    async def send_message(self, text: str) -> None:
        await self.start_assistant_message()
        if text:
            await self.send_text(text)
        await self.complete_assistant_message()

    async def end_conversation(self) -> None:
        await self._send_disposition(TurnDispositionKind.END_CONVERSATION)

    async def request_follow_up(self) -> None:
        await self._send_disposition(TurnDispositionKind.REQUEST_FOLLOW_UP)

    async def _send_disposition(self, kind: TurnDispositionKind) -> None:
        self._require_active_turn("turn disposition")
        if self._stream_open:
            raise AssertionError("turn disposition emitted while assistant stream is open")
        if self._disposition_sent:
            raise AssertionError("duplicate turn disposition")
        self._disposition_sent = True
        await self._outgoing.send(TurnDisposition(kind))
        self._phase = (
            _AgentChannelPhase.ENDED
            if kind is TurnDispositionKind.END_CONVERSATION
            else _AgentChannelPhase.WAITING_FOR_FOLLOW_UP
        )

    @property
    def active_turn(self) -> bool:
        return self._phase is _AgentChannelPhase.ACTIVE

    @property
    def waiting_for_input(self) -> bool:
        return self._phase in (
            _AgentChannelPhase.WAITING_FOR_INITIAL_INPUT,
            _AgentChannelPhase.WAITING_FOR_FOLLOW_UP,
        )

    def _require_active_turn(self, operation: str) -> None:
        if self._phase is not _AgentChannelPhase.ACTIVE:
            raise AssertionError(f"{operation} emitted before user input acceptance or after disposition")


class _AgentChannelPhase(Enum):
    WAITING_FOR_INITIAL_INPUT = "waiting_for_initial_input"
    ACTIVE = "active"
    WAITING_FOR_FOLLOW_UP = "waiting_for_follow_up"
    ENDED = "ended"


class ConversationAgent(ABC):
    def open_conversation(self, context: ConversationContext) -> AbstractAsyncContextManager[AgentConversation]:
        return _AgentConversationScope(self, context)

    @abstractmethod
    async def run_agent_conversation(self, context: ConversationContext, channel: AgentChannel) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError


class _AgentConversationScope(AbstractAsyncContextManager[AgentConversation]):
    def __init__(self, agent: ConversationAgent, context: ConversationContext) -> None:
        self._active = _ActiveAgentConversation(agent, context)

    async def __aenter__(self) -> AgentConversation:
        self._active.start()
        return self._active

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self._active.close()


class _ActiveAgentConversation(AgentConversation):
    def __init__(self, agent: ConversationAgent, context: ConversationContext) -> None:
        self._agent = agent
        self._context = context
        self._incoming: Rendezvous[UserMessage] = Rendezvous()
        self._outgoing: Rendezvous[AgentEvent] = Rendezvous()
        self._task: asyncio.Task[None] | None = None
        self._cancel_reason: AgentCancellationReason | None = None
        self._cancel_lock = asyncio.Lock()
        self._closed = False
        self._worker_failure: BaseException | None = None
        self._logger = logging.getLogger(
            f"{__name__}.AgentConversation[{context.conversation_id}]"
        )

    def start(self) -> None:
        assert self._task is None
        self._task = asyncio.create_task(self._run(), name=f"agent-conversation-{self._context.conversation_id}")

    async def send_user_message(self, message: UserMessage) -> AgentInputAccepted:
        if self._closed:
            raise RuntimeError("agent conversation is closed")
        await self._incoming.send(message)
        return AgentInputAccepted()

    async def receive_event(self) -> AgentEvent:
        try:
            return await self._outgoing.receive()
        except RendezvousClosed as exc:
            raise RuntimeError("agent conversation ended without a pending event") from exc

    async def cancel(self, reason: AgentCancellationReason) -> AgentCancellationAcknowledged:
        async with self._cancel_lock:
            if self._cancel_reason is None:
                self._cancel_reason = reason
                self._logger.info("cancellation requested reason=%s", reason.value)
                await self._incoming.close(asyncio.CancelledError())
                if self._task is not None and not self._task.done():
                    self._task.cancel()
            committed_reason = self._cancel_reason
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._logger.info("cancellation acknowledged reason=%s", committed_reason.value)
        return AgentCancellationAcknowledged(committed_reason)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._incoming.close()
        await self._outgoing.close()

    async def _run(self) -> None:
        channel = AgentChannel(self._incoming, self._outgoing)
        try:
            await self._agent.run_agent_conversation(self._context, channel)
            if channel.waiting_for_input:
                raise RuntimeError("agent conversation ended before accepting required user input")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._worker_failure = exc
            self._logger.exception("agent conversation failed")
            if channel.active_turn:
                with contextlib.suppress(RendezvousClosed, asyncio.CancelledError):
                    await self._outgoing.send(AgentConversationFailed(detail=str(exc)))
        finally:
            await self._incoming.close(
                self._worker_failure
                or RuntimeError("agent conversation is no longer accepting user input")
            )
            await self._outgoing.close()
