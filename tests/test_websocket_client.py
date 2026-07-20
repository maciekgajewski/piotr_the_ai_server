from __future__ import annotations

import asyncio
import logging

import pytest

from ai_server.websocket_client.aiohttp_transport import TransportDisconnected
from ai_server.websocket_client.batch_presenter import BatchPresenter
from ai_server.websocket_client.engine import ClientEngine
from ai_server.websocket_client.messages import ExitKind, InputKind, InputResult, InputResultKind
from ai_server.websocket_client.state import ClientState, StreamState
from ai_server.websocket_messages import AssistantMessageAborted, AssistantMessageCompleted, AssistantMessageStarted, AssistantTextChunk
from ai_server.websocket_messages import ConversationEnded, ConversationReady, ConversationStarted, FollowUpMessage, FollowUpRequested
from ai_server.websocket_messages import ProcessingUpdate, ProtocolRejected
from ai_server.websocket_messages import ProtocolRejectionCode, SessionAccepted
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
            if isinstance(event, ConversationStarted):
                required_starts = 1 if event.conversation_id == "conversation-1" else 2
                if sum(isinstance(sent, StartConversation) for sent in self.sent) < required_starts:
                    self._sent_event.clear()
                    await self._sent_event.wait()
                    continue
            return self._events.pop(0)
        await asyncio.Future()

    async def send(self, event) -> None:
        self.sent.append(event)
        self._sent_event.set()

    def commit_send(self, event):
        return asyncio.create_task(self.send(event))

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

    @property
    def has_pending_messages(self) -> bool:
        return not self._results.empty()

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


def test_engine_accepts_processing_update_and_aborted_assistant_stream() -> None:
    async def run() -> None:
        transport = _Transport((
            SessionAccepted(), ConversationReady(), ConversationStarted("conversation-1"), ProcessingUpdate(),
            AssistantMessageStarted("message-1"), AssistantMessageAborted("message-1", "agent_failed"),
            ConversationEnded("completed"),
        ))
        presenter = _Presenter((InputResult(InputResultKind.TEXT, "hello", 0),))
        result = await ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.COMPLETED
        assert presenter.system_messages == ["processing..."]

    asyncio.run(run())


def test_engine_does_not_commit_session_start_after_handoff_interruption() -> None:
    async def run() -> None:
        transport = _Transport(())
        stop_event = asyncio.Event()
        stop_event.set()
        result = await ClientEngine(transport, _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=stop_event).run()
        assert result.kind is ExitKind.INTERRUPTED
        assert transport.sent == []

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


def test_batch_engine_sends_each_scripted_message_on_its_own_ready_offer() -> None:
    async def run() -> None:
        transport = _Transport((
            SessionAccepted(), ConversationReady(), ConversationStarted("conversation-1"), ConversationEnded("completed"),
            ConversationReady(), ConversationStarted("conversation-2"), ConversationEnded("completed"),
        ))
        presenter = _Presenter((
            InputResult(InputResultKind.TEXT, "first", 0), InputResult(InputResultKind.TEXT, "second", 1),
        ))
        result = await ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.COMPLETED
        assert [event.message for event in transport.sent if isinstance(event, StartConversation)] == ["first", "second"]

    asyncio.run(run())


def test_engine_rejects_terminal_event_while_assistant_stream_is_open() -> None:
    async def run() -> None:
        transport = _Transport((
            SessionAccepted(), ConversationReady(), ConversationStarted("conversation-1"), AssistantMessageStarted("message-1"),
            ConversationEnded("completed"),
        ))
        result = await ClientEngine(transport, _Presenter((InputResult(InputResultKind.TEXT, "first", 0),)),
                                    user=None, area=None, follow_up_timeout_seconds=15, stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.PROTOCOL_ERROR

    asyncio.run(run())


def test_engine_rejects_reused_conversation_id() -> None:
    async def run() -> None:
        transport = _Transport((
            SessionAccepted(), ConversationReady(), ConversationStarted("conversation-1"), ConversationEnded("completed"),
            ConversationReady(), ConversationStarted("conversation-1"),
        ))
        presenter = _Presenter((InputResult(InputResultKind.TEXT, "one", 0), InputResult(InputResultKind.TEXT, "two", 1)))
        result = await ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.PROTOCOL_ERROR

    asyncio.run(run())


def test_engine_rejects_assistant_stream_id_reused_in_later_conversation() -> None:
    async def run() -> None:
        transport = _Transport((
            SessionAccepted(), ConversationReady(), ConversationStarted("conversation-1"),
            AssistantMessageStarted("message-1"), AssistantMessageCompleted("message-1"), ConversationEnded("completed"),
            ConversationReady(), ConversationStarted("conversation-2"), AssistantMessageStarted("message-1"),
        ))
        presenter = _Presenter((
            InputResult(InputResultKind.TEXT, "one", 0), InputResult(InputResultKind.TEXT, "two", 1),
        ))
        result = await ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.PROTOCOL_ERROR
        assert transport.closed

    asyncio.run(run())


@pytest.mark.parametrize("input_kind", [InputResultKind.EXIT, InputResultKind.EOF])
@pytest.mark.parametrize(
    "terminal_result",
    [
        ProtocolRejected(code=ProtocolRejectionCode.INVALID_STATE),
        ConversationEnded("completed"),
        TransportDisconnected("peer closed"),
    ],
)
def test_local_exit_and_eof_beat_simultaneous_terminal_server_input(input_kind, terminal_result) -> None:
    class _RaceTransport(_Transport):
        def __init__(self) -> None:
            super().__init__(())
            self.receive_started = asyncio.Event()
            self.release_terminal = asyncio.Event()

        async def receive(self):
            self.receive_started.set()
            await self.release_terminal.wait()
            if isinstance(terminal_result, Exception):
                raise terminal_result
            return terminal_result

    async def read_input(release_input: asyncio.Event) -> InputResult:
        await release_input.wait()
        return InputResult(input_kind, None, 0)

    async def run() -> None:
        transport = _RaceTransport()
        engine = ClientEngine(transport, _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                              stop_event=asyncio.Event())
        engine._state = ClientState.AWAITING_FOLLOW_UP_INPUT
        release_input = asyncio.Event()
        engine._input_task = asyncio.create_task(read_input(release_input))
        next_result = asyncio.create_task(engine._next_result())
        await transport.receive_started.wait()
        release_input.set()
        transport.release_terminal.set()
        await asyncio.sleep(0)
        result = await next_result
        assert result is not None and result.kind is ExitKind.EXITED
        assert transport.sent == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "terminal_result",
    [
        ProtocolRejected(code=ProtocolRejectionCode.INVALID_STATE),
        ConversationEnded("completed"),
        TransportDisconnected("peer closed"),
    ],
)
def test_terminal_server_input_beats_simultaneous_read_input_failure(terminal_result) -> None:
    class _RaceTransport(_Transport):
        def __init__(self) -> None:
            super().__init__(())
            self.receive_started = asyncio.Event()
            self.release_terminal = asyncio.Event()

        async def receive(self):
            self.receive_started.set()
            await self.release_terminal.wait()
            if isinstance(terminal_result, Exception):
                raise terminal_result
            return terminal_result

    async def failing_input(release_input: asyncio.Event) -> InputResult:
        await release_input.wait()
        raise OSError("terminal input failed")

    async def run() -> None:
        transport = _RaceTransport()
        engine = ClientEngine(transport, _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                              stop_event=asyncio.Event())
        engine._state = ClientState.AWAITING_FOLLOW_UP_INPUT
        release_input = asyncio.Event()
        engine._input_task = asyncio.create_task(failing_input(release_input))
        next_result = asyncio.create_task(engine._next_result())
        await transport.receive_started.wait()
        release_input.set()
        transport.release_terminal.set()
        await asyncio.sleep(0)
        if isinstance(terminal_result, TransportDisconnected):
            with pytest.raises(TransportDisconnected, match="peer closed"):
                await next_result
        else:
            result = await next_result
            if isinstance(terminal_result, ProtocolRejected):
                assert result is not None and result.kind is ExitKind.REJECTED
            else:
                assert result is not None and result.kind is ExitKind.COMPLETED

    asyncio.run(run())


def test_injected_clock_drives_end_to_end_follow_up_timeout() -> None:
    class _Clock:
        def __init__(self) -> None:
            self.now = 10.0
            self.waiters: list[tuple[float, asyncio.Future[None]]] = []

        def monotonic(self) -> float:
            return self.now

        async def sleep_until(self, deadline: float) -> None:
            waiter = asyncio.get_running_loop().create_future()
            self.waiters.append((deadline, waiter))
            try:
                await waiter
            finally:
                if (deadline, waiter) in self.waiters:
                    self.waiters.remove((deadline, waiter))

        def advance_to(self, value: float) -> None:
            self.now = value
            for deadline, waiter in tuple(self.waiters):
                if deadline <= value and not waiter.done():
                    waiter.set_result(None)

    class _TimeoutTransport(_Transport):
        async def receive(self):
            if self._events:
                return await super().receive()
            while not any(isinstance(event, FollowUpTimedOut) for event in self.sent):
                self._sent_event.clear()
                await self._sent_event.wait()
            return ConversationEnded("follow_up_timeout")

    async def run() -> None:
        clock = _Clock()
        transport = _TimeoutTransport((
            SessionAccepted(), ConversationReady(), ConversationStarted("conversation-1"), FollowUpRequested(),
        ))
        result_task = asyncio.create_task(
            ClientEngine(transport, BatchPresenter(("first",), clock), user=None, area=None,
                         follow_up_timeout_seconds=15, stop_event=asyncio.Event(), clock=clock).run()
        )
        while not clock.waiters:
            await asyncio.sleep(0)
        assert [deadline for deadline, _ in clock.waiters] == [25.0]
        clock.advance_to(25.0)
        result = await result_task
        assert result.kind is ExitKind.COMPLETED
        assert [type(event) for event in transport.sent] == [SessionStart, StartConversation, FollowUpTimedOut]

    asyncio.run(run())


def test_batch_completion_without_messages_sends_no_user_event() -> None:
    class _EmptyBatchPresenter(_Presenter):
        async def read_input(self, kind: InputKind) -> InputResult:
            assert kind is InputKind.START
            return InputResult(InputResultKind.BATCH_COMPLETE, None, 0)

    async def run() -> None:
        transport = _Transport((SessionAccepted(), ConversationReady()))
        presenter = _EmptyBatchPresenter(())
        result = await ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.COMPLETED
        assert transport.sent == [SessionStart()]

    asyncio.run(run())


def test_engine_logs_stable_event_and_send_context(caplog) -> None:
    async def run() -> None:
        transport = _Transport((SessionAccepted(), AssistantMessageStarted("message-1")))
        with caplog.at_level(logging.DEBUG, logger="ai_server.websocket_client.engine"):
            result = await ClientEngine(transport, _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                                        stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.PROTOCOL_ERROR

    asyncio.run(run())
    assert "session start" in caplog.text
    assert "send committed event=SessionStart" in caplog.text
    assert "received event=SessionAccepted state=handshaking" in caplog.text


def test_terminal_event_cancels_and_clears_pending_prompt_presentation() -> None:
    class _BlockingPresenter(_Presenter):
        def __init__(self) -> None:
            super().__init__(())
            self.presentation_started = asyncio.Event()
            self.presentation_cancelled = False
            self.read_called = False

        async def present_input(self, kind: InputKind) -> float:
            self.presentation_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.presentation_cancelled = True
                raise

        async def read_input(self, kind: InputKind) -> InputResult:
            self.read_called = True
            return await super().read_input(kind)

    class _PresentationRaceTransport(_Transport):
        def __init__(self, events, presenter) -> None:
            super().__init__(events)
            self._presenter = presenter

        async def receive(self):
            if self._events and isinstance(self._events[0], ProtocolRejected):
                await self._presenter.presentation_started.wait()
            return await super().receive()

    async def run() -> None:
        presenter = _BlockingPresenter()
        transport = _PresentationRaceTransport(
            (SessionAccepted(), ConversationReady(), ProtocolRejected(code=ProtocolRejectionCode.INVALID_STATE)), presenter,
        )
        result = await ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.REJECTED
        assert presenter.presentation_cancelled
        assert not presenter.read_called

    asyncio.run(run())


def test_engine_reports_known_unsent_failure_at_commit_boundary() -> None:
    class _UnsentTransport(_Transport):
        def commit_send(self, event):
            raise OSError("send queue unavailable")

    async def run() -> None:
        result = await ClientEngine(_UnsentTransport(()), _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.KNOWN_UNSENT

    asyncio.run(run())


def test_engine_reports_uncertain_delivery_after_commit() -> None:
    class _UncertainTransport(_Transport):
        def commit_send(self, event):
            async def fail_after_commit() -> None:
                await asyncio.sleep(0)
                raise OSError("connection lost after queue commit")

            return asyncio.create_task(fail_after_commit())

    async def run() -> None:
        result = await ClientEngine(_UncertainTransport(()), _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.DELIVERY_UNCERTAIN

    asyncio.run(run())


def test_disconnect_during_prompt_presentation_cancels_it_without_reading_input() -> None:
    class _BlockingPresenter(_Presenter):
        def __init__(self) -> None:
            super().__init__(())
            self.started = asyncio.Event()
            self.cancelled = False

        async def present_input(self, kind: InputKind) -> float:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class _DisconnectTransport(_Transport):
        def __init__(self, events, presenter) -> None:
            super().__init__(events)
            self._presenter = presenter

        async def receive(self):
            if not self._events:
                await self._presenter.started.wait()
                raise TransportDisconnected("peer closed")
            return await super().receive()

    async def run() -> None:
        presenter = _BlockingPresenter()
        transport = _DisconnectTransport((SessionAccepted(), ConversationReady()), presenter)
        result = await ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event()).run()
        assert result.kind is ExitKind.CONNECTION_LOST
        assert presenter.cancelled

    asyncio.run(run())


def test_interruption_during_prompt_presentation_cancels_it_without_send() -> None:
    class _BlockingPresenter(_Presenter):
        def __init__(self) -> None:
            super().__init__(())
            self.started = asyncio.Event()
            self.cancelled = False

        async def present_input(self, kind: InputKind) -> float:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class _IdleTransport(_Transport):
        async def receive(self):
            if self._events:
                return await super().receive()
            await asyncio.Future()

    async def run() -> None:
        presenter = _BlockingPresenter()
        stop_event = asyncio.Event()
        transport = _IdleTransport((SessionAccepted(), ConversationReady()))

        async def interrupt_after_presentation() -> None:
            await presenter.started.wait()
            stop_event.set()

        interrupter = asyncio.create_task(interrupt_after_presentation())
        result = await ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=stop_event).run()
        await interrupter
        assert result.kind is ExitKind.INTERRUPTED
        assert presenter.cancelled
        assert transport.sent == [SessionStart()]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("state", "event"),
    [
        (ClientState.HANDSHAKING, ConversationReady()),
        (ClientState.WAITING_FOR_READY, SessionAccepted()),
        (ClientState.AWAITING_START_INPUT, ConversationStarted("conversation-1")),
        (ClientState.WAITING_FOR_CONVERSATION_END, FollowUpRequested()),
        (ClientState.WAITING_FOR_READY, ConversationEnded("completed")),
    ],
)
def test_engine_fails_closed_for_representative_invalid_state_event_pairs(state, event) -> None:
    async def run() -> None:
        engine = ClientEngine(_Transport(()), _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                              stop_event=asyncio.Event())
        engine._state = state
        with pytest.raises(Exception, match="invalid"):
            await engine._handle_server(event)

    asyncio.run(run())


def test_engine_rejects_second_assistant_stream_and_post_terminal_processing() -> None:
    async def run() -> None:
        engine = ClientEngine(_Transport(()), _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                              stop_event=asyncio.Event())
        engine._state = ClientState.WAITING_FOR_AGENT
        engine._stream = StreamState.FINISHED
        engine._stream_id = "message-1"
        with pytest.raises(Exception, match="invalid"):
            await engine._handle_server(AssistantMessageStarted("message-2"))
        with pytest.raises(Exception, match="invalid"):
            await engine._handle_server(ProcessingUpdate())
        with pytest.raises(Exception, match="does not match"):
            await engine._handle_server(AssistantTextChunk("message-1", "late"))

    asyncio.run(run())


def test_follow_up_submission_at_deadline_wins_and_after_deadline_times_out() -> None:
    class _Clock:
        def monotonic(self) -> float:
            return 10.0

    async def run() -> None:
        equal_transport = _Transport(())
        equal_engine = ClientEngine(equal_transport, _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                                    stop_event=asyncio.Event(), clock=_Clock())
        equal_engine._state = ClientState.AWAITING_FOLLOW_UP_INPUT
        equal_engine._deadline = 10.0
        await equal_engine._handle_input(InputResult(InputResultKind.TEXT, "on time", 10.0))
        assert isinstance(equal_transport.sent[-1], FollowUpMessage)
        assert equal_engine._state is ClientState.WAITING_FOR_AGENT

        late_transport = _Transport(())
        late_engine = ClientEngine(late_transport, _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                                   stop_event=asyncio.Event(), clock=_Clock())
        late_engine._state = ClientState.AWAITING_FOLLOW_UP_INPUT
        late_engine._deadline = 10.0
        await late_engine._handle_input(InputResult(InputResultKind.TEXT, "late", 10.1))
        assert isinstance(late_transport.sent[-1], FollowUpTimedOut)
        assert late_engine._state is ClientState.WAITING_FOR_CONVERSATION_END

    asyncio.run(run())


@pytest.mark.parametrize("kind", [InputResultKind.EMPTY, InputResultKind.HELP, InputResultKind.UNKNOWN_COMMAND])
def test_local_follow_up_redraw_outcomes_do_not_move_absolute_deadline(kind: InputResultKind) -> None:
    async def run() -> None:
        presenter = _Presenter(())
        engine = ClientEngine(_Transport(()), presenter, user=None, area=None, follow_up_timeout_seconds=15,
                              stop_event=asyncio.Event())
        engine._state = ClientState.AWAITING_FOLLOW_UP_INPUT
        engine._deadline = 123.0
        await engine._handle_input(InputResult(kind, "/unknown" if kind is InputResultKind.UNKNOWN_COMMAND else None, 120.0))
        assert engine._deadline == 123.0
        assert engine._state is ClientState.AWAITING_FOLLOW_UP_INPUT
        await engine._cancel_input()

    asyncio.run(run())


@pytest.mark.parametrize("kind", [InputResultKind.EMPTY, InputResultKind.HELP, InputResultKind.UNKNOWN_COMMAND])
def test_simultaneous_follow_up_deadline_beats_redraw_only_input(kind: InputResultKind) -> None:
    async def input_result() -> InputResult:
        return InputResult(kind, "/unknown" if kind is InputResultKind.UNKNOWN_COMMAND else None, 10.0)

    async def run() -> None:
        transport = _Transport(())
        engine = ClientEngine(transport, _Presenter(()), user=None, area=None, follow_up_timeout_seconds=15,
                              stop_event=asyncio.Event())
        engine._state = ClientState.AWAITING_FOLLOW_UP_INPUT
        engine._deadline = asyncio.get_running_loop().time()
        engine._input_task = asyncio.create_task(input_result())
        await asyncio.sleep(0)
        await engine._next_result()
        assert isinstance(transport.sent[-1], FollowUpTimedOut)
        assert engine._state is ClientState.WAITING_FOR_CONVERSATION_END

    asyncio.run(run())


@pytest.mark.parametrize("kind", [InputResultKind.EMPTY, InputResultKind.HELP, InputResultKind.UNKNOWN_COMMAND])
def test_follow_up_redraw_that_crosses_deadline_times_out_before_returning(kind: InputResultKind) -> None:
    class _Clock:
        now = 9.0

        def monotonic(self) -> float:
            return self.now

        async def sleep_until(self, deadline: float) -> None:
            await asyncio.Future()

    clock = _Clock()

    class _RedrawPresenter(_Presenter):
        is_batch = False

        def __init__(self) -> None:
            super().__init__(())
            self.redraw_started = asyncio.Event()
            self.release_redraw = asyncio.Event()

        async def present_input(self, input_kind: InputKind) -> float:
            self.redraw_started.set()
            await self.release_redraw.wait()
            return clock.now

    async def input_result() -> InputResult:
        return InputResult(kind, "/unknown" if kind is InputResultKind.UNKNOWN_COMMAND else None, 9.0)

    async def run() -> None:
        transport = _Transport(())
        presenter = _RedrawPresenter()
        engine = ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                              stop_event=asyncio.Event(), clock=clock)
        engine._state = ClientState.AWAITING_FOLLOW_UP_INPUT
        engine._deadline = 10.0
        engine._input_task = asyncio.create_task(input_result())
        await asyncio.sleep(0)
        next_result = asyncio.create_task(engine._next_result())
        await presenter.redraw_started.wait()
        clock.now = 11.0
        presenter.release_redraw.set()
        await next_result
        assert isinstance(transport.sent[-1], FollowUpTimedOut)
        assert engine._state is ClientState.WAITING_FOR_CONVERSATION_END
        assert engine._input_task is None

    asyncio.run(run())


@pytest.mark.parametrize("kind", [InputResultKind.EMPTY, InputResultKind.HELP, InputResultKind.UNKNOWN_COMMAND])
def test_terminal_conversation_end_wins_while_follow_up_redraw_is_blocked(kind: InputResultKind) -> None:
    class _RedrawPresenter(_Presenter):
        is_batch = False

        def __init__(self) -> None:
            super().__init__(())
            self.redraw_started = asyncio.Event()
            self.cancelled = False

        async def present_input(self, input_kind: InputKind) -> float:
            self.redraw_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class _TerminalTransport(_Transport):
        def __init__(self, presenter) -> None:
            super().__init__(())
            self._presenter = presenter

        async def receive(self):
            await self._presenter.redraw_started.wait()
            return ConversationEnded("completed")

    async def input_result() -> InputResult:
        return InputResult(kind, "/unknown" if kind is InputResultKind.UNKNOWN_COMMAND else None, 1.0)

    async def run() -> None:
        presenter = _RedrawPresenter()
        transport = _TerminalTransport(presenter)
        engine = ClientEngine(transport, presenter, user=None, area=None, follow_up_timeout_seconds=15,
                              stop_event=asyncio.Event())
        engine._state = ClientState.AWAITING_FOLLOW_UP_INPUT
        engine._deadline = asyncio.get_running_loop().time() + 10
        engine._input_task = asyncio.create_task(input_result())
        await asyncio.sleep(0)
        assert await engine._next_result() is None
        assert engine._state is ClientState.WAITING_FOR_READY
        assert not any(isinstance(event, FollowUpTimedOut) for event in transport.sent)
        assert presenter.cancelled

    asyncio.run(run())
