from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AsyncExitStack
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, NoReturn, TypeVar

from ai_server.conversations.context_provider import ContextProvider, ContextRejected, ContextResolved, ContextUnavailable
from ai_server.conversations.interfaces import Agent, AgentConversation, InputConversation
from ai_server.conversations.messages import AgentCancellationAcknowledged, AgentCancellationReason
from ai_server.conversations.messages import AgentConversationFailed, AgentEvent, AgentInputAccepted
from ai_server.conversations.messages import AssistantAbortReason, AssistantMessageCompleted, AssistantMessageStarted
from ai_server.conversations.messages import AssistantSinkStarted, AssistantSinkTerminalResult
from ai_server.conversations.messages import AssistantTextAccepted, AssistantTextChunk, ConversationCancelled
from ai_server.conversations.messages import ConversationEnded, ConversationEndReason
from ai_server.conversations.messages import FollowUpRequestCommitted, FollowUpTimedOut
from ai_server.conversations.messages import InputConversationFailed, InputControlEvent, InputSessionClosed
from ai_server.conversations.messages import ProcessingUpdate, TurnDisposition, TurnDispositionKind, UserMessage


class BridgeState(Enum):
    STARTING = "starting"
    DELIVERING_USER_MESSAGE = "delivering_user_message"
    WAITING_FOR_AGENT = "waiting_for_agent"
    STREAMING_ASSISTANT = "streaming_assistant"
    WAITING_FOR_DISPOSITION = "waiting_for_disposition"
    COMMITTING_FOLLOW_UP = "committing_follow_up"
    WAITING_FOR_FOLLOW_UP = "waiting_for_follow_up"
    ENDING = "ending"
    CLOSED = "closed"


@dataclass(frozen=True)
class BridgeSettings:
    agent_cancellation_deadline_seconds: float
    fatal_notification_seconds: float

    def __post_init__(self) -> None:
        _require_positive_finite(self.agent_cancellation_deadline_seconds, "agent_cancellation_deadline_seconds")
        _require_positive_finite(self.fatal_notification_seconds, "fatal_notification_seconds")


class FatalTerminationController:
    async def terminate(self, detail: str) -> NoReturn:
        raise SystemExit(detail)


T = TypeVar("T")


async def bridge_conversation(
    *,
    input_conversation: InputConversation,
    agent: Agent,
    context_provider: ContextProvider,
    settings: BridgeSettings,
    fatal_termination: FatalTerminationController | None = None,
) -> None:
    bridge = _ConversationBridge(
        input_conversation=input_conversation,
        agent=agent,
        context_provider=context_provider,
        settings=settings,
        fatal_termination=fatal_termination or FatalTerminationController(),
    )
    await bridge.run()


class _ConversationBridge:
    def __init__(
        self,
        *,
        input_conversation: InputConversation,
        agent: Agent,
        context_provider: ContextProvider,
        settings: BridgeSettings,
        fatal_termination: FatalTerminationController,
    ) -> None:
        self._input = input_conversation
        self._agent = agent
        self._context_provider = context_provider
        self._settings = settings
        self._fatal_termination = fatal_termination
        self._state = BridgeState.STARTING
        self._input_task: asyncio.Task[InputControlEvent] | None = None
        self._agent_conversation: AgentConversation | None = None
        self._sink_started = False
        self._sink_completed = False
        self._logger = logging.getLogger(
            f"{__name__}.ConversationBridge[{input_conversation.context.conversation_id}]"
        )

    async def run(self) -> None:
        self._input_task = asyncio.create_task(self._input.receive_control())
        try:
            context_result = self._resolve_context()
            if not isinstance(
                context_result,
                (ContextResolved, ContextRejected, ContextUnavailable),
            ):
                await self._fatal("context provider returned an invalid result")
            if isinstance(context_result, ContextResolved):
                self._validate_resolved_context(context_result)
            ready_input = self._ready_input()
            if ready_input is not None:
                await self._finish_from_input(ready_input)
                return
            if isinstance(context_result, ContextRejected):
                await self._finish(
                    ConversationEnded(
                        ConversationEndReason.CONTEXT_REJECTED,
                        context_rejection_code=context_result.code,
                        detail=context_result.detail,
                    )
                )
                return
            if isinstance(context_result, ContextUnavailable):
                await self._finish(ConversationEnded(ConversationEndReason.CONTEXT_UNAVAILABLE, detail=context_result.detail))
                return
            assert isinstance(context_result, ContextResolved)

            async with AsyncExitStack() as resources:
                agent_conversation = await self._enter_agent(resources, context_result)
                if agent_conversation is None:
                    return
                self._agent_conversation = agent_conversation
                message = self._input.initial_message
                if not isinstance(message, UserMessage):
                    await self._fatal("input conversation returned an invalid initial message")
                while True:
                    if not await self._deliver_message(message):
                        return
                    disposition = await self._run_turn()
                    if disposition is None:
                        return
                    if disposition.kind is TurnDispositionKind.END_CONVERSATION:
                        await self._finish(ConversationEnded(ConversationEndReason.COMPLETED))
                        return
                    message = await self._follow_up()
                    if message is None:
                        return
        except SystemExit:
            raise
        except Exception as exc:
            self._logger.exception("bridge invariant failed")
            await self._fatal(str(exc))
        finally:
            if self._input_task is not None:
                self._input_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, InputSessionClosed):
                    await self._input_task
            if self._state is not BridgeState.CLOSED:
                self._transition(BridgeState.CLOSED, "scope_exit")

    def _resolve_context(self):
        try:
            return self._context_provider.resolve(self._input.context)
        except Exception as exc:
            raise RuntimeError("context provider raised") from exc

    async def _enter_agent(self, resources: AsyncExitStack, context_result: ContextResolved) -> AgentConversation | None:
        scope = self._agent.open_conversation(context_result.context)
        operation = asyncio.create_task(scope.__aenter__())
        result = await self._race_operation(
            operation,
            timeout_seconds=self._settings.agent_cancellation_deadline_seconds,
        )
        if isinstance(result, _DeadlineExpired):
            operation.cancel()
            await self._fatal("agent entry deadline exceeded")
        if isinstance(result, _InputWon):
            await self._cancel_task(operation)
            committed_entry = self._successful_task_result(operation)
            if committed_entry is not None and not isinstance(
                committed_entry,
                AgentConversation,
            ):
                await self._fatal("agent factory returned an invalid AgentConversation")
            if isinstance(committed_entry, AgentConversation):
                resources.push_async_callback(self._exit_agent_scope, scope)
                self._agent_conversation = committed_entry
                await self._cancel_agent(_agent_reason(result.event))
            await self._finish_from_input(result.event)
            return None
        if isinstance(result, BaseException):
            await self._finish(ConversationEnded(ConversationEndReason.AGENT_FAILED, detail=str(result)))
            return None
        if not isinstance(result, AgentConversation):
            await self._fatal("agent factory returned an invalid AgentConversation")
        resources.push_async_callback(self._exit_agent_scope, scope)
        self._transition(BridgeState.DELIVERING_USER_MESSAGE, "agent_entry")
        self._logger.info("agent conversation entered")
        return result

    async def _exit_agent_scope(
        self,
        scope: AbstractAsyncContextManager[AgentConversation],
    ) -> None:
        operation = asyncio.create_task(scope.__aexit__(None, None, None))
        done, _ = await asyncio.wait(
            (operation,),
            timeout=self._settings.agent_cancellation_deadline_seconds,
        )
        if operation not in done:
            operation.cancel()
            await self._fatal("agent scope exit deadline exceeded")
        operation.result()
        self._logger.info("agent conversation scope exited")

    async def _deliver_message(self, message: UserMessage) -> bool:
        assert self._agent_conversation is not None
        self._transition(BridgeState.DELIVERING_USER_MESSAGE, "deliver_user_message")
        operation = asyncio.create_task(self._agent_conversation.send_user_message(message))
        result = await self._race_operation(operation)
        if isinstance(result, _InputWon):
            await self._cancel_task(operation)
            committed_acceptance = self._successful_task_result(operation)
            if committed_acceptance is not None and not isinstance(
                committed_acceptance,
                AgentInputAccepted,
            ):
                await self._fatal(
                    "AgentConversation returned an invalid input-acceptance result"
                )
            await self._cancel_agent(_agent_reason(result.event))
            await self._finish_from_input(result.event)
            return False
        if isinstance(result, BaseException):
            await self._finish(ConversationEnded(ConversationEndReason.AGENT_FAILED, detail=str(result)))
            return False
        if not isinstance(result, AgentInputAccepted):
            await self._fatal("AgentConversation returned an invalid input-acceptance result")
        self._transition(BridgeState.WAITING_FOR_AGENT, "agent_input_accepted")
        self._logger.info("user input accepted")
        return True

    async def _run_turn(self) -> TurnDisposition | None:
        assert self._agent_conversation is not None
        while True:
            operation = asyncio.create_task(self._agent_conversation.receive_event())
            result = await self._race_operation(operation)
            if isinstance(result, _InputWon):
                await self._cancel_task(operation)
                await self._cancel_agent(_agent_reason(result.event))
                await self._finish_from_input(result.event)
                return None
            if isinstance(result, BaseException):
                await self._agent_failed(str(result))
                return None
            event = result
            if isinstance(event, ProcessingUpdate):
                if self._state is not BridgeState.WAITING_FOR_AGENT:
                    raise AssertionError("processing update outside waiting-for-agent")
                sink_result = await self._race_sink(self._input.processing_update(), type(None))
                if sink_result is None:
                    return None
                continue
            if isinstance(event, AssistantMessageStarted):
                if self._state is not BridgeState.WAITING_FOR_AGENT:
                    raise AssertionError("assistant stream start in invalid state")
                sink_result = await self._race_sink(
                    self._input.assistant_output.start(),
                    AssistantSinkStarted,
                )
                if sink_result is None:
                    return None
                self._sink_started = True
                self._transition(BridgeState.STREAMING_ASSISTANT, "assistant_started")
                self._logger.info("assistant stream started")
                continue
            if isinstance(event, AssistantTextChunk):
                if self._state is not BridgeState.STREAMING_ASSISTANT:
                    raise AssertionError("assistant text outside stream")
                sink_result = await self._race_sink(
                    self._input.assistant_output.send_text(event.text),
                    AssistantTextAccepted,
                )
                if sink_result is None:
                    return None
                continue
            if isinstance(event, AssistantMessageCompleted):
                if self._state is not BridgeState.STREAMING_ASSISTANT:
                    raise AssertionError("assistant completion outside stream")
                sink_result = await self._race_sink(
                    self._input.assistant_output.complete(),
                    AssistantSinkTerminalResult,
                )
                if sink_result is None:
                    return None
                terminal_result = sink_result.value
                if terminal_result is AssistantSinkTerminalResult.INPUT_SESSION_CLOSED:
                    await self._cancel_agent(AgentCancellationReason.INPUT_SESSION_CLOSED)
                    await self._finish_from_input(
                        InputSessionClosed("assistant sink closed the input session")
                    )
                    return None
                if terminal_result is AssistantSinkTerminalResult.ABORTED:
                    await self._fatal(
                        "assistant sink aborted completion without a terminal input event"
                    )
                self._sink_completed = True
                self._transition(BridgeState.WAITING_FOR_DISPOSITION, "assistant_completed")
                self._logger.info("assistant stream completed")
                continue
            if isinstance(event, TurnDisposition):
                if self._state not in (BridgeState.WAITING_FOR_AGENT, BridgeState.WAITING_FOR_DISPOSITION):
                    raise AssertionError("turn disposition in invalid state")
                return event
            if isinstance(event, AgentConversationFailed):
                await self._agent_failed(event.detail)
                return None
            raise AssertionError(f"unknown agent event: {type(event).__name__}")

    async def _follow_up(self) -> UserMessage | None:
        self._transition(BridgeState.COMMITTING_FOLLOW_UP, "follow_up_disposition")
        operation = asyncio.create_task(self._input.request_follow_up())
        result = await self._race_operation(operation)
        if isinstance(result, _InputWon):
            await self._cancel_task(operation)
            committed_follow_up = self._successful_task_result(operation)
            if committed_follow_up is not None and not isinstance(
                committed_follow_up,
                FollowUpRequestCommitted,
            ):
                await self._fatal(
                    "input adapter returned an invalid follow-up commit result"
                )
            await self._cancel_agent(_agent_reason(result.event))
            await self._finish_from_input(result.event)
            return None
        if isinstance(result, InputSessionClosed):
            await self._cancel_agent(AgentCancellationReason.INPUT_SESSION_CLOSED)
            await self._finish(ConversationEnded(ConversationEndReason.INPUT_SESSION_CLOSED, detail=str(result)))
            return None
        if isinstance(result, BaseException):
            await self._fatal(f"follow-up presentation failed: {result}")
        if not isinstance(result, FollowUpRequestCommitted):
            await self._fatal("input adapter returned an invalid follow-up commit result")
        self._transition(BridgeState.WAITING_FOR_FOLLOW_UP, "follow_up_committed")
        self._input.acknowledge_follow_up_ready(result)
        self._logger.info("follow-up committed token=%s", result.token)
        assert self._input_task is not None
        event = await self._input_task
        self._input_task = None
        self._validate_input_event(event)
        if isinstance(event, UserMessage):
            self._start_input_receive()
            return event
        await self._cancel_agent(_agent_reason(event))
        await self._finish_from_input(event)
        return None

    async def _race_sink(
        self,
        awaitable: Awaitable[object],
        expected_type: type,
    ) -> _SinkOperationResult | None:
        operation = asyncio.create_task(awaitable)
        result = await self._race_operation(operation, operation_failure_wins=True)
        if isinstance(result, _InputWon):
            await self._cancel_task(operation)
            committed_sink_result = self._successful_task_result(operation)
            if committed_sink_result is not None and not isinstance(
                committed_sink_result,
                expected_type,
            ):
                await self._fatal(
                    "input sink returned invalid result: "
                    f"{type(committed_sink_result).__name__}"
                )
            await self._cancel_agent(_agent_reason(result.event))
            await self._abort_sink(_abort_reason(result.event))
            await self._finish_from_input(result.event)
            return None
        if isinstance(result, InputSessionClosed):
            await self._cancel_agent(AgentCancellationReason.INPUT_SESSION_CLOSED)
            await self._finish_from_input(result)
            return None
        if isinstance(result, BaseException):
            await self._fatal(f"input sink failed: {result}")
        if not isinstance(result, expected_type):
            await self._fatal(
                f"input sink returned invalid result: {type(result).__name__}"
            )
        return _SinkOperationResult(result)

    async def _race_operation(
        self,
        operation: asyncio.Task[T],
        *,
        operation_failure_wins: bool = False,
        timeout_seconds: float | None = None,
    ) -> T | _InputWon | _DeadlineExpired | BaseException:
        assert self._input_task is not None
        done, _ = await asyncio.wait(
            (operation, self._input_task),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            self._logger.debug("race ready_set=deadline selected=deadline")
            return _DeadlineExpired()
        ready_set = ["operation" if task is operation else "input" for task in done]
        if operation_failure_wins and operation.done():
            try:
                operation.result()
            except BaseException as exc:
                self._logger.debug(
                    "race ready_set=%s selected=operation_failure",
                    ",".join(sorted(ready_set)),
                )
                return exc
        ready_input = self._ready_input()
        if ready_input is not None:
            self._logger.debug(
                "race ready_set=%s selected=input event=%s",
                ",".join(sorted(ready_set)),
                type(ready_input).__name__,
            )
            return _InputWon(ready_input)
        try:
            self._logger.debug(
                "race ready_set=%s selected=operation",
                ",".join(sorted(ready_set)),
            )
            return operation.result()
        except BaseException as exc:
            return exc

    def _ready_input(self) -> InputControlEvent | None:
        if self._input_task is None or not self._input_task.done():
            return None
        try:
            event = self._input_task.result()
        except InputSessionClosed as exc:
            event = exc
        self._input_task = None
        self._validate_input_event(event)
        return event

    def _validate_input_event(self, event: InputControlEvent) -> None:
        if isinstance(event, (ConversationCancelled, InputConversationFailed, InputSessionClosed)):
            return
        if isinstance(event, (UserMessage, FollowUpTimedOut)):
            if self._state is not BridgeState.WAITING_FOR_FOLLOW_UP:
                raise AssertionError(
                    f"{type(event).__name__} outside waiting-for-follow-up"
                )
            return
        raise AssertionError(f"unknown input event: {type(event).__name__}")

    def _start_input_receive(self) -> None:
        assert self._input_task is None
        self._input_task = asyncio.create_task(self._input.receive_control())

    async def _finish_from_input(self, event: InputControlEvent) -> None:
        if self._sink_started and not self._sink_completed:
            await self._abort_sink(_abort_reason(event))
        await self._finish(_ended_from_input(event))

    async def _agent_failed(self, detail: str | None) -> None:
        if self._sink_started and not self._sink_completed:
            await self._abort_sink(AssistantAbortReason.AGENT_FAILED, detail)
        await self._finish(ConversationEnded(ConversationEndReason.AGENT_FAILED, detail=detail))

    async def _abort_sink(self, reason: AssistantAbortReason, detail: str | None = None) -> None:
        result = await self._input.assistant_output.abort(reason, detail)
        if result not in (
            AssistantSinkTerminalResult.ABORTED,
            AssistantSinkTerminalResult.COMPLETED,
            AssistantSinkTerminalResult.INPUT_SESSION_CLOSED,
        ):
            await self._fatal("assistant sink returned invalid abort result")

    async def _cancel_agent(self, reason: AgentCancellationReason) -> None:
        if self._agent_conversation is None:
            return
        self._logger.info("agent cancellation requested reason=%s", reason.value)
        operation = asyncio.create_task(self._agent_conversation.cancel(reason))
        done, _ = await asyncio.wait(
            (operation,),
            timeout=self._settings.agent_cancellation_deadline_seconds,
        )
        if operation not in done:
            operation.cancel()
            await self._fatal("agent cancellation acknowledgement deadline exceeded")
        result = operation.result()
        if not isinstance(result, AgentCancellationAcknowledged) or result.reason is not reason:
            await self._fatal("agent returned an invalid cancellation acknowledgement")
        self._logger.info("agent cancellation acknowledged reason=%s", reason.value)

    def _validate_resolved_context(self, result: ContextResolved) -> None:
        input_context = self._input.context
        context = result.context
        if (
            context.conversation_id != input_context.conversation_id
            or context.input_session_id != input_context.input_session_id
            or context.medium is not input_context.medium
            or context.user != input_context.user
            or context.area != input_context.area
        ):
            raise RuntimeError("resolved context does not match input conversation scope")

    async def _finish(self, event: ConversationEnded) -> None:
        self._transition(BridgeState.ENDING, event.reason.value)
        context = self._input.context
        self._logger.info(
            "conversation terminal conversation_id=%s input_session_id=%s medium=%s user=%r area=%r reason=%s rejection_code=%s",
            context.conversation_id,
            context.input_session_id,
            context.medium.value,
            context.user,
            context.area,
            event.reason.value,
            event.context_rejection_code.value if event.context_rejection_code is not None else None,
        )
        with contextlib.suppress(InputSessionClosed):
            await self._input.end_conversation(event)
        self._transition(BridgeState.CLOSED, event.reason.value)

    async def _fatal(self, detail: str) -> NoReturn:
        if self._sink_started and not self._sink_completed:
            await self._bounded_best_effort(
                self._input.assistant_output.abort(
                    AssistantAbortReason.INTERNAL_FAILURE,
                    detail,
                )
            )
        await self._bounded_best_effort(
            self._input.end_conversation(
                ConversationEnded(ConversationEndReason.INTERNAL_FAILURE, detail=detail)
            )
        )
        await self._fatal_termination.terminate(detail)

    async def _bounded_best_effort(self, awaitable: Awaitable[object]) -> None:
        operation = asyncio.create_task(awaitable)
        done, _ = await asyncio.wait(
            (operation,),
            timeout=self._settings.fatal_notification_seconds,
        )
        if operation not in done:
            operation.cancel()
            return
        with contextlib.suppress(BaseException):
            operation.result()

    async def _cancel_task(self, task: asyncio.Task[object]) -> None:
        task.cancel()
        done, _ = await asyncio.wait(
            (task,),
            timeout=self._settings.agent_cancellation_deadline_seconds,
        )
        if task not in done:
            await self._fatal("owned operation cancellation deadline exceeded")
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    @staticmethod
    def _successful_task_result(task: asyncio.Task[object]) -> object | None:
        if not task.done() or task.cancelled():
            return None
        try:
            return task.result()
        except BaseException:
            return None

    def _transition(self, state: BridgeState, cause: str) -> None:
        old = self._state
        self._state = state
        self._logger.debug("state transition old=%s cause=%s new=%s", old.value, cause, state.value)


@dataclass(frozen=True)
class _InputWon:
    event: InputControlEvent


@dataclass(frozen=True)
class _SinkOperationResult:
    value: object


@dataclass(frozen=True)
class _DeadlineExpired:
    pass


def _ended_from_input(event: InputControlEvent) -> ConversationEnded:
    if isinstance(event, ConversationCancelled):
        return ConversationEnded(ConversationEndReason.INPUT_CANCELLED)
    if isinstance(event, FollowUpTimedOut):
        return ConversationEnded(ConversationEndReason.FOLLOW_UP_TIMEOUT)
    if isinstance(event, InputConversationFailed):
        return ConversationEnded(ConversationEndReason.INPUT_FAILED, detail=event.detail)
    if isinstance(event, InputSessionClosed):
        return ConversationEnded(ConversationEndReason.INPUT_SESSION_CLOSED, detail=event.detail)
    raise AssertionError(f"unexpected input terminal event: {type(event).__name__}")


def _agent_reason(event: InputControlEvent) -> AgentCancellationReason:
    if isinstance(event, ConversationCancelled):
        return AgentCancellationReason.INPUT_CANCELLED
    if isinstance(event, InputConversationFailed):
        return AgentCancellationReason.INPUT_FAILED
    if isinstance(event, InputSessionClosed):
        return AgentCancellationReason.INPUT_SESSION_CLOSED
    if isinstance(event, FollowUpTimedOut):
        return AgentCancellationReason.INPUT_CANCELLED
    raise AssertionError(f"unexpected cancellation event: {type(event).__name__}")


def _abort_reason(event: InputControlEvent) -> AssistantAbortReason:
    if isinstance(event, ConversationCancelled):
        return AssistantAbortReason.INPUT_CANCELLED
    if isinstance(event, InputConversationFailed):
        return AssistantAbortReason.INPUT_FAILED
    if isinstance(event, InputSessionClosed):
        return AssistantAbortReason.INPUT_SESSION_CLOSED
    raise AssertionError(f"event cannot abort an assistant stream: {type(event).__name__}")


def _require_positive_finite(value: float, field: str) -> None:
    import math

    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 or not math.isfinite(value):
        raise ValueError(f"{field} must be a positive finite number")
