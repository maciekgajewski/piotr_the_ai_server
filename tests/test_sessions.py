import asyncio
from contextlib import AbstractAsyncContextManager

import pytest

from ai_server.agent.echo import EchoAgent
from ai_server.conversations.bridge import BridgeSettings, bridge_conversation
from ai_server.conversations.context_provider import ConfigContextProvider
from ai_server.conversations.contexts import ConversationMedium, InputConversationContext
from ai_server.conversations.interfaces import AssistantOutputSink, InputConversation
from ai_server.conversations.messages import AssistantAbortReason, AssistantSinkStarted, AssistantSinkTerminalResult
from ai_server.conversations.messages import AssistantTextAccepted, ConversationCancelled, ConversationEnded
from ai_server.conversations.messages import ConversationEndReason, FollowUpRequestCommitted, InputControlEvent
from ai_server.conversations.messages import UserMessage
from ai_server.conversations.rendezvous import Rendezvous


SETTINGS = BridgeSettings(1.0, 0.1)


class FakeSink(AssistantOutputSink):
    def __init__(self) -> None:
        self.events = []
        self.state = "not_started"

    async def start(self):
        assert self.state == "not_started"
        self.state = "open"
        self.events.append("start")
        return AssistantSinkStarted()

    async def send_text(self, chunk: str):
        assert self.state == "open"
        self.events.append(("text", chunk))
        return AssistantTextAccepted()

    async def complete(self):
        assert self.state == "open"
        self.state = "completed"
        self.events.append("complete")
        return AssistantSinkTerminalResult.COMPLETED

    async def abort(self, reason: AssistantAbortReason, detail: str | None = None):
        if self.state == "completed":
            return AssistantSinkTerminalResult.COMPLETED
        self.state = "aborted"
        self.events.append(("abort", reason, detail))
        return AssistantSinkTerminalResult.ABORTED


class FakeInputConversation(InputConversation):
    def __init__(self, text: str = "hello") -> None:
        self._context = InputConversationContext("c1", "s1", ConversationMedium.TEXT)
        self._initial_message = UserMessage(text)
        self._sink = FakeSink()
        self.control: asyncio.Queue[InputControlEvent] = asyncio.Queue()
        self.processing_updates = 0
        self.ended: list[ConversationEnded] = []
        self.follow_up_token = FollowUpRequestCommitted("f1")
        self.acknowledged = False

    @property
    def context(self):
        return self._context

    @property
    def initial_message(self):
        return self._initial_message

    @property
    def assistant_output(self):
        return self._sink

    async def receive_control(self):
        return await self.control.get()

    async def processing_update(self):
        self.processing_updates += 1

    async def request_follow_up(self):
        return self.follow_up_token

    def acknowledge_follow_up_ready(self, token):
        assert token == self.follow_up_token
        self.acknowledged = True

    async def end_conversation(self, event):
        self.ended.append(event)


def test_bridge_runs_single_turn_and_closes_every_scope() -> None:
    async def run() -> FakeInputConversation:
        input_conversation = FakeInputConversation("hello")
        await bridge_conversation(
            input_conversation=input_conversation,
            agent=EchoAgent(),
            context_provider=ConfigContextProvider(),
            settings=SETTINGS,
        )
        return input_conversation

    result = asyncio.run(run())
    assert result.assistant_output.events == ["start", ("text", "hello"), "complete"]
    assert result.ended == [ConversationEnded(ConversationEndReason.COMPLETED)]


def test_input_cancellation_preempts_agent_work() -> None:
    async def run() -> FakeInputConversation:
        input_conversation = FakeInputConversation("hello")
        input_conversation.control.put_nowait(ConversationCancelled())
        await bridge_conversation(
            input_conversation=input_conversation,
            agent=EchoAgent(),
            context_provider=ConfigContextProvider(),
            settings=SETTINGS,
        )
        return input_conversation

    result = asyncio.run(run())
    assert result.ended == [ConversationEnded(ConversationEndReason.INPUT_CANCELLED)]


def test_unknown_user_is_typed_context_rejection() -> None:
    async def run() -> FakeInputConversation:
        input_conversation = FakeInputConversation()
        input_conversation._context = InputConversationContext(
            "c1", "s1", ConversationMedium.TEXT, user="Unknown"
        )
        await bridge_conversation(
            input_conversation=input_conversation,
            agent=EchoAgent(),
            context_provider=ConfigContextProvider({"Maciek": {}}),
            settings=SETTINGS,
        )
        return input_conversation

    result = asyncio.run(run())
    assert result.ended[0].reason is ConversationEndReason.CONTEXT_REJECTED
    assert result.ended[0].context_rejection_code.value == "unknown_user"


def test_zero_capacity_rendezvous_blocks_producer_until_receive() -> None:
    async def run() -> None:
        rendezvous: Rendezvous[str] = Rendezvous()
        producer = asyncio.create_task(rendezvous.send("value"))
        await asyncio.sleep(0)
        assert not producer.done()
        assert await rendezvous.receive() == "value"
        await producer

    asyncio.run(run())


def test_terminal_payload_rejects_invalid_context_code_shape() -> None:
    with pytest.raises(ValueError):
        ConversationEnded(ConversationEndReason.CONTEXT_REJECTED)
