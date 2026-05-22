from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from contextlib import suppress

from aiohttp import ClientSession, WSMsgType

from ai_server.messages import user_message_from_json


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the AI server over websocket.")
    parser.add_argument("url", help="Websocket URL, for example ws://127.0.0.1:2137/chat.")
    return parser.parse_args(argv)


async def _open_stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return reader


async def _read_stdin_line(reader: asyncio.StreamReader) -> str:
    line = await reader.readline()
    return line.decode()


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)
        except NotImplementedError:
            pass


async def run_chat(url: str) -> None:
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    stdin_reader = await _open_stdin_reader()

    async with ClientSession() as session:
        async with session.ws_connect(url) as websocket:
            receiver_task = asyncio.create_task(_receive_messages(websocket))

            try:
                while True:
                    line_task = asyncio.create_task(_read_stdin_line(stdin_reader))
                    stop_task = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        (line_task, stop_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    for task in pending:
                        with suppress(asyncio.CancelledError):
                            await task

                    if stop_task in done:
                        break

                    line = line_task.result()
                    if line == "":
                        break

                    text = line.rstrip("\n")
                    await websocket.send_str(json.dumps({"text": text}))
            finally:
                receiver_task.cancel()
                await websocket.close()
                with suppress(asyncio.CancelledError):
                    await receiver_task


async def _receive_messages(websocket) -> None:
    async for message in websocket:
        if message.type == WSMsgType.TEXT:
            user_message = user_message_from_json(message.data)
            print(user_message.text, flush=True)
        elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            break
        elif message.type == WSMsgType.ERROR:
            raise RuntimeError("websocket connection failed") from websocket.exception()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(run_chat(args.url))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
