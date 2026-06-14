import asyncio
import socket
from dataclasses import dataclass

from aiohttp import ClientSession, WSCloseCode
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
from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, NewConversation, RequestFollowUp
from ai_server.messages import SessionAttributes, TextMessage, WaitForNewConversation, endpoint_event_to_json
from ai_server.messages import session_event_from_json, text_message_to_events
from ai_server.websocket_server import create_app


class FakeWebsocket:
    def __init__(self) -> None:
        self.close_calls = []

    async def close(self, code, message) -> None:
        self.close_calls.append((code, message))


@dataclass(frozen=True)
class FakeWebsocketMessage:
    data: str


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


def test_batch_websocket_client_drives_interrogator_flow(capsys) -> None:
    async def run() -> None:
        port = _unused_port()
        config = Config(
            agent=AgentConfig(type="interrogator", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
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


def test_websocket_applies_default_user_and_user_settings_to_conversation() -> None:
    async def run() -> None:
        port = _unused_port()
        agent = CapturingAgent()
        config = Config(
            agent=AgentConfig(type="capturing", options={}),
            websocket=WebsocketConfig(host="127.0.0.1", port=port),
            default_user="Maciek",
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

        assert len(agent.conversations) == 1
        assert agent.conversations[0].user == "Maciek"
        assert agent.conversations[0].user_settings == {
            "media": {"liked_songs_media_id": "library://playlist/7"}
        }

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


def test_chat_client_help_command_prints_available_commands(capsys) -> None:
    result = chat_client._handle_client_command("/help")

    output = capsys.readouterr().out
    assert result == chat_client._ClientCommandResult.HANDLED
    assert "Commands:" in output
    assert "/help  Show this help." in output
    assert "/exit  Exit the chat client." in output
    assert chat_client.CLIENT_TEXT_STYLE in output


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


def test_batch_ws_client_main_returns_130_on_interrupt(monkeypatch) -> None:
    async def fake_run_batch_ws_client(options: BatchWsClientOptions) -> None:
        raise batch_ws_client.WsClientInterrupted()

    monkeypatch.setattr(batch_ws_client, "run_batch_ws_client", fake_run_batch_ws_client)

    assert batch_ws_client.main(["ws://127.0.0.1:2137/chat"]) == 130


def test_interactive_receive_loop_keeps_single_websocket_receive_after_cancelled_consumer() -> None:
    async def run() -> None:
        websocket = StrictReceiveWebsocket()
        stop_event = asyncio.Event()
        receive_loop = chat_client._WebsocketReceiveLoop(websocket, stop_event)
        receive_loop.start()

        cancelled_receive = asyncio.create_task(receive_loop.receive())
        await asyncio.sleep(0)
        cancelled_receive.cancel()
        try:
            await cancelled_receive
        except asyncio.CancelledError:
            pass

        second_receive = asyncio.create_task(receive_loop.receive())
        await websocket.messages.put(FakeWebsocketMessage(data="hello"))

        assert await asyncio.wait_for(second_receive, timeout=1) == FakeWebsocketMessage(data="hello")
        assert websocket.max_active_receives == 1

        await receive_loop.close()

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
