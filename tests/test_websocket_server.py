import asyncio
import socket
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, WSCloseCode, WSMsgType
from aiohttp import web
import pytest

from ai_server import batch_ws_client
from ai_server.agent.echo import EchoAgent
from ai_server.agent.interrogator import InterrogatorAgent
from ai_server import chat_client
from ai_server.batch_ws_client import BatchWsClientOptions, run_batch_ws_client
from ai_server.chat_client import ChatClientOptions
from ai_server.config import AgentConfig, Config, WebsocketConfig
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, NewConversation, ProcessingUpdate, RequestFollowUp
from ai_server.messages import SessionAttributes, SessionRejected, TextMessage, WaitForNewConversation, endpoint_event_from_json, endpoint_event_to_json
from ai_server.messages import session_event_from_json, session_event_to_json, text_message_to_events
from ai_server.websocket_server import create_app
from ai_server.ws_client_common import WebsocketSessionRejected, handle_websocket_message


class FakeWebsocket:
    def __init__(self) -> None:
        self.close_calls = []

    async def close(self, code, message) -> None:
        self.close_calls.append((code, message))


@dataclass(frozen=True)
class FakeWebsocketMessage:
    data: str
    type: WSMsgType = WSMsgType.TEXT


class SingleReplyAgent:
    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        async for message in endpoint.messages():
            await endpoint.send_message(TextMessage(text=f"reply:{message.text}"))

    async def close(self) -> None:
        pass


class CapturingAgent:
    def __init__(self) -> None:
        self.conversations: list[Conversation] = []

    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        self.conversations.append(conversation)
        async for message in endpoint.messages():
            await endpoint.send_message(TextMessage(text=f"reply:{message.text}"))

    async def close(self) -> None:
        pass


class FollowUpAgent:
    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        async for message in endpoint.messages():
            await endpoint.send_message(TextMessage(text=f"reply:{message.text}"))
            await endpoint.send(RequestFollowUp())

    async def close(self) -> None:
        pass


class ProcessingUpdateAgent:
    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        async for message in endpoint.messages():
            await endpoint.send(ProcessingUpdate())
            await endpoint.send_message(TextMessage(text=f"reply:{message.text}"))

    async def close(self) -> None:
        pass


class SlowReplyAgent:
    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        async for message in endpoint.messages():
            await asyncio.sleep(0.2)
            await endpoint.send_message(TextMessage(text=f"reply:{message.text}"))

    async def close(self) -> None:
        pass


class FakeUserSettingsProvider:
    def __init__(self) -> None:
        self.users = []

    async def settings_for_user(self, user: str | None) -> dict:
        self.users.append(user)
        return {"media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}}}

    async def user_exists(self, user: str) -> bool:
        return user.casefold() == "maciek"


def test_websocket_shutdown_closes_active_websockets() -> None:
    async def run() -> None:
        app = create_app(
            Config(
                agent=AgentConfig(type="echo", options={}),
                websocket=WebsocketConfig(port=2137),
            ),
            EchoAgent(),
        )
        websocket = FakeWebsocket()
        app["websockets"].add(websocket)

        await app.on_shutdown[0](app)

        assert websocket.close_calls == [(WSCloseCode.GOING_AWAY, b"server shutdown")]

    asyncio.run(run())


def test_websocket_applies_user_settings_from_provider_to_conversation() -> None:
    async def run() -> None:
        port = _unused_port()
        agent = CapturingAgent()
        provider = FakeUserSettingsProvider()
        config = Config(
            agent=AgentConfig(type="capturing", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
        )
        app = create_app(config, agent, user_settings_provider=provider)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/chat") as websocket:
                    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes={"user": "Maciek"})))

                    assert await _receive_session_event(websocket) == WaitForNewConversation()

                    await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
                    for event in text_message_to_events(TextMessage(text="hello")):
                        await websocket.send_str(endpoint_event_to_json(event))

                    assert await _receive_session_event(websocket) == MessageBegin()
                    assert await _receive_session_event(websocket) == MessageFragment(text="reply:hello")
                    assert await _receive_session_event(websocket) == MessageEnd()
                    assert await _receive_session_event(websocket) == WaitForNewConversation()
        finally:
            await runner.cleanup()

        assert provider.users == ["Maciek"]
        assert agent.conversations[0].user_settings == {
            "media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}}
        }

    asyncio.run(run())


def test_websocket_forwards_processing_update() -> None:
    async def run() -> None:
        port = _unused_port()
        config = Config(
            agent=AgentConfig(type="processing", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
        )
        app = create_app(config, ProcessingUpdateAgent())
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/chat") as websocket:
                    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes={})))

                    assert await _receive_session_event(websocket) == WaitForNewConversation()

                    await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
                    for event in text_message_to_events(TextMessage(text="hello")):
                        await websocket.send_str(endpoint_event_to_json(event))

                    assert await _receive_session_event(websocket) == ProcessingUpdate()
                    assert await _receive_session_event(websocket) == MessageBegin()
                    assert await _receive_session_event(websocket) == MessageFragment(text="reply:hello")
                    assert await _receive_session_event(websocket) == MessageEnd()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_websocket_stays_open_during_slow_agent_with_client_heartbeat() -> None:
    async def run() -> None:
        port = _unused_port()
        config = Config(
            agent=AgentConfig(type="slow_reply", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
        )
        app = create_app(config, SlowReplyAgent())
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/chat", heartbeat=0.05) as websocket:
                    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes={})))

                    assert await _receive_session_event(websocket) == WaitForNewConversation()

                    await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
                    for event in text_message_to_events(TextMessage(text="hello")):
                        await websocket.send_str(endpoint_event_to_json(event))

                    assert await _receive_session_event(websocket) == MessageBegin()
                    assert await _receive_session_event(websocket) == MessageFragment(text="reply:hello")
                    assert await _receive_session_event(websocket) == MessageEnd()
                    assert await _receive_session_event(websocket) == WaitForNewConversation()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_websocket_drops_empty_message_before_agent() -> None:
    async def run() -> None:
        port = _unused_port()
        agent = CapturingAgent()
        config = Config(
            agent=AgentConfig(type="capturing", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
        )
        app = create_app(config, agent)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/chat") as websocket:
                    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes={})))

                    assert await _receive_session_event(websocket) == WaitForNewConversation()

                    await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
                    await websocket.send_str(endpoint_event_to_json(MessageBegin()))
                    await websocket.send_str(endpoint_event_to_json(MessageFragment(text="  ")))
                    await websocket.send_str(endpoint_event_to_json(MessageEnd()))

                    assert await _receive_session_event(websocket) == WaitForNewConversation()
        finally:
            await runner.cleanup()

        assert len(agent.conversations) == 1

    asyncio.run(run())


def test_status_reports_user_settings_provider_without_private_settings() -> None:
    class StatusProvider(FakeUserSettingsProvider):
        def status(self) -> dict:
            return {
                "mode": "home_assistant",
                "mapped_users": ["Maciek"],
                "last_success_by_user": {"Maciek": "2026-06-18T00:00:00+00:00"},
                "failed_users": [],
                "unmapped_users": [],
            }

    async def run() -> dict:
        port = _unused_port()
        config = Config(
            agent=AgentConfig(type="single_reply", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
        )
        app = create_app(config, SingleReplyAgent(), user_settings_provider=StatusProvider())
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/api/status") as response:
                    assert response.status == 200
                    return await response.json()
        finally:
            await runner.cleanup()

    result = asyncio.run(run())

    assert result["user_settings"] == {
        "mode": "home_assistant",
        "mapped_users": ["Maciek"],
        "last_success_by_user": {"Maciek": "2026-06-18T00:00:00+00:00"},
        "failed_users": [],
        "unmapped_users": [],
    }
    assert "playlist_aliases" not in str(result)


def test_batch_websocket_client_drives_interrogator_flow(capsys) -> None:
    async def run() -> None:
        port = _unused_port()
        config = Config(
            agent=AgentConfig(type="interrogator", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
            users={"Maciek": {}},
        )
        app = create_app(config, InterrogatorAgent())
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            await asyncio.wait_for(
                run_batch_ws_client(
                    BatchWsClientOptions(
                        url=f"ws://127.0.0.1:{port}/chat",
                        user="Maciek",
                        area="office",
                        messages=("cześć", "koniec"),
                    )
                ),
                timeout=2,
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())

    output = capsys.readouterr().out
    assert "Twoja wiadomość numer 1 to: cześć\n" in output
    assert "Koniec konwersacji, wysłałeś 2 wiadomości.\n" in output


def test_websocket_returns_to_new_conversation_without_requested_follow_up() -> None:
    async def run() -> None:
        port = _unused_port()
        config = Config(
            agent=AgentConfig(type="single_reply", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
        )
        app = create_app(config, SingleReplyAgent())
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/chat") as websocket:
                    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes={})))

                    assert await _receive_session_event(websocket) == WaitForNewConversation()

                    await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
                    for event in text_message_to_events(TextMessage(text="hello")):
                        await websocket.send_str(endpoint_event_to_json(event))

                    assert await _receive_session_event(websocket) == MessageBegin()
                    assert await _receive_session_event(websocket) == MessageFragment(text="reply:hello")
                    assert await _receive_session_event(websocket) == MessageEnd()
                    assert await _receive_session_event(websocket) == WaitForNewConversation()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_websocket_applies_explicit_user_and_user_settings_to_conversation() -> None:
    async def run() -> None:
        port = _unused_port()
        agent = CapturingAgent()
        config = Config(
            agent=AgentConfig(type="capturing", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
            users={"Maciek": {"media": {"liked_songs_media_id": "library://playlist/7"}}},
        )
        app = create_app(config, agent)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/chat") as websocket:
                    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes={"user": "Maciek"})))

                    assert await _receive_session_event(websocket) == WaitForNewConversation()

                    await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
                    for event in text_message_to_events(TextMessage(text="hello")):
                        await websocket.send_str(endpoint_event_to_json(event))

                    assert await _receive_session_event(websocket) == MessageBegin()
                    assert await _receive_session_event(websocket) == MessageFragment(text="reply:hello")
                    assert await _receive_session_event(websocket) == MessageEnd()
                    assert await _receive_session_event(websocket) == WaitForNewConversation()
        finally:
            await runner.cleanup()

        assert len(agent.conversations) == 1
        assert agent.conversations[0].user == "Maciek"
        assert agent.conversations[0].user_settings == {
            "media": {"liked_songs_media_id": "library://playlist/7"}
        }

    asyncio.run(run())


def test_websocket_rejects_unknown_session_user() -> None:
    async def run() -> None:
        port = _unused_port()
        agent = CapturingAgent()
        config = Config(
            agent=AgentConfig(type="capturing", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
            users={"Maciek": {"media": {"liked_songs_media_id": "library://playlist/7"}}},
        )
        app = create_app(config, agent)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/chat") as websocket:
                    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes={"user": "Unknown"})))
                    assert await _receive_session_event(websocket) == SessionRejected(reason="unknown user: Unknown")
                    message = await websocket.receive()

                    assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)
                    assert websocket.close_code == WSCloseCode.PROTOCOL_ERROR
        finally:
            await runner.cleanup()

        assert agent.conversations == []

    asyncio.run(run())


def test_websocket_request_follow_up_times_out_to_new_conversation() -> None:
    async def run() -> None:
        port = _unused_port()
        config = Config(
            agent=AgentConfig(type="follow_up", options={}),
            websocket=WebsocketConfig(
                host="127.0.0.1",
                port=port,
                follow_up_timeout_seconds=0.05,
            ),
        )
        app = create_app(config, FollowUpAgent())
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            async with ClientSession() as session:
                async with session.ws_connect(f"ws://127.0.0.1:{port}/chat") as websocket:
                    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes={})))

                    assert await _receive_session_event(websocket) == WaitForNewConversation()

                    await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
                    for event in text_message_to_events(TextMessage(text="hello")):
                        await websocket.send_str(endpoint_event_to_json(event))

                    assert await _receive_session_event(websocket) == MessageBegin()
                    assert await _receive_session_event(websocket) == MessageFragment(text="reply:hello")
                    assert await _receive_session_event(websocket) == MessageEnd()
                    assert await _receive_session_event(websocket) == RequestFollowUp(
                        timeout_seconds=0.05,
                    )
                    assert await _receive_session_event(websocket) == WaitForNewConversation()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_batch_websocket_client_does_not_reconnect_after_drop(capsys) -> None:
    async def run() -> int:
        port = _unused_port()
        connection_count = 0

        async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
            nonlocal connection_count
            connection_count += 1
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.receive()
            await websocket.close()
            return websocket

        app = web.Application()
        app.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()

        try:
            await asyncio.wait_for(
                run_batch_ws_client(
                    BatchWsClientOptions(
                        url=f"ws://127.0.0.1:{port}/chat",
                        user=None,
                        area=None,
                        messages=("hello",),
                    )
                ),
                timeout=2,
            )
        finally:
            await runner.cleanup()

        return connection_count

    connection_count = asyncio.run(run())

    assert connection_count == 1
    assert "Connection lost: websocket closed." in capsys.readouterr().out


def test_batch_websocket_client_prints_session_rejection(capsys) -> None:
    async def run() -> None:
        port = _unused_port()
        config = Config(
            agent=AgentConfig(type="capturing", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
            users={"Maciek": {}},
        )
        app = create_app(config, CapturingAgent())
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        try:
            await asyncio.wait_for(
                run_batch_ws_client(
                    BatchWsClientOptions(
                        url=f"ws://127.0.0.1:{port}/chat",
                        user="Unknown",
                        area=None,
                        messages=("hello",),
                    )
                ),
                timeout=2,
            )
        finally:
            await runner.cleanup()

    asyncio.run(run())

    output = capsys.readouterr().out
    assert output == "Connection rejected: unknown user: Unknown.\n"


def test_chat_client_help_command_prints_available_commands(capsys) -> None:
    result = chat_client._handle_client_command("/help")

    output = capsys.readouterr().out
    assert result == chat_client._ClientCommandResult.HANDLED
    assert "Commands:" in output
    assert "/help  Show this help." in output
    assert "/exit  Exit the chat client." in output
    assert chat_client.CLIENT_TEXT_STYLE in output


def test_chat_client_initial_prompt_is_connecting() -> None:
    loop = asyncio.new_event_loop()
    try:
        input_session = chat_client._InteractiveInputSession(loop)

        assert input_session._current_prompt() == chat_client.CONNECTING_PROMPT
    finally:
        loop.close()


def test_ws_client_prints_processing_update(capsys) -> None:
    result = handle_websocket_message(None, FakeWebsocketMessage('{"type":"processing_update"}'))

    assert result is None
    assert capsys.readouterr().out == "processing...\n"


def test_ws_client_routes_processing_update_to_system_printer() -> None:
    system_messages = []

    result = handle_websocket_message(
        None,
        FakeWebsocketMessage('{"type":"processing_update"}'),
        system_message_printer=system_messages.append,
    )

    assert result is None
    assert system_messages == ["processing..."]


def test_ws_client_raises_terminal_error_on_session_rejected() -> None:
    with pytest.raises(WebsocketSessionRejected, match="unknown user: Unknown"):
        handle_websocket_message(
            None,
            FakeWebsocketMessage(session_event_to_json(SessionRejected(reason="unknown user: Unknown"))),
        )


def test_chat_client_suppresses_initial_wait_for_new_conversation_notice(capsys) -> None:
    async def run():
        websocket = StrictReceiveWebsocket()
        await websocket.messages.put(FakeWebsocketMessage(session_event_to_json(WaitForNewConversation())))
        return await chat_client._read_next_wait_state(
            websocket,
            asyncio.Event(),
            show_wait_for_new_conversation_message=False,
        )

    wait_state = asyncio.run(run())

    assert wait_state.starts_new_conversation is True
    assert capsys.readouterr().out == ""


def test_chat_client_dims_visible_wait_for_new_conversation_notice(capsys) -> None:
    result = handle_websocket_message(
        None,
        FakeWebsocketMessage(session_event_to_json(WaitForNewConversation())),
        system_message_printer=chat_client._print_client_message,
    )

    assert result is not None
    assert result.starts_new_conversation is True
    assert capsys.readouterr().out == chat_client._style_client_text(
        "Conversation ended; waiting for a new conversation."
    ) + "\n"


def test_interactive_chat_suppresses_only_first_new_conversation_notice(capsys) -> None:
    class FakeInteractiveInputSession:
        def __init__(self) -> None:
            self._lines: asyncio.Queue[str | None] = asyncio.Queue()
            self.prompts: list[str] = []

        def set_prompt(self, prompt: str) -> None:
            self.prompts.append(prompt)

        async def read_line(self) -> str | None:
            return await self._lines.get()

    class FakeInteractiveWebsocket(StrictReceiveWebsocket):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[str] = []

        async def send_str(self, data: str) -> None:
            self.sent.append(data)

    async def run() -> None:
        websocket = FakeInteractiveWebsocket()
        input_session = FakeInteractiveInputSession()
        stop_event = asyncio.Event()

        await websocket.messages.put(FakeWebsocketMessage(session_event_to_json(WaitForNewConversation())))
        await websocket.messages.put(FakeWebsocketMessage(session_event_to_json(MessageBegin())))
        await websocket.messages.put(FakeWebsocketMessage(session_event_to_json(MessageFragment(text="reply"))))
        await websocket.messages.put(FakeWebsocketMessage(session_event_to_json(MessageEnd())))
        await websocket.messages.put(FakeWebsocketMessage(session_event_to_json(WaitForNewConversation())))
        await input_session._lines.put("hello")
        await input_session._lines.put(None)

        await asyncio.wait_for(
            chat_client._run_interactive_connection(
                websocket,
                input_session,
                stop_event,
            ),
            timeout=1,
        )

    asyncio.run(run())

    assert capsys.readouterr().out == "reply\n" + chat_client._style_client_text(
        "Conversation ended; waiting for a new conversation."
    ) + "\n"


def test_interactive_chat_uses_connecting_prompt_before_first_connect(monkeypatch) -> None:
    class FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class FakeInputSession:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def set_prompt(self, prompt: str) -> None:
            self.prompts.append(prompt)

    async def fake_connect_interactive(
        session,
        options,
        input_session,
        stop_event,
    ):
        raise chat_client.ChatExited()

    async def run() -> list[str]:
        input_session = FakeInputSession()
        monkeypatch.setattr(chat_client, "ClientSession", FakeClientSession)
        monkeypatch.setattr(chat_client, "_connect_interactive", fake_connect_interactive)

        await chat_client._run_interactive_chat(
            ChatClientOptions(url="ws://127.0.0.1:2137/chat", user=None, area=None),
            input_session,
            asyncio.Event(),
        )
        return input_session.prompts

    assert asyncio.run(run()) == [chat_client.CONNECTING_PROMPT]


def test_interactive_chat_connects_with_websocket_heartbeat() -> None:
    class FakeClientSession:
        def __init__(self) -> None:
            self.heartbeat = None

        async def ws_connect(self, url: str, *, heartbeat: float | None = None):
            self.heartbeat = heartbeat
            return object()

    class FakeInputSession:
        def set_prompt(self, prompt: str) -> None:
            pass

        async def read_line(self) -> str | None:
            await asyncio.Future()

    async def run() -> float | None:
        session = FakeClientSession()
        result = await chat_client._connect_interactive(
            session,
            ChatClientOptions(url="ws://127.0.0.1:2137/chat", user=None, area=None),
            FakeInputSession(),
            asyncio.Event(),
        )

        assert result is not None
        return session.heartbeat

    assert asyncio.run(run()) == chat_client.WEBSOCKET_HEARTBEAT_SECONDS


def test_interactive_chat_prints_connect_failure_and_does_not_retry(monkeypatch, capsys) -> None:
    class FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def ws_connect(self, url: str, *, heartbeat: float | None = None):
            raise ClientError("server unavailable")

    class FakeInputSession:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def set_prompt(self, prompt: str) -> None:
            self.prompts.append(prompt)

        async def read_line(self) -> str | None:
            await asyncio.Future()

    async def run() -> FakeInputSession:
        input_session = FakeInputSession()
        monkeypatch.setattr(chat_client, "ClientSession", FakeClientSession)

        with pytest.raises(chat_client.ChatConnectionFailed):
            await chat_client._run_interactive_chat(
                ChatClientOptions(url="ws://127.0.0.1:2137/chat", user=None, area=None),
                input_session,
                asyncio.Event(),
            )
        return input_session

    input_session = asyncio.run(run())
    output = capsys.readouterr().out

    assert input_session.prompts == [chat_client.CONNECTING_PROMPT]
    assert output == (
        chat_client._style_client_text("Connecting to ws://127.0.0.1:2137/chat ...")
        + "\n"
        + chat_client._style_client_text("Connection failed: server unavailable.")
        + "\n"
    )


def test_interactive_chat_prints_rejection_and_does_not_reconnect(monkeypatch, capsys) -> None:
    class FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class FakeInputSession:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def set_prompt(self, prompt: str) -> None:
            self.prompts.append(prompt)

    class RejectedWebsocket(StrictReceiveWebsocket):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[str] = []
            self.closed = False

        async def send_str(self, data: str) -> None:
            self.sent.append(data)

        async def close(self) -> None:
            self.closed = True

    connect_calls = 0
    rejected_websocket = RejectedWebsocket()

    async def fake_connect_interactive(
        session,
        options,
        input_session,
        stop_event,
    ):
        nonlocal connect_calls
        connect_calls += 1
        await rejected_websocket.messages.put(
            FakeWebsocketMessage(session_event_to_json(SessionRejected(reason="unknown user: Unknown")))
        )
        return rejected_websocket

    async def run() -> FakeInputSession:
        input_session = FakeInputSession()
        monkeypatch.setattr(chat_client, "ClientSession", FakeClientSession)
        monkeypatch.setattr(chat_client, "_connect_interactive", fake_connect_interactive)

        await chat_client._run_interactive_chat(
            ChatClientOptions(url="ws://127.0.0.1:2137/chat", user="Unknown", area=None),
            input_session,
            asyncio.Event(),
        )
        return input_session

    input_session = asyncio.run(run())
    output = capsys.readouterr().out

    assert connect_calls == 1
    assert rejected_websocket.closed
    assert endpoint_event_from_json(rejected_websocket.sent[0]) == SessionAttributes(attributes={"user": "Unknown"})
    assert output == chat_client._style_client_text("Connection rejected: unknown user: Unknown.") + "\n"


def test_interactive_chat_exits_on_connection_drop_after_connect(monkeypatch, capsys) -> None:
    class FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class FakeInputSession:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def set_prompt(self, prompt: str) -> None:
            self.prompts.append(prompt)

    class DroppedWebsocket(StrictReceiveWebsocket):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[str] = []
            self.closed = False

        async def send_str(self, data: str) -> None:
            self.sent.append(data)

        async def close(self) -> None:
            self.closed = True

    connect_calls = 0
    dropped_websocket = DroppedWebsocket()

    async def fake_connect_interactive(
        session,
        options,
        input_session,
        stop_event,
    ):
        nonlocal connect_calls
        connect_calls += 1
        await dropped_websocket.messages.put(FakeWebsocketMessage(data="", type=WSMsgType.CLOSED))
        return dropped_websocket

    async def run() -> FakeInputSession:
        input_session = FakeInputSession()
        monkeypatch.setattr(chat_client, "ClientSession", FakeClientSession)
        monkeypatch.setattr(chat_client, "_connect_interactive", fake_connect_interactive)

        with pytest.raises(chat_client.ChatConnectionLost):
            await chat_client._run_interactive_chat(
                ChatClientOptions(url="ws://127.0.0.1:2137/chat", user=None, area=None),
                input_session,
                asyncio.Event(),
            )
        return input_session

    input_session = asyncio.run(run())
    output = capsys.readouterr().out

    assert connect_calls == 1
    assert dropped_websocket.closed
    assert endpoint_event_from_json(dropped_websocket.sent[0]) == SessionAttributes(attributes={})
    assert output == chat_client._style_client_text("Connection lost: websocket closed.") + "\n"


def test_interactive_chat_exits_when_server_closes_established_connection(monkeypatch, capsys) -> None:
    class FakeInputSession:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def set_prompt(self, prompt: str) -> None:
            self.prompts.append(prompt)

        async def read_line(self) -> str | None:
            await asyncio.Future()

    async def run() -> tuple[FakeInputSession, int]:
        monkeypatch.setattr(chat_client, "WEBSOCKET_LIVENESS_CHECK_SECONDS", 0.05)
        monkeypatch.setattr(chat_client, "WEBSOCKET_LISTENER_PROBE_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(chat_client, "WEBSOCKET_LISTENER_CLOSE_TIMEOUT_SECONDS", 0.01)
        port = _unused_port()
        accepted = asyncio.Event()
        close_connection = asyncio.Event()
        connection_count = 0
        websockets = set()

        async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
            nonlocal connection_count
            connection_count += 1
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            websockets.add(websocket)
            await websocket.receive()
            await websocket.send_str(session_event_to_json(WaitForNewConversation()))
            accepted.set()
            try:
                await close_connection.wait()
                await websocket.close(drain=False)
                return websocket
            finally:
                websockets.discard(websocket)

        app = web.Application()
        app.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()

        input_session = FakeInputSession()
        client_task = asyncio.create_task(
            chat_client._run_interactive_chat(
                ChatClientOptions(url=f"ws://127.0.0.1:{port}/chat", user=None, area=None),
                input_session,
                asyncio.Event(),
            )
        )
        try:
            await asyncio.wait_for(accepted.wait(), timeout=1)
            await site.stop()
            close_connection.set()
            for websocket in set(websockets):
                await websocket.close(drain=False)
            with pytest.raises(chat_client.ChatConnectionLost):
                await asyncio.wait_for(client_task, timeout=5)
        finally:
            if not client_task.done():
                client_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await client_task
            await runner.cleanup()

        return input_session, connection_count

    input_session, connection_count = asyncio.run(run())
    output = capsys.readouterr().out

    assert connection_count == 1
    assert input_session.prompts == [
        chat_client.CONNECTING_PROMPT,
        chat_client.WAITING_FOR_SERVER_PROMPT,
        chat_client.WAITING_FOR_NEW_CONVERSATION_PROMPT,
    ]
    assert "Connection lost:" in output


def test_interactive_chat_does_not_cancel_websocket_receive_while_waiting_for_input() -> None:
    class SlowReplyWebsocket(StrictReceiveWebsocket):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

    async def run() -> None:
        websocket = SlowReplyWebsocket()
        wait_state_task = asyncio.create_task(
            chat_client._read_next_wait_state(
                websocket,
                asyncio.Event(),
                "ws:///chat",
                show_wait_for_new_conversation_message=False,
            )
        )

        await asyncio.sleep(0.03)
        await websocket.messages.put(FakeWebsocketMessage(session_event_to_json(WaitForNewConversation())))

        wait_state = await asyncio.wait_for(wait_state_task, timeout=1)

        assert wait_state.starts_new_conversation is True
        assert websocket.max_active_receives == 1

    asyncio.run(run())


def test_chat_client_accepts_area_option() -> None:
    args = chat_client.parse_args(["--area", "office", "ws://127.0.0.1:2137/chat"])

    assert args.area == "office"


def test_chat_client_accepts_user_option() -> None:
    args = chat_client.parse_args(["--user", "Maciek", "ws://127.0.0.1:2137/chat"])

    assert args.user == "Maciek"


def test_batch_ws_client_accepts_area_option() -> None:
    args = batch_ws_client.parse_args(["--area", "office", "--message", "cześć"])

    assert args.area == "office"


def test_batch_ws_client_accepts_user_option() -> None:
    args = batch_ws_client.parse_args(["--user", "Maciek", "--message", "cześć"])

    assert args.user == "Maciek"


def test_ws_clients_reject_location_option() -> None:
    with pytest.raises(SystemExit):
        chat_client.parse_args(["--location", "office"])

    with pytest.raises(SystemExit):
        batch_ws_client.parse_args(["--location", "office"])


def test_chat_client_main_returns_130_on_interrupt(monkeypatch) -> None:
    async def fake_run_chat(options: ChatClientOptions) -> None:
        raise chat_client.ChatInterrupted()

    monkeypatch.setattr(chat_client, "run_chat", fake_run_chat)

    assert chat_client.main(["ws://127.0.0.1:2137/chat"]) == 130


def test_chat_client_main_returns_1_on_connection_lost(monkeypatch) -> None:
    async def fake_run_chat(options: ChatClientOptions) -> None:
        raise chat_client.ChatConnectionLost()

    monkeypatch.setattr(chat_client, "run_chat", fake_run_chat)

    assert chat_client.main(["ws://127.0.0.1:2137/chat"]) == 1


def test_chat_client_main_returns_1_on_connection_failed(monkeypatch) -> None:
    async def fake_run_chat(options: ChatClientOptions) -> None:
        raise chat_client.ChatConnectionFailed()

    monkeypatch.setattr(chat_client, "run_chat", fake_run_chat)

    assert chat_client.main(["ws://127.0.0.1:2137/chat"]) == 1


def test_batch_ws_client_main_returns_130_on_interrupt(monkeypatch) -> None:
    async def fake_run_batch_ws_client(options: BatchWsClientOptions) -> None:
        raise batch_ws_client.WsClientInterrupted()

    monkeypatch.setattr(batch_ws_client, "run_batch_ws_client", fake_run_batch_ws_client)

    assert batch_ws_client.main(["ws://127.0.0.1:2137/chat"]) == 130


def test_interactive_text_keeps_single_websocket_receive_during_liveness_probes(monkeypatch) -> None:
    class SlowReplyWebsocket(StrictReceiveWebsocket):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

    class FakeInputSession:
        async def read_line(self) -> str | None:
            await asyncio.Future()

    async def run() -> None:
        monkeypatch.setattr(chat_client, "WEBSOCKET_LIVENESS_CHECK_SECONDS", 0.01)
        websocket = SlowReplyWebsocket()
        read_task = asyncio.create_task(
            chat_client._read_next_interactive_text(
                websocket,
                asyncio.Event(),
                FakeInputSession(),
                chat_client.WaitState(starts_new_conversation=True),
                "ws:///chat",
            )
        )

        await asyncio.sleep(0.03)
        await websocket.messages.put(FakeWebsocketMessage(data="", type=WSMsgType.CLOSED))

        with pytest.raises(chat_client.WebsocketDisconnected, match="websocket closed"):
            await asyncio.wait_for(read_task, timeout=1)

        assert websocket.max_active_receives == 1

    asyncio.run(run())


def test_chat_client_drops_offline_messages(capsys) -> None:
    assert chat_client._handle_offline_line("hello") is False

    assert "Disconnected; message not sent." in capsys.readouterr().out


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _receive_session_event(websocket):
    message = await asyncio.wait_for(websocket.receive(), timeout=1)
    return session_event_from_json(message.data)


class StrictReceiveWebsocket:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[FakeWebsocketMessage] = asyncio.Queue()
        self._active_receives = 0
        self.max_active_receives = 0

    async def receive(self) -> FakeWebsocketMessage:
        self._active_receives += 1
        self.max_active_receives = max(self.max_active_receives, self._active_receives)
        if self._active_receives > 1:
            raise RuntimeError("Concurrent call to receive() is not allowed")
        try:
            return await self.messages.get()
        finally:
            self._active_receives -= 1
