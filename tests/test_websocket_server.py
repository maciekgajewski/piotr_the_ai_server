import asyncio
import logging
import socket
from types import SimpleNamespace

from aiohttp import ClientConnectionResetError, ClientSession, WSServerHandshakeError, WSMsgType, web
import pytest

from ai_server.agent.echo import EchoAgent
from ai_server.agent.interrogator import InterrogatorAgent
from ai_server.batch_ws_client import BatchWsClientOptions, _wait_for_server_or_follow_up_timeout
from ai_server.batch_ws_client import parse_args as parse_batch_args, run_batch_ws_client
from ai_server.chat_client import parse_args as parse_chat_args
from ai_server.chat_client import _read_next_interactive_text
from ai_server.config import AgentConfig, Config, ConversationConfig, ShutdownConfig, WebsocketConfig
from ai_server.conversations.agent_runtime import AgentChannel, ConversationAgent
from ai_server.conversations.contexts import ConversationContext, ConversationMedium
from ai_server.conversations.contexts import InputConversationContext, InputSessionContext
from ai_server.conversations.messages import AssistantAbortReason, FollowUpRequestCommitted, InputSessionClosed
from ai_server.conversations.messages import UserMessage
from ai_server.websocket_messages import AssistantMessageAborted, AssistantMessageCompleted, AssistantMessageStarted
from ai_server.websocket_messages import AssistantTextChunk, CancelConversation, FollowUpMessage
from ai_server.websocket_messages import ConversationEnded, ConversationReady, ConversationStarted, FollowUpRequested
from ai_server.websocket_messages import FollowUpTimedOut as WsFollowUpTimedOut
from ai_server.websocket_messages import ProtocolRejected, ProtocolRejectionCode, SessionAccepted, SessionStart
from ai_server.websocket_messages import StartConversation, client_event_to_json, server_event_from_json
from ai_server.websocket_messages import server_event_to_json
from ai_server.websocket_server import WebsocketInputSession, WebsocketState, _WebsocketAssistantSink
from ai_server.websocket_server import _WebsocketInputConversation
from ai_server.websocket_server import create_app
from ai_server.ws_client_common import WaitState, validate_follow_up_timeout


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _config(
    port: int,
    *,
    max_connections: int = 4,
    lease: float = 1.0,
    handshake: float = 1.0,
    max_frame_bytes: int = 4096,
    ingress_queue_capacity: int = 4,
    heartbeat: float = 1.0,
) -> Config:
    return Config(
        agent=AgentConfig("echo", {}),
        websocket=WebsocketConfig(
            port=port,
            max_connections=max_connections,
            capacity_retry_after_seconds=7,
            follow_up_idle_lease_seconds=lease,
            max_frame_bytes=max_frame_bytes,
            ingress_queue_capacity=ingress_queue_capacity,
            heartbeat_seconds=heartbeat,
            handshake_timeout_seconds=handshake,
            host="127.0.0.1",
        ),
        conversation=ConversationConfig(1.0, 0.1),
        shutdown=ShutdownConfig(2.0),
    )


async def _start(config: Config, agent):
    runner = web.AppRunner(create_app(config, agent))
    await runner.setup()
    site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
    await site.start()
    return runner


async def _receive(websocket):
    message = await websocket.receive(timeout=2)
    assert message.type is WSMsgType.TEXT, message
    return server_event_from_json(message.data)


def test_one_turn_websocket_sequence_uses_clean_break_vocabulary() -> None:
    async def run() -> None:
        config = _config(_unused_port())
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat") as websocket:
                    await websocket.send_str(client_event_to_json(SessionStart(area="office")))
                    assert await _receive(websocket) == SessionAccepted()
                    assert await _receive(websocket) == ConversationReady()
                    await websocket.send_str(client_event_to_json(StartConversation("hello")))
                    assert isinstance(await _receive(websocket), ConversationStarted)
                    started = await _receive(websocket)
                    assert isinstance(started, AssistantMessageStarted)
                    assert await _receive(websocket) == AssistantTextChunk(started.message_id, "hello")
                    assert await _receive(websocket) == AssistantMessageCompleted(started.message_id)
                    assert await _receive(websocket) == ConversationEnded("completed")
                    assert await _receive(websocket) == ConversationReady()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_one_input_session_runs_sequential_conversations_without_overlap() -> None:
    async def consume_echo_turn(websocket, text: str) -> str:
        await websocket.send_str(client_event_to_json(StartConversation(text)))
        started = await _receive(websocket)
        assert isinstance(started, ConversationStarted)
        stream = await _receive(websocket)
        assert isinstance(stream, AssistantMessageStarted)
        assert await _receive(websocket) == AssistantTextChunk(stream.message_id, text)
        assert await _receive(websocket) == AssistantMessageCompleted(stream.message_id)
        assert await _receive(websocket) == ConversationEnded("completed")
        assert await _receive(websocket) == ConversationReady()
        return started.conversation_id

    async def run() -> None:
        config = _config(_unused_port())
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(
                    f"ws://127.0.0.1:{config.websocket.port}/chat"
                ) as websocket:
                    await websocket.send_str(client_event_to_json(SessionStart()))
                    assert await _receive(websocket) == SessionAccepted()
                    assert await _receive(websocket) == ConversationReady()
                    first = await consume_echo_turn(websocket, "first")
                    second = await consume_echo_turn(websocket, "second")
                    assert first != second
        finally:
            await runner.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"session_attributes","attributes":{"medium":"text"}}',
        '{"type":"new_conversation","attributes":{}}',
        '{"type":"message_begin","message_id":"m1"}',
    ],
)
def test_old_vocabulary_is_rejected(payload: str) -> None:
    async def run() -> None:
        config = _config(_unused_port())
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat") as websocket:
                    await websocket.send_str(payload)
                    rejected = await _receive(websocket)
                    assert rejected == ProtocolRejected(ProtocolRejectionCode.INVALID_EVENT, rejected.detail)
                    close = await websocket.receive(timeout=2)
                    assert close.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
                    assert websocket.close_code == 1002
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_capacity_is_reserved_before_upgrade_and_returns_retry_after() -> None:
    async def run() -> None:
        config = _config(_unused_port(), max_connections=1)
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                first = await client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat")
                try:
                    with pytest.raises(WSServerHandshakeError) as exc_info:
                        await client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat")
                    assert exc_info.value.status == 503
                    assert exc_info.value.headers["Retry-After"] == "7"
                finally:
                    await first.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_capacity_is_released_when_websocket_preparation_fails() -> None:
    async def run() -> None:
        config = _config(_unused_port(), max_connections=1)
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                async with client.get(
                    f"http://127.0.0.1:{config.websocket.port}/chat"
                ) as response:
                    assert response.status == 400
                websocket = await client.ws_connect(
                    f"ws://127.0.0.1:{config.websocket.port}/chat"
                )
                await websocket.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_handshake_timeout_closes_with_policy_violation() -> None:
    async def run() -> None:
        config = _config(_unused_port(), handshake=0.01)
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat") as websocket:
                    await websocket.receive(timeout=1)
                    assert websocket.close_code == 1008
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_handshaking_session_is_registered_before_readiness_exposure() -> None:
    async def run() -> None:
        config = _config(_unused_port(), handshake=1)
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                websocket = await client.ws_connect(
                    f"ws://127.0.0.1:{config.websocket.port}/chat"
                )
                try:
                    async with client.get(
                        f"http://127.0.0.1:{config.websocket.port}/api/status"
                    ) as response:
                        status = await response.json()
                    assert status["websocket"]["active_sessions"] == 1
                    assert status["websocket"]["active_connections"] == 1
                finally:
                    await websocket.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_follow_up_resource_lease_closes_without_forging_timeout() -> None:
    async def run() -> None:
        config = _config(_unused_port(), lease=0.03, heartbeat=0.005)
        runner = await _start(config, InterrogatorAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat") as websocket:
                    await websocket.send_str(client_event_to_json(SessionStart()))
                    assert await _receive(websocket) == SessionAccepted()
                    assert await _receive(websocket) == ConversationReady()
                    await websocket.send_str(client_event_to_json(StartConversation("hello")))
                    events = []
                    follow_up_committed_at = None
                    while True:
                        message = await websocket.receive(timeout=2)
                        if message.type is not WSMsgType.TEXT:
                            assert websocket.close_code == 1013
                            break
                        event = server_event_from_json(message.data)
                        events.append(event)
                        if isinstance(event, FollowUpRequested):
                            follow_up_committed_at = asyncio.get_running_loop().time()
                    assert any(isinstance(event, FollowUpRequested) for event in events)
                    assert follow_up_committed_at is not None
                    elapsed = asyncio.get_running_loop().time() - follow_up_committed_at
                    assert 0.02 <= elapsed < 0.5
                    assert not any(isinstance(event, ConversationEnded) and event.reason == "follow_up_timeout" for event in events)
        finally:
            await runner.cleanup()

    asyncio.run(run())


class SlowAgent(ConversationAgent):
    async def run_agent_conversation(self, context: ConversationContext, channel: AgentChannel) -> None:
        await channel.receive_user_message()
        await asyncio.sleep(10)

    async def close(self) -> None:
        pass


def test_background_reader_observes_disconnect_while_agent_is_busy() -> None:
    async def run() -> None:
        config = _config(_unused_port())
        runner = await _start(config, SlowAgent())
        try:
            async with ClientSession() as client:
                websocket = await client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat")
                await websocket.send_str(client_event_to_json(SessionStart()))
                assert await _receive(websocket) == SessionAccepted()
                assert await _receive(websocket) == ConversationReady()
                await websocket.send_str(client_event_to_json(StartConversation("hello")))
                assert isinstance(await _receive(websocket), ConversationStarted)
                await websocket.close()
                for _ in range(50):
                    async with client.get(
                        f"http://127.0.0.1:{config.websocket.port}/api/status"
                    ) as status:
                        payload = await status.json()
                    if payload["websocket"]["active_sessions"] == 0:
                        break
                    await asyncio.sleep(0.01)
                assert payload["websocket"]["active_sessions"] == 0
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_back_to_back_start_events_cannot_create_a_future_conversation() -> None:
    async def run() -> None:
        config = _config(_unused_port())
        runner = await _start(config, SlowAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(
                    f"ws://127.0.0.1:{config.websocket.port}/chat"
                ) as websocket:
                    await websocket.send_str(client_event_to_json(SessionStart()))
                    assert await _receive(websocket) == SessionAccepted()
                    assert await _receive(websocket) == ConversationReady()
                    await websocket.send_str(client_event_to_json(StartConversation("first")))
                    await websocket.send_str(client_event_to_json(StartConversation("second")))
                    events = []
                    while True:
                        message = await websocket.receive(timeout=2)
                        if message.type is not WSMsgType.TEXT:
                            break
                        events.append(server_event_from_json(message.data))
                    rejection = next(event for event in events if isinstance(event, ProtocolRejected))
                    assert rejection.code is ProtocolRejectionCode.INVALID_STATE
                    assert websocket.close_code == 1002
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_back_to_back_session_start_events_reject_the_duplicate() -> None:
    async def run() -> None:
        config = _config(_unused_port())
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(
                    f"ws://127.0.0.1:{config.websocket.port}/chat"
                ) as websocket:
                    await websocket.send_str(client_event_to_json(SessionStart()))
                    await websocket.send_str(client_event_to_json(SessionStart()))
                    events = []
                    while True:
                        message = await websocket.receive(timeout=2)
                        if message.type is not WSMsgType.TEXT:
                            break
                        events.append(server_event_from_json(message.data))
                    rejection = next(event for event in events if isinstance(event, ProtocolRejected))
                    assert rejection.code is ProtocolRejectionCode.INVALID_STATE
                    assert websocket.close_code == 1002
        finally:
            await runner.cleanup()

    asyncio.run(run())


class _BlockedWebsocket:
    def __init__(self) -> None:
        self.closed = False
        self.send_called = asyncio.Event()
        self.release_send = asyncio.Event()

    async def receive(self):
        await asyncio.Event().wait()

    async def send_str(self, payload: str) -> None:
        del payload
        self.send_called.set()
        await self.release_send.wait()

    async def close(self, code: int, message: bytes) -> None:
        del code, message
        self.closed = True


class _DrainFailureWebsocket:
    def __init__(self) -> None:
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.payloads: list[str] = []

    async def receive(self):
        await asyncio.Event().wait()

    async def send_str(self, payload: str) -> None:
        self.payloads.append(payload)
        raise ClientConnectionResetError("writer drain failed")

    async def close(self, code: int, message: bytes) -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = message.decode("utf-8")


def test_follow_up_commit_occurs_in_writer_task_not_before_writer_entry() -> None:
    async def run() -> None:
        config = _config(2137)
        websocket = _BlockedWebsocket()
        session = WebsocketInputSession(websocket, "test-peer", config.websocket)
        session._session_context = InputSessionContext(
            "session-1",
            ConversationMedium.TEXT,
        )
        context = InputConversationContext(
            "conversation-1",
            "session-1",
            ConversationMedium.TEXT,
        )
        session._state = WebsocketState.ACTIVE
        session._control = asyncio.Queue(maxsize=1)
        conversation = _WebsocketInputConversation(
            session,
            context,
            UserMessage("hello"),
        )
        await session._writer_lock.acquire()
        request = asyncio.create_task(conversation.request_follow_up())
        await asyncio.sleep(0)
        assert session._state is WebsocketState.ACTIVE
        session._writer_lock.release()
        await websocket.send_called.wait()
        assert session._state is WebsocketState.FOLLOW_UP_COMMITTING
        websocket.release_send.set()
        await request
        session._cancel_lease()
        session._reader_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session._reader_task

    asyncio.run(run())


def test_follow_up_drain_failure_after_handoff_closes_typed_committed_interval() -> None:
    async def run() -> None:
        websocket = _DrainFailureWebsocket()
        session = WebsocketInputSession(websocket, "test-peer", _config(2137, lease=10).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.ACTIVE
        session._control = asyncio.Queue(maxsize=1)
        conversation = _WebsocketInputConversation(
            session,
            InputConversationContext("conversation-1", "session-1", ConversationMedium.TEXT),
            UserMessage("hello"),
        )
        with pytest.raises(InputSessionClosed, match="transport reset"):
            await conversation.request_follow_up()
        assert session._follow_up_token is not None
        assert session._follow_up_deadline is None
        assert session._state is WebsocketState.CLOSING
        assert isinstance(await session._control.get(), InputSessionClosed)
        await _dispose_private_session(session)

    asyncio.run(run())


def test_follow_up_lease_expiry_during_writer_drain_closes_and_joins_tasks() -> None:
    async def run() -> None:
        websocket = _BlockedWebsocket()
        session = WebsocketInputSession(websocket, "test-peer", _config(2137, lease=0.01).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.ACTIVE
        session._control = asyncio.Queue(maxsize=1)
        conversation = _WebsocketInputConversation(
            session,
            InputConversationContext("conversation-1", "session-1", ConversationMedium.TEXT),
            UserMessage("hello"),
        )
        request = asyncio.create_task(conversation.request_follow_up())
        await websocket.send_called.wait()
        await asyncio.wait_for(session._control.get(), timeout=1)
        assert session._state is WebsocketState.CLOSING
        assert websocket.closed
        websocket.release_send.set()
        assert isinstance(await request, FollowUpRequestCommitted)
        await _dispose_private_session(session)

    asyncio.run(run())


def test_follow_up_outcome_at_lease_deadline_wins() -> None:
    async def run() -> None:
        config = _config(2137)
        websocket = _BlockedWebsocket()
        session = WebsocketInputSession(websocket, "test-peer", config.websocket)
        session._state = WebsocketState.AWAITING_FOLLOW_UP
        session._control = asyncio.Queue(maxsize=1)
        session._follow_up_deadline = 42.0
        assert await session._commit_client_event(WsFollowUpTimedOut(), 42.0)
        outcome = await session._control.get()
        assert type(outcome).__name__ == "FollowUpTimedOut"
        assert session._state is WebsocketState.ACTIVE
        session._reader_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session._reader_task

    asyncio.run(run())


def test_batch_follow_up_timer_is_cancelled_by_terminal_server_event() -> None:
    class ImmediateTerminalWebsocket:
        def __init__(self) -> None:
            self.sent = []

        async def receive(self):
            return SimpleNamespace(
                type=WSMsgType.TEXT,
                data=server_event_to_json(ConversationEnded("completed")),
            )

        async def send_str(self, payload: str) -> None:
            self.sent.append(payload)

    async def run() -> None:
        websocket = ImmediateTerminalWebsocket()
        message = await _wait_for_server_or_follow_up_timeout(
            websocket,
            asyncio.Event(),
            10.0,
        )
        assert message.type is WSMsgType.TEXT
        assert websocket.sent == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("receive_delay", "line_delay", "expected_text", "terminal_won"),
    [
        (0.01, 0.0, "before terminal", False),
        (0.0, 0.01, None, True),
    ],
)
def test_interactive_follow_up_submission_vs_terminal_boundary(
    receive_delay: float,
    line_delay: float,
    expected_text: str | None,
    terminal_won: bool,
) -> None:
    class TerminalWebsocket:
        async def receive(self):
            await asyncio.sleep(receive_delay)
            return SimpleNamespace(
                type=WSMsgType.TEXT,
                data=server_event_to_json(ConversationEnded("completed")),
            )

    class InteractiveInput:
        async def read_line(self):
            await asyncio.sleep(line_delay)
            return "before terminal"

        def set_prompt(self, prompt: str) -> None:
            del prompt

    async def run() -> None:
        text, state, interval_closed = await _read_next_interactive_text(
            TerminalWebsocket(),
            asyncio.Event(),
            InteractiveInput(),
            WaitState(False, follow_up_requested=True, timeout_seconds=10),
            10,
        )
        assert text == expected_text
        assert state.follow_up_requested
        assert interval_closed is terminal_won

    asyncio.run(run())


def test_interactive_terminal_wins_when_terminal_and_line_are_both_committed(monkeypatch) -> None:
    class TerminalWebsocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def receive(self):
            return SimpleNamespace(
                type=WSMsgType.TEXT,
                data=server_event_to_json(ConversationEnded("completed")),
            )

        async def send_str(self, payload: str) -> None:
            self.sent.append(payload)

    class InteractiveInput:
        async def read_line(self):
            return "stale follow-up"

        def set_prompt(self, prompt: str) -> None:
            del prompt

    real_wait = asyncio.wait
    controlling_outer_race = False

    async def wait_until_all_candidates_commit(tasks, **kwargs):
        nonlocal controlling_outer_race
        if controlling_outer_race:
            return await real_wait(tasks, **kwargs)
        candidates = tuple(tasks)
        controlling_outer_race = True
        try:
            await asyncio.gather(*candidates)
            return set(candidates), set()
        finally:
            controlling_outer_race = False

    async def run() -> None:
        monkeypatch.setattr(asyncio, "wait", wait_until_all_candidates_commit)
        websocket = TerminalWebsocket()
        try:
            text, state, interval_closed = await _read_next_interactive_text(
                websocket,
                asyncio.Event(),
                InteractiveInput(),
                WaitState(False, follow_up_requested=True, timeout_seconds=0),
                10,
            )
        finally:
            monkeypatch.setattr(asyncio, "wait", real_wait)
        assert text is None
        assert state.follow_up_requested
        assert interval_closed
        assert websocket.sent == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("line_delay", "timeout_seconds", "expected_text", "timed_out"),
    [
        (0.0, 0.01, "follow-up", False),
        (0.01, 0.0, None, True),
    ],
)
def test_interactive_submission_before_and_after_expiry_boundary(
    line_delay: float,
    timeout_seconds: float,
    expected_text: str | None,
    timed_out: bool,
) -> None:
    class WaitingWebsocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def receive(self):
            await asyncio.Event().wait()

        async def send_str(self, payload: str) -> None:
            self.sent.append(payload)

    class InteractiveInput:
        async def read_line(self):
            await asyncio.sleep(line_delay)
            return "follow-up"

        def set_prompt(self, prompt: str) -> None:
            del prompt

    async def run() -> None:
        websocket = WaitingWebsocket()
        text, _state, interval_closed = await _read_next_interactive_text(
            websocket,
            asyncio.Event(),
            InteractiveInput(),
            WaitState(False, follow_up_requested=True, timeout_seconds=timeout_seconds),
            10,
        )
        assert text == expected_text
        assert interval_closed is timed_out
        assert len(websocket.sent) == int(timed_out)

    asyncio.run(run())


def test_interactive_submission_wins_at_exact_expiry_boundary(monkeypatch) -> None:
    class WaitingWebsocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def receive(self):
            await asyncio.Event().wait()

        async def send_str(self, payload: str) -> None:
            self.sent.append(payload)

    class InteractiveInput:
        async def read_line(self):
            return "at boundary"

        def set_prompt(self, prompt: str) -> None:
            del prompt

    real_wait = asyncio.wait
    outer_race_selected = False

    async def commit_line_and_timeout_together(tasks, **kwargs):
        nonlocal outer_race_selected
        if outer_race_selected:
            return await real_wait(tasks, **kwargs)
        outer_race_selected = True
        candidates = tuple(tasks)
        selected = tuple(
            task
            for task in candidates
            if "read_line" in task.get_coro().__qualname__
            or task.get_coro().__qualname__ == "sleep"
        )
        await asyncio.gather(*selected)
        return set(selected), set(candidates) - set(selected)

    async def run() -> None:
        websocket = WaitingWebsocket()
        monkeypatch.setattr(asyncio, "wait", commit_line_and_timeout_together)
        try:
            text, _state, interval_closed = await _read_next_interactive_text(
                websocket,
                asyncio.Event(),
                InteractiveInput(),
                WaitState(False, follow_up_requested=True, timeout_seconds=0),
                10,
            )
        finally:
            monkeypatch.setattr(asyncio, "wait", real_wait)
        assert text == "at boundary"
        assert not interval_closed
        assert websocket.sent == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("payload", "rejection_code", "close_code"),
    [
        ("{", ProtocolRejectionCode.INVALID_JSON, 1002),
        ("[]", ProtocolRejectionCode.INVALID_EVENT, 1002),
        ('{"type":"session_start","user":null}', ProtocolRejectionCode.INVALID_EVENT, 1002),
        ('{"type":"start_conversation","message":"early"}', ProtocolRejectionCode.INVALID_STATE, 1002),
    ],
)
def test_external_violation_sends_typed_rejection_then_closes(
    payload: str,
    rejection_code: ProtocolRejectionCode,
    close_code: int,
) -> None:
    async def run() -> None:
        config = _config(_unused_port())
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(
                    f"ws://127.0.0.1:{config.websocket.port}/chat"
                ) as websocket:
                    await websocket.send_str(payload)
                    rejection = await _receive(websocket)
                    assert isinstance(rejection, ProtocolRejected)
                    assert rejection.code is rejection_code
                    close = await websocket.receive(timeout=2)
                    assert close.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
                    assert websocket.close_code == close_code
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_oversized_and_binary_frames_use_documented_rejections() -> None:
    async def rejected(send_frame, expected_code, expected_close) -> None:
        config = _config(_unused_port(), max_frame_bytes=32)
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                async with client.ws_connect(
                    f"ws://127.0.0.1:{config.websocket.port}/chat"
                ) as websocket:
                    await send_frame(websocket)
                    event = await _receive(websocket)
                    assert isinstance(event, ProtocolRejected)
                    assert event.code is expected_code
                    await websocket.receive(timeout=2)
                    assert websocket.close_code == expected_close
        finally:
            await runner.cleanup()

    async def run() -> None:
        await rejected(
            lambda websocket: websocket.send_str(
                client_event_to_json(SessionStart(user="x" * 100))
            ),
            ProtocolRejectionCode.MESSAGE_TOO_LARGE,
            1009,
        )
        await rejected(
            lambda websocket: websocket.send_bytes(b"binary"),
            ProtocolRejectionCode.INVALID_EVENT,
            1002,
        )

    asyncio.run(run())


def test_capacity_is_exact_and_released_after_ordinary_close() -> None:
    async def run() -> None:
        config = _config(_unused_port(), max_connections=2)
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                first = await client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat")
                second = await client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat")
                try:
                    with pytest.raises(WSServerHandshakeError) as exc_info:
                        await client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat")
                    assert exc_info.value.status == 503
                    await first.close()
                    for _ in range(100):
                        try:
                            replacement = await client.ws_connect(
                                f"ws://127.0.0.1:{config.websocket.port}/chat"
                            )
                            break
                        except WSServerHandshakeError as exc:
                            assert exc.status == 503
                            await asyncio.sleep(0.01)
                    else:
                        raise AssertionError("capacity slot was not released")
                    await replacement.close()
                finally:
                    await first.close()
                    await second.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_capacity_is_released_after_invalid_handshake_and_timeout() -> None:
    async def replacement_is_accepted(client, config) -> None:
        for _ in range(100):
            try:
                websocket = await client.ws_connect(
                    f"ws://127.0.0.1:{config.websocket.port}/chat"
                )
                break
            except WSServerHandshakeError as exc:
                assert exc.status == 503
                await asyncio.sleep(0.01)
        else:
            raise AssertionError("capacity slot was not released")
        async with websocket:
            await websocket.send_str(client_event_to_json(SessionStart()))
            assert await _receive(websocket) == SessionAccepted()

    async def invalid_handshake() -> None:
        config = _config(_unused_port(), max_connections=1)
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                websocket = await client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat")
                await websocket.send_str("[]")
                await _receive(websocket)
                await websocket.receive(timeout=2)
                await replacement_is_accepted(client, config)
        finally:
            await runner.cleanup()

    async def handshake_timeout() -> None:
        config = _config(_unused_port(), max_connections=1, handshake=0.01)
        runner = await _start(config, EchoAgent())
        try:
            async with ClientSession() as client:
                websocket = await client.ws_connect(f"ws://127.0.0.1:{config.websocket.port}/chat")
                close = await websocket.receive(timeout=2)
                assert close.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
                assert websocket.close_code == 1008
                assert close.extra == "session start timed out"
                await replacement_is_accepted(client, config)
        finally:
            await runner.cleanup()

    asyncio.run(invalid_handshake())
    asyncio.run(handshake_timeout())


class _RecordingWebsocket:
    def __init__(self) -> None:
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.payloads: list[str] = []

    async def receive(self):
        await asyncio.Event().wait()

    async def send_str(self, payload: str) -> None:
        self.payloads.append(payload)

    async def close(self, code: int, message: bytes) -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = message.decode("utf-8")


async def _dispose_private_session(session: WebsocketInputSession) -> None:
    await session.close("test cleanup")
    assert session._reader_task.done()
    assert session._follow_up_lease is None
    assert not session._retired_tasks


@pytest.mark.parametrize(
    "state",
    [WebsocketState.HANDSHAKE, WebsocketState.IDLE, WebsocketState.ACCEPTING],
)
def test_websocket_close_matrix_quiesces_non_active_session_states(state: WebsocketState) -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        session._state = state
        if state is not WebsocketState.HANDSHAKE:
            session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        await asyncio.gather(session.close("shutdown"), session.close("shutdown"))
        assert session.closed
        assert session._reader_task.done()
        assert websocket.closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "state",
    [
        WebsocketState.ACCEPTING,
        WebsocketState.ACTIVE,
        WebsocketState.CLOSING,
        WebsocketState.CLOSED,
    ],
)
def test_websocket_input_session_accept_operation_matrix_rejects_every_non_idle_state(
    state: WebsocketState,
) -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = state
        with pytest.raises(AssertionError, match="only in IDLE"):
            await session.accept_conversation().__aenter__()
        if state is not WebsocketState.CLOSED:
            session._state = WebsocketState.IDLE
        await session.close("test cleanup")

    asyncio.run(run())


def test_websocket_session_rejects_overlapping_acceptance_and_close_wins() -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.IDLE
        first_scope = session.accept_conversation()
        first_accept = asyncio.create_task(first_scope.__aenter__())
        while session._state is not WebsocketState.ACCEPTING:
            await asyncio.sleep(0)
        with pytest.raises(AssertionError, match="only in IDLE"):
            await session.accept_conversation().__aenter__()
        await asyncio.gather(session.close("shutdown"), session.close("shutdown"))
        with pytest.raises(InputSessionClosed):
            await first_accept
        assert session.closed
        assert session._reader_task.done()

    asyncio.run(run())


def test_websocket_active_close_waits_for_conversation_scope_cleanup() -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.ACTIVE
        session._control = asyncio.Queue(maxsize=1)
        session._active_conversation = object()
        session._active_scope_exited.clear()

        closing = asyncio.create_task(session.close("server shutting down"))
        assert isinstance(await session._control.get(), InputSessionClosed)
        await asyncio.sleep(0)
        assert session._state is WebsocketState.CLOSING
        assert not closing.done()
        assert not websocket.closed

        session._active_conversation = None
        session._active_scope_exited.set()
        await closing
        assert session.closed
        assert websocket.close_code == 1001
        assert session._reader_task.done()

    asyncio.run(run())


@pytest.mark.parametrize("closure_path", ["protocol_rejection", "lease_expiry"])
def test_websocket_protocol_closure_remains_closing_until_active_scope_exits(
    closure_path: str,
) -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.ACTIVE
        session._control = asyncio.Queue(maxsize=1)
        session._active_conversation = object()
        session._active_scope_exited.clear()
        if closure_path == "protocol_rejection":
            await session._reject(ProtocolRejectionCode.INVALID_STATE, "rejected", 1002)
        else:
            session._state = WebsocketState.AWAITING_FOLLOW_UP
            await session._expire_follow_up_lease()
        assert session._state is WebsocketState.CLOSING
        assert not session.closed
        assert isinstance(await session._control.get(), InputSessionClosed)

        closing = asyncio.create_task(session.close("cleanup"))
        await asyncio.sleep(0)
        assert not closing.done()
        session._active_conversation = None
        session._active_scope_exited.set()
        await closing
        assert session.closed

    asyncio.run(run())


def test_websocket_reader_failure_remains_closing_until_active_scope_exits() -> None:
    class FailingWebsocket(_RecordingWebsocket):
        async def receive(self):
            raise RuntimeError("reader failed")

    async def run() -> None:
        websocket = FailingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.ACTIVE
        session._control = asyncio.Queue(maxsize=1)
        session._active_conversation = object()
        session._active_scope_exited.clear()
        await session._reader_task
        assert session._state is WebsocketState.CLOSING
        assert not session.closed
        assert isinstance(await session._control.get(), InputSessionClosed)

        closing = asyncio.create_task(session.close("cleanup"))
        await asyncio.sleep(0)
        assert not closing.done()
        session._active_conversation = None
        session._active_scope_exited.set()
        await closing
        assert session.closed

    asyncio.run(run())


def test_websocket_sink_uses_fresh_ids_and_ordered_terminal_events() -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        first = _WebsocketAssistantSink(session)
        await first.start()
        await first.send_text("one")
        await first.send_text("two")
        assert await first.complete() is not None

        second = _WebsocketAssistantSink(session)
        await second.start()
        await second.abort(AssistantAbortReason.AGENT_FAILED, "failed")

        events = [server_event_from_json(payload) for payload in websocket.payloads]
        assert [type(event) for event in events] == [
            AssistantMessageStarted,
            AssistantTextChunk,
            AssistantTextChunk,
            AssistantMessageCompleted,
            AssistantMessageStarted,
            AssistantMessageAborted,
        ]
        first_id = events[0].message_id
        second_id = events[4].message_id
        assert first_id != second_id
        assert all(event.message_id == first_id for event in events[:4])
        assert events[-1] == AssistantMessageAborted(
            second_id,
            AssistantAbortReason.AGENT_FAILED.value,
            "failed",
        )
        await _dispose_private_session(session)

    asyncio.run(run())


def test_writer_cancellation_before_handoff_creates_no_follow_up_interval() -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.ACTIVE
        conversation = _WebsocketInputConversation(
            session,
            InputConversationContext("conversation-1", "session-1", ConversationMedium.TEXT),
            UserMessage("initial"),
        )
        await session._writer_lock.acquire()
        request = asyncio.create_task(conversation.request_follow_up())
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert session._state is WebsocketState.ACTIVE
        assert session._follow_up_token is None
        assert session._follow_up_deadline is None
        session._writer_lock.release()
        await _dispose_private_session(session)

    asyncio.run(run())


def test_follow_up_gate_retains_one_early_value_until_matching_ack() -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137, lease=10).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.ACTIVE
        session._control = asyncio.Queue(maxsize=1)
        conversation = _WebsocketInputConversation(
            session,
            InputConversationContext("conversation-1", "session-1", ConversationMedium.TEXT),
            UserMessage("initial"),
        )
        token = await conversation.request_follow_up()
        assert session._state is WebsocketState.FOLLOW_UP_COMMITTING
        assert await session._commit_client_event(
            FollowUpMessage("early"),
            session._follow_up_deadline,
        )
        assert session._control.empty()
        with pytest.raises(AssertionError, match="token mismatch"):
            conversation.acknowledge_follow_up_ready(FollowUpRequestCommitted("wrong"))
        conversation.acknowledge_follow_up_ready(token)
        assert await session._control.get() == UserMessage("early")
        assert session._state is WebsocketState.ACTIVE

        assert not await session._commit_client_event(
            WsFollowUpTimedOut(),
            asyncio.get_running_loop().time(),
        )
        assert websocket.close_code == 1002
        rejection = server_event_from_json(websocket.payloads[-1])
        assert rejection == ProtocolRejected(
            ProtocolRejectionCode.DUPLICATE_FOLLOW_UP_OUTCOME,
            "duplicate follow-up outcome",
        )
        await _dispose_private_session(session)

    asyncio.run(run())


def test_terminal_cancel_bypasses_follow_up_gate_and_clears_lease() -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137, lease=10).websocket)
        session._session_context = InputSessionContext("session-1", ConversationMedium.TEXT)
        session._state = WebsocketState.FOLLOW_UP_COMMITTING
        session._control = asyncio.Queue(maxsize=1)
        session._retained_follow_up = UserMessage("retained")
        session._follow_up_outcome_committed = True
        session._follow_up_deadline = asyncio.get_running_loop().time() + 10
        session._follow_up_lease = asyncio.create_task(asyncio.sleep(10))
        assert await session._commit_client_event(
            CancelConversation(),
            asyncio.get_running_loop().time(),
        )
        assert type(await session._control.get()).__name__ == "ConversationCancelled"
        assert session._retained_follow_up is None
        assert session._follow_up_deadline is None
        await _dispose_private_session(session)

    asyncio.run(run())


def test_follow_up_outcome_after_lease_deadline_closes_without_semantic_timeout() -> None:
    async def run() -> None:
        websocket = _RecordingWebsocket()
        session = WebsocketInputSession(websocket, "peer", _config(2137).websocket)
        session._state = WebsocketState.AWAITING_FOLLOW_UP
        session._control = asyncio.Queue(maxsize=1)
        session._follow_up_deadline = 42.0
        assert not await session._commit_client_event(WsFollowUpTimedOut(), 42.0001)
        assert websocket.close_code == 1013
        assert websocket.close_reason == "follow-up resource lease expired"
        assert isinstance(await session._control.get(), InputSessionClosed)
        await _dispose_private_session(session)

    asyncio.run(run())


@pytest.mark.parametrize("value", [False, 0, -1, float("nan"), float("inf")])
def test_repository_clients_reject_invalid_follow_up_timeout(value) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        validate_follow_up_timeout(value)


def test_repository_client_clis_require_explicit_follow_up_timeout() -> None:
    with pytest.raises(SystemExit):
        parse_batch_args([])
    with pytest.raises(SystemExit):
        parse_chat_args([])


class _DelayedReplyAgent(ConversationAgent):
    async def run_agent_conversation(
        self,
        context: ConversationContext,
        channel: AgentChannel,
    ) -> None:
        del context
        await channel.receive_user_message()
        await asyncio.sleep(2.1)
        await channel.send_message("delayed reply")
        await channel.end_conversation()

    async def close(self) -> None:
        return None


def test_real_server_and_repository_batch_client_survive_delayed_agent(capsys) -> None:
    async def run() -> None:
        config = _config(_unused_port())
        runner = await _start(config, _DelayedReplyAgent())
        try:
            await run_batch_ws_client(
                BatchWsClientOptions(
                    url=f"ws://127.0.0.1:{config.websocket.port}/chat",
                    user=None,
                    area="office",
                    messages=("hello",),
                    follow_up_timeout_seconds=5,
                )
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert "delayed reply" in capsys.readouterr().out


def test_websocket_observability_has_stable_session_and_admission_context(caplog) -> None:
    async def run() -> None:
        config = _config(_unused_port())
        with caplog.at_level(logging.DEBUG, logger="ai_server.websocket_server"):
            runner = await _start(config, EchoAgent())
            try:
                async with ClientSession() as client:
                    async with client.ws_connect(
                        f"ws://127.0.0.1:{config.websocket.port}/chat"
                    ) as websocket:
                        await websocket.send_str(client_event_to_json(SessionStart(area="office")))
                        assert await _receive(websocket) == SessionAccepted()
                        assert await _receive(websocket) == ConversationReady()
            finally:
                await runner.cleanup()

        assert "WebsocketAdmission" in caplog.text
        assert "slot reserved peer=127.0.0.1:" in caplog.text
        assert "slot released peer=127.0.0.1:" in caplog.text
        assert "WebsocketInputSession[127.0.0.1:" in caplog.text
        assert "state transition old=handshake cause=handshake_accepted new=idle" in caplog.text
        assert "write handoff event=SessionAccepted" in caplog.text
        assert "write drain completed event=ConversationReady" in caplog.text

    asyncio.run(run())
