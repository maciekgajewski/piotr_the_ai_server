from __future__ import annotations

import asyncio

from ai_server.websocket_client.messages import InputKind
from ai_server.websocket_client.terminal_presenter import TerminalPresenter


async def run() -> None:
    presenter = TerminalPresenter()
    try:
        await presenter.start()
        await presenter.present_input(InputKind.START)
        await asyncio.sleep(0.5)
        presenter.system("asynchronous status")
        result = await presenter.read_input(InputKind.START)
        print(f"RESULT:{result.text}", flush=True)
    finally:
        await presenter.close()


if __name__ == "__main__":
    asyncio.run(run())
