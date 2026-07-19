from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager

import pytest

from ai_server.conversations.agent_runtime import AgentChannel, ConversationAgent
from ai_server.conversations.bridge import BridgeSettings, FatalTerminationController, bridge_conversation
from ai_server.conversations.bridge import _ConversationBridge, _DeadlineExpired, _InputWon
from ai_server.conversations.context_provider import ContextRejected, ContextResolved, ContextUnavailable
from ai_server.conversations.contexts import ConversationContext, ConversationMedium, InputConversationContext
from ai_server.conversations.interfaces import AgentConversation, AssistantOutputSink, InputConversation
from ai_server.conversations.messages import AgentCancellationAcknowledged, AgentCancellationReason
from ai_server.conversations.messages import AgentConversationFailed, AgentInputAccepted, AssistantAbortReason
from ai_server.conversations.messages import AssistantMessageCompleted, AssistantMessageStarted, AssistantSinkStarted
from ai_server.conversations.messages import AssistantSinkTerminalResult, AssistantTextAccepted, AssistantTextChunk
from ai_server.conversations.messages import ContextRejectionCode, ConversationCancelled, ConversationEnded
from ai_server.conversations.messages import ConversationEndReason, FollowUpRequestCommitted, FollowUpTimedOut
from ai_server.conversations.messages import InputConversationFailed, InputControlEvent, InputSessionClosed
from ai_server.conversations.messages import ProcessingUpdate, TurnDisposition, TurnDispositionKind, UserMessage


SETTINGS = BridgeSettings(1.0, 0.1)


class _FatalExit(FatalTerminationController):
    async def terminate(self, detail: str):
        raise SystemExit(detail)


class _Provider:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def resolve(self, input_context: InputConversationContext):
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return ContextResolved(
            ConversationContext(
                conversation_id=input_context.conversation_id,
                input_session_id=input_context.input_session_id,
                medium=input_context.medium,
                user=input_context.user,
                area=input_context.area,
                user_settings={},
            )
        )


class _RecordingSink(AssistantOutputSink):
    def __init__(self) -> None:
        self.trace: list[object] = []
        self.state = "not_started"
        self.started = asyncio.Event()
        self.send_started = asyncio.Event()
        self.complete_started = asyncio.Event()
        self.release_start = asyncio.Event()
        self.release_send = asyncio.Event()
        self.release_complete = asyncio.Event()
        self.block_start = False
        self.block_send = False
        self.block_complete = False

    async def start(self) -> AssistantSinkStarted:
        assert self.state == "not_started"
        self.started.set()
        if self.block_start:
            await self.release_start.wait()
        self.state = "open"
        self.trace.append("start")
        return AssistantSinkStarted()

    async def send_text(self, chunk: str) -> AssistantTextAccepted:
        assert self.state == "open"
        self.send_started.set()
        if self.block_send:
            await self.release_send.wait()
        self.trace.append(("text", chunk))
        return AssistantTextAccepted()

    async def complete(self) -> AssistantSinkTerminalResult:
        assert self.state == "open"
        self.complete_started.set()
        if self.block_complete:
            await self.release_complete.wait()
        self.state = "completed"
        self.trace.append("complete")
        return AssistantSinkTerminalResult.COMPLETED

    async def abort(
        self,
        reason: AssistantAbortReason,
        detail: str | None = None,
    ) -> AssistantSinkTerminalResult:
        if self.state == "completed":
            return AssistantSinkTerminalResult.COMPLETED
        self.state = "aborted"
        self.trace.append(("abort", reason, detail))
        return AssistantSinkTerminalResult.ABORTED


class _RecordingInput(InputConversation):
    def __init__(self, *, context: InputConversationContext | None = None) -> None:
        self._context = context or InputConversationContext(
            "conversation-1",
            "session-1",
            ConversationMedium.TEXT,
        )
        self._sink = _RecordingSink()
        self.control: asyncio.Queue[InputControlEvent] = asyncio.Queue()
        self.ended: list[ConversationEnded] = []
        self.processing_updates = 0
        self.request_started = asyncio.Event()
        self.release_request = asyncio.Event()
        self.block_request = False
        self.acknowledged: list[FollowUpRequestCommitted] = []

    @property
    def context(self) -> InputConversationContext:
        return self._context

    @property
    def initial_message(self) -> UserMessage:
        return UserMessage("initial")

    @property
    def assistant_output(self) -> AssistantOutputSink:
        return self._sink

    async def receive_control(self) -> InputControlEvent:
        return await self.control.get()

    async def processing_update(self) -> None:
        self.processing_updates += 1

    async def request_follow_up(self) -> FollowUpRequestCommitted:
        self.request_started.set()
        if self.block_request:
            await self.release_request.wait()
        return FollowUpRequestCommitted("follow-up-1")

    def acknowledge_follow_up_ready(self, token: FollowUpRequestCommitted) -> None:
        self.acknowledged.append(token)

    async def end_conversation(self, event: ConversationEnded) -> None:
        self.ended.append(event)


class _ScriptedConversation(AgentConversation):
    def __init__(self, events: list[object]) -> None:
        self.events: asyncio.Queue[object] = asyncio.Queue()
        for event in events:
            self.events.put_nowait(event)
        self.messages: list[UserMessage] = []
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.block_send = False
        self.receive_waiting = asyncio.Event()
        self.cancelled: list[AgentCancellationReason] = []

    async def send_user_message(self, message: UserMessage) -> AgentInputAccepted:
        self.send_started.set()
        if self.block_send:
            await self.release_send.wait()
        self.messages.append(message)
        return AgentInputAccepted()

    async def receive_event(self):
        if self.events.empty():
            self.receive_waiting.set()
        event = await self.events.get()
        if isinstance(event, BaseException):
            raise event
        return event

    async def cancel(self, reason: AgentCancellationReason) -> AgentCancellationAcknowledged:
        self.cancelled.append(reason)
        return AgentCancellationAcknowledged(reason)


class _Scope(AbstractAsyncContextManager[AgentConversation]):
    def __init__(self, conversation: AgentConversation) -> None:
        self.conversation = conversation
        self.entry_started = asyncio.Event()
        self.release_entry = asyncio.Event()
        self.block_entry = False
        self.exit_count = 0

    async def __aenter__(self) -> AgentConversation:
        self.entry_started.set()
        if self.block_entry:
            await self.release_entry.wait()
        return self.conversation

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exit_count += 1


class _Agent:
    def __init__(self, conversation: AgentConversation) -> None:
        self.scope = _Scope(conversation)

    def open_conversation(self, context: ConversationContext):
        del context
        return self.scope

    async def close(self) -> None:
        return None


async def _run(
    input_conversation: _RecordingInput,
    agent_conversation: _ScriptedConversation,
    *,
    provider=None,
) -> None:
    await bridge_conversation(
        input_conversation=input_conversation,
        agent=_Agent(agent_conversation),
        context_provider=provider or _Provider(),
        settings=SETTINGS,
        fatal_termination=_FatalExit(),
    )


@pytest.mark.parametrize("code", list(ContextRejectionCode))
def test_context_rejection_matrix_preserves_typed_code(code: ContextRejectionCode) -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        await _run(
            input_conversation,
            _ScriptedConversation([]),
            provider=_Provider(ContextRejected(code, "diagnostic")),
        )
        assert input_conversation.ended == [
            ConversationEnded(ConversationEndReason.CONTEXT_REJECTED, code, "diagnostic")
        ]

    asyncio.run(scenario())


def test_context_unavailable_is_recoverable_and_typed() -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        await _run(
            input_conversation,
            _ScriptedConversation([]),
            provider=_Provider(ContextUnavailable("snapshot unavailable")),
        )
        assert input_conversation.ended == [
            ConversationEnded(ConversationEndReason.CONTEXT_UNAVAILABLE, detail="snapshot unavailable")
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("provider", "detail"),
    [
        (_Provider(object()), "context provider returned an invalid result"),
        (_Provider(error=RuntimeError("offline")), "context provider raised"),
    ],
)
def test_malformed_or_exceptional_context_result_is_fatal(provider, detail: str) -> None:
    async def scenario() -> None:
        with pytest.raises(SystemExit, match=detail):
            await _run(_RecordingInput(), _ScriptedConversation([]), provider=provider)

    asyncio.run(scenario())


def test_resolved_context_must_match_immutable_input_scope() -> None:
    async def scenario() -> None:
        mismatched = ContextResolved(
            ConversationContext(
                "different",
                "session-1",
                ConversationMedium.TEXT,
                {},
            )
        )
        with pytest.raises(SystemExit, match="resolved context does not match"):
            await _run(
                _RecordingInput(),
                _ScriptedConversation([]),
                provider=_Provider(mismatched),
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("events", "expected_trace", "processing_updates"),
    [
        ([TurnDisposition(TurnDispositionKind.END_CONVERSATION)], [], 0),
        (
            [
                ProcessingUpdate(),
                ProcessingUpdate(),
                AssistantMessageStarted(),
                AssistantMessageCompleted(),
                TurnDisposition(TurnDispositionKind.END_CONVERSATION),
            ],
            ["start", "complete"],
            2,
        ),
        (
            [
                AssistantMessageStarted(),
                AssistantTextChunk("one"),
                AssistantTextChunk("two"),
                AssistantMessageCompleted(),
                TurnDisposition(TurnDispositionKind.END_CONVERSATION),
            ],
            ["start", ("text", "one"), ("text", "two"), "complete"],
            0,
        ),
    ],
)
def test_successful_turn_matrix_requires_explicit_disposition(
    events: list[object],
    expected_trace: list[object],
    processing_updates: int,
) -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        await _run(input_conversation, _ScriptedConversation(events))
        assert input_conversation._sink.trace == expected_trace
        assert input_conversation.processing_updates == processing_updates
        assert input_conversation.ended == [ConversationEnded(ConversationEndReason.COMPLETED)]

    asyncio.run(scenario())


def test_follow_up_is_one_agent_conversation_and_ack_precedes_delivery() -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        agent_conversation = _ScriptedConversation(
            [
                TurnDisposition(TurnDispositionKind.REQUEST_FOLLOW_UP),
                TurnDisposition(TurnDispositionKind.END_CONVERSATION),
            ]
        )
        bridge = asyncio.create_task(_run(input_conversation, agent_conversation))
        await input_conversation.request_started.wait()
        assert agent_conversation.messages == [UserMessage("initial")]
        while not input_conversation.acknowledged:
            await asyncio.sleep(0)
        input_conversation.control.put_nowait(UserMessage("follow-up"))
        await bridge
        assert input_conversation.acknowledged == [FollowUpRequestCommitted("follow-up-1")]
        assert agent_conversation.messages == [UserMessage("initial"), UserMessage("follow-up")]
        assert input_conversation.ended == [ConversationEnded(ConversationEndReason.COMPLETED)]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("events", "expected_abort"),
    [
        ([AgentConversationFailed("before output")], None),
        ([ProcessingUpdate(), AgentConversationFailed("during progress")], None),
        (
            [AssistantMessageStarted(), AssistantTextChunk("partial"), AgentConversationFailed("during stream")],
            AssistantAbortReason.AGENT_FAILED,
        ),
        ([RuntimeError("receive failed")], None),
    ],
)
def test_agent_failure_matrix_is_typed_and_aborts_only_open_stream(
    events: list[object],
    expected_abort: AssistantAbortReason | None,
) -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        await _run(input_conversation, _ScriptedConversation(events))
        assert input_conversation.ended[-1].reason is ConversationEndReason.AGENT_FAILED
        aborts = [item for item in input_conversation._sink.trace if isinstance(item, tuple) and item[0] == "abort"]
        if expected_abort is None:
            assert aborts == []
        else:
            assert aborts[0][1] is expected_abort

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "events",
    [
        [AssistantTextChunk("outside")],
        [AssistantMessageCompleted()],
        [AssistantMessageStarted(), ProcessingUpdate()],
        [AssistantMessageStarted(), AssistantMessageStarted()],
        [AssistantMessageStarted(), TurnDisposition(TurnDispositionKind.END_CONVERSATION)],
        [
            AssistantMessageStarted(),
            AssistantMessageCompleted(),
            AssistantMessageStarted(),
        ],
    ],
)
def test_illegal_agent_event_sequences_are_fatal(events: list[object]) -> None:
    async def scenario() -> None:
        with pytest.raises(SystemExit):
            await _run(_RecordingInput(), _ScriptedConversation(events))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("input_event", "end_reason", "cancel_reason"),
    [
        (ConversationCancelled(), ConversationEndReason.INPUT_CANCELLED, AgentCancellationReason.INPUT_CANCELLED),
        (InputConversationFailed("capture failed"), ConversationEndReason.INPUT_FAILED, AgentCancellationReason.INPUT_FAILED),
        (InputSessionClosed("disconnected"), ConversationEndReason.INPUT_SESSION_CLOSED, AgentCancellationReason.INPUT_SESSION_CLOSED),
    ],
)
@pytest.mark.parametrize(
    "stage",
    [
        "starting",
        "delivering",
        "waiting_agent",
        "streaming",
        "waiting_disposition",
        "committing_follow_up",
        "waiting_follow_up",
    ],
)
def test_terminal_input_is_observable_in_every_nonterminal_bridge_stage(
    stage: str,
    input_event: InputControlEvent,
    end_reason: ConversationEndReason,
    cancel_reason: AgentCancellationReason,
) -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        if stage == "streaming":
            events = [AssistantMessageStarted(), AssistantTextChunk("blocked")]
            input_conversation._sink.block_send = True
        elif stage == "waiting_disposition":
            events = [AssistantMessageStarted(), AssistantMessageCompleted()]
        elif stage in ("committing_follow_up", "waiting_follow_up"):
            events = [TurnDisposition(TurnDispositionKind.REQUEST_FOLLOW_UP)]
            input_conversation.block_request = stage == "committing_follow_up"
        else:
            events = []
        agent_conversation = _ScriptedConversation(events)
        agent = _Agent(agent_conversation)
        if stage == "starting":
            agent.scope.block_entry = True
        if stage == "delivering":
            agent_conversation.block_send = True

        bridge = asyncio.create_task(
            bridge_conversation(
                input_conversation=input_conversation,
                agent=agent,
                context_provider=_Provider(),
                settings=SETTINGS,
                fatal_termination=_FatalExit(),
            )
        )
        if stage == "starting":
            await agent.scope.entry_started.wait()
        elif stage == "delivering":
            await agent_conversation.send_started.wait()
        elif stage in ("waiting_agent", "waiting_disposition"):
            await agent_conversation.receive_waiting.wait()
        elif stage == "streaming":
            await input_conversation._sink.send_started.wait()
        elif stage == "committing_follow_up":
            await input_conversation.request_started.wait()
        else:
            while not input_conversation.acknowledged:
                await asyncio.sleep(0)

        input_conversation.control.put_nowait(input_event)
        await bridge
        assert input_conversation.ended[-1].reason is end_reason
        if stage == "starting":
            assert agent_conversation.cancelled == []
            assert agent.scope.exit_count == 0
        else:
            assert agent_conversation.cancelled[-1] is cancel_reason
            assert agent.scope.exit_count == 1
        if stage == "streaming":
            abort = next(item for item in input_conversation._sink.trace if item[0] == "abort")
            assert abort[1].value == end_reason.value

    asyncio.run(scenario())


@pytest.mark.parametrize("sink_operation", ["start", "send", "complete"])
@pytest.mark.parametrize(
    ("input_event", "end_reason"),
    [
        (ConversationCancelled(), ConversationEndReason.INPUT_CANCELLED),
        (InputConversationFailed("input failed"), ConversationEndReason.INPUT_FAILED),
        (InputSessionClosed("closed"), ConversationEndReason.INPUT_SESSION_CLOSED),
    ],
)
def test_terminal_input_preempts_each_inflight_sink_operation(
    sink_operation: str,
    input_event: InputControlEvent,
    end_reason: ConversationEndReason,
) -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        sink = input_conversation._sink
        if sink_operation == "start":
            sink.block_start = True
            events = [AssistantMessageStarted()]
            committed = sink.started
        elif sink_operation == "send":
            sink.block_send = True
            events = [AssistantMessageStarted(), AssistantTextChunk("blocked")]
            committed = sink.send_started
        else:
            sink.block_complete = True
            events = [AssistantMessageStarted(), AssistantMessageCompleted()]
            committed = sink.complete_started
        agent_conversation = _ScriptedConversation(events)
        bridge = asyncio.create_task(_run(input_conversation, agent_conversation))
        await committed.wait()
        input_conversation.control.put_nowait(input_event)
        await bridge
        assert input_conversation.ended[-1].reason is end_reason
        assert agent_conversation.cancelled
        if sink_operation == "start":
            assert sink.state == "aborted"
        else:
            assert any(item[0] == "abort" for item in sink.trace if isinstance(item, tuple))

    asyncio.run(scenario())


def test_follow_up_timeout_is_valid_only_after_follow_up_acknowledgement() -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        agent_conversation = _ScriptedConversation(
            [TurnDisposition(TurnDispositionKind.REQUEST_FOLLOW_UP)]
        )
        bridge = asyncio.create_task(_run(input_conversation, agent_conversation))
        while not input_conversation.acknowledged:
            await asyncio.sleep(0)
        input_conversation.control.put_nowait(FollowUpTimedOut())
        await bridge
        assert input_conversation.ended == [
            ConversationEnded(ConversationEndReason.FOLLOW_UP_TIMEOUT)
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("event", [UserMessage("early"), FollowUpTimedOut()])
def test_follow_up_outcomes_before_acknowledged_interval_are_internal_protocol_violations(event) -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        input_conversation.control.put_nowait(event)
        with pytest.raises(SystemExit, match="outside waiting-for-follow-up"):
            await _run(input_conversation, _ScriptedConversation([]))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("input_ready", "operation_result", "operation_failure_wins", "expected"),
    [
        (True, "pending", False, "input"),
        (False, "success", False, "operation"),
        (True, "success", False, "input"),
        (True, RuntimeError("operation failed"), False, "input"),
        (True, RuntimeError("operation failed"), True, "failure"),
    ],
)
def test_race_operation_pairwise_and_multi_ready_precedence(
    input_ready: bool,
    operation_result,
    operation_failure_wins: bool,
    expected: str,
) -> None:
    async def return_or_raise(value):
        if isinstance(value, BaseException):
            raise value
        return value

    async def scenario() -> None:
        input_conversation = _RecordingInput()
        bridge = _ConversationBridge(
            input_conversation=input_conversation,
            agent=_Agent(_ScriptedConversation([])),
            context_provider=_Provider(),
            settings=SETTINGS,
            fatal_termination=_FatalExit(),
        )
        operation = (
            asyncio.create_task(asyncio.Event().wait())
            if operation_result == "pending"
            else asyncio.create_task(return_or_raise(operation_result))
        )
        if input_ready:
            bridge._input_task = asyncio.create_task(
                return_or_raise(ConversationCancelled())
            )
        else:
            bridge._input_task = asyncio.create_task(asyncio.Event().wait())
        if input_ready:
            if operation_result == "pending":
                await asyncio.gather(bridge._input_task, return_exceptions=True)
            else:
                await asyncio.gather(operation, bridge._input_task, return_exceptions=True)
        else:
            await asyncio.gather(operation, return_exceptions=True)
        result = await bridge._race_operation(
            operation,
            operation_failure_wins=operation_failure_wins,
        )
        if expected == "input":
            assert isinstance(result, _InputWon)
        elif expected == "failure":
            assert isinstance(result, RuntimeError)
        else:
            assert result == "success"
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        if bridge._input_task is not None:
            bridge._input_task.cancel()
            await asyncio.gather(bridge._input_task, return_exceptions=True)

    asyncio.run(scenario())


def test_race_operation_deadline_is_selected_only_when_neither_candidate_commits() -> None:
    async def scenario() -> None:
        bridge = _ConversationBridge(
            input_conversation=_RecordingInput(),
            agent=_Agent(_ScriptedConversation([])),
            context_provider=_Provider(),
            settings=SETTINGS,
            fatal_termination=_FatalExit(),
        )
        operation = asyncio.create_task(asyncio.Event().wait())
        bridge._input_task = asyncio.create_task(asyncio.Event().wait())
        result = await bridge._race_operation(operation, timeout_seconds=0.001)
        assert isinstance(result, _DeadlineExpired)
        operation.cancel()
        bridge._input_task.cancel()
        await asyncio.gather(operation, bridge._input_task, return_exceptions=True)

    asyncio.run(scenario())


class _BackpressureAgent(ConversationAgent):
    def __init__(self) -> None:
        self.completion_send_started = asyncio.Event()
        self.completion_send_finished = asyncio.Event()

    async def run_agent_conversation(
        self,
        context: ConversationContext,
        channel: AgentChannel,
    ) -> None:
        del context
        await channel.receive_user_message()
        await channel.start_assistant_message()
        await channel.send_text("blocked")
        self.completion_send_started.set()
        await channel.complete_assistant_message()
        self.completion_send_finished.set()
        await channel.end_conversation()

    async def close(self) -> None:
        return None


def test_bridge_and_agent_output_are_zero_capacity_under_slow_sink() -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        input_conversation._sink.block_send = True
        agent = _BackpressureAgent()
        bridge = asyncio.create_task(
            bridge_conversation(
                input_conversation=input_conversation,
                agent=agent,
                context_provider=_Provider(),
                settings=SETTINGS,
                fatal_termination=_FatalExit(),
            )
        )
        await input_conversation._sink.send_started.wait()
        await agent.completion_send_started.wait()
        assert not agent.completion_send_finished.is_set()
        input_conversation._sink.release_send.set()
        await bridge
        assert agent.completion_send_finished.is_set()

    asyncio.run(scenario())


class _FailsBeforeInputAgent(ConversationAgent):
    async def run_agent_conversation(self, context, channel) -> None:
        del context, channel
        raise RuntimeError("failed before input")

    async def close(self) -> None:
        return None


class _ReturnsBeforeInputAgent(ConversationAgent):
    async def run_agent_conversation(self, context, channel) -> None:
        del context, channel

    async def close(self) -> None:
        return None


class _EmitsBeforeInputAgent(ConversationAgent):
    def __init__(self, operation: str) -> None:
        self.operation = operation

    async def run_agent_conversation(self, context, channel) -> None:
        del context
        if self.operation == "output":
            await channel.start_assistant_message()
        else:
            await channel.end_conversation()

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    "agent",
    [
        _FailsBeforeInputAgent(),
        _ReturnsBeforeInputAgent(),
        _EmitsBeforeInputAgent("output"),
        _EmitsBeforeInputAgent("disposition"),
    ],
)
def test_real_agent_wrapper_failure_before_initial_acceptance_never_deadlocks(agent) -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        await asyncio.wait_for(
            bridge_conversation(
                input_conversation=input_conversation,
                agent=agent,
                context_provider=_Provider(),
                settings=SETTINGS,
                fatal_termination=_FatalExit(),
            ),
            timeout=1,
        )
        assert input_conversation.ended[-1].reason is ConversationEndReason.AGENT_FAILED

    asyncio.run(scenario())


class _FailsBeforeFollowUpAcceptanceAgent(ConversationAgent):
    async def run_agent_conversation(self, context, channel) -> None:
        del context
        await channel.receive_user_message()
        await channel.request_follow_up()
        raise RuntimeError("failed before follow-up input")

    async def close(self) -> None:
        return None


def test_real_agent_wrapper_failure_before_follow_up_acceptance_never_deadlocks() -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        bridge = asyncio.create_task(
            bridge_conversation(
                input_conversation=input_conversation,
                agent=_FailsBeforeFollowUpAcceptanceAgent(),
                context_provider=_Provider(),
                settings=SETTINGS,
                fatal_termination=_FatalExit(),
            )
        )
        while not input_conversation.acknowledged:
            await asyncio.sleep(0)
        input_conversation.control.put_nowait(UserMessage("follow-up"))
        await asyncio.wait_for(bridge, timeout=1)
        assert input_conversation.ended[-1].reason is ConversationEndReason.AGENT_FAILED

    asyncio.run(scenario())


def test_bridge_transition_logs_have_stable_id_old_cause_and_new(caplog) -> None:
    async def scenario() -> None:
        input_conversation = _RecordingInput()
        with caplog.at_level(logging.DEBUG, logger="ai_server.conversations.bridge"):
            await _run(
                input_conversation,
                _ScriptedConversation([TurnDisposition(TurnDispositionKind.END_CONVERSATION)]),
            )
        assert "ConversationBridge[conversation-1]" in caplog.text
        assert "old=starting cause=agent_entry new=delivering_user_message" in caplog.text
        assert "old=ending cause=completed new=closed" in caplog.text
        assert "race ready_set=operation selected=operation" in caplog.text
        assert "conversation_id=conversation-1 input_session_id=session-1 medium=text" in caplog.text
        assert "reason=completed" in caplog.text

    asyncio.run(scenario())
