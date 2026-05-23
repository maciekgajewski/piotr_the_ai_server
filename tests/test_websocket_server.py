import asyncio

from aiohttp import WSCloseCode

from ai_server.agent.echo import EchoAgent
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
