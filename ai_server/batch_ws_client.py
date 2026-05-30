from __future__ import annotations

import argparse
import asyncio
import signal
from dataclasses import dataclass

from aiohttp import ClientSession

from ai_server.ws_client_common import DEFAULT_WEBSOCKET_URL, INTERRUPTED_EXIT_CODE, WebsocketDisconnected
from ai_server.ws_client_common import WsClientInterrupted
from ai_server.ws_client_common import handle_websocket_message, receive_websocket_message, send_session_attributes
from ai_server.ws_client_common import send_user_text


@dataclass(frozen=True)
class BatchWsClientOptions:
    url: str
    user: str | None
    area: str | None
    messages: tuple[str, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send batch messages to the AI server over websocket.")
    parser.add_argument("--user", help="Optional session user attribute sent in the websocket handshake.")
    parser.add_argument("--area", help="Optional Home Assistant area attribute sent in the websocket handshake.")
    parser.add_argument(
        "--message",
        action="append",
        default=[],
        help="Message to send. Can be repeated. The client exits on the next wait-state event.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_WEBSOCKET_URL,
        help=f"Websocket URL. Defaults to {DEFAULT_WEBSOCKET_URL}.",
    )
    return parser.parse_args(argv)


async def run_batch_ws_client(options: BatchWsClientOptions) -> None:
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    async with ClientSession() as session:
        async with session.ws_connect(options.url) as websocket:
            await send_session_attributes(websocket, options.user, options.area)
            messages = list(options.messages)
            sent_all_messages = not messages

            try:
                while True:
                    message = await receive_websocket_message(websocket, stop_event)
                    wait_state = handle_websocket_message(websocket, message)
                    if wait_state is None:
                        continue

                    if messages:
                        text = messages.pop(0)
                        sent_all_messages = not messages
                        _print_client_message(f"> {text}")
                        await send_user_text(
                            websocket,
                            text,
                            starts_new_conversation=wait_state.starts_new_conversation,
                        )
                        continue

                    if sent_all_messages:
                        return
            except WebsocketDisconnected as exc:
                _print_client_message(f"Connection lost: {exc}.")


def _print_client_message(text: str) -> None:
    print(text, flush=True)


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)
        except NotImplementedError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options = BatchWsClientOptions(
        url=args.url,
        user=args.user,
        area=args.area,
        messages=tuple(args.message),
    )
    try:
        asyncio.run(run_batch_ws_client(options))
    except (WsClientInterrupted, KeyboardInterrupt):
        return INTERRUPTED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
