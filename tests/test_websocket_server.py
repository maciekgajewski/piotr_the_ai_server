import asyncio
import socket

from aiohttp import WSCloseCode
from aiohttp import web

from ai_server.agent.echo import EchoAgent
from ai_server.agent.interrogator import InterrogatorAgent
from ai_server import chat_client
from ai_server.chat_client import ChatClientOptions, run_chat
from ai_server.config import AgentConfig, Config, WebsocketConfig
from ai_server.websocket_server import create_app


class FakeWebsocket:
    def __init__(self) -> None:
        self.close_calls = []

    async def close(self, code, message) -> None:
        self.close_calls.append((code, message))


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


def test_scripted_websocket_client_drives_interrogator_flow(capsys) -> None:
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
                run_chat(
                    ChatClientOptions(
                        url=f"ws://127.0.0.1:{port}/chat",
                        user="Maciek",
                        location="office",
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


def test_chat_client_main_returns_130_on_interrupt(monkeypatch) -> None:
    async def fake_run_chat(options: ChatClientOptions) -> None:
        raise chat_client.ChatInterrupted()

    monkeypatch.setattr(chat_client, "run_chat", fake_run_chat)

    assert chat_client.main(["ws://127.0.0.1:2137/chat"]) == 130


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
