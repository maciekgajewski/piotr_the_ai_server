from __future__ import annotations

import asyncio

from ai_server.websocket_client.engine import ClientEngine
from ai_server.websocket_client.messages import ExitKind, InputKind, InputResult, InputResultKind
from ai_server.websocket_messages import AssistantMessageCompleted, AssistantMessageStarted, AssistantTextChunk
from ai_server.websocket_messages import ConversationEnded, ConversationReady, ConversationStarted, FollowUpRequested, SessionAccepted
from ai_server.websocket_messages import FollowUpTimedOut, SessionStart, StartConversation


class _Transport:
    def __init__(self, events) -> None:
        self._events = list(events)
        self._sent_event = asyncio.Event()
        self.sent = []
        self.closed = False

    async def receive(self):
        while self._events:
            event = self._events[0]
            if isinstance(event, ConversationStarted) and not any(isinstance(sent, StartConversation) for sent in self.sent):
                self._sent_event.clear()
                await self._sent_event.wait()
                continue
            return self._events.pop(0)
        await asyncio.Future()

    async def send(self, event) -> None:
        self.sent.append(event)
        self._sent_event.set()

    async def close(self) -> None:
        self.closed = True


class _Presenter:
    is_batch = True

    def __init__(self, results) -> None:
        self._results = asyncio.Queue()
        for result in results:
            self._results.put_nowait(result)
        self.system_messages = []
        self.text = []

    async def present_input(self, kind: InputKind) -> float:
        return asyncio.get_running_loop().time()

    async def read_input(self, kind: InputKind) -> InputResult:
        return await self._results.get()

    async def cancel_input(self) -> None:
        return None

    def system(self, text: str) -> None:
        self.system_messages.append(text)

    def assistant_started(self) -> None:
        return None

    def assistant_text(self, text: str) -> None:
        self.text.append(text)

    def assistant_finished(self, aborted_reason: str | None = None) -> None:
        return None


def test_engine_accepts_one_strict_complete_turn() -> None:
    async def run() -> None:
        transport = _Transport((
            SessionAccepted(), ConversationReady(), ConversationStarted("conversation-1"),
            AssistantMessageStarted("message-1"), AssistantTextChunk("message-1", "hello"),
            AssistantMessageCompleted("message-1"), ConversationEnded("completed"),
        ))
        presenter = _Presenter((InputResult(InputResultKind.TEXT, "hello", 0),))
        result = await ClientEngine(transport, presenter, user=None, area="office", follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.COMPLETED
        assert [type(event) for event in transport.sent] == [SessionStart, StartConversation]
        assert presenter.text == ["hello"]
        assert transport.closed

    asyncio.run(run())


def test_engine_rejects_assistant_output_before_conversation_start() -> None:
    async def run() -> None:
        transport = _Transport((SessionAccepted(), AssistantMessageStarted("message-1")))
        result = await ClientEngine(transport, _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.PROTOCOL_ERROR
        assert transport.closed

    asyncio.run(run())


def test_engine_sends_one_follow_up_timeout_after_presented_deadline() -> None:
    class _TimeoutTransport(_Transport):
        def __init__(self) -> None:
            super().__init__((
                SessionAccepted(), ConversationReady(), ConversationStarted("conversation-1"), FollowUpRequested(),
            ))
            self._terminal_at: float | None = None

        async def receive(self):
            if self._events:
                return await super().receive()
            loop = asyncio.get_running_loop()
            if self._terminal_at is None:
                self._terminal_at = loop.time() + 0.02
            await asyncio.sleep(max(0, self._terminal_at - loop.time()))
            return ConversationEnded("follow_up_timeout")

    async def run() -> None:
        transport = _TimeoutTransport()
        presenter = _Presenter((InputResult(InputResultKind.TEXT, "first", 0),))
        result = await asyncio.wait_for(
            ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=0.001,
                         stop_event=asyncio.Event()).run(),
            timeout=1,
        )
        assert result.kind is ExitKind.COMPLETED
        assert sum(isinstance(event, FollowUpTimedOut) for event in transport.sent) == 1

    asyncio.run(run())
