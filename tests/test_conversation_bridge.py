import asyncio
from contextlib import AbstractAsyncContextManager

import pytest

from ai_server.agent.echo import EchoAgent
from ai_server.conversations.bridge import BridgeSettings, FatalTerminationController, bridge_conversation
from ai_server.conversations.context_provider import ConfigContextProvider
from ai_server.conversations.contexts import ConversationContext, ConversationMedium, InputConversationContext
from ai_server.conversations.interfaces import AgentConversation, AssistantOutputSink, InputConversation
from ai_server.conversations.messages import AgentCancellationAcknowledged, AgentCancellationReason
from ai_server.conversations.messages import AgentInputAccepted, AssistantAbortReason, AssistantSinkStarted
from ai_server.conversations.messages import AssistantSinkTerminalResult, AssistantTextAccepted
from ai_server.conversations.messages import ConversationCancelled, ConversationEndReason, ConversationEnded
from ai_server.conversations.messages import FollowUpRequestCommitted
from ai_server.conversations.messages import InputControlEvent, TurnDisposition, TurnDispositionKind, UserMessage
from ai_server.conversations.rendezvous import Rendezvous


class _FatalExit(FatalTerminationController):
    async def terminate(self, detail: str):
        raise SystemExit(detail)


class _Sink(AssistantOutputSink):
    def __init__(
        self,
        completion: AssistantSinkTerminalResult = AssistantSinkTerminalResult.COMPLETED,
    ) -> None:
        self.completion = completion
        self.open = False

    async def start(self):
        self.open = True
        return AssistantSinkStarted()

    async def send_text(self, chunk: str):
        assert self.open and chunk
        return AssistantTextAccepted()

    async def complete(self):
        self.open = False
        return self.completion

    async def abort(self, reason: AssistantAbortReason, detail: str | None = None):
        del reason, detail
        self.open = False
        return AssistantSinkTerminalResult.ABORTED


class _Input(InputConversation):
    def __init__(self, sink: _Sink | None = None) -> None:
        self._context = InputConversationContext("conversation-1", "session-1", ConversationMedium.TEXT)
        self._sink = sink or _Sink()
        self.control: asyncio.Queue[InputControlEvent] = asyncio.Queue()
        self.ended: list[ConversationEnded] = []

    @property
    def context(self):
        return self._context

    @property
    def initial_message(self):
        return UserMessage("hello")

    @property
    def assistant_output(self):
        return self._sink

    async def receive_control(self):
        return await self.control.get()

    async def processing_update(self):
        return None

    async def request_follow_up(self):
        return FollowUpRequestCommitted("follow-up-1")

    def acknowledge_follow_up_ready(self, token):
        assert token == FollowUpRequestCommitted("follow-up-1")

    async def end_conversation(self, event):
        self.ended.append(event)


class _HangingEntryScope(AbstractAsyncContextManager[AgentConversation]):
    async def __aenter__(self):
        await asyncio.Event().wait()

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class _HangingEntryAgent:
    def open_conversation(self, context: ConversationContext):
        del context
        return _HangingEntryScope()

    async def close(self):
        return None


class _EndConversation(AgentConversation):
    async def send_user_message(self, message: UserMessage):
        del message
        return AgentInputAccepted()

    async def receive_event(self):
        return TurnDisposition(TurnDispositionKind.END_CONVERSATION)

    async def cancel(self, reason: AgentCancellationReason):
        return AgentCancellationAcknowledged(reason)


class _HangingExitScope(AbstractAsyncContextManager[AgentConversation]):
    async def __aenter__(self):
        return _EndConversation()

    async def __aexit__(self, exc_type, exc, traceback):
        await asyncio.Event().wait()


class _HangingExitAgent:
    def open_conversation(self, context: ConversationContext):
        del context
        return _HangingExitScope()

    async def close(self):
        return None


class _HangingCancellationConversation(AgentConversation):
    def __init__(self) -> None:
        self.accepted = asyncio.Event()

    async def send_user_message(self, message: UserMessage):
        del message
        self.accepted.set()
        return AgentInputAccepted()

    async def receive_event(self):
        await asyncio.Event().wait()

    async def cancel(self, reason: AgentCancellationReason):
        del reason
        await asyncio.Event().wait()


class _ImmediateScope(AbstractAsyncContextManager[AgentConversation]):
    def __init__(self, conversation: AgentConversation) -> None:
        self._conversation = conversation

    async def __aenter__(self):
        return self._conversation

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class _SingleConversationAgent:
    def __init__(self, conversation: AgentConversation) -> None:
        self._conversation = conversation

    def open_conversation(self, context: ConversationContext):
        del context
        return _ImmediateScope(self._conversation)

    async def close(self):
        return None


def test_agent_entry_deadline_invokes_process_fatal_controller() -> None:
    async def run() -> None:
        with pytest.raises(SystemExit, match="agent entry deadline exceeded"):
            await bridge_conversation(
                input_conversation=_Input(),
                agent=_HangingEntryAgent(),
                context_provider=ConfigContextProvider(),
                settings=BridgeSettings(0.01, 0.01),
                fatal_termination=_FatalExit(),
            )

    asyncio.run(run())


def test_agent_scope_exit_deadline_invokes_process_fatal_controller() -> None:
    async def run() -> None:
        with pytest.raises(SystemExit, match="agent scope exit deadline exceeded"):
            await bridge_conversation(
                input_conversation=_Input(),
                agent=_HangingExitAgent(),
                context_provider=ConfigContextProvider(),
                settings=BridgeSettings(0.01, 0.01),
                fatal_termination=_FatalExit(),
            )

    asyncio.run(run())


def test_agent_cancellation_deadline_invokes_process_fatal_controller() -> None:
    async def run() -> None:
        input_conversation = _Input()
        agent_conversation = _HangingCancellationConversation()
        async def cancel_after_acceptance() -> None:
            await agent_conversation.accepted.wait()
            input_conversation.control.put_nowait(ConversationCancelled())

        trigger = asyncio.create_task(cancel_after_acceptance())
        with pytest.raises(SystemExit, match="cancellation acknowledgement deadline exceeded"):
            await bridge_conversation(
                input_conversation=input_conversation,
                agent=_SingleConversationAgent(agent_conversation),
                context_provider=ConfigContextProvider(),
                settings=BridgeSettings(0.01, 0.01),
                fatal_termination=_FatalExit(),
            )
        await trigger

    asyncio.run(run())


def test_aborted_completion_is_internal_fatal_instead_of_success() -> None:
    async def run() -> None:
        input_conversation = _Input(_Sink(AssistantSinkTerminalResult.ABORTED))
        with pytest.raises(SystemExit, match="aborted completion"):
            await bridge_conversation(
                input_conversation=input_conversation,
                agent=EchoAgent(),
                context_provider=ConfigContextProvider(),
                settings=BridgeSettings(0.1, 0.01),
                fatal_termination=_FatalExit(),
            )
        assert not any(event.reason is ConversationEndReason.COMPLETED for event in input_conversation.ended)

    asyncio.run(run())


def test_input_session_closed_completion_stays_typed() -> None:
    async def run() -> _Input:
        input_conversation = _Input(_Sink(AssistantSinkTerminalResult.INPUT_SESSION_CLOSED))
        await bridge_conversation(
            input_conversation=input_conversation,
            agent=EchoAgent(),
            context_provider=ConfigContextProvider(),
            settings=BridgeSettings(0.1, 0.01),
        )
        return input_conversation

    result = asyncio.run(run())
    assert result.ended[-1].reason is ConversationEndReason.INPUT_SESSION_CLOSED


def test_rendezvous_cancellation_before_acceptance_removes_offer() -> None:
    async def run() -> None:
        rendezvous: Rendezvous[str] = Rendezvous()
        sender = asyncio.create_task(rendezvous.send("value"))
        await asyncio.sleep(0)
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender
        receiver = asyncio.create_task(rendezvous.receive())
        await asyncio.sleep(0)
        assert not receiver.done()
        receiver.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receiver

    asyncio.run(run())


def test_rendezvous_acceptance_commit_survives_sender_cancellation() -> None:
    async def run() -> None:
        rendezvous: Rendezvous[str] = Rendezvous()
        sender = asyncio.create_task(rendezvous.send("value"))
        await asyncio.sleep(0)
        assert await rendezvous.receive() == "value"
        sender.cancel()
        await sender

    asyncio.run(run())
